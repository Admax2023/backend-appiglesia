"""Test setup: a REAL PostgreSQL database (default iglesia_test on the docker dev server, port 5433).

The schema is created by running the Alembic migrations (not create_all). Every test starts from
empty tables (TRUNCATE) and default business settings.

Concurrent runs against the same test database would wipe each other's schema, so the whole session
holds a PostgreSQL advisory lock (a second run waits until the first one finishes).
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

# ---- environment must be configured BEFORE importing the app
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://iglesia:iglesia_dev_password@localhost:5433/iglesia_test"
)
_STORAGE = Path(tempfile.mkdtemp(prefix="iglesia_test_storage_"))
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["STORAGE_DIR"] = str(_STORAGE)
os.environ["ENVIRONMENT"] = "test"
os.environ["CORS_ORIGINS"] = "http://localhost:5173"
os.environ["DUPLICATE_ORDER_WINDOW_SECONDS"] = "120"
os.environ["IDEMPOTENCY_PURGE_INTERVAL_MINUTES"] = "0"  # no background purge thread in tests
os.environ["TRUST_PROXY_HEADERS"] = "false"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _ensure_database() -> None:
    url = make_url(TEST_DB_URL)
    admin_url = url.set(database="postgres")
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    eng.dispose()


_LOCK_KEY = 0x1617_7E57  # arbitrary constant: "iglesia test run"


def _acquire_run_lock():
    """Session-long advisory lock on the server's 'postgres' DB, keyed by the test DB name."""
    url = make_url(TEST_DB_URL)
    eng = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    conn = eng.connect()
    conn.execute(text("SELECT pg_advisory_lock(:k, hashtext(:n))"), {"k": _LOCK_KEY, "n": url.database})
    return eng, conn


def _reset_schema() -> None:
    eng = create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    lock_eng, lock_conn = _acquire_run_lock()
    _ensure_database()
    _reset_schema()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    yield
    from app.db import engine

    engine.dispose()
    lock_conn.close()  # closing the session releases the advisory lock
    lock_eng.dispose()


TABLES = [
    "audit_logs", "idempotency_keys", "cash_count_lines", "cash_movements", "payments", "payment_proofs",
    "debt_writeoffs", "order_status_history", "order_items", "orders", "cash_sessions", "customers",
    "products", "refresh_tokens", "users",
]


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    from app.db import engine
    from app.ratelimit import limiter

    with engine.begin() as conn:
        # (CASCADE also empties business_settings because of its FK to users; re-seed it below)
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)}, business_settings RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO business_settings (id, business_name, accepting_orders, cash_enabled, transfer_enabled, "
                "credit_enabled, allow_partial_payments, bank_name, bank_account_type, bank_account_number, "
                "bank_account_holder, bank_holder_id, transfer_instructions, timezone) VALUES "
                "(1, 'Fritos de la Iglesia', true, true, true, true, false, 'Bancolombia', 'Ahorros', "
                "'123-456789-00', 'Iglesia Ejemplo', '900123456', 'Envía el comprobante', 'America/Bogota')"
            )
        )
    limiter.enabled = False
    limiter.reset()
    yield


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


ADMIN_PASSWORD = "Sup3r-Secret-Pass"


@pytest.fixture
def admin_user(db):
    from app.services.auth import create_user

    u = create_user(db, "admin", "Admin Principal", ADMIN_PASSWORD)
    db.commit()
    return u


@pytest.fixture
def admin_tokens(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def auth(admin_tokens):
    return {"Authorization": f"Bearer {admin_tokens['access_token']}"}


# ---------------------------------------------------------------- helpers


def idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def make_product(db):
    from app.models import Product

    def _make(name: str | None = None, price: str = "2500", active: bool = True, available: bool = True):
        p = Product(name=name or f"Producto {uuid.uuid4().hex[:6]}", price=Decimal(price), is_active=active,
                    is_available=available)
        db.add(p)
        db.commit()
        return p

    return _make


@pytest.fixture
def place_order(client):
    def _place(items, method="cash", name="Cliente Prueba", phone=None, key=None, extra=None):
        body = {"customer_name": name, "payment_method": method,
                "items": [{"product_id": str(p.id), "quantity": q} for p, q in items]}
        if phone:
            body["customer_phone"] = phone
        if extra:
            body.update(extra)
        headers = {"Idempotency-Key": key or str(uuid.uuid4())}
        return client.post("/api/public/orders", json=body, headers=headers)

    return _place


@pytest.fixture
def order_id_by_code(db):
    from sqlalchemy import select

    from app.models import Order

    def _get(code: str) -> str:
        db.expire_all()
        return str(db.scalar(select(Order.id).where(Order.code == code)))

    return _get


@pytest.fixture
def open_cash(client, auth):
    def _open(amount="50000"):
        r = client.post("/api/admin/cash/open", json={"opening_amount": amount}, headers=auth)
        assert r.status_code == 201, r.text
        return r.json()

    return _open


def image_bytes(fmt: str = "PNG", size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()
