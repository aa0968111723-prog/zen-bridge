"""後台資訊管理系統: sessions, workbench, glossary en/ja, TM, search, exports, config, audit,
users/roles, observability. Every route is exercised through the same security stack."""
import json
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from starlette.testclient import TestClient  # noqa: E402

from app.admin import db, info, observability, security  # noqa: E402
from app.admin.server import API, create_admin_app  # noqa: E402
from tests.local_fakes import seed_segment  # noqa: E402
from tests.test_admin_api import BASE, BEARER, ORIGIN, PORT, TOKEN, FakeLive, browser_login, writes  # noqa: E402


class Live(FakeLive):
    def __init__(self):
        super().__init__()
        self.pushed = []

    def push_correction(self, room_id, session_id, seq, en, zh=None):
        self.pushed.append((room_id, session_id, seq, en, zh))
        return 200, {"en": en, "seq": seq}


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "zen.sqlite3"
    live = Live()
    log = tmp_path / "asr-worker.log"
    log.write_text('x BREEZE_TIMING {"audio_s": 6.0, "asr_s": 2.4, "rtf": 0.4, "audio_ctx": 768, "max_tokens": 96, '
                   '"retried_full_ctx": false}\nnoise\nBREEZE_TIMING {"audio_s": 5.0, "asr_s": 5.0, "rtf": 1.0, '
                   '"retried_full_ctx": true}\nBREEZE_TIMING {bad json\n', encoding="utf-8")
    app = create_admin_app(path, token_hash_hex=security.token_hash(TOKEN), port=PORT,
                           identity_path=tmp_path / "zen-identity.sqlite3",
                           probes={"live": lambda: "up", "ollama": lambda: "down"}, live_client=live,
                           backup_dir=lambda: tmp_path / "backups", start_worker=False, sse_interval_s=0.0,
                           sse_max_events=2, asr_log_path=log, export_dir=tmp_path / "exports",
                           config_fn=lambda: {"api_token": "s3cret", "port": 8780}, llama_probe=lambda: "up")
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    c = db.connect(path)
    seed_segment(c, seg="s1-1", seq=1, zh="今天講因緣具足", en="Today, conditions")
    seed_segment(c, seg="s1-2", seq=2, zh="佛法", en="Dharma")
    c.close()
    with client:
        yield {"app": app, "client": client, "path": path, "live": live, "tmp": tmp_path}


