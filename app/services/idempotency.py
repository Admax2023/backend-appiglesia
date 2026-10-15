"""Idempotency-Key handling.

The key row is inserted (INSERT ... ON CONFLICT DO NOTHING) in the SAME transaction as the business
operation, so:
  * a concurrent request with the same key blocks on the unique index until the first commits,
    then sees the stored response and replays it;
  * if the business operation fails (4xx/5xx) everything rolls back, including the key, so the
    client may retry with the same key;
  * same key + different request body  -> 409 idempotency_key_conflict.

Secrets in stored responses (the order tracking token): the DB must never hold a usable tracking
token. Fields listed in `secret_fields` are stored encrypted with a one-time keystream derived from
HMAC-SHA256(JWT_SECRET, scope | raw Idempotency-Key). The raw key itself is NOT stored either (the
`key` column holds its SHA-256), so a database dump or backup alone cannot recover a token: it takes
the server secret AND the client's Idempotency-Key, which only the original client knows. A genuine
retry (same key) gets the original token back.

Retention: rows are deleted after IDEMPOTENCY_RETENTION_HOURS by a background task started with the
app (see `purger`) and by `python -m app.cli purge-idempotency`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import AppError
from app.models import IdempotencyKey

REPLAY_HEADER = "Idempotent-Replayed"
_ENC_MARKER = "__enc_v1__"
_PRINTABLE = re.compile(rb"[!-~]*")
log = logging.getLogger("app.idempotency")


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _keystream(scope: str, key: str, field: str, n: int) -> bytes:
    secret = get_settings().jwt_secret.encode()
    out = b""
    counter = 0
    while len(out) < n:
        msg = f"idem-secret|{scope}|{key}|{field}|{counter}".encode()
        out += hmac.new(secret, msg, hashlib.sha256).digest()
        counter += 1
    return out[:n]


def _xor(data: bytes, ks: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, ks))


def _seal(data: Any, scope: str, key: str, secret_fields: tuple[str, ...]) -> Any:
    if not secret_fields or not isinstance(data, dict):
        return data
    out = dict(data)
    for f in secret_fields:
        v = out.get(f)
        if isinstance(v, str):
            raw = v.encode()
            out[f] = {_ENC_MARKER: base64.b64encode(_xor(raw, _keystream(scope, key, f, len(raw)))).decode()}
    return out


def _unseal(data: Any, scope: str, key: str, secret_fields: tuple[str, ...]) -> Any:
    if not secret_fields or not isinstance(data, dict):
        return data
    out = dict(data)
    for f in secret_fields:
        v = out.get(f)
        if isinstance(v, dict) and _ENC_MARKER in v:
            raw = base64.b64decode(v[_ENC_MARKER])
            plain = _xor(raw, _keystream(scope, key, f, len(raw)))
            if not _PRINTABLE.fullmatch(plain):  # e.g. JWT_SECRET was rotated since it was stored
                raise AppError(
                    409, "idempotency_replay_unavailable",
                    "No se puede reproducir la respuesta original: consulta el pedido o reintenta con una clave nueva",
                )
            out[f] = plain.decode()
    return out


def request_hash(payload: Any) -> str:
    raw = json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _dump(body: Any) -> Any:
    if isinstance(body, BaseModel):
        return body.model_dump(mode="json")
    return jsonable_encoder(body)


def run(
    db: Session,
    *,
    scope: str,
    key: str | None,
    payload: Any,
    operation: Callable[[], tuple[int, Any]],
    after_commit_failure: Callable[[], None] | None = None,
    secret_fields: tuple[str, ...] = (),
) -> JSONResponse:
    """Run `operation` (which must NOT commit) exactly once per (scope, key) and commit.

    `secret_fields`: top-level response fields stored encrypted (see module docstring)."""
    if key is None:
        status, body = operation()
        data = _dump(body)
        _commit(db, after_commit_failure)
        return JSONResponse(status_code=status, content=data)

    rhash = request_hash(payload)
    inserted_id = db.execute(
        pg_insert(IdempotencyKey)
        .values(id=uuid.uuid4(), scope=scope, key=key_digest(key), request_hash=rhash)
        .on_conflict_do_nothing(constraint="uq_idempotency_keys_scope_key")
        .returning(IdempotencyKey.id)
    ).scalar_one_or_none()

    if inserted_id is None:
        row = db.execute(
            select(IdempotencyKey.request_hash, IdempotencyKey.response_status, IdempotencyKey.response_body).where(
                IdempotencyKey.scope == scope, IdempotencyKey.key == key_digest(key)
            )
        ).one()
        db.rollback()
        if row.request_hash != rhash:
            raise AppError(
                409,
                "idempotency_key_conflict",
                "La Idempotency-Key ya se usó con una solicitud diferente",
            )
        if row.response_status is None:  # pragma: no cover - only if a writer crashed mid-flight
            raise AppError(409, "idempotency_in_progress", "La solicitud original aún se está procesando")
        return JSONResponse(
            status_code=row.response_status,
            content=_unseal(row.response_body, scope, key, secret_fields),
            headers={REPLAY_HEADER: "true"},
        )

    status, body = operation()
    data = _dump(body)
    db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == inserted_id)
        .values(response_status=status, response_body=_seal(data, scope, key, secret_fields))
    )
    _commit(db, after_commit_failure)
    return JSONResponse(status_code=status, content=data)


def _commit(db: Session, after_commit_failure: Callable[[], None] | None) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        if after_commit_failure:
            after_commit_failure()
        raise


def purge_older_than(db: Session, hours: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    res = db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    db.commit()
    return res.rowcount or 0  # type: ignore[attr-defined]


class _Purger:
    """Background thread that deletes expired idempotency rows every IDEMPOTENCY_PURGE_INTERVAL_MINUTES."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if get_settings().idempotency_purge_interval_minutes <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="idempotency-purge", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        from app.db import SessionLocal

        s = get_settings()
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    n = purge_older_than(db, s.idempotency_retention_hours)
                if n:
                    log.info("Purged %d idempotency keys older than %dh", n, s.idempotency_retention_hours)
            except Exception:  # never kill the thread (e.g. DB briefly down)
                log.exception("Idempotency purge failed")
            self._stop.wait(s.idempotency_purge_interval_minutes * 60)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None


purger = _Purger()
