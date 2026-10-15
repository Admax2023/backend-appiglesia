from __future__ import annotations

import threading
import uuid

from sqlalchemy import func, select

from app.models import CashMovement, Payment, PaymentProof
from tests.conftest import image_bytes


def _transfer_order(place_order, make_product, price="8000", qty=1):
    p = make_product(price=price)
    return place_order([(p, qty)], method="transfer").json()


def _upload(client, token, data=None, name="comprobante.png", amount="8000", ctype="image/png"):
    return client.post(
        f"/api/public/orders/{token}/proofs",
        data={"declared_amount": amount},
        files={"file": (name, data if data is not None else image_bytes(), ctype)},
    )


def test_upload_valid_proofs(client, make_product, place_order, auth):
    o = _transfer_order(place_order, make_product)
    r = _upload(client, o["tracking_token"], image_bytes("JPEG"), "foto.jpeg", ctype="image/jpeg")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_verification" and body["declared_amount"] == "8000.00"
    pub = client.get(f"/api/public/orders/{o['tracking_token']}").json()
    assert pub["proofs"][0]["status"] == "pending_verification"
    lst = client.get("/api/admin/proofs?status=pending_verification", headers=auth).json()
    assert lst["total"] == 1
    proof = lst["items"][0]
    assert proof["mime_type"] == "image/jpeg" and proof["order_code"] == o["order"]["code"]
    assert proof["original_filename"] == "foto.jpeg"
    # file served only to admins with the right content type and no-store
    assert client.get(proof["file_url"]).status_code == 401
    f = client.get(proof["file_url"], headers=auth)
    assert f.status_code == 200
    assert f.headers["content-type"] == "image/jpeg"
    assert "no-store" in f.headers["cache-control"]
    assert f.content == image_bytes("JPEG")


def test_upload_webp(client, make_product, place_order):
    o = _transfer_order(place_order, make_product)
    r = _upload(client, o["tracking_token"], image_bytes("WEBP"), "x.webp", ctype="image/webp")
    assert r.status_code == 201, r.text


def test_upload_validation(client, make_product, place_order, db):
    o = _transfer_order(place_order, make_product)
    t = o["tracking_token"]
    # text file disguised with an image extension and content-type
    r = _upload(client, t, b"hello, I am not an image at all" * 10, "foto.jpg", ctype="image/jpeg")
    assert r.status_code == 415 and r.json()["code"] == "unsupported_file_type"
    # real PNG named .jpg => extension/content mismatch
    r = _upload(client, t, image_bytes("PNG"), "foto.jpg", ctype="image/jpeg")
    assert r.status_code == 415 and r.json()["code"] == "file_type_mismatch"
    # PNG magic bytes but garbage content
    r = _upload(client, t, b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 50, "x.png")
    assert r.status_code == 415 and r.json()["code"] == "invalid_image"
    # executable / pdf
    r = _upload(client, t, b"%PDF-1.4 fake", "doc.pdf", ctype="application/pdf")
    assert r.status_code == 415
    # oversized (> 5 MB)
    big = image_bytes("PNG") + b"\x00" * (5 * 1024 * 1024)
    r = _upload(client, t, big, "big.png")
    assert r.status_code == 413
    # empty
    r = _upload(client, t, b"", "e.png")
    assert r.status_code in (415, 422)
    # amount validations
    r = _upload(client, t, amount="8000.01")
    assert r.status_code == 422 and r.json()["code"] == "amount_exceeds_balance"
    r = _upload(client, t, amount="0")
    assert r.status_code == 422
    assert db.scalar(select(func.count()).select_from(PaymentProof)) == 0


def test_path_traversal_filename_is_sanitized(client, make_product, place_order, auth):
    o = _transfer_order(place_order, make_product)
    r = _upload(client, o["tracking_token"], name="../../etc/passwd.png")
    assert r.status_code == 201
    proof = client.get("/api/admin/proofs", headers=auth).json()["items"][0]
    assert proof["original_filename"] == "passwd.png"


def test_pending_proof_limit(client, make_product, place_order, auth):
    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    o = _transfer_order(place_order, make_product)
    for _ in range(3):
        assert _upload(client, o["tracking_token"], amount="1000").status_code == 201
    r = _upload(client, o["tracking_token"], amount="1000")
    assert r.status_code == 409 and r.json()["code"] == "too_many_pending_proofs"


def _proof_id(client, auth):
    return client.get("/api/admin/proofs", headers=auth).json()["items"][0]["id"]