def mk_user(cl, name, role, scopes="read,write"):
    r = cl.post(f"{API}/users", json={"username": name, "role": role}, headers=BEARER)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = cl.post(f"{API}/users/{uid}/tokens", json={"scopes": scopes}, headers=BEARER)
    assert r.status_code == 201 and r.json()["shown_once"]
    return uid, {"authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def roles(env):
    cl = env["client"]
    return {"admin": mk_user(cl, "ad", "admin")[1], "editor": mk_user(cl, "ed", "editor")[1],
            "viewer": mk_user(cl, "vw", "viewer", "read")[1]}


# ------------------------------------------------------------------ pure helpers
def test_helpers():
    assert info.redact({"api_token": "x", "nested": {"password": 1, "ok": 2}, "l": [{"secret": 3}]}) == \
        {"api_token": "***", "nested": {"password": "***", "ok": 2}, "l": [{"secret": "***"}]}
    assert info.csv_cell("=1+1") == "'=1+1" and info.csv_cell("ok") == "ok" and info.csv_cell(None) == ""
    assert info.fmt_ts(3723004, ",") == "01:02:03,004"
    assert info.rtf_color(0.3) == "green" and info.rtf_color(0.7) == "amber" and info.rtf_color(1.2) == "red"
    assert info.rtf_color(None) == "unknown"
    rows = [{"t0_ms": 0, "t1_ms": 1500, "zh": "佛法", "tgt": "Dharma"}, {"t0_ms": 1500, "t1_ms": 2000, "zh": "", "tgt": ""}]
    srt = info.render_export(rows, "srt", "bilingual", "t", "en")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,500\n佛法\nDharma") and "2\n" not in srt
    assert info.render_export(rows, "vtt", "en", "t", "en").startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nDharma")
    assert "# t" in info.render_export(rows, "md", "zh", "t", "en")
    assert info.parse_timing_log(None) == [] and info.parse_timing_log("/nonexistent") == []


def test_hardware_degrades(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n == "psutil" else real(n, *a, **k))
    assert info.hardware({}) == {"available": False}


def test_sample_row_no_text():
    out = observability.sample_row({"rtf": 0.5, "pending": 2, "rooms": [{"zh": "secret"}], "flag": True},
                                   {"cpu_per_core": [10, 30], "ram": {"percent": 50}})
    assert out == {"rtf": 0.5, "pending": 2, "cpu_pct": 20.0, "ram_pct": 50}


# ------------------------------------------------------------------ sessions
def test_session_crud_and_lang(env, roles):
    cl = env["client"]
    r = cl.post(f"{API}/sessions", json={"room_id": "class", "title": "週日", "tgt_lang": "ko"}, headers=roles["editor"])
    assert r.status_code == 422
    r = cl.post(f"{API}/sessions", json={"room_id": "class", "title": "週日", "tgt_lang": "ja"}, headers=roles["viewer"])
    assert r.status_code == 403
    r = cl.post(f"{API}/sessions", json={"room_id": "class", "title": "週日", "tgt_lang": "ja", "id": "s2"}, headers=roles["editor"])
    assert r.status_code == 201 and r.json()["tgt_lang"] == "ja"
    etag = r.headers["etag"]
    assert cl.post(f"{API}/sessions", json={"room_id": "class", "id": "s2"}, headers=roles["editor"]).status_code == 409
    assert cl.patch(f"{API}/sessions/s2", json={"title": "x"}, headers=roles["editor"]).status_code == 428
    assert cl.patch(f"{API}/sessions/s2", json={"title": "x"}, headers={**roles["editor"], "if-match": '"bad"'}).status_code == 412
    assert cl.patch(f"{API}/sessions/s2", json={"legal_hold": True}, headers={**roles["editor"], "if-match": etag}).status_code == 403
    r = cl.patch(f"{API}/sessions/s2", json={"tgt_lang": "en", "title": "改"}, headers={**roles["editor"], "if-match": etag})
    assert r.status_code == 200 and r.json()["tgt_lang"] == "en"
    r2 = cl.get(f"{API}/sessions/s2/etag", headers=roles["viewer"])
    assert r2.headers["etag"] == r.headers["etag"]
    r = cl.patch(f"{API}/sessions/s2", json={"legal_hold": True, "purge_after": 5000},
                 headers={**roles["admin"], "if-match": r.headers["etag"]})
    assert r.status_code == 200 and r.json()["legal_hold"] == 1
    assert cl.post(f"{API}/sessions/s2/close", headers=roles["editor"]).json()["status"] == "ended"
    assert cl.post(f"{API}/sessions/s2/close", headers=roles["editor"]).status_code == 409
    assert cl.post(f"{API}/sessions/nope/close", headers=roles["editor"]).status_code == 404


def test_delete_preview_and_cascade(env, roles):
    cl = env["client"]
    assert cl.post(f"{API}/sessions/s1/delete-preview", headers=roles["editor"]).status_code == 403
    r = cl.post(f"{API}/sessions/s1/delete-preview", headers=roles["admin"])
    assert r.status_code == 200 and r.json()["counts"]["segments"] == 2
    ic = db.connect(env["tmp"] / "zen-identity.sqlite3")
    ic.execute("INSERT INTO speaker_identities(speaker_id, session_id, display_name) VALUES (1, 's1', '王師兄')")
    ic.close()
    assert cl.request("DELETE", f"{API}/sessions/s1", json={"confirm": "s1"}, headers=roles["editor"]).status_code == 403
    r = cl.request("DELETE", f"{API}/sessions/s1", json={"confirm": "s1", "force": True}, headers=roles["admin"])
    assert r.status_code == 200, r.text
    ic = db.connect(env["tmp"] / "zen-identity.sqlite3")
    assert ic.execute("SELECT COUNT(*) FROM speaker_identities WHERE session_id='s1'").fetchone()[0] == 0   # identity DB too
    ic.close()
    c = db.connect(env["path"])
    assert c.execute("SELECT COUNT(*) FROM segments WHERE session_id='s1'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 0
    c.close()
    assert cl.get(f"{API}/sessions/s1", headers=roles["viewer"]).status_code == 404


def test_retention(env, roles):
    cl = env["client"]
    r = cl.get(f"{API}/retention", headers=roles["viewer"])
    assert r.status_code == 200 and any(i["item"] == "session_content" for i in r.json()["items"])
    assert cl.put(f"{API}/retention/session_content", json={"keep_days": 90}, headers=roles["editor"]).status_code == 403
    assert cl.put(f"{API}/retention/session_content", json={"keep_days": 0}, headers=roles["admin"]).status_code == 422
    assert cl.put(f"{API}/retention/nope", json={"keep_days": 9}, headers=roles["admin"]).status_code == 404
    assert cl.put(f"{API}/retention/session_content", json={"keep_days": 90}, headers=roles["admin"]).json()["keep_days"] == 90


# ------------------------------------------------------------------ workbench
def test_edit_history_undo_push(env, roles):
    cl = env["client"]
    h = cl.get(f"{API}/segments/s1-2/history", headers=roles["viewer"]).json()
    assert h["etags"]["en"] == '"v1"' and h["etags"]["zh"] == '"v1"'
    body = {"target": "en", "text": "The Dharma"}
    assert cl.patch(f"{API}/segments/s1-2/text", json=body, headers=roles["viewer"]).status_code == 403
    assert cl.patch(f"{API}/segments/s1-2/text", json=body, headers=roles["editor"]).status_code == 428
    r = cl.patch(f"{API}/segments/s1-2/text", json=body, headers={**roles["editor"], "if-match": '"v1"'})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["tm_pending"] is True
    assert cl.patch(f"{API}/segments/s1-2/text", json=body, headers={**roles["editor"], "if-match": '"v1"'}).status_code == 412
    q = cl.get(f"{API}/review/queue", headers=roles["editor"]).json()
    assert q["tm_pending"] == []                          # round3 §5-1: nothing is written to TM before approval
    st = q["staging_pending"]
    assert len(st) == 1 and st[0]["tgt_text"] == "The Dharma" and st[0]["kind"] == "tm"
    # push human text, never retranslate
    r = cl.post(f"{API}/segments/s1-2/push", headers=roles["editor"])
    assert r.status_code == 200 and env["live"].pushed[-1][3] == "The Dharma"
    assert cl.post(f"{API}/segments/s1-1/push", headers=roles["editor"]).status_code == 409   # MT line
    # zh correction
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "zh", "text": "佛陀的法"}, headers={**roles["editor"], "if-match": '"v1"'})
    assert r.status_code == 200
    assert cl.patch(f"{API}/segments/s1-2/text", json={"target": "ko", "text": "x"}, headers=roles["editor"]).status_code == 422
    # undo
    assert cl.post(f"{API}/segments/s1-2/undo", json={"target": "en"}, headers={**roles["editor"], "if-match": '"v1"'}).status_code == 412
    r = cl.post(f"{API}/segments/s1-2/undo", json={"target": "en"}, headers={**roles["editor"], "if-match": '"v2"'})
    assert r.status_code == 200 and r.json()["version"] == 1
    assert cl.post(f"{API}/segments/s1-2/undo", json={"target": "en"}, headers={**roles["editor"], "if-match": '"v1"'}).status_code == 409
    h = cl.get(f"{API}/segments/s1-2/history", headers=roles["viewer"]).json()
    assert len(h["translations"]) == 2 and len(h["corrections"]) == 2 and len(h["transcripts"]) == 2
    assert cl.get(f"{API}/segments/nope/history", headers=roles["viewer"]).status_code == 404


