"""round3 §5-1 admin staging approval flow (app/admin/staging.py, migration 0010)."""
import json
import sqlite3
import threading
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from starlette.testclient import TestClient  # noqa: E402

from app import tm as ztm  # noqa: E402
from app.admin import db, security, staging  # noqa: E402
from app.admin.server import API, create_admin_app, enc_cursor  # noqa: E402
from tests.local_fakes import seed_segment  # noqa: E402
from tests.test_admin_api import BASE, BEARER, ORIGIN, PORT, TOKEN, FakeLive, browser_login, env, writes  # noqa: E402,F401
from tests.test_admin_info import mk_user  # noqa: E402

PROBLEM = "application/problem+json"
URL = f"{API}/staging"


@pytest.fixture
def roles(env):  # noqa: F811
    cl = env["client"]
    return {"admin": mk_user(cl, "ad", "admin")[1], "editor": mk_user(cl, "ed", "editor")[1],
            "editor2": mk_user(cl, "ed2", "editor")[1], "viewer": mk_user(cl, "vw", "viewer", "read")[1]}


def q(env, sql, args=()):
    c = db.connect(env["path"])
    try:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def etag_of(cl, sid, h=BEARER):
    r = cl.get(f"{URL}/{sid}", headers=h)
    assert r.status_code == 200, r.text
    return r.headers["etag"]


def new_item(cl, h=BEARER, **kw):
    body = {"kind": "term", "tgt_lang": "en", "src_text": "因緣", "tgt_text": "causes and conditions", **kw}
    r = cl.post(URL, json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def approve(cl, sid, h=BEARER, **body):
    return cl.post(f"{URL}/{sid}/approve", json=body, headers={**h, "if-match": etag_of(cl, sid, h)})


# ------------------------------------------------------------------ migration
def _v2(path: Path) -> Path:
    c = db.connect(path)
    try:
        db._apply_schema(c, db.SCHEMA_PATH, db.BASELINE_VERSION, "資料庫", record_checksum=True)
        c.execute("INSERT INTO glossaries(name, scope) VALUES ('global', 'global')")
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en) VALUES (1, '空性', 'emptiness')")
    finally:
        c.close()
    return path


def test_migration_up_from_existing_v3_db(tmp_path):
    p = _v2(tmp_path / "zen.sqlite3")
    c = db.connect(p)
    assert db.run_migrations(c, target=3) == 3                 # the release before staging
    c.execute("INSERT INTO tm_units(src_text, src_norm, tgt_text, src_hash) VALUES ('佛法', '佛法', 'Dharma', ?)",
              (ztm.src_hash("佛法"),))
    c.close()
    assert db.migrate(p) == db.SCHEMA_VERSION == 12
    assert (tmp_path / "pre-migrate-v3.sqlite3").is_file()     # data existed -> verified snapshot first
    c = db.connect(p)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
        assert {"staging_items", "staging_audit", "staging_items_one_pending"} <= names
        row = c.execute("SELECT name, checksum FROM schema_migrations WHERE version=10").fetchone()
        assert row["name"] == "admin_staging" and len(row["checksum"]) == 64
        assert c.execute("SELECT count(*) FROM tm_units").fetchone()[0] == 1          # data untouched
        assert c.execute("SELECT en FROM glossary_terms").fetchone()[0] == "emptiness"
    finally:
        c.close()
    assert db.migrate(p) == db.SCHEMA_VERSION                    # idempotent, no second snapshot
    assert sorted(x.name for x in tmp_path.glob("pre-migrate-*")) == ["pre-migrate-v2.sqlite3", "pre-migrate-v3.sqlite3"]


def test_runner_allows_reserved_gap_but_refuses_late_lower_number(tmp_path):
    d = tmp_path / "mig"
    d.mkdir()
    (d / "0003_a.sql").write_text("CREATE TABLE IF NOT EXISTS a3(x);\n", encoding="utf-8")
    (d / "0010_b.sql").write_text("CREATE TABLE IF NOT EXISTS b10(x);\n", encoding="utf-8")
    assert [v for v, _, _ in db.migration_steps(d)] == [3, 10]
    p = _v2(tmp_path / "zen.sqlite3")
    c = db.connect(p)
    try:
        assert db.run_migrations(c, directory=d, target=10) == 10
        (d / "0004_late.sql").write_text("CREATE TABLE late4(x);\n", encoding="utf-8")
        with pytest.raises(db.SchemaError, match="0004_late.sql"):
            db.run_migrations(c, directory=d, target=10)     # never silently skipped
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='late4'").fetchone()
    finally:
        c.close()
    (d / "0004_late.sql").rename(d / "0010_dup.sql")
    with pytest.raises(db.SchemaError):
        db.migration_steps(d)                                   # same number twice


