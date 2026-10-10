"""資安長 #1-#6: 8645 in translate/embed URLs + redirects, handler-level log redaction,
redact gaps, NUL search, room_id splicing, SSE after logout / stream caps."""
import http.server
import io
import json
import logging
import threading
import urllib.error
import urllib.request

import pytest

from app import net
from app.admin import security
from app.admin.live_client import LiveDown, LiveRoomClient
from app.translate_config import TranslateConfigError, validate_base_url
from tests.test_admin_api import API, BEARER, FakeLive, browser_login, env  # noqa: F401


# #1 ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://127.0.0.1:8645", "http://localhost:8645/v1", "http://[::1]:8645",
                                 "http://127.0.0.1:08645", "http://[::ffff:127.0.0.1]:8645", "http://LOCALHOST:8645"])
def test_validate_base_url_blocks_hermes(url):
    with pytest.raises(TranslateConfigError):
        validate_base_url(url)


def test_embedder_env_refuses_8645():
    from app.embed import embedder_from_env
    with pytest.raises(Exception):
        embedder_from_env({"ZEN_EMBED_BASE_URL": "http://127.0.0.1:8645"})


def _drain_body(handler) -> None:
    """Read the request body before answering. Windows root cause of WinError 10053: replying to a POST
    and closing with its body still unread makes the stack send RST, so the client sees "connection
    aborted" instead of the 307 under test (Linux tolerates it)."""
    n = int(handler.headers.get("Content-Length") or 0)
    if n > 0:
        handler.rfile.read(n)


class _Redirector(http.server.BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        _drain_body(self)
        self.send_response(307)
        self.send_header("Location", self.target + self.path)
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *a):
        pass


class _Canary(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _drain_body(self)
        _Canary.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"embeddings": [[1.0]]}')

    do_POST = do_GET

    def log_message(self, *a):
        pass


@pytest.fixture
def redirect_pair():
    canary = http.server.HTTPServer(("127.0.0.1", 0), _Canary)
    _Canary.hits = []
    _Redirector.target = f"http://127.0.0.1:{canary.server_port}"
    redir = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    for s in (canary, redir):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{redir.server_port}"
    for s in (canary, redir):
        s.shutdown()
        s.server_close()


def test_safe_opener_never_follows_redirects(redirect_pair):
    with pytest.raises(urllib.error.HTTPError) as ei:
        net.safe_opener()(redirect_pair + "/api/version", timeout=2)
    assert ei.value.code == 307 and _Canary.hits == []


def test_embedder_does_not_follow_redirect(redirect_pair):
    from app.embed import EmbedError, OllamaEmbedder
    with pytest.raises(EmbedError):
        OllamaEmbedder(base_url=redirect_pair).embed(["x"])
    assert _Canary.hits == []


def test_ollama_probe_does_not_follow_redirect(redirect_pair, monkeypatch):
    from app.admin import server
    monkeypatch.setattr(server, "OLLAMA_URL", redirect_pair)
    assert server._ollama_probe(timeout=2) == "down" and _Canary.hits == []


def test_translator_default_opener_does_not_follow_redirect(redirect_pair):
    from app.translate import _default_opener
    req = urllib.request.Request(redirect_pair + "/v1/chat/completions", data=b"{}",
                                 headers={"Authorization": "Bearer k"})
    with pytest.raises(urllib.error.HTTPError):
        _default_opener(redirect_pair)(req, timeout=2)
    assert _Canary.hits == []


# #2 / #3 ----------------------------------------------------------------------
def test_child_logger_secrets_redacted_on_handler():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(h)
    try:
        assert security.install_redaction_on_handlers() >= 1
        for name in ("zen.admin", "zen.admin.jobs", "uvicorn.error"):
            # other tests may have run uvicorn's dictConfig ("uvicorn" propagate=False): open the chain
            chain, parts = [], name.split(".")
            for i in range(len(parts), 0, -1):
                lg = logging.getLogger(".".join(parts[:i]))
                chain.append((lg, lg.level, lg.propagate, lg.disabled))
                lg.propagate, lg.disabled = True, False
            chain[0][0].setLevel(logging.INFO)
            chain[0][0].info("Authorization: Bearer SYNTHSECRET1 password=SYNTHSECRET2 {'token': 'SYNTHSECRET3'}")
            for lg, level, prop, dis in chain:
                lg.level, lg.propagate, lg.disabled = level, prop, dis
    finally:
        root.removeHandler(h)
    out = buf.getvalue()
    assert out.count("Bearer ***") == 3 and "SYNTHSECRET" not in out


