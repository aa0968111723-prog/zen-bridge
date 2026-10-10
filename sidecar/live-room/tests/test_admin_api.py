"""Admin backend: security layers and API conventions (backend-review §1.4, §2 P0)."""
import json
import logging
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from starlette.testclient import TestClient  # noqa: E402

from app.admin import db, security  # noqa: E402
from app.admin.live_client import LiveDown, LiveRoomClient  # noqa: E402
from app.admin.run import resolve_bind  # noqa: E402
from app.admin.server import API, create_admin_app, dec_cursor, enc_cursor  # noqa: E402
from tests.local_fakes import seed_segment  # noqa: E402

TOKEN = "test-admin-token-0123456789"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE
BEARER = {"authorization": f"Bearer {TOKEN}"}


class FakeLive:
    def __init__(self):
        self.metrics_data = {"pending": 0, "inflight": 0, "translate_queued": 0, "rtf": 0.4}
        self.room = {"version": 3, "terms": [{"zh": "禪修", "en": "meditation"}, {"zh": "房間詞", "en": "room only"}]}
        self.puts = []
        self.put_status = 200
        self.down = False

    def metrics(self):
        if self.down:
            raise LiveDown("down")
        return self.metrics_data

    def room_glossary(self, room_id):
        return self.room

    def put_room_glossary(self, room_id, terms, if_version):
        self.puts.append({"terms": terms, "if_version": if_version})
        return self.put_status, {"version": if_version + 1}

    def retranslate(self, room_id, session_id, seq, zh=None):
        return 200, {"en": "new", "status": "ready", "seq": seq}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "zen.sqlite3"
    live = FakeLive()
    app = create_admin_app(path, token_hash_hex=security.token_hash(TOKEN), port=PORT,
                           identity_path=tmp_path / "zen-identity.sqlite3", probes={"live": lambda: "up"},
                           live_client=live, backup_dir=lambda: tmp_path / "backups", start_worker=False,
                           sse_interval_s=0.0, sse_max_events=2)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    c = db.connect(path)
    seed_segment(c, seg="s1-1", seq=1, zh="今天講因緣具足", en="Today, conditions")
    seed_segment(c, seg="s1-2", seq=2, zh="佛法", en="Dharma")
    c.close()
    with client:
        yield {"app": app, "client": client, "path": path, "live": live, "tmp": tmp_path}