# ------------------------------------------------------------------ correction endpoints only stage
def test_corrections_never_write_tm_or_glossary_directly(env, roles):
    cl = env["client"]
    body = {"segment_id": "s1-1", "target_type": "translation", "text": "Today, causes and conditions ripen.",
            "propose_term": {"zh": "因緣", "en": "causes and conditions"}}
    r = cl.post(f"{API}/corrections", json=body, headers=roles["admin"])       # even an admin
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["tm_id"] is None and out["term_id"] is None and out["tm_staging_id"] and out["term_staging_id"]
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "The Dharma"},
                 headers={**roles["editor"], "if-match": '"v1"'})
    assert r.status_code == 200 and r.json()["tm_staging_id"]
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 0
    assert q(env, "SELECT count(*) FROM glossary_terms")[0][0] == 0
    items = cl.get(URL, headers=roles["editor"]).json()["items"]
    assert sorted(i["kind"] for i in items) == ["term", "tm", "tm"]
    assert all(i["state"] == "pending" and i["etag"].startswith('"s') for i in items)


def test_tm_happy_path_audit_and_served(env, roles):
    cl = env["client"]
    r = cl.post(f"{API}/corrections", json={"segment_id": "s1-1", "target_type": "translation",
                                            "text": "Today, causes and conditions ripen."}, headers=roles["editor"])
    sid = r.json()["tm_staging_id"]
    d = cl.get(f"{URL}/{sid}", headers=roles["editor"])
    assert d.status_code == 200 and d.headers["etag"] == '"s%d-r1"' % sid
    j = d.json()
    assert j["src_text"] == "今天講因緣具足" and j["conflict"] is False and j["base"] == []
    assert [a["action"] for a in j["audit"]] == ["create"]
    r = approve(cl, sid, roles["admin"], note="ok")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["item"]["state"] == "approved" and r.headers["etag"] == '"s%d-r2"' % sid and res["override_used"] is False
    tm_id = res["target_id"]
    assert q(env, "SELECT quality, origin, segment_id FROM tm_units WHERE id=?", (tm_id,))[0] == (5, "approved", "s1-1")
    assert q(env, "SELECT promoted_tm_id FROM corrections")[0][0] == tm_id
    mem = ztm.TranslationMemory(lambda: db.connect(env["path"], readonly=True))
    assert mem.exact("今天講因緣具足").tgt == "Today, causes and conditions ripen."
    audit = cl.get(f"{URL}/{sid}", headers=roles["editor"]).json()["audit"]
    assert [a["action"] for a in audit] == ["create", "approve"]
    assert audit[1]["before"] == [] and audit[1]["after"][0]["id"] == tm_id and audit[1]["actor_id"] is not None
    assert audit[1]["note"] == "ok"
    ev = q(env, "SELECT payload FROM events WHERE kind='audit.staging.approve'")
    assert ev and "Today" not in ev[0][0]                       # events stay text-free


