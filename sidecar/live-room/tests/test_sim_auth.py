"""B-h. Every host route requires the bearer token, and public routes do not leak it."""

import inspect
import json

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from app.server import create_app
from tests.sim import TextAsr, copy_decoder, serving, sim_settings, token_of
from tests.test_round2 import auth, stop


def _host_routes(app):
    found = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except (OSError, TypeError):
            continue
        if "require_host(" in source:
            found.append(route)
    return found


@pytest.mark.anyio
async def test_every_host_route_requires_token():
    """B-h1. Reflect require_host. The count is part of the contract: a new host route must update it."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        routes = _host_routes(app)
        assert len(routes) == 14
        for route in routes:
            method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
            path = route.path
            params = {"room_id": "class", "session_id": "s", "seq": "1", "kind": "txt"}
            missing = await client.request(method, path, params=params)
            assert missing.status_code == 401, (method, path, missing.status_code, missing.text)
            bad = await client.request(method, path, params=params, headers={"authorization": "Bearer 123"})
            assert bad.status_code == 401, (method, path, bad.status_code, bad.text)
            evil = await client.request(
                method,
                path,
                params=params,
                headers={**auth(token), "origin": "http://evil.example"},
                json={"room_id": "class", "session_id": "s", "seq": 1},
            )
            assert evil.status_code == 403, (method, path, evil.status_code, evil.text)


@pytest.mark.anyio
async def test_host_token_only_for_loopback():
    """B-h2. A non-loopback ASGI client does not receive a token."""
    app = create_app(sim_settings(translate=False), asr=TextAsr(0), decoder=copy_decoder)
    try:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/host-token",
            "raw_path": b"/api/host-token",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8780")],
            "client": ("192.168.1.20", 5000),
            "server": ("127.0.0.1", 8780),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 403
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        text = body.decode("utf-8", "replace")
        assert "token" not in text.lower() or "主持" in text
        assert app.state.token.encode() not in body
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_listener_ws_commands_are_ignored():
    """B-h3."""
    from tests.sim import Listener, export_json, open_room, post_segment

    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        pushed = await post_segment(client, token, "class", "s", 1, "第1句".encode(), 0, 6000)
        assert pushed.status_code == 200, pushed.text
        before = len(await export_json(client, token, "class"))
        async with Listener(app, "class") as listener:
            for payload in (b'{"type":"stop"}', b'{"type":"delete"}', b"not-json"):
                await listener.sock.inc.put({"type": "websocket.receive", "text": payload.decode()})
            again = await post_segment(client, token, "class", "s", 2, "第2句".encode(), 6000, 12000)
            assert again.status_code == 200, again.text
            assert listener.closed is False
        after = await export_json(client, token, "class")
        assert len(after) == before + 1


@pytest.mark.anyio
async def test_public_endpoints_do_not_leak_secrets(monkeypatch):
    """B-h4. The app reads OPENAI_API_KEY itself. Setup keeps host_token null."""
    secret = "sk-sim-secret-not-a-real-key"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    app = create_app(sim_settings(), asr=TextAsr(0), translator=None, decoder=copy_decoder)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            for path in ("/api/setup", "/api/health", "/api/qr"):
                resp = await client.get(path, params={"room_id": "class"})
                assert resp.status_code in {200, 503}, (path, resp.status_code, resp.text)
                blob = resp.content
                assert secret.encode() not in blob
                assert token.encode() not in blob
            setup = (await client.get("/api/setup", params={"room_id": "class"})).json()
            assert setup["host_token"] is None
    finally:
        await stop(app)
