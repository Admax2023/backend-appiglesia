"""POST /api/public/orders/quote and expected_total / 409 price_changed on order creation."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models import Customer, IdempotencyKey, Order, OrderItem


def _quote(client, items):
    body = {"items": [{"product_id": str(p.id), "quantity": q} for p, q in items]}
    return client.post("/api/public/orders/quote", json=body)


def _counts(db):
    db.expire_all()
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (Customer, Order, OrderItem, IdempotencyKey))


def test_quote_matches_created_order_total(client, make_product, place_order):
    a = make_product(price="2500")
    b = make_product(price="4000.50")
    r = _quote(client, [(a, 2), (b, 3), (a, 1)])
    assert r.status_code == 200, r.text
    qd = r.json()
    assert qd["is_valid"] is True and qd["errors"] == []
    assert qd["total"] == "19501.50"
    lines = {line["product_id"]: line for line in qd["lines"]}
    assert lines[str(a.id)] == {"product_id": str(a.id), "product_name": a.name, "unit_price": "2500.00",
                                "quantity": 3, "line_total": "7500.00"}
    assert lines[str(b.id)]["line_total"] == "12001.50"

    r2 = place_order([(a, 2), (b, 3), (a, 1)], extra={"expected_total": qd["total"]})
    assert r2.status_code == 201, r2.text
    order = r2.json()["order"]
    assert order["total"] == qd["total"]
    assert [(i["product_id"], i["unit_price"], i["quantity"], i["line_total"]) for i in order["items"]] == [
        (ln["product_id"], ln["unit_price"], ln["quantity"], ln["line_total"]) for ln in qd["lines"]
    ]


def test_quote_creates_no_rows(client, make_product, db):
    p = make_product(price="2500")
    before = _counts(db)
    headers = {"Idempotency-Key": str(uuid.uuid4())}  # even if a client sends one, nothing is stored
    body = {"customer_name": "Ana", "customer_phone": "3001234567", "payment_method": "cash",
            "items": [{"product_id": str(p.id), "quantity": 2}]}
    r = client.post("/api/public/orders/quote", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert _counts(db) == before == (0, 0, 0, 0)


def test_quote_ignores_client_prices(client, make_product):
    p = make_product(price="2500")
    body = {"total": "1.00",
            "items": [{"product_id": str(p.id), "quantity": 2, "unit_price": "1", "line_total": "2"}]}
    r = client.post("/api/public/orders/quote", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["unit_price"] == "2500.00"
    assert r.json()["total"] == "5000.00"


def test_quote_reports_inactive_unavailable_unknown(client, make_product):
    ok = make_product(price="1000")
    inactive = make_product(name="Oculto", active=False)
    unavailable = make_product(name="Agotado", available=False)
    missing = uuid.uuid4()
    body = {"items": [{"product_id": str(ok.id), "quantity": 1},
                      {"product_id": str(inactive.id), "quantity": 1},
                      {"product_id": str(unavailable.id), "quantity": 2},
                      {"product_id": str(missing), "quantity": 1}]}
    r = client.post("/api/public/orders/quote", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["is_valid"] is False
    assert d["total"] == "1000.00"
    assert [ln["product_id"] for ln in d["lines"]] == [str(ok.id)]
    errs = {e["product_id"]: e for e in d["errors"]}
    assert errs[str(inactive.id)]["code"] == "product_unavailable"
    assert errs[str(unavailable.id)]["code"] == "product_unavailable"
    assert errs[str(missing)]["code"] == "product_not_found" and errs[str(missing)]["product_name"] is None


def test_quote_quantity_limits(client, make_product):
    p = make_product()
    r = client.post("/api/public/orders/quote", json={"items": [{"product_id": str(p.id), "quantity": 51}]})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    r = client.post("/api/public/orders/quote", json={"items": [{"product_id": str(p.id), "quantity": 30},
                                                                {"product_id": str(p.id), "quantity": 30}]})
    assert r.status_code == 422 and r.json()["code"] == "quantity_limit"
    r = client.post("/api/public/orders/quote", json={"items": []})
    assert r.status_code == 422


def test_quote_rate_limited(client, make_product):
    from app.ratelimit import LIMITS, limiter

    p = make_product()
    limiter.enabled = True
    body = {"items": [{"product_id": str(p.id), "quantity": 1}]}
    for _ in range(LIMITS["order_quote"][0]):
        assert client.post("/api/public/orders/quote", json=body).status_code == 200
    r = client.post("/api/public/orders/quote", json=body)
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_price_changed_conflict(client, make_product, place_order, auth, db):
    p = make_product(price="2500")
    reviewed = _quote(client, [(p, 2)]).json()
    assert reviewed["total"] == "5000.00"
    assert client.patch(f"/api/admin/products/{p.id}", json={"price": "3000"}, headers=auth).status_code == 200

    key = str(uuid.uuid4())
    r = place_order([(p, 2)], key=key, extra={"expected_total": reviewed["total"]})
    assert r.status_code == 409, r.text
    err = r.json()
    assert err["code"] == "price_changed"
    assert err["quote"]["total"] == "6000.00"
    assert err["quote"]["lines"][0]["unit_price"] == "3000.00"
    assert _counts(db) == (0, 0, 0, 0)  # nothing stored, not even the idempotency key

    # Re-confirmation with the new total and a fresh key succeeds at the new price.
    r2 = place_order([(p, 2)], extra={"expected_total": err["quote"]["total"]})
    assert r2.status_code == 201, r2.text
    assert r2.json()["order"]["total"] == "6000.00"


def test_expected_total_optional_and_numeric_equivalence(client, make_product, place_order):
    p = make_product(price="2500")
    assert place_order([(p, 1)], name="Uno").status_code == 201
    assert place_order([(p, 2)], name="Dos", extra={"expected_total": 5000}).status_code == 201
    assert place_order([(p, 3)], name="Tres", extra={"expected_total": "7500.0"}).status_code == 201
    r = place_order([(p, 4)], name="Cuatro", extra={"expected_total": "9999.99"})
    assert r.status_code == 409 and r.json()["code"] == "price_changed"
    r = place_order([(p, 4)], name="Cuatro", extra={"expected_total": "1.234"})
    assert r.status_code == 422
