"""Regression tests for the security/correctness review findings."""

from __future__ import annotations

import logging
import threading
import uuid
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.models import AuditLog, Customer, IdempotencyKey
from tests.conftest import image_bytes


def _pay(client, auth, oid, amount, method="cash"):
    return client.post(f"/api/admin/orders/{oid}/payments", json={"method": method, "amount": amount},
                       headers={**auth, "Idempotency-Key": str(uuid.uuid4())})


def _upload(client, token, amount, data=None):
    return client.post(
        f"/api/public/orders/{token}/proofs",
        data={"declared_amount": amount},
        files={"file": ("c.png", data if data is not None else image_bytes(), "image/png")},
    )


# ------------------------------------------------------------------ client IP / rate limiting


def test_client_ip_uses_rightmost_trusted_hop(monkeypatch):
    from starlette.requests import Request

    from app.config import get_settings
    from app.deps import client_ip

    def req(xff: list[str]):
        headers = [(b"x-forwarded-for", v.encode()) for v in xff]
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.1", 1234)})

    s = get_settings()
    assert client_ip(req(["6.6.6.6, 1.2.3.4"])) == "10.0.0.1"  # proxy headers not trusted by default
    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxy_hops", 1)
    # nginx appends the real peer: the client-supplied leftmost value is ignored
    assert client_ip(req(["6.6.6.6, 1.2.3.4"])) == "1.2.3.4"
    assert client_ip(req(["6.6.6.6", "1.2.3.4"])) == "1.2.3.4"
    assert client_ip(req([])) == "10.0.0.1"
    monkeypatch.setattr(s, "trusted_proxy_hops", 2)  # CDN -> nginx -> app
    assert client_ip(req(["spoof, 5.5.5.5, 172.16.0.9"])) == "5.5.5.5"
    assert client_ip(req(["5.5.5.5"])) == "5.5.5.5"


def test_spoofed_forwarded_for_does_not_bypass_rate_limit(client, make_product, place_order, monkeypatch):
    from app.config import get_settings
    from app.ratelimit import limiter

    s = get_settings()
    monkeypatch.setattr(s, "trust_proxy_headers", True)
    monkeypatch.setattr(s, "trusted_proxy_hops", 1)
    p = make_product()
    limiter.enabled = True
    codes = []
    for i in range(11):
        body = {"customer_name": f"Cliente {i}", "payment_method": "cash",
                "items": [{"product_id": str(p.id), "quantity": i + 1}]}
        r = client.post("/api/public/orders", json=body,
                        headers={"Idempotency-Key": str(uuid.uuid4()), "X-Forwarded-For": f"9.9.9.{i}, 1.2.3.4"})
        codes.append(r.status_code)
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_login_limit_not_reset_by_changing_username(client, admin_user):
    from app.ratelimit import limiter

    limiter.enabled = True
    codes = [
        client.post("/api/auth/login", json={"username": f"user{i}", "password": "bad-password"}).status_code
        for i in range(10)
    ]
    assert codes == [401] * 10
    r = client.post("/api/auth/login", json={"username": "admin", "password": "bad-password"})
    assert r.status_code == 429


def test_rate_limiter_never_clears_active_buckets(monkeypatch):
    from app import ratelimit
    from app.errors import AppError

    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    rl = ratelimit.RateLimiter()
    rl.enabled = True
    for _ in range(3):
        rl.check("login", "1.2.3.4", 3, 300)
    for i in range(60_000):  # flood with other keys (old code wiped everything above 50k)
        rl.check("login_user", f"u{i}", 30, 1)
    with pytest.raises(AppError):
        rl.check("login", "1.2.3.4", 3, 300)
    # idle keys expire on the periodic sweep, the active lockout survives it
    now[0] += 60
    with pytest.raises(AppError):
        rl.check("login", "1.2.3.4", 3, 300)
    assert rl.size() <= 2


# ------------------------------------------------------------------ idempotency storage


def test_tracking_token_and_raw_key_not_stored(client, make_product, db):
    p = make_product()
    key = str(uuid.uuid4())
    body = {"customer_name": "Ana", "payment_method": "cash", "items": [{"product_id": str(p.id), "quantity": 1}]}
    r1 = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": key})
    assert r1.status_code == 201
    token = r1.json()["tracking_token"]
    dump = db.execute(text("SELECT key, response_body::text FROM idempotency_keys")).all()
    assert len(dump) == 1
    assert token not in dump[0][1] and key not in dump[0][0] and key not in dump[0][1]
    # a genuine retry still gets the original, working token back
    r2 = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": key})
    assert r2.status_code == 201 and r2.headers["idempotent-replayed"] == "true"
    assert r2.json() == r1.json()
    assert client.get(f"/api/public/orders/{token}").status_code == 200


