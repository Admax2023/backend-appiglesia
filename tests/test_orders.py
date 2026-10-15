from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models import Order


def test_menu_lists_only_active_products(client, make_product):
    make_product("Empanada", "2500")
    make_product("Oculto", "1000", active=False)
    make_product("Agotado", "1000", available=False)
    r = client.get("/api/public/menu")
    assert r.status_code == 200
    names = {p["name"]: p for p in r.json()["products"]}
    assert set(names) == {"Empanada", "Agotado"}
    assert names["Empanada"]["price"] == "2500.00"
    assert names["Agotado"]["is_available"] is False


def test_public_settings(client):
    r = client.get("/api/public/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "COP"
    assert set(body["enabled_payment_methods"]) == {"cash", "transfer", "credit"}
    assert body["bank_account_number"] == "123-456789-00"
    assert body["limits"]["max_proof_bytes"] == 5 * 1024 * 1024


def test_server_side_total_ignores_client_prices(client, make_product, db):
    a = make_product(price="2500")
    b = make_product(price="4000")
    body = {
        "customer_name": "Ana",
        "payment_method": "cash",
        "total": "1.00",
        "items": [
            {"product_id": str(a.id), "quantity": 2, "unit_price": "1", "price": 1},
            {"product_id": str(b.id), "quantity": 3, "unit_price": "0.01"},
            {"product_id": str(a.id), "quantity": 1},  # duplicated line is merged
        ],
    }
    r = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201, r.text
    data = r.json()
    order = data["order"]
    assert order["total"] == "19500.00"  # 3*2500 + 3*4000
    assert order["balance_due"] == "19500.00"
    assert order["status"] == "pending_confirmation"
    assert order["payment_status"] == "unpaid"
    assert order["code"].startswith("F-") and len(order["code"]) == 7
    assert len(data["tracking_token"]) >= 30
    lines = {i["product_id"]: i for i in order["items"]}
    assert lines[str(a.id)]["quantity"] == 3
    assert lines[str(a.id)]["unit_price"] == "2500.00"
    # DB stores only the hash of the token
    stored = db.scalar(select(Order.tracking_token_hash).where(Order.code == order["code"]))
    assert stored != data["tracking_token"] and len(stored) == 64


def test_unit_price_snapshot_survives_price_change(client, make_product, place_order, auth):
    p = make_product(price="2500")
    r = place_order([(p, 2)])
    token = r.json()["tracking_token"]
    r2 = client.patch(f"/api/admin/products/{p.id}", json={"price": "9000"}, headers=auth)
    assert r2.status_code == 200
    o = client.get(f"/api/public/orders/{token}").json()
    assert o["items"][0]["unit_price"] == "2500.00"
    assert o["total"] == "5000.00"
    logs = client.get("/api/admin/audit-logs?action=product_price_changed", headers=auth).json()
    assert logs["total"] == 1


def test_inactive_unavailable_unknown_products_rejected(client, make_product, place_order):
    inactive = make_product(active=False)
    unavailable = make_product(available=False)
    r = place_order([(inactive, 1)])
    assert r.status_code == 422 and r.json()["code"] == "product_unavailable"
    r = place_order([(unavailable, 1)])
    assert r.status_code == 422 and r.json()["code"] == "product_unavailable"

    class Fake:
        id = uuid.uuid4()

    r = place_order([(Fake, 1)])
    assert r.status_code == 422 and r.json()["code"] == "product_not_found"


def test_quantity_and_line_limits(client, make_product, place_order):
    p = make_product()
    r = place_order([(p, 0)])
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    r = place_order([(p, 51)])
    assert r.status_code == 422
    r = place_order([(p, 30), (p, 30)])  # merged 60 > 50
    assert r.status_code == 422 and r.json()["code"] == "quantity_limit"
    body = {"customer_name": "X Y", "payment_method": "cash",
            "items": [{"product_id": str(uuid.uuid4()), "quantity": 1} for _ in range(31)]}
    r = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 422


def test_idempotent_order_creation_replay_and_conflict(client, make_product, place_order, db):
    p = make_product()
    key = str(uuid.uuid4())
    r1 = place_order([(p, 2)], key=key)
    r2 = place_order([(p, 2)], key=key)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert db.scalar(select(func.count()).select_from(Order)) == 1
    r3 = place_order([(p, 3)], key=key)
    assert r3.status_code == 409
    assert r3.json()["code"] == "idempotency_key_conflict"
    assert db.scalar(select(func.count()).select_from(Order)) == 1


def test_idempotency_key_required(client, make_product):
    p = make_product()
    body = {"customer_name": "Ana", "payment_method": "cash", "items": [{"product_id": str(p.id), "quantity": 1}]}
    r = client.post("/api/public/orders", json=body)
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"
    r = client.post("/api/public/orders", json=body, headers={"Idempotency-Key": "bad key!"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_idempotency_key"


def test_duplicate_order_guard(client, make_product, place_order):
    p = make_product()
    r1 = place_order([(p, 2)], phone="300 123 4567")
    assert r1.status_code == 201
    r2 = place_order([(p, 2)], phone="+57 300-123-4567")  # same normalized phone, new key
    assert r2.status_code == 409 and r2.json()["code"] == "duplicate_order"
    r3 = place_order([(p, 3)], phone="3001234567")  # different content is fine
    assert r3.status_code == 201


def test_honeypot_and_disabled_method(client, make_product, place_order, auth):
    p = make_product()
    r = place_order([(p, 1)], extra={"website": "http://spam"})
    assert r.status_code == 400 and r.json()["code"] == "spam_detected"
    client.patch("/api/admin/settings", json={"enabled_payment_methods": ["cash"]}, headers=auth)
    r = place_order([(p, 1)], method="transfer")
    assert r.status_code == 422 and r.json()["code"] == "payment_method_disabled"
    client.patch("/api/admin/settings", json={"accepting_orders": False}, headers=auth)
    r = place_order([(p, 1)], method="cash")
    assert r.status_code == 409 and r.json()["code"] == "orders_closed"


def test_tracking_token_isolation(client, make_product, place_order):
    p = make_product()
    a = place_order([(p, 1)], name="Alicia", phone="3001111111").json()
    b = place_order([(p, 2)], name="Bruno", phone="3002222222").json()
    ra = client.get(f"/api/public/orders/{a['tracking_token']}")
    assert ra.status_code == 200
    body = ra.json()
    assert body["code"] == a["order"]["code"]
    assert body["customer_name"] == "Alicia"
    # minimal data only: no phone, no internal ids, no customer id
    assert "customer_phone" not in body and "id" not in body and "customer_id" not in body
    assert b["order"]["code"] not in ra.text and "Bruno" not in ra.text
    # an order code is NOT a valid token; random tokens are 404
    assert client.get(f"/api/public/orders/{b['order']['code']}").status_code == 404
    assert client.get("/api/public/orders/" + "A" * 32).status_code == 404
    # cannot upload proofs to someone else's order without the token
    r = client.post(f"/api/public/orders/{'Z' * 32}/proofs", data={"declared_amount": "100"},
                    files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50, "image/png")})
    assert r.status_code in (404, 415)


def test_admin_order_list_filters_and_detail(client, make_product, place_order, auth):
    p = make_product(price="1000")
    o1 = place_order([(p, 1)], name="Carla", phone="3005550000").json()["order"]
    place_order([(p, 2)], name="Dario", method="transfer").json()
    r = client.get("/api/admin/orders", headers=auth)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 2 and page["page"] == 1 and page["pages"] == 1
    r = client.get("/api/admin/orders?search=carla", headers=auth).json()
    assert r["total"] == 1 and r["items"][0]["code"] == o1["code"]
    r = client.get("/api/admin/orders?payment_method=transfer", headers=auth).json()
    assert r["total"] == 1
    r = client.get("/api/admin/orders?status=pending_confirmation&status=confirmed&page_size=1", headers=auth).json()
    assert r["total"] == 2 and len(r["items"]) == 1 and r["pages"] == 2
    oid = client.get("/api/admin/orders?search=carla", headers=auth).json()["items"][0]["id"]
    d = client.get(f"/api/admin/orders/{oid}", headers=auth).json()
    assert d["customer_phone"] == "3005550000"
    assert d["status_history"][0]["to_status"] == "pending_confirmation"


def test_status_transition_graph(client, make_product, place_order, order_id_by_code, auth, open_cash):
    p = make_product(price="1000")
    o = place_order([(p, 2)]).json()["order"]
    oid = order_id_by_code(o["code"])

    def go(status):
        return client.post(f"/api/admin/orders/{oid}/status", json={"status": status}, headers=auth)

    assert go("ready").status_code == 409  # pending -> ready not allowed
    assert go("confirmed").status_code == 200
    assert go("preparing").status_code == 200
    assert go("ready").status_code == 200
    r = go("delivered")
    assert r.status_code == 409 and r.json()["code"] == "balance_due_requires_credit"
    open_cash()
    r = client.post(f"/api/admin/orders/{oid}/payments", json={"method": "cash", "amount": "2000"},
                    headers={**auth, "Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201
    r = go("delivered")
    assert r.status_code == 200 and r.json()["status"] == "delivered"
    assert go("confirmed").status_code == 409  # final state
    r = client.post(f"/api/admin/orders/{oid}/cancel", json={"reason": "x cliente"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"


def test_cancel_requires_reason_and_rejects_pending_proofs(client, make_product, place_order, order_id_by_code, auth):
    from tests.conftest import image_bytes

    p = make_product(price="1000")
    created = place_order([(p, 1)], method="transfer").json()
    oid = order_id_by_code(created["order"]["code"])
    client.post(f"/api/public/orders/{created['tracking_token']}/proofs", data={"declared_amount": "1000"},
                files={"file": ("c.png", image_bytes(), "image/png")})
    r = client.post(f"/api/admin/orders/{oid}/cancel", json={}, headers=auth)
    assert r.status_code == 422
    r = client.post(f"/api/admin/orders/{oid}/cancel", json={"reason": "Cliente no vino"}, headers=auth)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "cancelled" and d["balance_due"] == "0.00"
    assert d["proofs"][0]["status"] == "rejected"
    assert "Pedido cancelado" in d["proofs"][0]["rejection_reason"]
    pub = client.get(f"/api/public/orders/{created['tracking_token']}").json()
    assert pub["status"] == "cancelled"


def test_admin_counter_order(client, make_product, auth):
    p = make_product(price="3000")
    body = {"customer_name": "Mostrador", "payment_method": "cash",
            "items": [{"product_id": str(p.id), "quantity": 2}]}
    r = client.post("/api/admin/orders", json=body, headers=auth)
    assert r.status_code == 201, r.text
    o = r.json()["order"]
    assert o["status"] == "confirmed" and o["source"] == "admin" and o["total"] == "6000.00"
