"""CTO gate batch 1 P1: the visual LLM's local-URL check must refuse Hermes' 8645 and follow no redirect."""
import urllib.request

import pytest

from app import visual


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8645/v1", "http://localhost:8645", "http://[::1]:8645/v1", "http://127.0.0.2:8645",
    "http://user:pw@127.0.0.1:11434/v1", "http://10.0.0.5:11434/v1", "ftp://127.0.0.1/v1", "http://127.0.0.1:99999",
])
def test_refused(url):
    assert visual.is_loopback_url(url) is False


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434/v1", "http://localhost:8080/v1", "http://[::1]:11434"])
def test_allowed(url):
    assert visual.is_loopback_url(url) is True


def test_env_with_8645_disables_visuals():
    env = {"BREEZE_VISUAL_LLM_BASE_URL": "http://127.0.0.1:8645/v1", "BREEZE_VISUAL_LLM_MODEL": "m"}
    assert visual.build_llm_from_env(env) is None


def test_post_uses_no_redirect_opener(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_opener(*a, **k):
        def open_(req, timeout=None):
            seen["url"] = req.full_url
            return Resp()
        seen["safe"] = True
        return open_
    monkeypatch.setattr("app.net.safe_opener", fake_opener)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("plain urlopen")))
    env = {"BREEZE_VISUAL_LLM_BASE_URL": "http://127.0.0.1:11434/v1", "BREEZE_VISUAL_LLM_MODEL": "m"}
    llm = visual.build_llm_from_env(env)
    assert llm is not None
    llm._post({"x": 1}, 1.0)
    assert seen == {"safe": True, "url": "http://127.0.0.1:11434/v1/chat/completions"}
    llm.base_url = "http://127.0.0.1:8645/v1"
    with pytest.raises(ValueError):
        llm._post({"x": 1}, 1.0)


def test_127_lookalike_hostname_is_not_loopback():
    assert visual.is_loopback_url("http://127.evil.example:11434/v1") is False
    assert visual.is_loopback_url("http://127.0.0.2:11434/v1") is True


@pytest.mark.anyio
async def test_visual_history_needs_listen_key():
    from httpx import ASGITransport, AsyncClient
    from tests.test_round2 import app_for, auth, open_room, stop, token_of
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            r = await open_room(client, token, "class")
            key = r.json()["listen_key"]
            assert (await client.get("/api/visual/class")).status_code == 403
            assert (await client.get("/api/visual/class?k=wrong")).status_code == 403
            assert (await client.get("/api/visual/nosuch")).status_code == 403
            assert (await client.get(f"/api/visual/class?k={key}")).status_code == 200
            assert (await client.get("/api/visual/class", headers=auth(token))).status_code == 200
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_visual_trigger_needs_the_host_token():
    from httpx import ASGITransport, AsyncClient
    from tests.test_round2 import app_for, auth, open_room, stop, token_of
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            assert (await client.post("/api/visual/class/trigger")).status_code == 401
            assert (await client.post("/api/visual/class/trigger", headers={"authorization": "Bearer nope"})).status_code == 401
            ok = await client.post("/api/visual/class/trigger", headers=auth(token))
            assert ok.status_code == 200 and ok.json()["accepted"] is False   # disabled without a visual LLM
            evil = await client.post("/api/visual/class/trigger", headers={**auth(token), "origin": "http://evil.example"})
            assert evil.status_code == 403
            xsite = await client.post("/api/visual/class/trigger", headers={**auth(token), "sec-fetch-site": "cross-site"})
            assert xsite.status_code == 403
            # the token travels only in the Authorization header; ?token= or a cookie is not a host credential
            assert (await client.post(f"/api/visual/class/trigger?token={token}")).status_code == 401
            assert (await client.post("/api/visual/class/trigger", cookies={"token": token})).status_code == 401
    finally:
        await stop(app)