def test_replay_after_secret_rotation_is_409_not_500(client, make_product, monkeypatch):
    from app.config import get_settings

    p = make_product()
    key = str(uuid.uuid4())
    body = {"customer_name": "Rota", "payment_method": "cash", "items": [{"product_id": str(p.id), "quantity": 1}]}
    assert client.post("/api/public/orders", json=body, headers={"Idempotency-Key": key}).status_code == 201
    monkeypatch.setattr(get_settings(), "jwt_secret", "another-secret-" + "Q" * 40)
    r = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": key})
    assert r.status_code == 409 and r.json()["code"] == "idempotency_replay_unavailable"


def test_admin_order_token_not_stored(client, auth, make_product, db):
    p = make_product()
    key = str(uuid.uuid4())
    body = {"customer_name": "Mostrador", "payment_method": "cash",
            "items": [{"product_id": str(p.id), "quantity": 1}]}
    r1 = client.post("/api/admin/orders", json=body, headers={**auth, "Idempotency-Key": key})
    assert r1.status_code == 201
    stored = db.scalar(text("SELECT response_body::text FROM idempotency_keys"))
    assert r1.json()["tracking_token"] not in stored
    r2 = client.post("/api/admin/orders", json=body, headers={**auth, "Idempotency-Key": key})
    assert r2.json()["tracking_token"] == r1.json()["tracking_token"]


def test_idempotency_purge(client, make_product, place_order, db):
    from app.cli import main
    from app.services.idempotency import purge_older_than

    p = make_product()
    place_order([(p, 1)])
    place_order([(p, 2)], name="Otro")
    db.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '3 days' WHERE ctid IN "
                    "(SELECT ctid FROM idempotency_keys LIMIT 1)"))
    db.commit()
    assert purge_older_than(db, 48) == 1
    assert db.scalar(select(text("count(*)")).select_from(IdempotencyKey)) == 1
    assert main(["purge-idempotency", "--hours", "0"]) == 0
    db.expire_all()
    assert db.scalar(select(text("count(*)")).select_from(IdempotencyKey)) == 0


# ------------------------------------------------------------------ customers: no attach by phone


