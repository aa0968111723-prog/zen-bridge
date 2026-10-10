"""Admin backend security regression tests."""
import logging
import re

import pytest
from starlette.testclient import TestClient

from app.admin import security
from app.admin.run import resolve_bind
from app.admin.server import API
from tests.test_admin_api import BASE, ORIGIN, env


def _route_url(path):
    examples = {"sid": "missing", "segment_id": "missing", "term_id": "1", "tm_id": "1",
                "corr_id": "missing", "room_id": "class", "jid": "missing"}
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}",
                  lambda match: examples.get(match.group(1), "1"), path)


def _write_routes(app):
    safe = security.SAFE_METHODS
    # FastAPI now keeps included routers lazy. OpenAPI expands their prefixes;
    # inspecting only route.methods would omit the new staging write endpoints.
    return [(method.upper(), path) for path, operations in app.openapi()['paths'].items()
            for method in operations
            if method.upper() in {'POST', 'PUT', 'PATCH', 'DELETE'} and method.upper() not in safe]


def test_admin_bind_and_foreign_host_are_refused(env):
    for host in ("0.0.0.0", "192.0.2.1", "::"):
        with pytest.raises(SystemExit):
            resolve_bind({"ZEN_ADMIN_HOST": host})

    remote = TestClient(env["app"], base_url=BASE, client=("192.0.2.1", 50000))
    assert remote.get(f"{API}/health").status_code == 403
    assert env["client"].get(f"{API}/health", headers={"host": "attacker.example"}).status_code == 403


def test_every_state_changing_route_rejects_foreign_origin(env):
    client = TestClient(env["app"], base_url=BASE, client=("127.0.0.1", 50000))
    routes = _write_routes(env["app"])
    assert routes
    for method, path in routes:
        response = client.request(
            method, _route_url(path), headers={"origin": "http://attacker.example",
                                               "sec-fetch-site": "same-origin"},
            json={},
        )
        assert response.status_code == 403, f"{method} {path}: {response.text}"


def test_every_authenticated_write_route_requires_session_csrf(env):
    from tests.test_admin_api import browser_login

    browser_login(env["client"], env["app"])
    routes = [(method, path) for method, path in _write_routes(env["app"])
              if path != f"{API}/auth/login"]
    assert routes
    for method, path in routes:
        response = env["client"].request(
            method, _route_url(path),
            headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"},
            json={},
        )
        assert response.status_code == 403, f"{method} {path}: {response.text}"
        assert response.json().get("code") == "csrf", f"{method} {path}: {response.text}"


def test_login_code_is_single_use_and_expires(env):
    code = env["app"].state.codes.issue()
    headers = {"origin": ORIGIN, "sec-fetch-site": "same-origin"}
    first = env["client"].post(f"{API}/auth/login", json={"code": code}, headers=headers)
    assert first.status_code == 200
    assert env["client"].post(f"{API}/auth/login", json={"code": code}, headers=headers).status_code == 401

    now = [100.0]
    codes = security.LoginCodes(clock=lambda: now[0], ttl=5)
    expired = codes.issue()
    now[0] += 5.01
    assert not codes.redeem(expired)


def test_login_cookie_flags(env):
    code = env["app"].state.codes.issue()
    response = env["client"].post(
        f"{API}/auth/login", json={"code": code},
        headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"},
    )
    cookie = response.headers["set-cookie"].lower()
    assert response.status_code == 200
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_login_is_rate_limited(env):
    headers = {"origin": ORIGIN, "sec-fetch-site": "same-origin"}
    for _ in range(10):
        assert env["client"].post(f"{API}/auth/login", json={"code": "invalid"}, headers=headers).status_code == 401
    limited = env["client"].post(f"{API}/auth/login", json={"code": "invalid"}, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


@pytest.mark.parametrize("path", [
    "/admin/static/%2e%2e/server.py",
    "/admin/static/%2e%2e%2fserver.py",
    "/admin/static/%2e%2e%2fvendor/mermaid.min.js",
    "/admin/static/vendor",
    "/admin/static/vendor/",
    "/admin/static/vendor/mermaid.min.js",
])
def test_static_traversal_and_vendor_listing_are_blocked(env, path):
    assert env["client"].get(path).status_code == 404


def test_tokens_and_passwords_are_masked_in_logs(caplog):
    logger = logging.getLogger("zen.admin.security_regression")
    redactor = security.RedactingFilter()
    logger.addFilter(redactor)
    token, password = "token-secret-regression", "password-secret-regression"
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            logger.warning("token=%s %s=%s", token, "password", password)
    finally:
        logger.removeFilter(redactor)

    assert token not in caplog.text
    assert password not in caplog.text


def test_admin_probe_never_connects_to_port_8645(monkeypatch):
    from app.admin import server

    attempts = []

    def fake_create_connection(address, *args, **kwargs):
        attempts.append(address)
        raise OSError("network disabled in test")

    monkeypatch.setattr(server.socket, "create_connection", fake_create_connection)
    assert server._tcp_probe("127.0.0.1", 8645) == "blocked"
    assert not any(address[1] == 8645 for address in attempts)
