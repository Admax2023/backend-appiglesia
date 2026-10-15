from __future__ import annotations

import threading
import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.models import CashMovement, Payment


def _pay(client, auth, oid, amount, method="cash", key=None):
    return client.post(
        f"/api/admin/orders/{oid}/payments",
        json={"method": method, "amount": amount},
        headers={**auth, "Idempotency-Key": key or str(uuid.uuid4())},
    )


def test_cash_payment_requires_open_session(client, auth, make_product, place_order, order_id_by_code, db):
    p = make_product(price="5000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    r = _pay(client, auth, oid, "5000")
    assert r.status_code == 409 and r.json()["code"] == "no_open_cash_session"
    assert db.scalar(select(func.count()).select_from(Payment)) == 0


def test_register_payment_requires_idempotency_key(client, auth, make_product, place_order, order_id_by_code,
                                                    open_cash):
    p = make_product(price="5000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    open_cash()
    r = client.post(f"/api/admin/orders/{oid}/payments", json={"method": "cash", "amount": "5000"}, headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"


def test_cash_payment_idempotent_replay_and_conflict(client, auth, make_product, place_order, order_id_by_code,
                                                     open_cash, db):
    p = make_product(price="5000")
    oid = order_id_by_code(place_order([(p, 2)]).json()["order"]["code"])
    open_cash("20000")
    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    key = str(uuid.uuid4())
    r1 = _pay(client, auth, oid, "4000", key=key)
    r2 = _pay(client, auth, oid, "4000", key=key)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers["Idempotent-Replayed"] == "true"
    body = r1.json()
    assert body["order"]["payment_status"] == "partial"
    assert body["order"]["balance_due"] == "6000.00"
    assert body["cash_movement_id"]
    assert db.scalar(select(func.count()).select_from(Payment)) == 1
    assert db.scalar(select(func.count()).select_from(CashMovement)) == 1
    r3 = _pay(client, auth, oid, "5000", key=key)
    assert r3.status_code == 409 and r3.json()["code"] == "idempotency_key_conflict"
    cur = client.get("/api/admin/cash/current", headers=auth).json()
    assert cur["summary"]["cash_sales"] == "4000.00"
    assert cur["summary"]["expected_cash"] == "24000.00"


def test_concurrent_same_idempotency_key_creates_one_payment(admin_user, make_product, place_order,
                                                             order_id_by_code, open_cash, db):
    from app.db import SessionLocal
    from app.errors import AppError
    from app.schemas.orders import PaymentResultOut, PaymentOut, RegisterPaymentIn
    from app.services import idempotency, payments
    from app.services import orders as orders_svc

    p = make_product(price="7000")
    oid = uuid.UUID(order_id_by_code(place_order([(p, 1)]).json()["order"]["code"]))
    open_cash()
    key = str(uuid.uuid4())
    body = RegisterPaymentIn(method="cash", amount=Decimal("7000"))
    barrier = threading.Barrier(2)
    results: list[object] = []

    def worker():
        s = SessionLocal()
        try:
            user = s.get(type(admin_user), admin_user.id)
            barrier.wait()

            def op():
                pay, order, mv = payments.register_payment(s, oid, body, user, None)
                return 201, PaymentResultOut(payment=PaymentOut.model_validate(pay),
                                             order=orders_svc.summary(s, order), cash_movement_id=mv.id)

            resp = idempotency.run(s, scope=f"admin:payment:{oid}", key=key, payload=body.model_dump(mode="json"),
                                   operation=op)
            results.append(resp.status_code)
        except AppError as e:
            results.append(e.code)
        finally:
            s.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results == [201, 201]
    assert db.scalar(select(func.count()).select_from(Payment)) == 1
    assert db.scalar(select(func.count()).select_from(CashMovement)) == 1


def test_overpayment_rejected(client, auth, make_product, place_order, order_id_by_code, open_cash):
    p = make_product(price="3000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    open_cash()
    r = _pay(client, auth, oid, "3000.01")
    assert r.status_code == 422 and r.json()["code"] == "amount_exceeds_balance"
    assert _pay(client, auth, oid, "3000").status_code == 201
    r = _pay(client, auth, oid, "1")
    assert r.status_code == 409 and r.json()["code"] == "order_already_paid"


def test_partial_payment_policy(client, auth, make_product, place_order, order_id_by_code, open_cash):
    p = make_product(price="3000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    open_cash()
    r = _pay(client, auth, oid, "1000")
    assert r.status_code == 422 and r.json()["code"] == "partial_payments_not_allowed"
    client.patch("/api/admin/settings", json={"allow_partial_payments": True}, headers=auth)
    r = _pay(client, auth, oid, "1000")
    assert r.status_code == 201 and r.json()["order"]["payment_status"] == "partial"
    r = _pay(client, auth, oid, "2000", method="transfer")
    assert r.status_code == 201
    assert r.json()["order"]["payment_status"] == "paid"
    assert r.json()["cash_movement_id"] is None


def test_money_validation(client, auth, make_product, place_order, order_id_by_code, open_cash):
    p = make_product(price="3000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    open_cash()
    for bad in ("0", "-5", "10.123", "abc"):
        r = _pay(client, auth, oid, bad)
        assert r.status_code == 422, bad


def test_credit_requires_authorization_then_debt_and_abonos(client, auth, make_product, place_order,
                                                            order_id_by_code, open_cash):
    p = make_product(price="10000")
    created = place_order([(p, 1)], method="credit", name="Doña Rosa", phone="3104445566").json()
    assert created["order"]["credit_status"] == "pending_authorization"
    oid = order_id_by_code(created["order"]["code"])
    r = client.post(f"/api/admin/orders/{oid}/status", json={"status": "confirmed"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "credit_authorization_required"
    assert client.get("/api/admin/debts", headers=auth).json()["total"] == 0

    r = client.post(f"/api/admin/orders/{oid}/credit/authorize", json={"note": "Conocida"}, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["credit_status"] == "authorized" and d["status"] == "confirmed" and d["is_debt"] is True
    r = client.post(f"/api/admin/orders/{oid}/credit/authorize", json={}, headers=auth)
    assert r.status_code == 409

    debts = client.get("/api/admin/debts", headers=auth).json()
    assert debts["total"] == 1 and debts["items"][0]["balance_due"] == "10000.00"
    # delivery allowed with balance because credit is authorized
    client.post(f"/api/admin/orders/{oid}/status", json={"status": "delivered"}, headers=auth)
    # abono (partial) allowed for authorized credit even if partial payments are disabled
    open_cash()
    r = _pay(client, auth, oid, "4000")
    assert r.status_code == 201 and r.json()["order"]["balance_due"] == "6000.00"
    by_c = client.get("/api/admin/debts/by-customer", headers=auth).json()
    assert by_c["items"][0]["total_due"] == "6000.00"
    assert by_c["items"][0]["customer_name"] == "Doña Rosa"
    cust_id = by_c["items"][0]["customer_id"]
    cd = client.get(f"/api/admin/customers/{cust_id}", headers=auth).json()
    assert cd["debt_total"] == "6000.00" and len(cd["recent_payments"]) == 1

    # write-off requires reason, is audited and zeroes the debt
    r = client.post(f"/api/admin/orders/{oid}/write-off", json={}, headers=auth)
    assert r.status_code == 422
    r = client.post(f"/api/admin/orders/{oid}/write-off", json={"reason": "Donación"}, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["order"]["balance_due"] == "0.00"
    assert r.json()["order"]["payment_status"] == "written_off"
    assert client.get("/api/admin/debts", headers=auth).json()["total"] == 0
    logs = client.get("/api/admin/audit-logs?action=debt_written_off", headers=auth).json()
    assert logs["total"] == 1 and logs["items"][0]["user_name"] == "Admin Principal"


def test_credit_disabled_rejected_publicly(client, auth, make_product, place_order):
    client.patch("/api/admin/settings", json={"enabled_payment_methods": ["cash", "transfer"]}, headers=auth)
    p = make_product()
    r = place_order([(p, 1)], method="credit")
    assert r.status_code == 422 and r.json()["code"] == "payment_method_disabled"


def test_credit_reject(client, auth, make_product, place_order, order_id_by_code):
    p = make_product()
    oid = order_id_by_code(place_order([(p, 1)], method="credit").json()["order"]["code"])
    r = client.post(f"/api/admin/orders/{oid}/credit/reject", json={"reason": "No conocido"}, headers=auth)
    assert r.status_code == 200 and r.json()["credit_status"] == "rejected"
    r = client.post(f"/api/admin/orders/{oid}/write-off", json={"reason": "x y z"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "not_a_debt"


def test_cash_refund_creates_outflow(client, auth, make_product, place_order, order_id_by_code, open_cash):
    p = make_product(price="5000")
    oid = order_id_by_code(place_order([(p, 1)]).json()["order"]["code"])
    open_cash("10000")
    pay = _pay(client, auth, oid, "5000").json()
    pid = pay["payment"]["id"]
    r = client.post(f"/api/admin/payments/{pid}/refund", json={"reason": "Producto agotado"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["status"] == "refunded"
    # active order that owes its total again: 'unpaid' (not 'refunded'), stays in the unpaid filter
    assert r.json()["order"]["payment_status"] == "unpaid"
    assert r.json()["order"]["balance_due"] == "5000.00"
    r = client.post(f"/api/admin/payments/{pid}/refund", json={"reason": "otra vez"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "payment_already_refunded"
    cur = client.get("/api/admin/cash/current", headers=auth).json()["summary"]
    assert cur["cash_refunds"] == "5000.00" and cur["expected_cash"] == "10000.00"
    # now the order can be cancelled
    r = client.post(f"/api/admin/orders/{oid}/cancel", json={"reason": "Reembolsado"}, headers=auth)
    assert r.status_code == 200