def test_public_order_does_not_attach_to_existing_customer(client, auth, make_product, place_order,
                                                           order_id_by_code, db):
    marta = client.post("/api/admin/customers", json={"name": "Doña Marta", "phone": "3001234567"},
                        headers=auth).json()
    p = make_product(price="10000")
    o = place_order([(p, 1)], method="credit", name="Atacante", phone="+57 300 123 4567").json()
    oid = order_id_by_code(o["order"]["code"])
    d = client.get(f"/api/admin/orders/{oid}", headers=auth).json()
    assert d["customer_id"] != marta["id"] and d["customer_verified"] is False
    assert [m["id"] for m in d["possible_customer_matches"]] == [marta["id"]]
    # authorizing the fiado does NOT put debt on Marta
    assert client.post(f"/api/admin/orders/{oid}/credit/authorize", json={}, headers=auth).status_code == 200
    md = client.get(f"/api/admin/customers/{marta['id']}", headers=auth).json()
    assert md["orders_count"] == 0 and md["debt_total"] == "0.00"
    by_c = client.get("/api/admin/debts/by-customer", headers=auth).json()["items"]
    assert [c["customer_name"] for c in by_c] == ["Atacante"]
    # only an explicit, audited admin action moves the order (and its debt)
    r = client.post(f"/api/admin/orders/{oid}/customer", json={"customer_id": marta["id"], "note": "Es ella"},
                    headers=auth)
    assert r.status_code == 200 and r.json()["customer_id"] == marta["id"]
    assert client.get(f"/api/admin/customers/{marta['id']}", headers=auth).json()["debt_total"] == "10000.00"
    log = db.scalar(select(AuditLog).where(AuditLog.action == "order_customer_linked"))
    assert log is not None and log.details["order_customer_name"] == "Atacante"
    r = client.post(f"/api/admin/orders/{oid}/customer", json={"customer_id": marta["id"]}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "customer_already_linked"


def test_verify_customer_conflicts_with_verified_phone(client, auth, make_product, place_order, db):
    client.post("/api/admin/customers", json={"name": "Luis", "phone": "3159998877"}, headers=auth)
    p = make_product()
    place_order([(p, 1)], name="Luis W", phone="3159998877")
    unverified = db.scalar(select(Customer).where(Customer.is_verified.is_(False)))
    r = client.post(f"/api/admin/customers/{unverified.id}/verify", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"


def test_counter_order_reuses_verified_customer(client, auth, make_product):
    c = client.post("/api/admin/customers", json={"name": "Pedro", "phone": "3111111111"}, headers=auth).json()
    p = make_product()
    body = {"customer_name": "Pedro", "customer_phone": "311 111 1111", "payment_method": "cash",
            "items": [{"product_id": str(p.id), "quantity": 1}]}
    r = client.post("/api/admin/orders", json=body, headers=auth)
    assert r.status_code == 201 and r.json()["order"]["customer_id"] == c["id"]


# ------------------------------------------------------------------ proofs: reuse and pending totals


def test_duplicate_proof_image_flagged_and_needs_override(client, auth, make_product, place_order):
    p = make_product(price="8000")
    o1 = place_order([(p, 1)], method="transfer", name="Uno").json()
    o2 = place_order([(p, 1)], method="transfer", name="Dos").json()
    img = image_bytes()
    a = _upload(client, o1["tracking_token"], "8000", img).json()
    b = _upload(client, o2["tracking_token"], "8000", img).json()
    pa = client.get(f"/api/admin/proofs/{a['id']}", headers=auth).json()
    pb = client.get(f"/api/admin/proofs/{b['id']}", headers=auth).json()
    assert pa["duplicate_of_proof_ids"] == [b["id"]] and pb["duplicate_of_proof_ids"] == [a["id"]]
    assert "duplicate_of_proof_ids" not in client.get(f"/api/public/orders/{o1['tracking_token']}").json()["proofs"][0]
    assert client.post(f"/api/admin/proofs/{a['id']}/approve", json={}, headers=auth).status_code == 200
    r = client.post(f"/api/admin/proofs/{b['id']}/approve", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "duplicate_proof"
    r = client.post(f"/api/admin/proofs/{b['id']}/approve", json={"allow_duplicate": True}, headers=auth)
    assert r.status_code == 200
    # a different image has no duplicates
    o3 = place_order([(p, 1)], method="transfer", name="Tres").json()
    c = _upload(client, o3["tracking_token"], "8000", image_bytes(size=(50, 50))).json()
    assert client.get(f"/api/admin/proofs/{c['id']}", headers=auth).json()["duplicate_of_proof_ids"] == []


def test_pending_proofs_count_against_balance(client, auth, make_product, place_order):
    p = make_product(price="8000")
    o = place_order([(p, 1)], method="transfer").json()
    t = o["tracking_token"]
    assert _upload(client, t, "8000").status_code == 201
    r = _upload(client, t, "8000", image_bytes(size=(41, 30)))
    assert r.status_code == 409 and r.json()["code"] == "pending_proofs_cover_balance"
    pub = client.get(f"/api/public/orders/{t}").json()
    assert pub["pending_proofs_total"] == "8000.00"

    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    o = place_order([(p, 1)], method="transfer", name="Parcial").json()
    t = o["tracking_token"]
    assert _upload(client, t, "5000").status_code == 201
    r = _upload(client, t, "4000", image_bytes(size=(42, 30)))
    assert r.status_code == 422 and r.json()["code"] == "amount_exceeds_balance"
    assert _upload(client, t, "3000", image_bytes(size=(43, 30))).status_code == 201
    assert client.get(f"/api/public/orders/{t}").json()["pending_proofs_total"] == "8000.00"


# ------------------------------------------------------------------ duplicate order guard under concurrency


def test_concurrent_identical_orders_create_one(make_product):
    from app.db import SessionLocal
    from app.errors import AppError
    from app.schemas.orders import OrderCreate
    from app.services import orders as orders_svc

    p = make_product()
    body = OrderCreate(customer_name="Rápido", customer_phone="3109998888", payment_method="cash",
                       items=[{"product_id": p.id, "quantity": 2}])
    n = 6
    barrier = threading.Barrier(n)
    results: list[object] = []

    def worker():
        s = SessionLocal()
        try:
            barrier.wait()
            orders_svc.create_order(s, body, actor=None, ip=None)
            s.commit()
            results.append(201)
        except AppError as e:
            s.rollback()
            results.append(e.code)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert sorted(results, key=str) == [201] + ["duplicate_order"] * (n - 1)


# ------------------------------------------------------------------ fiado paid before authorization


def test_credit_paid_before_authorization_is_not_stuck(client, auth, make_product, place_order, order_id_by_code):
    p = make_product(price="6000")
    o = place_order([(p, 1)], method="credit", name="Paga ya").json()
    oid = order_id_by_code(o["order"]["code"])
    d = client.get(f"/api/admin/orders/{oid}", headers=auth).json()
    assert d["allowed_transitions"] == []
    r = _pay(client, auth, oid, "6000", method="transfer")
    assert r.status_code == 201
    assert r.json()["order"]["credit_status"] == "not_needed" and r.json()["order"]["payment_status"] == "paid"
    d = client.get(f"/api/admin/orders/{oid}", headers=auth).json()
    assert d["allowed_transitions"] == ["confirmed"] and d["can_cancel"] is False
    r = client.post(f"/api/admin/orders/{oid}/status", json={"status": "confirmed"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["allowed_transitions"] == ["preparing", "ready", "delivered"]
    r = client.post(f"/api/admin/orders/{oid}/status", json={"status": "delivered"}, headers=auth)
    assert r.status_code == 200 and r.json()["is_debt"] is False


# ------------------------------------------------------------------ cash


def test_closed_session_transfer_income_is_frozen(client, auth, make_product, place_order, order_id_by_code,
                                                  open_cash):
    s = open_cash("50000")
    p = make_product(price="5000")
    oid = order_id_by_code(place_order([(p, 1)], method="transfer").json()["order"]["code"])
    pay = _pay(client, auth, oid, "5000", method="transfer").json()["payment"]
    r = client.post(f"/api/admin/cash/sessions/{s['id']}/close",
                    json={"counts": [{"kind": "bill", "denomination": "50000", "quantity": 1}]}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["transfer_income"] == "5000.00"
    open_cash("0")
    r = client.post(f"/api/admin/payments/{pay['id']}/refund", json={"reason": "Error"}, headers=auth)
    assert r.status_code == 200
    closed = client.get(f"/api/admin/cash/sessions/{s['id']}", headers=auth).json()
    assert closed["summary"]["transfer_income"] == "5000.00"
    assert closed["summary"]["transfer_payments_count"] == 1


def test_cash_count_and_order_overflow_are_422(client, auth, open_cash, make_product, place_order):
    s = open_cash("0")
    r = client.post(f"/api/admin/cash/sessions/{s['id']}/close",
                    json={"counts": [{"kind": "bill", "denomination": "100000", "quantity": 100000}]}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    p = make_product(price="9000000000")
    r = place_order([(p, 2)])
    assert r.status_code == 422 and r.json()["code"] == "amount_too_large"


def test_numeric_overflow_maps_to_422():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import DataError

    from app.errors import install_error_handlers

    class _Orig(Exception):
        sqlstate = "22003"

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise DataError("UPDATE ...", {}, _Orig("numeric field overflow"))

    r = TestClient(app).get("/boom")
    assert r.status_code == 422 and r.json()["code"] == "amount_too_large"


# ------------------------------------------------------------------ config / logs / QR


def test_placeholder_and_weak_jwt_secrets_rejected():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(jwt_secret="change-me-to-a-long-random-string-of-at-least-32-chars")
    with pytest.raises(ValidationError):
        Settings(jwt_secret="")
    with pytest.raises(ValidationError):
        Settings(jwt_secret="ab" * 30, environment="production")
    Settings(jwt_secret="ab" * 30, environment="development")
    Settings(jwt_secret="Zq3-vN8_xK1pL7wR2tY9uB4mC6dF0gH5jS-eA", environment="production")


def test_access_log_redacts_tracking_token():
    from app.logfilters import RedactTrackingTokenFilter, redact_path

    assert redact_path("/api/public/orders/abcDEF123_-x/proofs") == "/api/public/orders/<token>/proofs"
    assert redact_path("/api/public/orders/abc?x=1") == "/api/public/orders/<token>?x=1"
    assert redact_path("/api/public/orders") == "/api/public/orders"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/public/orders/SECRET", "1.1", 200), None)
    RedactTrackingTokenFilter().filter(rec)
    assert "SECRET" not in rec.getMessage()
    assert any(isinstance(f, RedactTrackingTokenFilter) for f in logging.getLogger("uvicorn.access").filters)


def test_qr_cli_generates_png_and_svg(tmp_path):
    from app import qr
    from app.cli import main

    url = "https://pedidos.example.org/"
    png = tmp_path / "menu.png"
    assert main(["qr", "--url", url, "--output", str(png)]) == 0
    with Image.open(png) as im:
        assert im.format == "PNG" and im.size[0] == im.size[1] and im.size[0] >= 21 * 20
    svg = tmp_path / "menu.svg"
    assert main(["qr", "--url", url, "--output", str(svg), "--error", "Q"]) == 0
    assert svg.read_text(encoding="utf-8").lstrip().startswith("<?xml")
    assert qr.build(url).data_list[0].data == url.encode()
    assert main(["qr", "--url", "javascript:alert(1)", "--output", str(tmp_path / "x.png")]) == 1
    assert main(["qr", "--url", url, "--output", str(tmp_path / "x.gif")]) == 1


def test_money_constant_matches_column():
    from app.money import MAX_MONEY

    assert MAX_MONEY == Decimal("9999999999.99")