def test_ja_tm_and_term_approval_bumps_version_without_live_push(env, roles):
    cl = env["client"]
    it = new_item(cl, roles["editor"], kind="tm", tgt_lang="ja", src_text="佛法", tgt_text="仏法")
    assert approve(cl, it["id"], roles["admin"]).status_code == 200
    assert q(env, "SELECT tgt_lang, tgt_text FROM tm_units")[0] == ("ja", "仏法")
    t = new_item(cl, roles["editor"], aliases=["因缘法"])
    r = approve(cl, t["id"], roles["admin"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["live_push"]["pushed"] is False and "if_room_version" in out["live_push"]["how"]
    assert env["live"].puts == []                               # never pushed to the live room
    row = q(env, "SELECT t.status, t.en, t.aliases, t.source, g.version FROM glossary_terms t "
                 "JOIN glossaries g ON g.id=t.glossary_id WHERE t.zh='因緣'")[0]
    assert row[:4] == ("active", "causes and conditions", '["因缘法"]', "manual") and row[4] == out["glossary_version"] >= 2
    # existing term updated: rev + version bump again
    t2 = new_item(cl, roles["editor"], tgt_text="dependent arising")
    out2 = approve(cl, t2["id"], roles["admin"]).json()
    assert out2["glossary_version"] == out["glossary_version"] + 1
    assert q(env, "SELECT en, rev FROM glossary_terms WHERE zh='因緣'")[0] == ("dependent arising", 2)


# ------------------------------------------------------------------ auth / CSRF / problem+json
def test_auth_roles_csrf_and_problem_json(env, roles):
    cl = env["client"]
    r = cl.get(URL)
    assert r.status_code == 401 and r.headers["content-type"].startswith(PROBLEM)
    assert {"type", "title", "status", "detail"} <= set(r.json())
    assert cl.get(URL, headers=roles["viewer"]).status_code == 403
    it = new_item(cl, roles["editor"])
    r = cl.post(f"{URL}/{it['id']}/approve", json={}, headers={**roles["editor"], "if-match": it["etag"]})
    assert r.status_code == 403 and r.headers["content-type"].startswith(PROBLEM)
    csrf = browser_login(cl, env["app"])                        # owner session
    body = {"kind": "tm", "src_text": "佛法", "tgt_text": "The Dharma"}
    assert cl.post(URL, json=body, headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"}).status_code == 403
    assert cl.post(URL, json=body, headers={**writes(csrf), "origin": "http://127.0.0.1:8645"}).status_code == 403
    r = cl.post(URL, json=body, headers=writes(csrf))
    assert r.status_code == 201, r.text
    r = cl.post(f"{URL}/{r.json()['id']}/approve", json={}, headers={**writes(csrf), "if-match": r.headers["etag"]})
    assert r.status_code == 200
    for bad in (cl.get(f"{URL}/999", headers=BEARER), cl.get(f"{URL}/abc", headers=BEARER),
                cl.post(URL, content=b"{nope", headers={**BEARER, "content-type": "application/json"})):
        assert bad.status_code in (400, 404, 422) and bad.headers["content-type"].startswith(PROBLEM)
        assert "Traceback" not in bad.text


def test_if_match_required_strong_and_current(env, roles):
    cl = env["client"]
    it = new_item(cl, roles["editor"])
    u = f"{URL}/{it['id']}/approve"
    r = cl.post(u, json={}, headers=roles["admin"])
    assert r.status_code == 428 and r.json()["etag"] == it["etag"]
    assert cl.post(u, json={}, headers={**roles["admin"], "if-match": "*"}).status_code == 428
    assert cl.post(u, json={}, headers={**roles["admin"], "if-match": "W/" + it["etag"]}).status_code == 412
    r = cl.post(u, json={}, headers={**roles["admin"], "if-match": '"s999-r1"'})
    assert r.status_code == 412 and r.json()["code"] == "precondition_failed"
    e = cl.patch(f"{URL}/{it['id']}", json={"tgt_text": "conditions"}, headers={**roles["editor"], "if-match": it["etag"]})
    assert e.status_code == 200 and e.headers["etag"] != it["etag"] and e.json()["rev"] == 2
    assert cl.post(u, json={}, headers={**roles["admin"], "if-match": it["etag"]}).status_code == 412   # stale
    assert cl.post(u, json={}, headers={**roles["admin"], "if-match": e.headers["etag"]}).status_code == 200


# ------------------------------------------------------------------ state machine
def test_state_transitions_reject_withdraw_supersede(env, roles):
    cl = env["client"]
    a = new_item(cl, roles["editor"])
    same = cl.post(URL, json={"kind": "term", "src_text": "因緣", "tgt_text": "causes and conditions"},
                   headers=roles["editor2"])
    assert same.status_code == 200 and same.json()["id"] == a["id"]       # identical proposal: no duplicate
    b = new_item(cl, roles["editor2"], tgt_text="conditions")              # new proposal supersedes
    assert q(env, "SELECT state, superseded_by FROM staging_items WHERE id=?", (a["id"],))[0] == ("superseded", b["id"])
    r = approve(cl, a["id"], roles["admin"])
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
    # reject needs a reason
    e = etag_of(cl, b["id"])
    assert cl.post(f"{URL}/{b['id']}/reject", json={}, headers={**roles["admin"], "if-match": e}).status_code == 422
    r = cl.post(f"{URL}/{b['id']}/reject", json={"reason": "譯名不統一"}, headers={**roles["admin"], "if-match": e})
    assert r.status_code == 200 and r.json()["item"]["state"] == "rejected" and r.json()["item"]["decision_note"] == "譯名不統一"
    assert approve(cl, b["id"], roles["admin"]).status_code == 409             # rejected is final
    assert q(env, "SELECT count(*) FROM glossary_terms")[0][0] == 0
    # withdraw: author or admin only
    c = new_item(cl, roles["editor"], src_text="佛法", tgt_text="Dharma")
    r = cl.post(f"{URL}/{c['id']}/withdraw", headers={**roles["editor2"], "if-match": c["etag"]})
    assert r.status_code == 403
    assert cl.patch(f"{URL}/{c['id']}", json={"note": "x"}, headers={**roles["editor2"], "if-match": c["etag"]}).status_code == 403
    r = cl.post(f"{URL}/{c['id']}/withdraw", headers={**roles["editor"], "if-match": c["etag"]})
    assert r.status_code == 200 and r.json()["item"]["state"] == "withdrawn"
    r = cl.patch(f"{URL}/{c['id']}", json={"note": "x"}, headers={**roles["editor"], "if-match": r.headers["etag"]})
    assert r.status_code == 409
    states = {i["id"]: i["state"] for i in cl.get(f"{URL}?state=all", headers=roles["editor"]).json()["items"]}
    assert states == {a["id"]: "superseded", b["id"]: "rejected", c["id"]: "withdrawn"}
    actions = [x[0] for x in q(env, "SELECT action FROM staging_audit ORDER BY id")]
    assert actions == ["create", "create", "supersede", "reject", "create", "withdraw"]


def test_validation(env, roles):
    cl = env["client"]
    ed = roles["editor"]
    cases = [
        ({"kind": "term", "src_text": "因缘", "tgt_text": "conditions"}, "not_traditional"),   # simplified zh
        ({"kind": "term", "src_text": "因緣", "tgt_text": "因緣 conditions"}, "invalid"),
        ({"kind": "term", "src_text": "因緣", "tgt_text": "x", "aliases": ["a"] * 9}, "invalid"),
        ({"kind": "term", "src_text": "很" * 21, "tgt_text": "x"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "佛法 Dharma"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "<think>x</think> Dharma"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "x" * 2001}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "Dharma", "tgt_lang": "ko"}, "invalid"),
        ({"kind": "glossary", "src_text": "佛法", "tgt_text": "Dharma"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "Dharma", "room_id": "a b"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "Dharma", "room_id": "x?y"}, "invalid"),
        ({"kind": "tm", "src_text": "佛法", "tgt_text": "Dharma", "state": "approved"}, "invalid"),
    ]
    for body, code in cases:
        r = cl.post(URL, json=body, headers=ed)
        assert r.status_code == 422 and r.json()["code"] == code, (body, r.text)
    assert cl.post(URL, json={"kind": "tm", "src_text": "佛法", "tgt_text": "Dharma", "segment_id": "nope"},
                   headers=ed).status_code == 404
    assert q(env, "SELECT count(*) FROM staging_items")[0][0] == 0


# ------------------------------------------------------------------ conflicts
def test_tm_conflict_override_and_rebase(env, roles):
    cl = env["client"]
    it = new_item(cl, roles["editor"], kind="tm", src_text="佛法", tgt_text="The Buddha's teaching")
    c = db.connect(env["path"])
    ztm.add_unit(c, "佛法", "Buddhadharma", origin="import", quality=4)        # someone else changed TM
    c.close()
    assert cl.get(f"{URL}/{it['id']}", headers=roles["editor"]).json()["conflict"] is True
    r = cl.post(f"{URL}/{it['id']}/approve", json={}, headers={**roles["admin"], "if-match": it["etag"]})
    assert r.status_code == 409 and r.json()["code"] == "target_changed"
    assert r.json()["base"] == [] and r.json()["current"][0]["tgt_text"] == "Buddhadharma"
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 1                   # nothing written
    # rebase after reviewing, then approve normally
    r = cl.patch(f"{URL}/{it['id']}", json={"rebase": True}, headers={**roles["editor"], "if-match": it["etag"]})
    assert r.status_code == 200 and r.json()["base"][0]["tgt_text"] == "Buddhadharma"
    r = approve(cl, it["id"], roles["admin"])
    assert r.status_code == 200 and r.json()["override_used"] is False
    # override path is audited
    it2 = new_item(cl, roles["editor"], kind="tm", src_text="今天講因緣具足", tgt_text="Today all conditions are met.")
    c = db.connect(env["path"])
    ztm.add_unit(c, "今天講因緣具足", "Conditions are complete today.", origin="import", quality=4)
    c.close()
    r = approve(cl, it2["id"], roles["admin"], override_conflict=True, note="checked")
    assert r.status_code == 200 and r.json()["override_used"] is True
    assert q(env, "SELECT note FROM staging_audit WHERE item_id=? AND action='approve'", (it2["id"],))[0][0] == \
        "override_conflict: checked"
    mem = ztm.TranslationMemory(lambda: db.connect(env["path"], readonly=True))
    assert mem.exact("今天講因緣具足").tgt == "Today all conditions are met."   # approved line served first
    assert approve(cl, new_item(cl, roles["editor"], kind="tm", src_text="佛", tgt_text="Buddha")["id"],
                   roles["admin"], override_conflict="yes").status_code == 422


def test_term_conflict_and_locked(env, roles):
    cl = env["client"]
    it = new_item(cl, roles["editor"])
    r = cl.post(f"{API}/glossary/terms", json={"zh": "因緣", "target": "conditions", "tgt_lang": "en"},
                headers=roles["editor"])
    assert r.status_code == 201                                  # direct admin glossary edit after staging
    r = cl.post(f"{URL}/{it['id']}/approve", json={}, headers={**roles["admin"], "if-match": it["etag"]})
    assert r.status_code == 409 and r.json()["code"] == "target_changed" and r.json()["base"] is None
    tid = q(env, "SELECT id FROM glossary_terms WHERE zh='因緣'")[0][0]
    e = cl.get(f"{API}/glossary/terms/{tid}", headers=BEARER).headers["etag"]
    assert cl.patch(f"{API}/glossary/terms/{tid}", json={"locked": True}, headers={**BEARER, "if-match": e}).status_code == 200
    it2 = new_item(cl, roles["editor"], tgt_text="causality")
    r = approve(cl, it2["id"], roles["admin"], override_conflict=True)
    assert r.status_code == 409 and r.json()["code"] == "term_locked"              # override never unlocks
    assert q(env, "SELECT en FROM glossary_terms WHERE id=?", (tid,))[0][0] == "conditions"


# ------------------------------------------------------------------ concurrency
def test_two_approvals_same_item_exactly_one_wins(env, roles):
    cl = env["client"]
    it = new_item(cl, roles["editor"], kind="tm", src_text="佛法", tgt_text="The Dharma")
    barrier = threading.Barrier(2)
    results = []

    def go():
        c = db.connect(env["path"])
        try:
            barrier.wait()
            c.execute("BEGIN IMMEDIATE")
            try:
                staging.approve(c, it["id"], actor={"user_id": None, "via": "bearer", "role": "owner"},
                                if_match=it["etag"])
                c.execute("COMMIT")
                results.append(200)
            except staging.StagingError as exc:
                c.execute("ROLLBACK")
                results.append(exc.status)
        finally:
            c.close()

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(results) == [200, 412]
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 1
    assert q(env, "SELECT count(*) FROM staging_audit WHERE action='approve'")[0][0] == 1


def test_two_http_approvals_race(env, roles):
    it = new_item(env["client"], roles["editor"], kind="tm", src_text="佛法", tgt_text="The Dharma")
    clients = [TestClient(env["app"], base_url=BASE, client=("127.0.0.1", 50010 + i)) for i in range(4)]
    barrier = threading.Barrier(4)
    codes = []

    def go(c):
        barrier.wait()
        codes.append(c.post(f"{URL}/{it['id']}/approve", json={},
                            headers={**roles["admin"], "if-match": it["etag"]}).status_code)

    ts = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert sorted(codes) == [200, 412, 412, 412]
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 1


# ------------------------------------------------------------------ idempotency
def test_idempotency_key_on_create_and_bulk(env, roles):
    cl = env["client"]
    body = {"kind": "tm", "src_text": "佛法", "tgt_text": "The Dharma"}
    h = {**roles["editor"], "idempotency-key": "stage-key-0001"}
    r1 = cl.post(URL, json=body, headers=h)
    r2 = cl.post(URL, json=body, headers=h)
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert r2.headers["idempotent-replayed"] == "true" and r2.headers["location"] == r1.headers["location"]
    r3 = cl.post(URL, json={**body, "tgt_text": "Dharma"}, headers=h)
    assert r3.status_code == 422 and r3.json()["code"] == "idempotency_mismatch"
    assert cl.post(URL, json=body, headers={**roles["editor"], "idempotency-key": "bad key"}).status_code == 400
    assert q(env, "SELECT count(*) FROM staging_items")[0][0] == 1
    bh = {**roles["admin"], "idempotency-key": "bulk-key-0001"}
    bb = {"items": [{"id": r1.json()["id"], "etag": r1.json()["etag"]}]}
    b1 = cl.post(f"{URL}/bulk-approve", json=bb, headers=bh)
    b2 = cl.post(f"{URL}/bulk-approve", json=bb, headers=bh)
    assert b1.status_code == b2.status_code == 200 and b1.json() == b2.json() and b1.json()["approved"] == 1
    assert q(env, "SELECT count(*) FROM staging_audit WHERE action='approve'")[0][0] == 1


# ------------------------------------------------------------------ bulk
def test_bulk_approve_per_item_results(env, roles):
    cl = env["client"]
    ok = new_item(cl, roles["editor"], kind="tm", src_text="佛法", tgt_text="The Dharma")
    stale = new_item(cl, roles["editor"], kind="tm", src_text="佛", tgt_text="Buddha")
    done = new_item(cl, roles["editor"], kind="tm", src_text="法", tgt_text="Dharma")
    assert approve(cl, done["id"], roles["admin"]).status_code == 200
    locked_term = new_item(cl, roles["editor"], src_text="空性", tgt_text="voidness")
    c = db.connect(env["path"])
    c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, locked) VALUES ((SELECT id FROM glossaries WHERE name='global'), "
              "'空性', 'emptiness', 1)")
    c.close()
    body = {"items": [{"id": ok["id"], "etag": ok["etag"]}, {"id": stale["id"], "etag": '"s1-r9"'},
                      {"id": done["id"], "etag": etag_of(cl, done["id"])}, {"id": 999, "etag": '"s999-r1"'},
                      {"id": locked_term["id"], "etag": locked_term["etag"]}], "override_conflict": True}
    r = cl.post(f"{URL}/bulk-approve", json=body, headers=roles["admin"])
    assert r.status_code == 200, r.text
    res = {x["id"]: x for x in r.json()["results"]}
    assert res[ok["id"]]["ok"] is True and res[ok["id"]]["target_id"]
    assert (res[stale["id"]]["status"], res[done["id"]]["status"], res[999]["status"]) == (412, 409, 404)
    assert res[locked_term["id"]]["code"] == "term_locked"
    assert r.json()["approved"] == 1 and r.json()["failed"] == 4
    assert q(env, "SELECT state FROM staging_items WHERE id=?", (stale["id"],))[0][0] == "pending"
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 2
    assert q(env, "SELECT en FROM glossary_terms WHERE zh='空性'")[0][0] == "emptiness"
    for bad in ({"items": []}, {"items": [{"id": 1, "etag": "x"}] * 2}, {"items": [{"id": "1", "etag": "x"}]},
                {"items": [{"id": i, "etag": "x"} for i in range(101)]}):
        assert cl.post(f"{URL}/bulk-approve", json=bad, headers=roles["admin"]).status_code == 422
    assert cl.post(f"{URL}/bulk-approve", json=body, headers=roles["editor"]).status_code == 403


# ------------------------------------------------------------------ pagination
def test_keyset_pagination_ties_and_tamper(env, roles):
    cl = env["client"]
    ids = [new_item(cl, roles["editor"], src_text=f"詞{i}", tgt_text=f"term {i}")["id"] for i in range(7)]
    c = db.connect(env["path"])
    c.execute("UPDATE staging_items SET created_at=1000.0")                 # all timestamps equal
    c.close()
    seen, cur = [], None
    while True:
        r = cl.get(URL, params={"limit": 3, **({"cursor": cur} if cur else {})}, headers=roles["editor"])
        assert r.status_code == 200, r.text
        seen += [i["id"] for i in r.json()["items"]]
        cur = r.json()["next"]
        if cur is None:
            break
        if len(seen) == 3:                                                  # insert between pages
            new_item(cl, roles["editor"], src_text="新詞", tgt_text="new term")
    assert seen == sorted(ids, reverse=True)                                # no skip, no dup, id tie-breaker
    bad = [cur or "", "abc", enc_cursor([1000.0, 5])[:-2] + "00", enc_cursor(["x", "y"]), enc_cursor([1000.0])]
    for b in bad[1:]:
        r = cl.get(URL, params={"cursor": b}, headers=roles["editor"])
        assert r.status_code == 400 and r.headers["content-type"].startswith(PROBLEM), b
    for lim in (0, 201):
        assert cl.get(URL, params={"limit": lim}, headers=roles["editor"]).status_code == 422
    assert cl.get(URL, params={"state": "nope"}, headers=roles["editor"]).status_code == 422
    assert cl.get(URL, params={"room_id": "x?y"}, headers=roles["editor"]).status_code == 422
    assert len(cl.get(URL, params={"kind": "tm"}, headers=roles["editor"]).json()["items"]) == 0


# ------------------------------------------------------------------ privacy: purge follows the segment
def test_deleting_segment_cascades_staging(env, roles):
    cl = env["client"]
    r = cl.post(f"{API}/corrections", json={"segment_id": "s1-1", "target_type": "translation",
                                            "text": "Today, causes and conditions ripen."}, headers=roles["editor"])
    sid = r.json()["tm_staging_id"]
    c = db.connect(env["path"])
    c.execute("DELETE FROM segments WHERE id='s1-1'")
    c.close()
    assert q(env, "SELECT count(*) FROM staging_items WHERE id=?", (sid,))[0][0] == 0
    assert q(env, "SELECT count(*) FROM staging_audit WHERE item_id=?", (sid,))[0][0] == 0


def test_unique_pending_index_enforced_at_db_level(env):
    c = db.connect(env["path"])
    try:
        sig = "0" * 64
        c.execute("INSERT INTO staging_items(kind, src_text, tgt_text, target_key, base_sig) VALUES ('tm','a','b','k',?)", (sig,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO staging_items(kind, src_text, tgt_text, target_key, base_sig) VALUES ('tm','a','c','k',?)",
                      (sig,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO staging_items(kind, state, src_text, tgt_text, target_key, base_sig) "
                      "VALUES ('tm','done','a','c','k2',?)", (sig,))
    finally:
        c.close()
    assert json.loads(json.dumps(staging.TRANSITIONS["pending"] and sorted(staging.TRANSITIONS["pending"])))


# ------------------------------------------------------------------ review page (/admin/staging)
def test_staging_page_is_served_and_linked(env):
    cl = env["client"]
    r = cl.get("/admin/staging")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "待審修正" in r.text and '<script type="module" src="/admin/staging/staging.js">' in r.text
    assert "<script>" not in r.text                                       # no inline script (CSP)
    assert "script-src 'self'" in r.headers["content-security-policy"]
    js = cl.get("/admin/staging/staging.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    for needle in ('"X-Zen-CSRF"', '"If-Match"', '"Idempotency-Key"', "/staging/bulk-approve", "cursor", "reading",
                   'method: "PATCH"'):
        assert needle in js.text, needle
    assert cl.get("/admin/staging/staging.css").headers["content-type"].startswith("text/css")
    bad = cl.get("/admin/staging/../server.py")
    assert bad.status_code == 404
    r = cl.get("/admin/staging/app.py")
    assert r.status_code == 404 and r.headers["content-type"].startswith(PROBLEM)
    assert 'href="/admin/staging"' in cl.get("/admin").text
    # the page is static; its data still needs a session (no data leaks to an anonymous fetch)
    assert cl.get(URL).status_code == 401


def test_staging_js_syntax():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不在 PATH")
    js = Path(staging.__file__).with_name("static") / "staging.js"
    out = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


# ------------------------------------------------------------------ admin self-approve switch (default OFF)
def test_self_approve_switch_parsing_and_default_off(env, roles):  # noqa: F811
    assert staging.self_approve_from_env({}) is False
    assert staging.self_approve_from_env({"ZEN_ADMIN_SELF_APPROVE": "true"}) is False
    assert staging.self_approve_from_env({"ZEN_ADMIN_SELF_APPROVE": " 1 "}) is True
    assert env["app"].state.staging_self_approve is False
    r = env["client"].post(f"{API}/corrections", json={"segment_id": "s1-1", "target_type": "translation",
                                                      "text": "Today, causes and conditions ripen."}, headers=roles["admin"])
    assert r.json()["tm_pending"] is True and "auto_approved" not in r.json()
    assert q(env, "SELECT count(*) FROM tm_units")[0][0] == 0


@pytest.fixture
def auto_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEN_ADMIN_SELF_APPROVE", "1")          # the real switch, read by create_admin_app
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
        cl = client
        yield {"app": app, "client": cl, "path": path, "live": live,
               "admin": mk_user(cl, "ad", "admin")[1], "editor": mk_user(cl, "ed", "editor")[1]}


def test_self_approve_on_admin_fix_is_written_and_fully_audited(auto_env):
    cl, env_ = auto_env["client"], auto_env
    assert env_["app"].state.staging_self_approve is True
    body = {"segment_id": "s1-1", "target_type": "translation", "text": "Today, causes and conditions ripen.",
            "propose_term": {"zh": "因緣", "en": "causes and conditions"}}
    r = cl.post(f"{API}/corrections", json=body, headers=env_["admin"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["auto_approved"]["tm"]["ok"] is True and out["auto_approved"]["term"]["ok"] is True
    assert out["tm_pending"] is False
    assert q(env_, "SELECT quality, origin FROM tm_units")[0] == (5, "approved")
    assert q(env_, "SELECT status, en FROM glossary_terms WHERE zh='因緣'")[0] == ("active", "causes and conditions")
    rows = q(env_, "SELECT state, approval_mode, author_id = decided_by, decision_note FROM staging_items ORDER BY id")
    assert [(r_[0], r_[1], r_[2]) for r_ in rows] == [("approved", "self_auto", 1)] * 2
    assert all("ZEN_ADMIN_SELF_APPROVE" in r_[3] for r_ in rows)
    audit = q(env_, "SELECT action, mode, actor_id IS NOT NULL, before_json IS NOT NULL, after_json IS NOT NULL "
                    "FROM staging_audit ORDER BY id")
    # TM: before = [] (no units yet). Term: no glossary row existed, so before_json is NULL; after is the new row.
    assert audit == [("create", None, 1, 1, 1), ("approve", "self_auto", 1, 1, 1),
                     ("create", None, 1, 0, 1), ("approve", "self_auto", 1, 0, 1)]
    ev = [json.loads(p[0]) for p in q(env_, "SELECT payload FROM events WHERE kind='audit.staging.approve'")]
    assert [e["mode"] for e in ev] == ["self_auto", "self_auto"]
    d = cl.get(f"{URL}/{out['tm_staging_id']}", headers=env_["admin"]).json()
    assert [a["mode"] for a in d["audit"]] == [None, "self_auto"]
    assert env_["live"].puts == []                                  # still no live push


def test_self_approve_never_for_editors_and_respects_lock_validation(auto_env):
    cl, env_ = auto_env["client"], auto_env
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "The Dharma"},
                 headers={**env_["editor"], "if-match": '"v1"'})
    assert r.status_code == 200 and r.json()["tm_pending"] is True and "auto_approved" not in r.json()
    assert q(env_, "SELECT count(*) FROM tm_units")[0][0] == 0
    # admin edit through the workbench is auto-approved too
    r = cl.patch(f"{API}/segments/s1-2/text", json={"target": "en", "text": "The Buddhadharma"},
                 headers={**env_["admin"], "if-match": '"v2"'})
    assert r.status_code == 200 and r.json()["tm_pending"] is False
    # locked term: the auto-approval fails inside its SAVEPOINT, item stays pending, nothing half-written
    c = db.connect(env_["path"])
    from app import feedback
    gid = feedback._global_glossary(c)
    c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, locked) VALUES (?, '因緣', 'conditions', 1)", (gid,))
    ver = c.execute("SELECT version FROM glossaries WHERE id=?", (gid,)).fetchone()[0]
    c.close()
    r = cl.post(f"{API}/corrections", json={"segment_id": "s1-1", "target_type": "translation",
                                            "text": "Today, causes and conditions ripen.",
                                            "propose_term": {"zh": "因緣", "en": "causality"}}, headers=env_["admin"])
    assert r.status_code == 201, r.text
    auto = r.json()["auto_approved"]
    assert auto["tm"]["ok"] is True and auto["term"] == {"ok": False, "status": 409, "code": "term_locked",
                                                          "detail": auto["term"]["detail"]}
    assert q(env_, "SELECT en FROM glossary_terms WHERE zh='因緣'")[0][0] == "conditions"
    assert q(env_, "SELECT version FROM glossaries WHERE id=?", (gid,))[0][0] == ver
    tid = r.json()["term_staging_id"]
    assert q(env_, "SELECT state, approval_mode FROM staging_items WHERE id=?", (tid,))[0] == ("pending", None)
    assert q(env_, "SELECT count(*) FROM staging_audit WHERE item_id=? AND action='approve'", (tid,))[0][0] == 0
    # invalid input is still refused before anything is staged
    bad = cl.post(f"{API}/corrections", json={"segment_id": "s1-1", "text": "Fix.",
                                              "propose_term": {"zh": "因缘", "en": "x"}}, headers=env_["admin"])
    assert bad.status_code == 422 and bad.json()["code"] == "not_traditional"


def test_self_auto_mode_refuses_someone_elses_item(env, roles):  # noqa: F811
    it = new_item(env["client"], roles["editor"], kind="tm", src_text="佛法", tgt_text="The Dharma")
    c = db.connect(env["path"])
    try:
        c.execute("BEGIN IMMEDIATE")
        with pytest.raises(staging.StagingError) as exc:
            staging.approve(c, it["id"], actor={"user_id": 99, "via": "bearer", "role": "admin"},
                            if_match=it["etag"], mode="self_auto")
        assert exc.value.status == 403
        c.execute("ROLLBACK")
    finally:
        c.close()


# ------------------------------------------------------------------ ja reading (round4 ruby)
def test_reading_validation_rules(env, roles):  # noqa: F811
    cl, ed = env["client"], roles["editor"]
    base = {"kind": "term", "tgt_lang": "ja", "src_text": "公案", "tgt_text": "公案"}
    for bad in ("", "kouan", "公案", "こう\u0000あん", "あ" * 81, 123):
        r = cl.post(URL, json={**base, "reading": bad}, headers=ed)
        assert r.status_code == 422 and r.json()["code"] == "invalid_reading", (bad, r.text)
    for wrong in ({"kind": "term", "tgt_lang": "en", "src_text": "公案", "tgt_text": "koan"},
                  {"kind": "tm", "tgt_lang": "ja", "src_text": "佛法", "tgt_text": "仏法"}):
        r = cl.post(URL, json={**wrong, "reading": "こうあん"}, headers=ed)   # refused, never silently dropped
        assert r.status_code == 422 and r.json()["code"] == "invalid_reading"
    r = cl.post(URL, json={**base, "reading": " ｺｳｱﾝ "}, headers=ed)          # half-width -> NFKC full-width
    assert r.status_code == 201 and r.json()["reading"] == "コウアン"
    assert staging.clean_reading("ぶっ・ぽう ー", "term", "ja") == "ぶっ・ぽう ー"
    c = db.connect(env["path"])
    try:
        with pytest.raises(sqlite3.IntegrityError):                     # DB CHECK backs the rule up
            c.execute("UPDATE staging_items SET tgt_lang='en' WHERE reading IS NOT NULL")
    finally:
        c.close()


def test_reading_carried_to_glossary_on_approve_and_editable(env, roles):  # noqa: F811
    cl = env["client"]
    it = new_item(cl, roles["editor"], tgt_lang="ja", src_text="公案", tgt_text="公案", reading="こうあん")
    assert it["reading"] == "こうあん"
    e = cl.patch(f"{URL}/{it['id']}", json={"reading": "コウアン"}, headers={**roles["editor"], "if-match": it["etag"]})
    assert e.status_code == 200 and e.json()["reading"] == "コウアン"
    assert cl.patch(f"{URL}/{it['id']}", json={"reading": "x"}, headers={**roles["editor"], "if-match": e.headers["etag"]}
                    ).status_code == 422
    d = cl.get(f"{URL}/{it['id']}", headers=roles["editor"]).json()
    assert d["diff"]["after"]["reading"] == "コウアン" and d["audit"][-1]["after"]["reading"] == "コウアン"
    r = approve(cl, it["id"], roles["admin"])
    assert r.status_code == 200, r.text
    tid = r.json()["target_id"]
    assert q(env, "SELECT reading FROM admin_term_meta WHERE term_id=?", (tid,))[0][0] == "コウアン"
    assert q(env, "SELECT tgt_lang, text, reading FROM glossary_term_targets WHERE term_id=?", (tid,))[0] == \
        ("ja", "公案", "コウアン")                                         # 0003 trigger sync
    assert r.json()["target"]["reading"] == "コウアン"
    # a later item without reading keeps the stored one; a different reading on a locked term is refused
    it2 = new_item(cl, roles["editor"], tgt_lang="ja", src_text="公案", tgt_text="こうあん")
    assert approve(cl, it2["id"], roles["admin"]).status_code == 200
    assert q(env, "SELECT reading FROM admin_term_meta WHERE term_id=?", (tid,))[0][0] == "コウアン"
    c = db.connect(env["path"])
    c.execute("UPDATE glossary_terms SET locked=1 WHERE id=?", (tid,))
    c.close()
    it3 = new_item(cl, roles["editor"], tgt_lang="ja", src_text="公案", tgt_text="こうあん", reading="こうあん")
    r = approve(cl, it3["id"], roles["admin"], override_conflict=True)
    assert r.status_code == 409 and r.json()["code"] == "term_locked"
    assert q(env, "SELECT reading FROM admin_term_meta WHERE term_id=?", (tid,))[0][0] == "コウアン"


def test_reading_from_correction_proposal(env, roles):  # noqa: F811
    r = env["client"].post(f"{API}/corrections", headers=roles["editor"], json={
        "segment_id": "s1-1", "target_type": "translation", "text": "Today, causes and conditions ripen.",
        "propose_term": {"zh": "因緣", "tgt_lang": "ja", "target": "因縁", "reading": "いんねん"}})
    assert r.status_code == 201, r.text
    sid = r.json()["term_staging_id"]
    assert q(env, "SELECT tgt_lang, tgt_text, reading FROM staging_items WHERE id=?", (sid,))[0] == ("ja", "因縁", "いんねん")
