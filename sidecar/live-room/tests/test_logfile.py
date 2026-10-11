"""QA CTO-07: live-room and admin write a persistent, rotating, redacted log file."""
import logging

from app.logfile import install_file_log


def test_file_log_rotates_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEN_LOG", "on")
    monkeypatch.setenv("ZEN_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        path = install_file_log("admin", max_bytes=2000, backups=2)
        assert path == tmp_path / "admin.log"
        assert install_file_log("admin") == path                 # idempotent
        assert len(root.handlers) == len(before) + 1
        lg = logging.getLogger("zen.test.logfile")
        for i in range(60):
            lg.info("filler line %d %s", i, "x" * 40)
        lg.info("login ok token=abcdef0123456789abcdef")
        for h in root.handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert "login ok" in text and "abcdef0123456789abcdef" not in text
        assert (tmp_path / "admin.log.1").exists()
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(level)


def test_file_log_off(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEN_LOG", "off")
    monkeypatch.setenv("ZEN_LOG_DIR", str(tmp_path))
    assert install_file_log("live-room") is None
    assert not (tmp_path / "live-room.log").exists()