@pytest.mark.parametrize("raw", ["{'token': 'S1'}", "token='S1'", "code=S1", "?code=S1&x=1", "sid=S1",
                                 '{"sid": "S1"}'])
def test_redact_gaps_closed(raw):
    assert "S1" not in security.redact(raw)


def test_redact_keeps_ordinary_text():
    assert security.redact("今天講因緣 code review at 3pm") == "今天講因緣 code review at 3pm"


# #4 ---------------------------------------------------------------------------
@pytest.mark.parametrize("q", ["%00", "因%00緣", "a%0Ab"])
def test_search_control_chars_422(env, q):  # noqa: F811
    for scope in ("transcript", "translation"):
        r = env["client"].get(f"{API}/segments/search?q={q}&scope={scope}", headers=BEARER)
        assert r.status_code == 422, r.text


# #5 ---------------------------------------------------------------------------
@pytest.mark.parametrize("rid", ["..", "%2e%2e", "x%3Fadmin%3D1", "x%23frag", "a%20b"])
def test_glossary_push_rejects_bad_room_id(env, rid):  # noqa: F811
    r = env["client"].post(f"{API}/rooms/{rid}/glossary/push", json={"dry_run": True}, headers=BEARER)
    assert r.status_code in (404, 422), r.text
    assert env["live"].puts == []


def test_live_client_quotes_room_id_and_wraps_invalid_url():
    seen = []

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Op:
        def open(self, req, timeout=None):
            if req.full_url.endswith("/api/host-token"):
                return Resp(b'{"token": "h"}')
            seen.append(req.full_url)
            raise urllib.error.URLError("refused")

    c = LiveRoomClient("http://127.0.0.1:8780", opener=Op())
    with pytest.raises(LiveDown):
        c.room_glossary("x?admin=1")
    assert seen and "x%3Fadmin%3D1" in seen[0] and "?" not in seen[0].split("/api/")[1]

    class Bad:
        def open(self, req, timeout=None):
            if req.full_url.endswith("/api/host-token"):
                return Resp(b'{"token": "h"}')
            import http.client
            raise http.client.InvalidURL("bad")
    with pytest.raises(LiveDown):
        LiveRoomClient("http://127.0.0.1:8780", opener=Bad()).room_glossary("x")


# #6 ---------------------------------------------------------------------------
def test_sse_ends_after_logout(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from app.admin.server import create_admin_app
    app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=security.token_hash("t" * 30), port=8791,
                           identity_path=tmp_path / "id.sqlite3", probes={}, live_client=FakeLive(),
                           start_worker=False, sse_interval_s=0.0, sse_max_events=50)
    client = TestClient(app, base_url="http://127.0.0.1:8791", client=("127.0.0.1", 50000))
    with client:
        browser_login(client, app)
        calls = {"n": 0}
        real = security.SessionStore.lookup

        def flaky(self, sid):
            calls["n"] += 1
            return real(self, sid) if calls["n"] <= 2 else None   # dependency + first tick, then logged out
        monkeypatch.setattr(security.SessionStore, "lookup", flaky)
        with client.stream("GET", f"{API}/live/stream") as r:
            body = "".join(r.iter_text())
    assert "event: logout" in body and body.count("event: metrics") <= 1


def test_sse_per_identity_cap(tmp_path):
    from app.admin import server
    from starlette.testclient import TestClient
    app = server.create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=security.token_hash("t" * 30),
                                  port=8791, probes={}, live_client=FakeLive(), start_worker=False,
                                  sse_interval_s=0.0, sse_max_events=1)
    client = TestClient(app, base_url="http://127.0.0.1:8791", client=("127.0.0.1", 50000))
    hdr = {"authorization": "Bearer " + "t" * 30}
    with client:
        # finished streams release their slot
        for _ in range(server.SSE_PER_USER + 2):
            with client.stream("GET", f"{API}/live/stream", headers=hdr) as r:
                assert r.status_code == 200
                "".join(r.iter_text())
    assert server.SSE_PER_USER >= 1 and server.SSE_TOTAL >= server.SSE_PER_USER