def test_admin_edit_goes_live_and_ja_push_refused(env, roles):
    cl = env["client"]
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "Dharma teaching"},
                 headers={**roles["admin"], "if-match": '"v1"'})
    # round3 §5-1: even an admin's fix is staged first, then approved explicitly
    assert r.json()["tm_pending"] is True and r.json()["tm_id"] is None
    assert cl.get(f"{API}/tm?status=live", headers=roles["viewer"]).json()["items"] == []
    sid = r.json()["tm_staging_id"]
    etag = cl.get(f"{API}/staging/{sid}", headers=roles["admin"]).headers["etag"]
    assert cl.post(f"{API}/staging/{sid}/approve", json={}, headers={**roles["admin"], "if-match": etag}).status_code == 200
    tm = cl.get(f"{API}/tm?status=live", headers=roles["viewer"]).json()["items"]
    assert tm and tm[0]["tgt_text"] == "Dharma teaching"
    c = db.connect(env["path"])
    c.execute("UPDATE sessions SET tgt_lang='ja' WHERE id='s1'")
    c.close()
    assert cl.post(f"{API}/segments/s1-2/push", headers=roles["editor"]).status_code == 422


# ------------------------------------------------------------------ glossary
def test_glossary_en_ja_lock_alias_csv(env, roles):
    cl = env["client"]
    ed = roles["editor"]
    assert cl.post(f"{API}/glossary/terms", json={"zh": "禪", "target": "禅", "tgt_lang": "en"}, headers=ed).status_code == 422
    r = cl.post(f"{API}/glossary/terms", json={"zh": "禪", "target": "Zen", "tgt_lang": "en", "aliases": ["禪宗"]}, headers=ed)
    assert r.status_code == 201
    en_id, en_etag = r.json()["id"], r.headers["etag"]
    r = cl.post(f"{API}/glossary/terms", json={"zh": "禪", "target": "禅", "tgt_lang": "ja", "reading": "ぜん"}, headers=ed)
    assert r.status_code == 201 and r.json()["reading"] == "ぜん" and r.json()["tgt_lang"] == "ja"
    ja_id = r.json()["id"]
    assert cl.post(f"{API}/glossary/terms", json={"zh": "禪", "target": "Zen"}, headers=ed).status_code == 409
    assert [t["zh"] for t in cl.get(f"{API}/glossary/terms?lang=ja", headers=roles["viewer"]).json()["items"]] == ["禪"]
    assert cl.get(f"{API}/glossary/terms?lang=en&q=禪宗", headers=roles["viewer"]).json()["items"][0]["id"] == en_id
    # lock
    r = cl.patch(f"{API}/glossary/terms/{en_id}", json={"locked": True}, headers={**ed, "if-match": en_etag})
    assert r.status_code == 200 and r.json()["locked"] == 1
    et = r.headers["etag"]
    assert cl.patch(f"{API}/glossary/terms/{en_id}", json={"target": "Chan"}, headers={**ed, "if-match": et}).status_code == 409
    assert cl.request("DELETE", f"{API}/glossary/terms/{en_id}", headers={**ed, "if-match": et}).status_code == 409
    r = cl.patch(f"{API}/glossary/terms/{en_id}", json={"locked": False, "target": "Chan"}, headers={**ed, "if-match": et})
    assert r.status_code == 200 and r.json()["en"] == "Chan"
    r = cl.patch(f"{API}/glossary/terms/{ja_id}", json={"reading": "ゼン"},
                 headers={**ed, "if-match": cl.get(f"{API}/glossary/terms?lang=ja", headers=ed).json()["items"][0]["etag"]})
    assert r.json()["reading"] == "ゼン"
    # suggestion -> merge as alias
    r = cl.post(f"{API}/glossary/terms", json={"zh": "禪那", "target": "Dhyana", "suggest": True}, headers=ed)
    sid, setag = r.json()["id"], r.headers["etag"]
    assert r.json()["status"] == "proposed"
    assert cl.post(f"{API}/glossary/proposals/{sid}/merge", json={}, headers={**ed, "if-match": setag}).status_code == 422
    r = cl.post(f"{API}/glossary/proposals/{sid}/merge", json={"into_term_id": en_id}, headers={**ed, "if-match": setag})
    assert r.status_code == 200 and "禪那" in r.json()["aliases"]
    assert cl.post(f"{API}/glossary/proposals/{sid}/merge", json={"into_term_id": en_id}, headers={**ed, "if-match": '"x"'}).status_code == 412
    # CSV export (formula-safe) + import dry run + real
    r = cl.get(f"{API}/glossary/export.csv?lang=en", headers=roles["viewer"])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "Chan" in r.text
    csvt = "zh,target,aliases\n佛法,Dharma,法\n開示,=cmd,\n" + "x" * 30 + ",bad,\n"
    r = cl.post(f"{API}/glossary/import", json={"csv": csvt, "dry_run": True}, headers=ed)
    assert r.status_code == 200 and r.json()["counts"]["add"] == 2 and r.json()["counts"]["errors"] == 1
    assert cl.get(f"{API}/glossary/terms?lang=en&q=佛法", headers=ed).json()["items"] == []
    r = cl.post(f"{API}/glossary/import", json={"csv": csvt}, headers=ed)
    assert r.json()["counts"]["add"] == 2
    assert "'=cmd" in cl.get(f"{API}/glossary/export.csv?lang=en", headers=ed).text
    r = cl.post(f"{API}/glossary/import", json={"csv": "zh,target,reading\n法,法,ほう\n", "tgt_lang": "ja"}, headers=ed)
    assert r.json()["counts"]["add"] == 1
    assert cl.post(f"{API}/glossary/import", json={"csv": ""}, headers=ed).status_code == 422
    assert cl.post(f"{API}/glossary/import", json={"csv": csvt}, headers=roles["viewer"]).status_code == 403
    # retire
    t = cl.get(f"{API}/glossary/terms?lang=en&q=佛法", headers=ed).json()["items"][0]
    assert cl.request("DELETE", f"{API}/glossary/terms/{t['id']}", headers={**ed, "if-match": t["etag"]}).status_code == 204


