"""round3 C1 (desktop build runs backup + retention) and C7 (desktop rotating log)."""
import io
import logging
import sqlite3
import sys
import time

import pytest

from app import desktop_maint
from app.admin import db as zdb
from app.desktop_maint import DesktopMaintenance


def make_db(tmp_path):
    p = tmp_path / "data" / "zen.sqlite3"
    p.parent.mkdir(parents=True)
    zdb.migrate(p)
    return p


def test_first_pass_makes_verified_backup_then_skips(tmp_path):
    db = make_db(tmp_path)
    m = DesktopMaintenance(db, None, tmp_path / "backups")
    out = m.run_once()
    assert out["backup"] and "backup_error" not in out
    files = zdb.list_backups(tmp_path / "backups")
    assert files
    main = [f for f in files if f.name.startswith("zen-") and not f.name.startswith("zen-identity")][0]
    with sqlite3.connect(main) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert m.run_once()["backup"] is None          # fresh backup exists -> not doubled
    assert out["retention"] is not None            # retention pass ran too


def test_interval_zero_disables_backup_but_not_retention(tmp_path):
    db = make_db(tmp_path)
    m = DesktopMaintenance.from_env({"ZEN_DB_PATH": str(db), "ZEN_BACKUP_DIR": str(tmp_path / "b"),
                                     "ZEN_DATA_DIR": str(tmp_path), "ZEN_BACKUP_INTERVAL_H": "0", "ZEN_RETENTION": "1"})
    out = m.run_once()
    assert out["backup"] is None and out["retention"] is not None
    assert not zdb.list_backups(tmp_path / "b")


def test_retention_off_and_missing_db(tmp_path):
    m = DesktopMaintenance.from_env({"ZEN_DB_PATH": str(tmp_path / "none.sqlite3"),
                                     "ZEN_BACKUP_DIR": str(tmp_path / "b"), "ZEN_RETENTION": "0"})
    assert m.retention_every_s == 0


def test_retention_requires_explicit_opt_in(tmp_path):
    m = DesktopMaintenance.from_env({"ZEN_DB_PATH": str(tmp_path / "none.sqlite3"),
                                     "ZEN_BACKUP_DIR": str(tmp_path / "b")})
    assert m.retention_every_s == 0, 'Installing the App must not opt into deleting old user records'
    assert m.run_once()["skipped"] == "no database"


def test_failure_is_logged_not_raised(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    monkeypatch.setattr(zdb, "backup_set", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = DesktopMaintenance(db, None, tmp_path / "b").run_once()
    assert "disk full" in out["backup_error"]


def test_thread_runs_after_first_delay_and_stops(tmp_path):
    db = make_db(tmp_path)
    m = DesktopMaintenance(db, None, tmp_path / "b", first_s=0.05, scan_s=60).start()
    try:
        deadline = time.time() + 10
        while not zdb.list_backups(tmp_path / "b") and time.time() < deadline:
            time.sleep(0.05)
        assert zdb.list_backups(tmp_path / "b")
    finally:
        m.stop()
    assert not m._thread.is_alive()


def test_desktop_service_installs_log_and_maintenance(tmp_path, monkeypatch):
    from app import desktop_service
    started, logs = [], []

    class FakeServer:
        def __init__(self, cfg):
            self.should_exit = False

        def run(self):
            started.append("run")

    class FakeMaint:
        @classmethod
        def from_env(cls):
            return cls()

        def start(self):
            started.append("maint")
            return self

        def stop(self):
            started.append("stop")
    monkeypatch.setattr(desktop_service.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(desktop_service, "cleanup_downloads", lambda: None)
    monkeypatch.setattr(desktop_service, "fill_process_environ", lambda p: None)
    monkeypatch.setattr(desktop_maint, "DesktopMaintenance", FakeMaint)
    monkeypatch.setattr("app.logfile.install_file_log", lambda name, *a, **k: logs.append(name))
    monkeypatch.setattr(sys, "stdin", io.StringIO("start\n"))
    monkeypatch.delenv("ZEN_DESKTOP_MAINT", raising=False)
    monkeypatch.chdir(tmp_path)
    desktop_service.main()
    assert logs == ["desktop"] and started == ["maint", "run", "stop"]


def test_desktop_log_rotates_instead_of_truncating(tmp_path):
    from app.logfile import install_file_log
    root = logging.getLogger()
    before = list(root.handlers)
    path = install_file_log("desktop", {"ZEN_LOG_DIR": str(tmp_path)}, max_bytes=2000, backups=3)
    try:
        lg = logging.getLogger("zen.desktop.test")
        lg.propagate = True
        lg.disabled = False
        for i in range(200):
            lg.warning("line %04d %s", i, "x" * 40)
        assert path.exists() and (tmp_path / "desktop.log.1").exists()
        assert "line 0199" in path.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
