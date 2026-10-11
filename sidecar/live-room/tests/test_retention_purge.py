"""DBA D4 / 備份 D2: whole-session delete across both DB files and retention enforcement."""
import struct

import pytest

from app.admin import db
from app.admin.retention import PurgeRefused, enforce_retention, purge_session, sweep_identity_orphans
from app.ledger import Ledger
from tests.local_fakes import seed_segment
from tests.test_admin_api import API, BEARER, env  # noqa: F401
from tests.test_corrections_stick import cap

DAY = 86400.0


def setup(tmp_path, now=10 * 365 * DAY):
    main, ident = tmp_path / "zen.sqlite3", tmp_path / "zen-identity.sqlite3"
    db.migrate(main)
    db.migrate_identity(ident)
    c = db.connect(main)
    seed_segment(c, seg="a-1", session="sa", seq=1, zh="甲場私密內容", en="secret A", started_at=now - 400 * DAY)
    seed_segment(c, seg="b-1", session="sb", seq=1, zh="乙場內容", en="content B", started_at=now - 10 * DAY)
    c.execute("UPDATE sessions SET status='ended', ended_at=started_at+3600")
    tid = c.execute("SELECT id FROM transcripts WHERE segment_id='a-1'").fetchone()[0]
    vec = struct.pack("<2f", 0.1, 0.2)
    c.execute("INSERT INTO embeddings(owner_type, owner_id, model, dim, vector, text_sha1) VALUES ('transcript',?,?,?,?,?)",
              (str(tid), "m", 2, vec, "x"))
    c.execute("INSERT INTO events(room_id, session_id, segment_id, kind) VALUES ('class','sa','a-1','live.caption')")
    c.execute("INSERT INTO events(room_id, session_id, segment_id, kind) VALUES ('class','sb','b-1','live.caption')")
    c.execute("INSERT INTO metrics(ts, name, value, session_id) VALUES (?,?,?,?)", (now, "rtf", 1.0, "sa"))
    c.execute("INSERT INTO speakers(id, session_id, label) VALUES (1,'sa','host'),(2,'sb','host')")
    c.commit()
    c.close()
    ic = db.connect(ident)
    ic.execute("INSERT INTO speaker_identities(speaker_id, session_id, display_name) VALUES (1,'sa','陳老師'),(2,'sb','林老師')")
    ic.commit()
    ic.close()
    return main, ident, now


def fts_hits(c, word):
    return c.execute("SELECT COUNT(*) FROM transcripts_fts WHERE transcripts_fts MATCH ?", (f'"{word}"',)).fetchone()[0]