def test_approve_once_creates_exactly_one_payment(client, make_product, place_order, auth, db):
    o = _transfer_order(place_order, make_product)
    _upload(client, o["tracking_token"])
    pid = _proof_id(client, auth)
    r1 = client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers=auth)
    assert r1.status_code == 200, r1.text
    b = r1.json()
    assert b["proof"]["status"] == "approved" and b["payment"]["method"] == "transfer"
    assert b["order"]["payment_status"] == "paid" and b["order"]["balance_due"] == "0.00"
    r2 = client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers=auth)
    assert r2.status_code == 409 and r2.json()["code"] == "proof_not_pending"
    # same idempotency key => replay
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers={**auth, **key}).status_code == 409
    assert db.scalar(select(func.count()).select_from(Payment)) == 1
    assert db.scalar(select(func.count()).select_from(CashMovement)) == 0  # transfers are not cash
    pub = client.get(f"/api/public/orders/{o['tracking_token']}").json()
    assert pub["payment_status"] == "paid"


def test_concurrent_approval_two_sessions(admin_user, client, make_product, place_order, auth, db):
    from app.db import SessionLocal
    from app.errors import AppError
    from app.models import User
    from app.services import payments

    o = _transfer_order(place_order, make_product)
    _upload(client, o["tracking_token"])
    pid = uuid.UUID(_proof_id(client, auth))
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        s = SessionLocal()
        try:
            user = s.get(User, admin_user.id)
            barrier.wait()
            payments.approve_proof(s, pid, None, None, user, None)
            s.commit()
            with lock:
                results.append("ok")
        except AppError as e:
            s.rollback()
            with lock:
                results.append(e.code)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["ok", "proof_not_pending"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Payment).where(Payment.proof_id == pid)) == 1
    assert db.scalar(select(func.count()).select_from(CashMovement)) == 0


def test_db_unique_constraint_blocks_second_payment_for_proof(client, make_product, place_order, auth, db):
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    from app.enums import PaymentMethod
    from app.models import Order

    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    o = _transfer_order(place_order, make_product)
    assert _upload(client, o["tracking_token"], amount="4000").status_code == 201
    pid = _proof_id(client, auth)
    client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers=auth)
    order_id = db.scalar(select(Order.id).where(Order.code == o["order"]["code"]))
    db.add(Payment(order_id=order_id, method=PaymentMethod.transfer, amount=Decimal("1"), proof_id=uuid.UUID(pid)))
    try:
        db.flush()
        raise AssertionError("expected unique violation")
    except IntegrityError:
        db.rollback()


def test_reject_requires_reason_and_keeps_history(client, make_product, place_order, auth):
    o = _transfer_order(place_order, make_product)
    _upload(client, o["tracking_token"])
    pid = _proof_id(client, auth)
    r = client.post(f"/api/admin/proofs/{pid}/reject", json={}, headers=auth)
    assert r.status_code == 422
    r = client.post(f"/api/admin/proofs/{pid}/reject", json={"reason": "  "}, headers=auth)
    assert r.status_code == 422
    r = client.post(f"/api/admin/proofs/{pid}/reject", json={"reason": "No llegó la transferencia"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["proof"]["status"] == "rejected" and r.json()["payment"] is None
    assert client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers=auth).status_code == 409
    # customer sees the reason and can upload a new proof; history keeps both
    pub = client.get(f"/api/public/orders/{o['tracking_token']}").json()
    assert pub["proofs"][0]["rejection_reason"] == "No llegó la transferencia"
    assert _upload(client, o["tracking_token"]).status_code == 201
    allp = client.get("/api/admin/proofs", headers=auth).json()
    assert allp["total"] == 2
    statuses = sorted(p["status"] for p in allp["items"])
    assert statuses == ["pending_verification", "rejected"]
    logs = client.get("/api/admin/audit-logs?action=proof_rejected", headers=auth).json()
    assert logs["total"] == 1


def test_refund_approved_proof(client, make_product, place_order, auth):
    o = _transfer_order(place_order, make_product)
    _upload(client, o["tracking_token"])
    pid = _proof_id(client, auth)
    client.post(f"/api/admin/proofs/{pid}/approve", json={}, headers=auth)
    r = client.post(f"/api/admin/proofs/{pid}/refund", json={"reason": "Pedido no entregado"}, headers=auth)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["proof"]["status"] == "refunded" and b["payment"]["status"] == "refunded"
    assert b["order"]["payment_status"] == "unpaid" and b["order"]["balance_due"] == "8000.00"
    r = client.post(f"/api/admin/proofs/{pid}/refund", json={"reason": "otra"}, headers=auth)
    assert r.status_code == 409


def test_approve_with_partial_amount(client, make_product, place_order, auth):
    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    o = _transfer_order(place_order, make_product)
    _upload(client, o["tracking_token"], amount="8000")
    pid = _proof_id(client, auth)
    r = client.post(f"/api/admin/proofs/{pid}/approve", json={"amount": "9000"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "amount_exceeds_balance"
    r = client.post(f"/api/admin/proofs/{pid}/approve", json={"amount": "5000", "note": "Llegó menos"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["order"]["balance_due"] == "3000.00" and r.json()["order"]["payment_status"] == "partial"
