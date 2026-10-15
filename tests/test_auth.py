from __future__ import annotations

import pytest

from tests.conftest import ADMIN_PASSWORD


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_success_and_me(client, admin_tokens):
    assert admin_tokens["token_type"] == "bearer"
    assert admin_tokens["expires_in"] == 15 * 60
    assert admin_tokens["user"]["username"] == "admin"
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin_tokens['access_token']}"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_login_wrong_password(client, admin_user):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope-nope-nope"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Usuario o contraseña incorrectos", "code": "invalid_credentials"}
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "whatever-123"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/orders"),
        ("get", "/api/admin/products"),
        ("post", "/api/admin/cash/open"),
        ("get", "/api/admin/reports/sales"),
        ("get", "/api/admin/proofs"),
        ("get", "/api/admin/audit-logs"),
        ("patch", "/api/admin/settings"),
    ],
)
def test_admin_routes_require_token(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"
    r = getattr(client, method)(path, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_token"


def test_refresh_token_rotation_and_reuse_detection(client, admin_tokens):
    rt1 = admin_tokens["refresh_token"]
    r = client.post("/api/auth/refresh", json={"refresh_token": rt1})
    assert r.status_code == 200, r.text
    rt2 = r.json()["refresh_token"]
    assert rt2 != rt1
    # rotated token works once more down the chain
    r = client.post("/api/auth/refresh", json={"refresh_token": rt2})
    assert r.status_code == 200
    rt3 = r.json()["refresh_token"]
    # reuse of an already-rotated token => 401 and the whole family is revoked
    r = client.post("/api/auth/refresh", json={"refresh_token": rt1})
    assert r.status_code == 401
    assert r.json()["code"] == "refresh_token_reused"
    r = client.post("/api/auth/refresh", json={"refresh_token": rt3})
    assert r.status_code == 401
    assert r.json()["code"] == "refresh_token_revoked"


def test_logout_revokes_refresh(client, admin_tokens):
    rt = admin_tokens["refresh_token"]
    r = client.post("/api/auth/logout", json={"refresh_token": rt})
    assert r.status_code == 204
    r = client.post("/api/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 401


def test_invalid_refresh_token(client):
    r = client.post("/api/auth/refresh", json={"refresh_token": "x" * 40})
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_refresh_token"


def test_change_password_revokes_sessions(client, admin_tokens, auth):
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "Another-Pass-123"},
        headers=auth,
    )
    assert r.status_code == 204
    r = client.post("/api/auth/refresh", json={"refresh_token": admin_tokens["refresh_token"]})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Another-Pass-123"})
    assert r.status_code == 200


def test_login_rate_limited(client, admin_user):
    from app.ratelimit import limiter

    limiter.enabled = True
    codes = [
        client.post("/api/auth/login", json={"username": "admin", "password": "bad-password"}).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_cli_reset_password(client, admin_tokens):
    from app import cli

    assert cli.main(["reset-password", "--username", " ADMIN ", "--password", "short"]) == 1
    assert cli.main(["reset-password", "--username", "ghost", "--password", "Nueva-clave-2026"]) == 1
    assert cli.main(["reset-password", "--username", " ADMIN ", "--password", "Nueva-clave-2026"]) == 0

    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Nueva-clave-2026"})
    assert r.status_code == 200
    # Sessions opened with the old password are closed.
    r = client.post("/api/auth/refresh", json={"refresh_token": admin_tokens["refresh_token"]})
    assert r.status_code == 401