def test_push_excludes_ja_terms(env, roles):
    cl = env["client"]
    cl.post(f"{API}/glossary/terms", json={"zh": "法師", "target": "法師", "tgt_lang": "ja"}, headers=roles["editor"])
    cl.post(f"{API}/glossary/terms", json={"zh": "法會", "target": "Dharma assembly"}, headers=roles["editor"])
    r = cl.post(f"{API}/rooms/class/glossary/push", json={"dry_run": True}, headers=roles["editor"])
    assert r.status_code == 200
    assert [a["zh"] for a in r.json()["added"]] == ["法會"]


# ------------------------------------------------------------------ TM
def test_tm_browser(env, roles):
    cl = env["client"]
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "The Dharma"}, headers={**roles["editor"], "if-match": '"v1"'})
    assert cl.get(f"{API}/tm?q=Dharma&status=pending&lang=en", headers=roles["viewer"]).json()["items"] == []
    sid = r.json()["tm_staging_id"]
    etag = cl.get(f"{API}/staging/{sid}", headers=roles["admin"]).headers["etag"]
    assert cl.post(f"{API}/staging/{sid}/approve", json={}, headers={**roles["admin"], "if-match": etag}).status_code == 200
    items = cl.get(f"{API}/tm?q=Dharma&status=live&lang=en", headers=roles["viewer"]).json()["items"]
    assert len(items) == 1
    tid = items[0]["id"]
    r = cl.get(f"{API}/tm/{tid}", headers=roles["viewer"])
    p = r.json()["provenance"]
    assert p["session_id"] == "s1" and p["seq"] == 2 and len(p["corrections"]) == 1
    assert cl.patch(f"{API}/tm/{tid}", json={"tgt_text": "x"}, headers=roles["editor"]).status_code == 428
    assert cl.patch(f"{API}/tm/{tid}", json={"tgt_text": "佛"}, headers={**roles["editor"], "if-match": r.headers["etag"]}).status_code == 422
    r = cl.patch(f"{API}/tm/{tid}", json={"tgt_text": "Buddha Dharma"}, headers={**roles["admin"], "if-match": r.headers["etag"]})
    assert r.status_code == 200 and r.json()["state"] == "live"
    assert cl.post(f"{API}/tm/{tid}/disable", headers=roles["viewer"]).status_code == 403
    assert cl.post(f"{API}/tm/{tid}/disable", headers=roles["editor"]).json()["state"] == "disabled"
    assert cl.get(f"{API}/tm/999", headers=roles["viewer"]).status_code == 404
    assert cl.get(f"{API}/tm?cursor=bogus", headers=roles["viewer"]).status_code == 400


