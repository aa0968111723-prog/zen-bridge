"""QA 後端 B3: a second admin instance must fail on the port before any job is claimed."""
import socket

import pytest

from app.admin import db, run
from app.admin.jobs import JobStore


def test_busy_port_exits_before_app_starts(tmp_path, monkeypatch):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    st = JobStore(path)
    job, _ = st.create("backup", {}, {})
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    monkeypatch.setenv("ZEN_ADMIN_PORT", str(port))
    monkeypatch.setenv("ZEN_DB_PATH", str(path))
    created = []
    import app.admin.server as server
    monkeypatch.setattr(server, "create_admin_app", lambda *a, **k: created.append(1))
    try:
        with pytest.raises(SystemExit) as ei:
            run.main([])
        assert "已被占用" in str(ei.value)
    finally:
        holder.close()
    assert created == [] and st.get(job["id"])["state"] == "queued" and st.get(job["id"])["attempt"] == 0


def test_bind_socket_returns_listening_socket():
    s = run.bind_socket("127.0.0.1", 0)
    try:
        assert s.getsockname()[1] > 0
    finally:
        s.close()