def browser_login(client, app):
    code = app.state.codes.issue()
    r = client.post(f"{API}/auth/login", json={"code": code},
                    headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def writes(csrf):
    return {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-zen-csrf": csrf}


# ------------------------------------------------------------------ transport-level checks
def test_non_loopback_peer_refused(env):
    c = TestClient(env["app"], base_url=BASE, client=("192.168.1.20", 4000))
    r = c.get(f"{API}/health", headers=BEARER)
    assert r.status_code == 403 and r.headers["content-type"].startswith("application/problem+json")


def test_ipv4_mapped_loopback_allowed():
    assert security.is_loopback_ip("::ffff:127.0.0.1") and security.is_loopback_ip("::1")
    assert not security.is_loopback_ip("::ffff:10.0.0.1") and not security.is_loopback_ip("testclient")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8780", "evil.com:8791", "127.0.0.1:8645", "localhost:80"])
def test_host_must_match_including_port(env, host):
    r = env["client"].get(f"{API}/health", headers={**BEARER, "host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8791", "localhost:8791", "[::1]:8791"])
def test_allowed_hosts(env, host):
    assert env["client"].get(f"{API}/health", headers={**BEARER, "host": host}).status_code == 200


def test_origin_must_match_port_on_writes(env):
    csrf = browser_login(env["client"], env["app"])
    for bad in ("http://127.0.0.1:8780", "http://127.0.0.1:8645", "http://127.0.0.1", "null"):
        r = env["client"].post(f"{API}/auth/logout", headers={**writes(csrf), "origin": bad})
        assert r.status_code == 403, bad


def test_sec_fetch_site_cross_or_same_site_refused(env):
    csrf = browser_login(env["client"], env["app"])
    for site in ("same-site", "cross-site", "none"):
        r = env["client"].post(f"{API}/auth/logout", headers={**writes(csrf), "sec-fetch-site": site})
        assert r.status_code == 403, site


def test_cookie_write_requires_sec_fetch_site_and_origin_present(env):
    csrf = browser_login(env["client"], env["app"])
    h = writes(csrf)
    del h["sec-fetch-site"]
    assert env["client"].post(f"{API}/auth/logout", headers=h).status_code == 403
    h = writes(csrf)
    del h["origin"]
    assert env["client"].post(f"{API}/auth/logout", headers=h).status_code == 403


def test_token_in_query_string_refused(env):
    for q in ("token=abc", "access_token=abc", "code=xyz", "sid=1"):
        r = env["client"].get(f"{API}/live/stream?{q}", headers=BEARER)
        assert r.status_code == 400, q


def test_security_headers_and_no_cors(env):
    r = env["client"].get(f"{API}/health", headers={**BEARER, "origin": "http://evil.com"})
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


def test_docs_disabled(env):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert env["client"].get(p).status_code == 404


# ------------------------------------------------------------------ auth
def test_unauthenticated_is_401_problem(env):
    r = env["client"].get(f"{API}/sessions")
    assert r.status_code == 401
    body = r.json()
    assert r.headers["content-type"].startswith("application/problem+json")
    assert {"type", "title", "status", "detail"} <= set(body)


def test_wrong_bearer_refused(env):
    assert env["client"].get(f"{API}/sessions", headers={"authorization": "Bearer nope"}).status_code == 401


def test_login_code_is_single_use_and_cookie_is_httponly_strict(env):
    code = env["app"].state.codes.issue()
    h = {"origin": ORIGIN, "sec-fetch-site": "same-origin"}
    r = env["client"].post(f"{API}/auth/login", json={"code": code}, headers=h)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    assert env["client"].post(f"{API}/auth/login", json={"code": code}, headers=h).status_code == 401


def test_login_requires_origin(env):
    code = env["app"].state.codes.issue()
    assert env["client"].post(f"{API}/auth/login", json={"code": code}).status_code == 403


def test_csrf_is_session_bound(env, tmp_path):
    csrf = browser_login(env["client"], env["app"])
    other = TestClient(env["app"], base_url=BASE, client=("127.0.0.1", 50001))
    csrf2 = browser_login(other, env["app"])
    assert csrf != csrf2
    r = env["client"].post(f"{API}/auth/logout", headers=writes(csrf2))
    assert r.status_code == 403 and r.json()["code"] == "csrf"
    assert env["client"].post(f"{API}/auth/logout", headers=writes(csrf)).status_code == 204


def test_login_code_endpoint_needs_cli_token(env):
    r = env["client"].post(f"{API}/auth/login-code", headers=BEARER)
    assert r.status_code == 200 and "#code=" in r.json()["url"]
    csrf = browser_login(env["client"], env["app"])
    assert env["client"].post(f"{API}/auth/login-code", headers=writes(csrf)).status_code == 403


def test_token_file_stores_only_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEN_ADMIN_TOKEN", raising=False)
    f = tmp_path / "admin.token"
    digest, plain = security.load_or_create_token(f)
    assert plain and plain not in f.read_text() and f.read_text().strip() == digest
    again, plain2 = security.load_or_create_token(f)
    assert again == digest and plain2 is None


def test_identity_api_token_read_only_scope(env):
    ident = env["tmp"] / "zen-identity.sqlite3"
    c = db.connect(env["path"])
    c.execute("INSERT INTO users(id, role) VALUES (7, 'editor')")
    c.close()
    c = db.connect(ident)
    c.execute("INSERT INTO user_accounts(user_id, username) VALUES (7, 'ed')")
    c.execute("INSERT INTO api_tokens(user_id, token_hash, scopes) VALUES (7, ?, 'read')", (security.token_hash("ro-token"),))
    c.close()
    h = {"authorization": "Bearer ro-token"}
    assert env["client"].get(f"{API}/sessions", headers=h).status_code == 200
    r = env["client"].post(f"{API}/corrections", headers=h, json={"segment_id": "s1-1", "text": "x"})
    assert r.status_code == 403


# ------------------------------------------------------------------ bind / outbound / logs
def test_bind_refuses_non_loopback_and_8645():
    assert resolve_bind({}) == ("127.0.0.1", 8791)
    for env in ({"ZEN_ADMIN_HOST": "0.0.0.0"}, {"ZEN_ADMIN_PORT": "8645"}, {"ZEN_ADMIN_HOST": "192.168.0.2"}):
        with pytest.raises(SystemExit):
            resolve_bind(env)


def test_create_app_refuses_8645(tmp_path):
    with pytest.raises(ValueError):
        create_admin_app(tmp_path / "z.sqlite3", token_hash_hex=None, port=8645)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8645/x", "http://10.0.0.1:8780", "https://127.0.0.1:8780",
                                 "http://user:pw@127.0.0.1:8780"])