# ------------------------------------------------------------------ search
def test_search_routes_and_filters(env, roles):
    cl = env["client"]
    r = cl.get(f"{API}/search", params={"q": "因緣具足"}, headers=roles["viewer"])
    assert r.status_code == 200 and r.json()["route"] == "trigram" and r.json()["items"]
    r = cl.get(f"{API}/search", params={"q": "佛"}, headers=roles["viewer"])
    assert r.json()["route"] == "unigram" and r.json()["items"]
    assert cl.get(f"{API}/search", params={"q": "因緣具足", "date_from": 5000}, headers=roles["viewer"]).json()["items"] == []
    rr = cl.get(f"{API}/search", params={"q": "因緣具足", "in_session": "zz"}, headers=roles["viewer"]); assert rr.json().get("items") == [], rr.text
    assert cl.get(f"{API}/search", params={"q": "Dharma", "lang": "en"}, headers=roles["viewer"]).json()["items"]
    assert cl.get(f"{API}/search", params={"q": "Dharma", "lang": "ja"}, headers=roles["viewer"]).json()["items"] == []
    assert cl.get(f"{API}/search", params={"q": "a", "lang": "ko"}, headers=roles["viewer"]).status_code == 422
    assert cl.get(f"{API}/search", params={"q": "a\x01"}, headers=roles["viewer"]).status_code == 422
    assert cl.get(f"{API}/search", params={"q": "x"}).status_code == 401


