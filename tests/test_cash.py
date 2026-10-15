from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.enums import CashSessionStatus
from app.models import CashSession


def _pay(client, auth, oid, amount, method="cash"):
    return client.post(f"/api/admin/orders/{oid}/payments", json={"method": method, "amount": amount},
                       headers={**auth, "Idempotency-Key": str(uuid.uuid4())})


def test_only_one_open_session(client, auth, open_cash, db):
    s = open_cash("50000")
    assert s["status"] == "open" and s["summary"]["expected_cash"] == "50000.00"
    r = client.post("/api/admin/cash/open", json={"opening_amount": "1000"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "cash_session_already_open"
    # DB-level guarantee: partial unique index
    db.add(CashSession(status=CashSessionStatus.open, opening_amount=0))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_current_without_session(client, auth):
    r = client.get("/api/admin/cash/current", headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "no_open_cash_session"


def test_manual_movements_and_expected_cash(client, auth, open_cash):
    open_cash("20000")
    r = client.post("/api/admin/cash/movements", json={"type": "expense", "amount": "5000", "reason": "Aceite"},
                    headers=auth)
    assert r.status_code == 201 and r.json()["direction"] == "out"
    r = client.post("/api/admin/cash/movements", json={"type": "deposit", "amount": "10000", "reason": "Sencillo"},
                    headers=auth)
    assert r.status_code == 201
    r = client.post("/api/admin/cash/movements", json={"type": "adjustment", "amount": "100", "reason": "x y z"},
                    headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "direction_required"
    r = client.post("/api/admin/cash/movements",
                    json={"type": "adjustment", "direction": "out", "amount": "100", "reason": "Faltante"},
                    headers=auth)
    assert r.status_code == 201
    r = client.post("/api/admin/cash/movements", json={"type": "expense", "amount": "5000"}, headers=auth)
    assert r.status_code == 422  # reason required
    r = client.post("/api/admin/cash/movements",
                    json={"type": "withdrawal", "amount": "999999", "reason": "Retiro"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "insufficient_cash"
    r = client.post("/api/admin/cash/movements", json={"type": "sale_cash", "amount": "10", "reason": "hack"},
                    headers=auth)
    assert r.status_code == 422  # sale_cash cannot be created manually
    cur = client.get("/api/admin/cash/current", headers=auth).json()
    s = cur["summary"]
    assert s["expenses"] == "5000.00" and s["deposits"] == "10000.00" and s["adjustments_out"] == "100.00"
    assert s["expected_cash"] == "24900.00"
    mv = client.get(f"/api/admin/cash/sessions/{cur['id']}/movements", headers=auth).json()
    assert mv["total"] == 3 and mv["items"][0]["created_by_name"] == "Admin Principal"


def test_close_computes_difference_from_denominations(client, auth, make_product, place_order,
                                                      order_id_by_code, open_cash):
    p = make_product(price="7500")
    oid = order_id_by_code(place_order([(p, 2)]).json()["order"]["code"])
    sess = open_cash("50000")
    assert _pay(client, auth, oid, "15000").status_code == 201
    # transfer income is reported but not part of expected cash
    p2 = make_product(price="3000")
    oid2 = order_id_by_code(place_order([(p2, 1)], method="transfer", name="Otro").json()["order"]["code"])
    assert _pay(client, auth, oid2, "3000", method="transfer").status_code == 201
    cur = client.get("/api/admin/cash/current", headers=auth).json()["summary"]
    assert cur["expected_cash"] == "65000.00" and cur["transfer_income"] == "3000.00"

    counts = [
        {"kind": "bill", "denomination": "50000", "quantity": 1},
        {"kind": "bill", "denomination": "10000", "quantity": 1},
        {"kind": "coin", "denomination": "1000", "quantity": 4},
        {"kind": "coin", "denomination": "500", "quantity": 1},
    ]  # = 64500 -> shortage 500
    url = f"/api/admin/cash/sessions/{sess['id']}/close"
    r = client.post(url, json={"counts": counts}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "closing_note_required"
    r = client.post(url, json={"counts": counts + [{"kind": "bill", "denomination": "3000", "quantity": 1}],
                               "note": "x"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "invalid_denomination"
    r = client.post(url, json={"counts": counts + [counts[0]], "note": "x"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "duplicate_denomination"
    r = client.post(url, json={"counts": counts, "note": "Faltan 500 del cambio"}, headers=auth)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["status"] == "closed"
    assert c["expected_cash"] == "65000.00" and c["counted_cash"] == "64500.00" and c["difference"] == "-500.00"
    assert len(c["count_lines"]) == 4
    assert {(x["kind"], x["denomination"], x["subtotal"]) for x in c["count_lines"]} >= {("coin", "1000.00", "4000.00")}

    # closed session is immutable
    r = client.post(url, json={"counts": counts, "note": "otra"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "cash_session_closed"
    r = client.post("/api/admin/cash/movements", json={"type": "expense", "amount": "1", "reason": "tarde"},
                    headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "no_open_cash_session"
    # and no cash payments without an open session
    oid3 = order_id_by_code(place_order([(p, 1)], name="Tercero").json()["order"]["code"])
    assert _pay(client, auth, oid3, "7500").json()["code"] == "no_open_cash_session"

    hist = client.get("/api/admin/cash/sessions?status=closed", headers=auth).json()
    assert hist["total"] == 1
    rep = client.get("/api/admin/reports/cash-closings", headers=auth).json()
    assert rep["sessions_count"] == 1 and rep["total_shortage"] == "500.00" and rep["total_difference"] == "-500.00"
    # a new session can be opened after closing
    assert client.post("/api/admin/cash/open", json={"opening_amount": "0"}, headers=auth).status_code == 201


def test_close_exact_without_note(client, auth, open_cash):
    s = open_cash("15000")
    r = client.post(f"/api/admin/cash/sessions/{s['id']}/close",
                    json={"counts": [{"kind": "bill", "denomination": "10000", "quantity": 1},
                                     {"kind": "bill", "denomination": "5000", "quantity": 1}]}, headers=auth)
    assert r.status_code == 200 and r.json()["difference"] == "0.00"


def test_closed_session_immutable_at_db_level(client, auth, open_cash, db):
    s = open_cash("0")
    client.post(f"/api/admin/cash/sessions/{s['id']}/close", json={"counts": []}, headers=auth)
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE cash_sessions SET opening_amount = 999 WHERE id = :i"), {"i": s["id"]})
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(
            text("INSERT INTO cash_movements (id, session_id, type, direction, amount, reason) "
                 "VALUES (gen_random_uuid(), :i, 'deposit', 'in', 10, 'hack')"),
            {"i": s["id"]},
        )
    db.rollback()


def test_denominations_endpoint(client, auth):
    r = client.get("/api/admin/cash/denominations", headers=auth).json()
    assert "100000.00" in r["bills"] and "50.00" in r["coins"]