def test_outbound_guard(url):
    with pytest.raises(security.OutboundBlocked):
        security.guard_outbound_url(url)


def test_live_client_refuses_8645():
    with pytest.raises(security.OutboundBlocked):
        LiveRoomClient("http://127.0.0.1:8645")


def test_live_client_token_in_memory_refetched_once_on_401():
    cl = LiveRoomClient()
    script = [(200, {"token": "t1"}), (401, {}), (200, {"token": "t2"}), (200, {"pending": 0})]
    sent = []

    def fake_send(method, path, body=None, headers=None, timeout=3.0):
        sent.append((method, path, (headers or {}).get("Authorization")))
        return script.pop(0)
    cl._send = fake_send
    assert cl.metrics() == {"pending": 0}
    assert sent[1][2] == "Bearer t1" and sent[3][2] == "Bearer t2"
    assert "t2" not in repr(cl)


def test_log_redaction():
    text = ('Authorization: Bearer abcdefghijklmnop cookie: zen_admin_sid=SECRET1 '
            '{"token": "SECRET2"} /admin/login#code=SECRET3 x-zen-csrf: SECRET4 sk-abcdefghijklmn')
    out = security.redact(text)
    for s in ("abcdefghijklmnop", "SECRET1", "SECRET2", "SECRET3", "SECRET4", "sk-abcdefghijklmn"):
        assert s not in out


def test_redacting_filter_on_records(caplog):
    logger = logging.getLogger("zen.test.redact")
    security.install_redaction(logger)
    with caplog.at_level(logging.INFO, logger="zen.test.redact"):
        logger.info("login with token=%s", "SUPERSECRET")
    assert "SUPERSECRET" not in caplog.text


# ------------------------------------------------------------------ API conventions
def test_health_and_metrics(env):
    h = env["client"].get(f"{API}/health", headers=BEARER).json()
    assert h["schema_version"] == db.SCHEMA_VERSION and h["live"] == "up" and h["db"] == "ok"
    m = env["client"].get(f"{API}/metrics", headers=BEARER).json()
    assert m["counts"]["segments"] == 2


def test_keyset_pagination_sessions(env):
    c = db.connect(env["path"])
    for i in range(5):
        c.execute("INSERT INTO sessions(id, room_id, started_at) VALUES (?, 'class', ?)", (f"x{i}", 2000 + i))
    c.close()
    seen, cursor = [], None
    for _ in range(10):
        url = f"{API}/sessions?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = env["client"].get(url, headers=BEARER).json()
        seen += [s["id"] for s in body["items"]]
        cursor = body["next"]
        if not cursor:
            break
    assert seen == ["x4", "x3", "x2", "x1", "x0", "s1"]
    assert env["client"].get(f"{API}/sessions?cursor=!!bad", headers=BEARER).status_code == 400


def test_cursor_roundtrip():
    assert dec_cursor(enc_cursor([1.5, "a"]), 2) == [1.5, "a"]


def test_segments_and_search(env):
    segs = env["client"].get(f"{API}/sessions/s1/segments", headers=BEARER).json()
    assert [s["seq"] for s in segs["items"]] == [1, 2]
    r = env["client"].get(f"{API}/segments/search", params={"q": "因緣"}, headers=BEARER).json()
    assert r["mode"].startswith("unigram") and r["items"][0]["segment_id"] == "s1-1"
    r = env["client"].get(f"{API}/segments/search", params={"q": "因緣具足"}, headers=BEARER).json()
    assert r["mode"] == "trigram" and r["items"]
    r = env["client"].get(f"{API}/segments/search", params={"q": "佛"}, headers=BEARER).json()
    assert [i["segment_id"] for i in r["items"]] == ["s1-2"]
    r = env["client"].get(f"{API}/segments/search", params={"q": "dharma", "scope": "translation"}, headers=BEARER)
    assert r.json()["items"][0]["segment_id"] == "s1-2"