# ------------------------------------------------------------------ exports
@pytest.mark.parametrize("fmt", ["srt", "vtt", "md"])
def test_exports_as_jobs(env, roles, fmt):
    cl = env["client"]
    assert cl.post(f"{API}/sessions/s1/exports", json={"format": "docx"}, headers=roles["editor"]).status_code == 422
    assert cl.post(f"{API}/sessions/s1/exports", json={"format": fmt, "variant": "xx"}, headers=roles["editor"]).status_code == 422
    assert cl.post(f"{API}/sessions/nope/exports", json={"format": fmt}, headers=roles["editor"]).status_code == 404
    r = cl.post(f"{API}/sessions/s1/exports", json={"format": fmt}, headers=roles["editor"])
    assert r.status_code == 202 and r.headers["location"].startswith(f"{API}/jobs/")
    from app.admin.jobs import JobContext
    store = env["app"].state.store
    job = store.get(r.json()["job"]["id"])
    out = env["app"].state.worker.handlers["export"](JobContext(store, job))
    assert out["segments"] == 2
    files = cl.get(f"{API}/exports?in_session=s1", headers=roles["viewer"]).json()["items"]
    assert files[0]["file"] == out["file"]
    f = cl.get(f"{API}/exports/file/{out['file']}", headers=roles["viewer"])
    assert f.status_code == 200 and "attachment" in f.headers["content-disposition"] and "佛法" in f.text
    assert cl.get(f"{API}/exports/file/..%2Fzen.sqlite3", headers=roles["viewer"]).status_code == 404
    assert cl.get(f"{API}/exports/file/nope.srt", headers=roles["viewer"]).status_code == 404


# ------------------------------------------------------------------ config / models
def test_config_redacted_and_settable(env, roles, monkeypatch):
    cl = env["client"]
    monkeypatch.setenv("ZEN_ADMIN_TOKEN", "abc")
    c = db.connect(env["path"])
    c.execute("INSERT INTO model_profiles(name, tier, config) VALUES ('fast','cpu','{\"api_key\":\"k\",\"model\":\"m\"}')")
    c.close()
    r = cl.get(f"{API}/config", headers=roles["viewer"]).json()
    assert r["env"]["ZEN_ADMIN_TOKEN"] == "***" and r["extra"]["api_token"] == "***" and r["extra"]["port"] == 8780
    assert r["profiles"][0]["config"] == {"api_key": "***", "model": "m"}
    assert cl.put(f"{API}/config/default_tgt_lang", json={"value": "ja"}, headers=roles["editor"]).status_code == 403
    assert cl.put(f"{API}/config/update_channel", json={"value": "x"}, headers=roles["admin"]).status_code == 403
    assert cl.put(f"{API}/config/default_tgt_lang", json={"value": "fr"}, headers=roles["admin"]).status_code == 422
    assert cl.put(f"{API}/config/default_tgt_lang", json={"value": "ja"}, headers=roles["admin"]).json()["value"] == "ja"
    assert cl.put(f"{API}/config/translate_profile", json={"value": "nope"}, headers=roles["admin"]).status_code == 422
    assert cl.put(f"{API}/config/translate_profile", json={"value": "fast"}, headers=roles["admin"]).json()["restart_required"]
    assert cl.get(f"{API}/config", headers=roles["viewer"]).json()["profiles"][0]["is_active"] == 1
    h = cl.get(f"{API}/models/health", headers=roles["viewer"]).json()
    assert h["ollama"] == "down" and h["llama_server"] == "up" and h["live"] == "up"


# ------------------------------------------------------------------ audit / events / users
def test_audit_and_events(env, roles):
    cl = env["client"]
    cl.post(f"{API}/sessions/s1/close", headers=roles["editor"])
    assert cl.get(f"{API}/audit", headers=roles["editor"]).status_code == 403
    items = cl.get(f"{API}/audit", headers=roles["admin"]).json()["items"]
    kinds = [i["kind"] for i in items]
    assert "audit.session.close" in kinds and "audit.user.create" in kinds and "audit.token.create" in kinds
    assert all("zat_" not in json.dumps(i["payload"]) for i in items)          # plaintext token never logged
    assert cl.get(f"{API}/events?kind=audit.session&in_session=s1", headers=roles["viewer"]).json()["items"][0]["session_id"] == "s1"
    assert cl.get(f"{API}/events?level=bogus", headers=roles["viewer"]).status_code == 422
    assert cl.get(f"{API}/audit?limit=1", headers=roles["admin"]).json()["next"]