def test_purge_session_removes_everything_in_both_files(tmp_path):
    main, ident, _ = setup(tmp_path)
    out = purge_session(main, ident, "sa")
    assert out["deleted"]["segments"] == 1 and out["deleted"]["identity"] == 1
    c = db.connect(main)
    assert c.execute("SELECT COUNT(*) FROM segments WHERE session_id='sa'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM transcripts WHERE segment_id='a-1'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert fts_hits(c, "甲場私密") == 0 and fts_hits(c, "乙場內容") == 1
    assert [r[0] for r in c.execute("SELECT kind FROM events WHERE session_id='sa'")] == ["session_purged"]
    assert c.execute("SELECT COUNT(*) FROM metrics WHERE session_id='sa'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM segments WHERE session_id='sb'").fetchone()[0] == 1
    c.close()
    ic = db.connect(ident)
    assert [r[0] for r in ic.execute("SELECT session_id FROM speaker_identities")] == ["sb"]
    ic.close()


def test_late_ledger_events_do_not_resurrect_purged_session(tmp_path):
    main, ident, _ = setup(tmp_path)
    purge_session(main, ident, "sa")
    led = Ledger(main)
    ev = cap(seq=9, zh="遲到的句子", status="ready")
    ev.update(session_id="sa", id="class:sa:9")
    led.submit(ev)
    assert led.wait_idle(5)
    led.close()
    c = db.connect(main)
    assert c.execute("SELECT COUNT(*) FROM sessions WHERE id='sa'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM segments WHERE session_id='sa'").fetchone()[0] == 0
    c.close()


def test_purge_refuses_legal_hold_and_live(tmp_path):
    main, ident, _ = setup(tmp_path)
    c = db.connect(main)
    c.execute("UPDATE sessions SET legal_hold=1 WHERE id='sa'")
    c.execute("UPDATE sessions SET status='live', ended_at=NULL WHERE id='sb'")
    c.commit()
    c.close()
    with pytest.raises(PurgeRefused) as e:
        purge_session(main, ident, "sa")
    assert e.value.code == "legal_hold"
    with pytest.raises(PurgeRefused) as e:
        purge_session(main, ident, "sb")
    assert e.value.code == "session_live"
    assert purge_session(main, ident, "sb", force=True)["deleted"]["segments"] == 1


def test_retention_pass(tmp_path):
    main, ident, now = setup(tmp_path)
    c = db.connect(main)
    seed_segment(c, seg="h-1", session="sh", seq=1, zh="保全", started_at=now - 900 * DAY)
    seed_segment(c, seg="p-1", session="sp", seq=1, zh="提早刪", started_at=now - 1 * DAY)
    c.execute("UPDATE sessions SET status='ended', ended_at=started_at+60 WHERE id IN ('sh','sp')")
    c.execute("UPDATE sessions SET legal_hold=1 WHERE id='sh'")
    c.execute("UPDATE sessions SET purge_after=? WHERE id='sp'", (now - 1,))
    c.execute("INSERT INTO events(ts, kind, level) VALUES (?, 'old.info', 'info')", (now - 40 * DAY,))
    c.execute("INSERT INTO metrics(ts, name, value) VALUES (?, 'rtf', 2.0)", (now - 40 * DAY,))
    c.commit()
    c.close()
    out = enforce_retention(main, ident, now=now)
    assert sorted(out["sessions"]) == ["sa", "sp"]
    assert out["events_debug_info"] >= 1 and out["metrics_raw"] == 1
    c = db.connect(main)
    left = {r[0] for r in c.execute("SELECT id FROM sessions")}
    assert left == {"sb", "sh"}
    assert c.execute("SELECT n FROM metrics_rollup WHERE name='rtf'").fetchone()[0] == 1
    # text_raw cleared for sessions ended > 30 days ago (sh is on hold, sb ended 10 days ago)
    assert c.execute("SELECT text_raw FROM transcripts WHERE segment_id='b-1'").fetchone()[0] is not None
    c.close()


def test_identity_orphan_sweep(tmp_path):
    main, ident, _ = setup(tmp_path)
    c = db.connect(main)
    c.execute("DELETE FROM sessions WHERE id='sa'")          # simulate a crash before the identity step
    c.commit()
    c.close()
    assert sweep_identity_orphans(main, ident) == 1


def test_delete_session_api(env):  # noqa: F811
    url = f"{API}/sessions/s1"
    c = env["client"]
    r = c.request("DELETE", url, json={}, headers=BEARER)
    assert r.status_code == 428
    r = c.request("DELETE", url, json={"confirm": "s1"}, headers=BEARER)
    assert r.status_code == 409 and r.json()["code"] == "session_live"
    r = c.request("DELETE", url, json={"confirm": "s1", "force": True}, headers=BEARER)
    assert r.status_code == 200 and r.json()["deleted"]["segments"] == 2
    assert c.request("DELETE", url, json={"confirm": "s1"}, headers=BEARER).status_code == 404


def test_server_never_purges_sessions_without_fresh_backup(env):  # noqa: F811
    from app.admin import db as zdb
    c = zdb.connect(env["path"])
    c.execute("UPDATE sessions SET status='ended', started_at=1.0, ended_at=2.0")
    c.commit()
    c.close()
    out = env["app"].state.run_retention()
    assert out["sessions_deferred"] is True and out["sessions"] == []
    zdb.backup_set(env["path"], env["tmp"] / "backups", env["tmp"] / "zen-identity.sqlite3")
    out = env["app"].state.run_retention()
    assert out["sessions"] == ["s1"]