def test_correction_with_idempotency_key_stages_tm(env):
    """round3 §5-1: a correction only stages TM / glossary; an admin approval writes them."""
    body = {"segment_id": "s1-1", "target_type": "translation", "text": "Today: causes and conditions.",
            "propose_term": {"zh": "因緣", "en": "causes and conditions"}}
    h = {**BEARER, "idempotency-key": "corr-0001-abcdef"}
    r1 = env["client"].post(f"{API}/corrections", json=body, headers=h)
    assert r1.status_code == 201 and r1.headers["location"].startswith(f"{API}/corrections/")
    r2 = env["client"].post(f"{API}/corrections", json=body, headers=h)
    assert r2.status_code == 201 and r2.json() == r1.json() and r2.headers["idempotent-replayed"] == "true"
    r3 = env["client"].post(f"{API}/corrections", json={**body, "text": "other"}, headers=h)
    assert r3.status_code == 422
    c = db.connect(env["path"])
    assert c.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM tm_units").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM glossary_terms WHERE zh='因緣'").fetchone()[0] == 0
    c.close()
    sid = r1.json()["tm_staging_id"]
    etag = env["client"].get(f"{API}/staging/{sid}", headers=BEARER).headers["etag"]
    assert env["client"].post(f"{API}/staging/{sid}/approve", json={}, headers={**BEARER, "if-match": etag}).status_code == 200
    c = db.connect(env["path"])
    assert c.execute("SELECT tgt_text FROM tm_units").fetchone()[0] == "Today: causes and conditions."
    c.close()


def test_glossary_etag_if_match(env):
    from app import feedback
    c = db.connect(env["path"])          # legacy proposal (corrections now go through /staging)
    feedback.propose_term(c, "因緣", "conditions")
    c.close()
    items = env["client"].get(f"{API}/glossary/proposals", headers=BEARER).json()["items"]
    tid, etag = items[0]["id"], items[0]["etag"]
    assert env["client"].get(f"{API}/glossary/terms/{tid}", headers=BEARER).headers["etag"] == etag
    url = f"{API}/glossary/proposals/{tid}/approve"
    assert env["client"].post(url, headers=BEARER).status_code == 428
    r = env["client"].post(url, headers={**BEARER, "if-match": '"t999-r1"'})
    assert r.status_code == 412 and r.json()["etag"] == etag
    r = env["client"].post(url, headers={**BEARER, "if-match": etag})
    assert r.status_code == 200 and r.json()["status"] == "active" and r.headers["etag"] != etag
    assert env["client"].post(url, headers={**BEARER, "if-match": etag}).status_code == 412


def test_glossary_push_diffs_and_sends_if_version(env):
    c = db.connect(env["path"])
    c.execute("INSERT INTO glossaries(id, name, scope) VALUES (1, 'g', 'global')")
    c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, status) VALUES (1, '禪修', 'Chan practice', 'active')")
    c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, status) VALUES (1, '因緣', 'conditions', 'active')")
    c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, status) VALUES (1, '提案', 'proposal', 'proposed')")
    c.close()
    url = f"{API}/rooms/class/glossary/push"
    dry = env["client"].post(url, json={"dry_run": True}, headers=BEARER).json()
    assert [a["zh"] for a in dry["added"]] == ["因緣"] and dry["updated"][0]["zh"] == "禪修"
    assert env["live"].puts == []
    r = env["client"].post(url, json={"if_room_version": 3}, headers=BEARER)
    assert r.status_code == 200, r.text
    put = env["live"].puts[0]
    assert put["if_version"] == 3
    zhs = [t["zh"] for t in put["terms"]]
    assert "房間詞" in zhs and "因緣" in zhs and "提案" not in zhs     # room-only term kept
    env["live"].put_status = 409
    assert env["client"].post(url, json={"if_room_version": 3}, headers=BEARER).status_code == 409