def test_users_roles(env, roles):
    cl = env["client"]
    lst = cl.get(f"{API}/users", headers=roles["admin"]).json()["items"]
    assert {u["username"] for u in lst} == {"ad", "ed", "vw"} and all(u["tokens"] == 1 for u in lst)
    assert cl.get(f"{API}/users", headers=roles["editor"]).status_code == 403
    assert cl.post(f"{API}/users", json={"username": "x2", "role": "admin"}, headers=roles["admin"]).status_code == 403
    assert cl.post(f"{API}/users", json={"username": "x2", "role": "god"}, headers=BEARER).status_code == 422
    assert cl.post(f"{API}/users", json={"username": "ed", "role": "viewer"}, headers=BEARER).status_code == 409
    vw = next(u for u in lst if u["username"] == "vw")
    r = cl.patch(f"{API}/users/{vw['id']}", json={"role": "editor"}, headers=roles["admin"])
    assert r.status_code == 200 and r.json()["role"] == "editor"
    ad = next(u for u in lst if u["username"] == "ad")
    assert cl.patch(f"{API}/users/{ad['id']}", json={"role": "viewer"}, headers=roles["admin"]).status_code == 409
    cl.patch(f"{API}/users/{vw['id']}", json={"disabled": True}, headers=roles["admin"])
    assert cl.get(f"{API}/overview", headers=roles["viewer"]).status_code == 401   # disabled user's token stops working
    assert cl.post(f"{API}/users/{vw['id']}/tokens", json={"scopes": "read,write,admin"}, headers=roles["admin"]).status_code == 422
    assert cl.post(f"{API}/users/999/tokens", json={}, headers=roles["admin"]).status_code == 404
    c = db.connect(env["tmp"] / "zen-identity.sqlite3")
    tid = c.execute("SELECT id FROM api_tokens WHERE user_id=?", (ad["id"],)).fetchone()[0]
    c.close()
    assert cl.request("DELETE", f"{API}/tokens/{tid}", headers=BEARER).status_code == 204
    assert cl.get(f"{API}/users", headers=roles["admin"]).status_code == 401
    assert cl.request("DELETE", f"{API}/tokens/{tid}", headers=BEARER).status_code == 404


def test_read_token_cannot_write(env, roles):
    _, h = mk_user(env["client"], "ro", "editor", "read")
    assert env["client"].post(f"{API}/sessions/s1/close", headers=h).status_code == 403


def test_cookie_writes_need_csrf_on_new_routes(env):
    cl = env["client"]
    csrf = browser_login(cl, env["app"])
    assert cl.post(f"{API}/sessions/s1/close", headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"}).status_code == 403
    assert cl.post(f"{API}/sessions/s1/close", headers={**writes(csrf), "sec-fetch-site": "cross-site"}).status_code == 403
    assert cl.post(f"{API}/sessions/s1/close", headers={**writes(csrf), "origin": "http://127.0.0.1:8645"}).status_code == 403
    assert cl.post(f"{API}/sessions/s1/close", headers=writes(csrf)).status_code == 200


def test_non_loopback_refused_on_new_routes(env):
    c = TestClient(env["app"], base_url=BASE, client=("10.0.0.5", 4000))
    assert c.get(f"{API}/overview", headers=BEARER).status_code == 403


# ------------------------------------------------------------------ observability
def test_overview_and_stale(env, roles):
    cl = env["client"]
    r = cl.get(f"{API}/overview", headers=roles["viewer"]).json()
    assert r["components"]["db"]["status"] == "ok" and r["components"]["admin"]["status"] == "up"
    assert r["components"]["llama_server"]["status"] == "up" and r["rtf_color"] == "green" and r["stale"] is False
    assert r["config"]["api_token"] == "***"
    env["live"].down = True
    r = cl.get(f"{API}/overview", headers=roles["viewer"]).json()
    assert r["live_error"] == "LiveDown" and r["live"]["rtf"] == 0.4      # last good snapshot kept


def test_hardware_pipeline_inventory_feed(env, roles):
    cl = env["client"]
    hw = cl.get(f"{API}/overview/hardware", headers=roles["viewer"]).json()
    assert hw["available"] is True and "data" in hw["disks"] and isinstance(hw["processes"], list)
    p = cl.get(f"{API}/overview/pipeline", headers=roles["viewer"]).json()
    assert len(p["segments"]) == 2 and set(p["segments"][0]["timeline"]) == {
        "audio_end", "asr_start", "asr_end", "translate_start", "translate_end", "published"}
    assert p["asr"]["count"] == 2 and p["asr"]["repetition_guard_hits"] == 1 and p["asr"]["rtf_avg"] == 0.7
    assert cl.get(f"{API}/overview/asr-timings", headers=roles["viewer"]).json()["rtf_last"] == 1.0
    inv = cl.get(f"{API}/overview/inventory", headers=roles["viewer"]).json()
    assert inv["counts"]["segments"] == 2 and inv["counts"]["translations_en"] == 2 and inv["sizes"]["main_db"] > 0
    assert inv["backups"]["count"] == 0
    cl.post(f"{API}/sessions/s1/close", headers=roles["editor"])
    f = cl.get(f"{API}/overview/feed", headers=roles["viewer"]).json()
    assert all(not e["kind"].startswith("audit.") for e in f["events"])
    f = cl.get(f"{API}/overview/feed", headers=roles["admin"]).json()
    assert any(e["kind"] == "audit.session.close" for e in f["events"])


def test_drilldown(env, roles):
    cl = env["client"]
    cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "The Dharma"}, headers={**roles["editor"], "if-match": '"v1"'})
    d = cl.get(f"{API}/sessions/s1/drilldown", headers=roles["viewer"]).json()
    assert len(d["segments"]) == 2 and d["segments"][1]["tgt"] == "The Dharma" and len(d["corrections"]) == 1
    assert set(d) == {"session", "segments", "corrections", "errors", "exports", "timings"}
    assert cl.get(f"{API}/sessions/zz/drilldown", headers=roles["viewer"]).status_code == 404


