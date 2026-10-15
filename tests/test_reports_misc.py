from __future__ import annotations

import uuid


def _pay(client, auth, oid, amount, method="cash"):
    return client.post(f"/api/admin/orders/{oid}/payments", json={"method": method, "amount": amount},
                       headers={**auth, "Idempotency-Key": str(uuid.uuid4())})


def test_reports(client, auth, make_product, place_order, order_id_by_code, open_cash):
    a = make_product("Empanada", "2500")
    b = make_product("Papa", "4000")
    o1 = order_id_by_code(place_order([(a, 2), (b, 1)], name="Uno").json()["order"]["code"])  # 9000
    o2 = order_id_by_code(place_order([(b, 2)], method="transfer", name="Dos").json()["order"]["code"])  # 8000
    o3 = order_id_by_code(place_order([(a, 1)], name="Tres").json()["order"]["code"])  # 2500 -> cancelled
    client.post(f"/api/admin/orders/{o3}/cancel", json={"reason": "No vino"}, headers=auth)
    open_cash("0")
    _pay(client, auth, o1, "9000")
    _pay(client, auth, o2, "8000", method="transfer")

    s = client.get("/api/admin/reports/sales", headers=auth).json()
    assert s["orders_count"] == 2 and s["cancelled_orders_count"] == 1
    assert s["total_sold"] == "17000.00" and s["collected"] == "17000.00" and s["pending"] == "0.00"
    assert s["payments_received"] == "17000.00" and s["net_received"] == "17000.00"
    assert len(s["by_day"]) == 1 and s["timezone"] == "America/Bogota"

    pr = client.get("/api/admin/reports/products", headers=auth).json()
    by = {i["product_name"]: i for i in pr["items"]}
    assert by["Papa"]["quantity"] == 3 and by["Papa"]["total"] == "12000.00"
    assert by["Empanada"]["quantity"] == 2

    pm = client.get("/api/admin/reports/payment-methods", headers=auth).json()
    pays = {x["method"]: x for x in pm["payments"]}
    assert pays["cash"]["amount"] == "9000.00" and pays["transfer"]["amount"] == "8000.00"
    orders = {x["payment_method"]: x for x in pm["orders"]}
    assert orders["cash"]["orders_count"] == 1 and orders["credit"]["orders_count"] == 0

    d = client.get("/api/admin/reports/debts", headers=auth).json()
    assert d["total_outstanding"] == "0.00"

    r = client.get("/api/admin/reports/sales?date_from=2026-01-10&date_to=2026-01-01", headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "invalid_date_range"


def test_products_crud(client, auth, make_product, place_order):
    r = client.post("/api/admin/products", json={"name": "Buñuelo", "price": "1500"}, headers=auth)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["price"] == "1500.00"
    assert client.post("/api/admin/products", json={"name": "X", "price": "-1"}, headers=auth).status_code == 422
    assert client.post("/api/admin/products", json={"name": "", "price": "1"}, headers=auth).status_code == 422
    r = client.post(f"/api/admin/products/{pid}/deactivate", headers=auth)
    assert r.json()["is_active"] is False
    assert all(p["id"] != pid for p in client.get("/api/public/menu").json()["products"])
    r = client.post(f"/api/admin/products/{pid}/activate", headers=auth)
    assert r.json()["is_active"] is True
    lst = client.get("/api/admin/products?search=buñ", headers=auth).json()
    assert lst["total"] == 1
    # delete only when unused
    used = make_product()
    place_order([(used, 1)])
    r = client.delete(f"/api/admin/products/{used.id}", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "product_in_use"
    assert client.delete(f"/api/admin/products/{pid}", headers=auth).status_code == 204
    assert client.get(f"/api/admin/products/{pid}", headers=auth).status_code == 404


def test_settings_update(client, auth):
    r = client.patch("/api/admin/settings", json={"enabled_payment_methods": []}, headers=auth)
    assert r.status_code == 422
    r = client.patch("/api/admin/settings", json={"timezone": "Mars/Base"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "invalid_timezone"
    r = client.patch("/api/admin/settings", json={"enabled_payment_methods": ["cash"], "bank_name": "Nequi"},
                     headers=auth)
    assert r.status_code == 200 and r.json()["enabled_payment_methods"] == ["cash"]
    pub = client.get("/api/public/settings").json()
    assert pub["bank_name"] is None  # bank details hidden when transfer disabled
    assert client.get("/api/admin/audit-logs?action=settings_updated", headers=auth).json()["total"] == 1


def test_customers(client, auth, make_product, place_order):
    p = make_product()
    place_order([(p, 1)], name="Elena", phone="3201234567")
    place_order([(p, 2)], name="Elena G", phone="320-123-4567")
    lst = client.get("/api/admin/customers?search=3201234", headers=auth).json()
    # anonymous web orders never merge by phone: two unverified customers, one order each
    assert lst["total"] == 2 and all(i["orders_count"] == 1 and not i["is_verified"] for i in lst["items"])
    first, second = sorted(lst["items"], key=lambda i: i["created_at"])
    cid = first["id"]
    r = client.post(f"/api/admin/customers/{cid}/verify", headers=auth)
    assert r.status_code == 200 and r.json()["is_verified"] is True
    second_order = client.get(f"/api/admin/orders?customer_id={second['id']}", headers=auth).json()["items"][0]
    r = client.post(f"/api/admin/orders/{second_order['id']}/customer", json={"customer_id": cid}, headers=auth)
    assert r.status_code == 200 and r.json()["customer_id"] == cid and r.json()["customer_verified"] is True
    # the orphaned unverified row is gone; the verified customer now has both orders
    lst = client.get("/api/admin/customers?search=3201234", headers=auth).json()
    assert lst["total"] == 1 and lst["items"][0]["orders_count"] == 2
    r = client.patch(f"/api/admin/customers/{cid}", json={"notes": "Vecina"}, headers=auth)
    assert r.status_code == 200 and r.json()["notes"] == "Vecina"
    r = client.post("/api/admin/customers", json={"name": "Otro", "phone": "3201234567"}, headers=auth)
    assert r.status_code == 409


def test_security_headers_cors_and_body_limit(client):
    r = client.get("/api/public/menu", headers={"Origin": "http://localhost:5173"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = client.get("/api/public/menu", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    big = {"customer_name": "A" * 100, "payment_method": "cash", "items": [], "notes": "x" * 70000}
    r = client.post("/api/public/orders", json=big, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_public_rate_limit(client, make_product, place_order):
    from app.ratelimit import limiter

    p = make_product()
    limiter.enabled = True
    codes = [place_order([(p, i + 1)], name=f"Cliente {i}").status_code for i in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_error_format_404(client, auth):
    r = client.get(f"/api/admin/orders/{uuid.uuid4()}", headers=auth)
    assert r.status_code == 404 and r.json() == {"detail": "Pedido no encontrado", "code": "order_not_found"}
    r = client.get("/api/admin/orders/not-a-uuid", headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "validation_error" and r.json()["errors"]