def test_retranslate_is_synchronous_200(env):
    r = env["client"].post(f"{API}/segments/s1-1/retranslate", headers=BEARER)
    assert r.status_code == 200 and r.json()["en"] == "new"


def test_jobs_202_location_and_poll(env):
    r = env["client"].post(f"{API}/jobs", json={"kind": "backup"}, headers={**BEARER, "idempotency-key": "job-key-000001"})
    assert r.status_code == 202
    loc = r.headers["location"]
    assert loc.startswith(f"{API}/jobs/job_")
    again = env["client"].post(f"{API}/jobs", json={"kind": "backup"}, headers={**BEARER, "idempotency-key": "job-key-000001"})
    assert again.json()["job"]["id"] == r.json()["job"]["id"]
    job = env["client"].get(loc, headers=BEARER)
    assert job.json()["state"] == "queued" and job.headers.get("retry-after")
    import asyncio
    assert asyncio.run(env["app"].state.worker.run_once()) is True
    done = env["client"].get(loc, headers=BEARER).json()
    assert done["state"] == "succeeded" and (env["tmp"] / "backups" / done["result"]["file"]).exists()
    assert env["client"].post(f"{API}/jobs", json={"kind": "summarize"}, headers=BEARER).status_code == 422
    assert env["client"].post(f"{API}/jobs", json={"kind": "rm -rf"}, headers=BEARER).status_code == 422


def test_job_cancel(env):
    r = env["client"].post(f"{API}/jobs", json={"kind": "backup", "params": {"n": 2}}, headers=BEARER)
    jid = r.json()["job"]["id"]
    c = env["client"].post(f"{API}/jobs/{jid}/cancel", headers=BEARER)
    assert c.status_code == 202 and c.json()["state"] == "cancelled"


def test_sse_stream_uses_header_auth_and_reports_down(env):
    with env["client"].stream("GET", f"{API}/live/stream", headers=BEARER) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert "event: metrics" in text and '"rtf": 0.4' in text
    env["live"].down = True
    with env["client"].stream("GET", f"{API}/live/stream", headers=BEARER) as r:
        assert "event: down" in "".join(r.iter_text())
    assert env["client"].get(f"{API}/live/stream").status_code == 401


def test_live_down_is_503_problem(env):
    env["live"].down = True
    r = env["client"].get(f"{API}/live/metrics", headers=BEARER)
    assert r.status_code == 503 and r.json()["code"] == "live_unavailable"


def test_static_pages_have_no_inline_script(env):
    for p in ("/admin", "/admin/login"):
        html = env["client"].get(p).text
        assert "<script>" not in html and 'src="/admin/static/' in html
    assert env["client"].get("/admin/static/../server.py").status_code in (400, 404)


def test_rtf_number_accepts_both_live_shapes():
    """Screenshot find: the live /api/metrics rtf became RtfMeter's dict; overview 500'd on rtf < 0.5."""
    from app.admin.info import rtf_color, rtf_number
    assert rtf_number(0.42) == 0.42 and rtf_color(0.42) == "green"
    snap = {"session": {"count": 3, "rtf": {"p50": 0.71, "p95": 0.9}}, "asr_rtf_p50": 0.6}
    assert rtf_number(snap) == 0.71 and rtf_color(snap) == "amber"
    assert rtf_number({"session": {"count": 0, "rtf": {}}}, {"asr_rtf_p50": 1.2}) == 1.2
    assert rtf_number({"session": {}}) is None and rtf_color({}) == "unknown" and rtf_number(True) is None


def test_monitor_card_uses_the_numeric_rtf():
    """Screenshot find: the live monitor card printed [object Object] (raw RtfMeter dict)."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "app/admin/static/app.js").read_text(encoding="utf-8")
    assert 'card("RTF", m.rtf' not in js and 'typeof d.rtf === "number"' in js
    src = (Path(__file__).resolve().parents[1] / "app/admin/observability.py").read_text(encoding="utf-8")
    assert '"rtf": rtf, "rtf_color": rtf_color(rtf), "ts": clock()}' in src
