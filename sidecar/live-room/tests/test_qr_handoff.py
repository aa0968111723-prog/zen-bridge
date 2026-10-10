"""round4 #6: two QR codes (name / IP) and the single-use 10-minute download hand-off."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.share import machine_share_name
from tests.test_round2 import app_for, auth, open_room, push, stop, token_of


def test_machine_share_name(monkeypatch):
    monkeypatch.setenv("BREEZE_SHARE_NAME", "DESKTOP-P8RGA3A")
    assert machine_share_name() == "desktop-p8rga3a"
    for bad in ("off", "bad name", "x" * 70, "a/b", "localhost"):
        monkeypatch.setenv("BREEZE_SHARE_NAME", bad)
        assert machine_share_name() is None


@pytest.mark.anyio
async def test_two_qrs_and_key_only_for_host(monkeypatch):
    monkeypatch.setenv("BREEZE_SHARE_NAME", "zenpc")
    monkeypatch.setenv("BREEZE_SHARE_HOST", "192.168.1.20")
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            host = (await client.get("/api/setup?room_id=class", headers=auth(token))).json()
            assert host["listen_url_name"].startswith("http://zenpc:8780/r/class?k=")
            anon = (await client.get("/api/setup?room_id=class")).json()
            assert anon["listen_url_name"] == "http://zenpc:8780/r/class"
            for variant in ("name", "ip"):
                r = await client.get(f"/api/qr?room_id=class&variant={variant}", headers=auth(token))
                assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
            # the name is an allowed audience host (the QR must actually open)
            page = await client.get("/r/class", headers={"host": "zenpc:8780"})
            assert page.status_code == 200
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_handoff_single_use_expiry_scope_and_auth(monkeypatch):
    monkeypatch.setenv("BREEZE_SHARE_HOST", "192.168.1.20")
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await push(client, token, "class", "s1", 1, "第一句".encode(), t0_ms=0, t1_ms=6000)
            assert (await client.post("/api/export-handoff", json={"room_id": "class"})).status_code == 401
            r = await client.post("/api/export-handoff", json={"room_id": "class", "kind": "srt"},
                                  headers={**auth(token), "content-type": "application/json"})
            body = r.json()
            assert r.status_code == 200 and body["expires_in"] == 600 and body["single_use"]
            assert body["url"] == f"http://192.168.1.20:8780/h/{body['token']}"
            assert (await client.get(f"/api/export-handoff/qr?token_id={body['token']}")).status_code == 401
            qr = await client.get(f"/api/export-handoff/qr?token_id={body['token']}", headers=auth(token))
            assert qr.status_code == 200 and qr.content[:4] == b"\x89PNG"
            got = await client.get(f"/h/{body['token']}")
            assert got.status_code == 200 and "第一句" in got.text
            assert "attachment" in got.headers["content-disposition"] and 'class.srt' in got.headers["content-disposition"]
            assert (await client.get(f"/h/{body['token']}")).status_code == 404, "single use"
            assert (await client.get("/h/not-a-token")).status_code == 404
            # expiry: 10 minutes
            import app.server as srv
            r = await client.post("/api/export-handoff", json={"room_id": "class"},
                                  headers={**auth(token), "content-type": "application/json"})
            tok = r.json()["token"]
            real = srv.time.monotonic
            monkeypatch.setattr(srv.time, "monotonic", lambda: real() + 601)
            assert (await client.get(f"/h/{tok}")).status_code == 404
            monkeypatch.setattr(srv.time, "monotonic", real)
            bad = await client.post("/api/export-handoff", json={"room_id": "class", "kind": "exe"},
                                    headers={**auth(token), "content-type": "application/json"})
            assert bad.status_code == 400
    finally:
        await stop(app)