def test_metrics_persist_rollup_export(env, roles):
    cl = env["client"]
    assert cl.post(f"{API}/metrics/sample", headers=roles["editor"]).status_code == 403
    r = cl.post(f"{API}/metrics/sample", headers=roles["admin"]).json()
    assert r["values"]["rtf"] == 0.4
    c = db.connect(env["path"])
    c.execute("INSERT INTO metrics(ts, name, value) VALUES (1000, 'rtf', 0.5), (1001, 'rtf', 0.7)")
    c.close()
    j = cl.get(f"{API}/metrics/export?format=json&name=rtf", headers=roles["viewer"]).json()
    assert j["columns"][0] == "ts" and len(j["rows"]) == 3
    csvr = cl.get(f"{API}/metrics/export?name=rtf", headers=roles["viewer"])
    assert csvr.text.splitlines()[0] == "ts,name,value,room_id,session_id"
    r = cl.post(f"{API}/metrics/rollup", headers=roles["admin"]).json()
    assert r["deleted"] == 2 and r["rolled_groups"] == 1
    j = cl.get(f"{API}/metrics/export?format=json&rollup=1", headers=roles["viewer"]).json()
    assert j["rows"][0][1] == "rtf" and j["rows"][0][3] == 2
    assert cl.get(f"{API}/metrics/export?format=xml", headers=roles["viewer"]).status_code == 422
    # no caption text in metrics
    c = db.connect(env["path"])
    assert not c.execute("SELECT 1 FROM metrics WHERE labels IS NOT NULL").fetchone()
    c.close()


def test_monitor_sse(env, roles):
    cl = env["client"]
    with cl.stream("GET", f"{API}/monitor/stream", headers=roles["viewer"]) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert body.startswith("retry: 3000") and body.count("event: metrics") == 2 and '"rtf_color": "green"' in body
    assert cl.get(f"{API}/monitor/stream").status_code == 401
    c = db.connect(env["path"])
    c.execute("INSERT INTO events(kind, level) VALUES ('asr.crash', 'error')")
    c.close()
    env["live"].down = True
    with cl.stream("GET", f"{API}/monitor/stream", headers=roles["viewer"]) as r:
        body = "".join(r.iter_text())
    assert "event: errors" in body and '"stale": true' in body


def test_sampler_registered_only_when_enabled(tmp_path):
    app = create_admin_app(tmp_path / "z.sqlite3", token_hash_hex=security.token_hash(TOKEN), port=PORT,
                           start_worker=False, probes={}, live_client=Live(), backup_dir=lambda: tmp_path / "b",
                           metrics_sample_s=5.0, export_dir=tmp_path / "e")
    assert [f.__name__ for f in app.state.bg_tasks] == ["sampler"]


# ------------------------------------------------------------------ static frontend (UIUX-A)
def test_static_pages_csp_viewport_no_inline(env):
    cl = env["client"]
    for page in ("/admin", "/admin/login"):
        r = cl.get(page)
        assert r.status_code == 200
        assert 'name="viewport"' in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]
        import re
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)        # no inline script
        assert " on" not in re.sub(r"\son\w+=\"[^\"]*\"", " on", "") and not re.search(r"\son[a-z]+=", r.text)
    r = cl.get("/admin/static/admin_logic.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    js = cl.get("/admin/static/app.js").text
    assert "cdn" not in js.lower() and "http://" not in js and "https://" not in js
    assert "problemMessage" in js and "backoff" in js and "showRelogin" in js and "aria-label" in js
    assert cl.get("/admin/static/evil.js").status_code == 404
    # zen-shi logo: the header/favicon files are served (they were 404 before), nothing else under brand/
    for name in ("favicon.ico", "logo-32.png", "logo-128.png", "logo-256.png"):
        assert cl.get(f"/admin/static/brand/{name}").status_code == 200, name
    assert cl.get("/admin/static/brand/../server.py").status_code in (400, 404)
    assert cl.get("/admin/static/brand/evil.png").status_code == 404
    html = cl.get("/admin").text
    assert 'aria-label="後台頁面"' in html and 'href="#overview"' in html
