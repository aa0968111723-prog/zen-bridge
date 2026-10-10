"""DBA D8 (flagged query uses the partial index), D9 (search with spaces / Latin+CJK),
D10 (to_uni fallback writes NULL), D13 (mapped network drive refused)."""
import pytest

from app.admin import db, search
from tests.local_fakes import seed_segment


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    c = db.connect(path)
    seed_segment(c, seg="a-1", seq=1, zh="因緣生法", en="Dependent origination")
    seed_segment(c, seg="a-2", seq=2, zh="AI翻譯很快", en="AI translation is fast")
    c.commit()
    yield c
    c.close()


def test_cjk_query_with_spaces(conn):
    hits, _ = search.search_transcripts(conn, "因 緣 生")
    assert [h["segment_id"] for h in hits] == ["a-1"]


def test_latin_plus_cjk_short_query(conn):
    hits, _ = search.search_transcripts(conn, "A翻")
    assert [h["segment_id"] for h in hits] == ["a-2"]


def test_flagged_query_uses_partial_index(conn):
    plan = " ".join(r[-1] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM translations WHERE is_current=1 AND term_flags <> '[]'"))
    assert "translations_flagged" in plan


def test_server_flag_queries_match_index():
    import pathlib
    src = pathlib.Path(search.__file__).with_name("server.py").read_text(encoding="utf-8")
    assert "term_flags NOT IN ('', '[]')" not in src


def test_ledger_to_uni_fallback_is_null():
    import subprocess
    import sys
    code = ("import sys, types; sys.modules['app.admin.db'] = types.ModuleType('app.admin.db'); "
            "import app.ledger as l; print(repr(l.to_uni('因緣')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "None", out.stderr


@pytest.mark.parametrize("kind,refused", [(4, True), (3, False), (2, False)])
def test_mapped_network_drive_refused(kind, refused):
    assert db._is_remote_drive(r"Z:\data\zen.sqlite3", get_drive_type=lambda root: kind) is refused
    assert db._is_remote_drive("/home/x/zen.sqlite3", get_drive_type=lambda root: 4) is False


def test_schema_checksum_verified_on_existing_db(tmp_path, monkeypatch):
    """DBA D6."""
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    assert db.migrate(path) == db.SCHEMA_VERSION                            # unchanged schema: fine
    tampered = tmp_path / "schema.sql"
    tampered.write_text(db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nCREATE TABLE IF NOT EXISTS tamper_marker(x);\n",
                        encoding="utf-8")
    monkeypatch.setattr(db, "SCHEMA_PATH", tampered)
    with pytest.raises(db.SchemaError):
        db.migrate(path)
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='tamper_marker'").fetchone()[0] == 0
    c.close()


def test_schema_checksum_tolerates_crlf_checkout(tmp_path, monkeypatch):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    crlf = tmp_path / "schema.sql"
    crlf.write_bytes(db.SCHEMA_PATH.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setattr(db, "SCHEMA_PATH", crlf)
    assert db.migrate(path) == db.SCHEMA_VERSION


def test_session_lookup_touches_at_most_once_a_minute(tmp_path):
    """DBA D12."""
    from app.admin.security import SessionStore
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    t = {"now": 1000.0}
    st = SessionStore(lambda: db.connect(path), clock=lambda: t["now"])
    sid, _ = st.create()
    seen = lambda: db.connect(path).execute("SELECT last_seen_at FROM admin_sessions").fetchone()[0]  # noqa: E731
    t["now"] = 1030.0
    assert st.lookup(sid) and seen() == 1000.0
    t["now"] = 1061.0
    assert st.lookup(sid) and seen() == 1061.0
    blocker = db.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    t["now"] = 1200.0
    st2 = SessionStore(lambda: db.connect(path, timeout_ms=50), clock=lambda: t["now"])
    assert st2.lookup(sid)                       # busy write lock never fails the request
    blocker.execute("ROLLBACK")
    blocker.close()


def test_retention_clears_idempotency_and_expired_sessions(tmp_path):
    """DBA D11."""
    from app.admin.retention import enforce_retention
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    c = db.connect(path)
    c.execute("INSERT INTO idempotency_keys(key, route, body_sha256, status, response_json, created_at) "
              "VALUES ('k','r','x',200,'{\"zh\":\"秘密\"}', 1.0)")
    c.execute("INSERT INTO admin_sessions(sid_hash, csrf_token, expires_at) VALUES ('h','c', 5.0)")
    c.commit()
    c.close()
    out = enforce_retention(path, None, now=10 * 86400.0)
    assert out["idempotency_keys"] == 1 and out["admin_sessions"] == 1
