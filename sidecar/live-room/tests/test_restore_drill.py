"""tools/restore_drill.py：備份 → 驗證 → 還原到暫存 → 比對筆數。全部在 tmp_path，不碰正式資料夾。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.admin import db as zdb

_SPEC = importlib.util.spec_from_file_location(
    "restore_drill", Path(__file__).resolve().parents[1] / "tools" / "restore_drill.py")
drill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drill)


def _seed(tmp_path: Path, rooms: int = 5) -> tuple[Path, Path]:
    main = tmp_path / "data" / "zen.sqlite3"
    ident = tmp_path / "data" / "zen-identity.sqlite3"
    zdb.migrate(main)
    zdb.migrate_identity(ident)
    with closing(sqlite3.connect(main)) as c:
        c.executemany("INSERT INTO rooms(id, title) VALUES (?, ?)", [(f"r{i}", f"課{i}") for i in range(rooms)])
        c.commit()
    with closing(sqlite3.connect(ident)) as c:
        c.execute("INSERT INTO user_accounts(user_id, username) VALUES (1, 'host')")
        c.commit()
    return main, ident


def _run(tmp_path, main, ident, **kw):
    return drill.run_drill(db_path=main, identity_path=ident, backup_dir=tmp_path / "bk",
                           work_dir=tmp_path / "drill", **kw)


def test_fresh_drill_passes_and_counts_match(tmp_path):
    main, ident = _seed(tmp_path)
    r = _run(tmp_path, main, ident)
    assert r["result"] == "PASS", r["problems"]
    cmp = r["steps"]["compare"]
    assert cmp["main"]["mismatch"] == {} and cmp["identity"]["mismatch"] == {}
    assert drill.table_counts(tmp_path / "drill" / "restore" / "zen.sqlite3")["rooms"] == 5
    assert drill.table_counts(tmp_path / "drill" / "restore" / "zen-identity.sqlite3")["user_accounts"] == 1
    assert json.loads(Path(r["report_file"]).read_text(encoding="utf-8"))["result"] == "PASS"


def test_production_files_untouched(tmp_path):
    main, ident = _seed(tmp_path)
    before = (drill.table_counts(main), drill.table_counts(ident))
    _run(tmp_path, main, ident)
    assert (drill.table_counts(main), drill.table_counts(ident)) == before
    assert not list(main.parent.glob("*.pre-restore-*"))     # 正式庫沒被移開


def test_live_writes_after_backup_reported_not_failed(tmp_path, monkeypatch):
    main, ident = _seed(tmp_path)
    real = zdb.backup_set

    def backup_then_write(*a, **k):
        out = real(*a, **k)
        with closing(sqlite3.connect(main)) as c:
            c.execute("INSERT INTO rooms(id) VALUES ('late')")
            c.commit()
        return out
    monkeypatch.setattr(zdb, "backup_set", backup_then_write)
    r = _run(tmp_path, main, ident)
    assert r["result"] == "PASS"
    assert r["steps"]["compare"]["main"]["live_delta_since_backup"]["rooms"] == 1


def test_latest_existing_backup_with_manifest_identity(tmp_path):
    main, ident = _seed(tmp_path)
    zdb.backup_set(main, tmp_path / "bk", ident, stamp="20261010-230000")
    r = _run(tmp_path, main, ident, latest=True)
    assert r["result"] == "PASS"
    assert r["steps"]["backup"]["fresh"] is False
    assert r["steps"]["backup"]["identity"].startswith("zen-identity-")


def test_tampered_backup_fails_sha256(tmp_path):
    main, ident = _seed(tmp_path)
    zdb.backup_set(main, tmp_path / "bk", ident, stamp="20261010-230000")
    bk = zdb.list_backups(tmp_path / "bk")[-1]
    with closing(sqlite3.connect(bk)) as c:            # 仍是合法 SQLite，但內容被改
        c.execute("INSERT INTO rooms(id) VALUES ('tamper')")
        c.commit()
    r = _run(tmp_path, main, ident, from_backup=bk)
    assert r["result"] == "FAIL"
    assert any("SHA256" in p for p in r["problems"])
    assert "restore" not in r["steps"]                  # 驗證不過就不還原


def test_corrupt_backup_fails_verify(tmp_path):
    main, ident = _seed(tmp_path)
    bad = tmp_path / "bk" / "zen-20261010-230000.sqlite3"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database" * 100)
    r = _run(tmp_path, main, None, from_backup=bad)
    assert r["result"] == "FAIL" and any("驗證失敗" in p or "還原失敗" in p for p in r["problems"])


def test_manifest_lists_missing_identity(tmp_path):
    main, ident = _seed(tmp_path)
    zdb.backup_set(main, tmp_path / "bk", ident, stamp="20261010-230000")
    idf = next((tmp_path / "bk").glob("zen-identity-*.sqlite3"))
    idf.rename(idf.with_name(idf.name + ".moved"))     # 模擬遺失（改名，不刪）
    r = _run(tmp_path, main, ident, latest=True)
    assert r["result"] == "FAIL" and any("個資庫備份" in p for p in r["problems"])


def test_refuses_production_as_restore_target(tmp_path):
    main, ident = _seed(tmp_path)
    with pytest.raises(drill.DrillError):
        drill.run_drill(db_path=tmp_path / "drill" / "restore" / "zen.sqlite3", identity_path=None,
                        backup_dir=tmp_path / "bk", work_dir=tmp_path / "drill",
                        from_backup=zdb.backup_to(main, tmp_path / "bk"))


def test_cli_exit_codes(tmp_path, capsys):
    main, ident = _seed(tmp_path)
    assert drill.main(["--db", str(main), "--identity", str(ident), "--work-dir", str(tmp_path / "w1")]) == 0
    assert "PASS" in capsys.readouterr().out
    assert drill.main(["--latest", "--backup-dir", str(tmp_path / "empty"),
                       "--work-dir", str(tmp_path / "w2")]) == 2
    assert drill.main(["--db", str(tmp_path / "nope.sqlite3"), "--work-dir", str(tmp_path / "w3")]) == 2


def test_report_has_no_row_content(tmp_path):
    main, ident = _seed(tmp_path)
    r = _run(tmp_path, main, ident)
    text = Path(r["report_file"]).read_text(encoding="utf-8")
    assert "課0" not in text and "host" not in text


def test_missing_identity_backup_arg_is_usage_error(tmp_path):
    main, ident = _seed(tmp_path)
    bk = zdb.backup_to(main, tmp_path / "bk")
    with pytest.raises(drill.DrillError):
        _run(tmp_path, main, ident, from_backup=bk, identity_backup=tmp_path / "nope.sqlite3")
    assert not (tmp_path / "nope.sqlite3").exists()      # 不會因 sqlite3.connect 而憑空建檔
