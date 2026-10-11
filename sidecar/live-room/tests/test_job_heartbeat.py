"""Job lease heartbeat: a long job is not recovered/stolen; a stolen job's late result is not recorded."""
import asyncio
import time

from app.admin import db, jobs
from app.admin.jobs import JobStore, JobWorker


def mk(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    return JobStore(path)


def test_long_job_keeps_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LEASE_S", 0.2)
    st = mk(tmp_path)
    other = JobStore(st.db_path)
    seen = {}

    def slow(ctx):                       # never calls progress()
        time.sleep(0.6)
        other.recover("intruder")        # lease would have expired 3x without heartbeat
        seen["stolen"] = other.claim("intruder")
        return {"ok": 1}
    w = JobWorker(st, {"backup": slow}, heartbeat_s=0.05)
    job, _ = st.create("backup", {}, {})
    asyncio.run(w.run_once())
    assert seen["stolen"] is None
    got = st.get(job["id"])
    assert got["state"] == "succeeded" and got["attempt"] == 1


def test_lost_lease_result_not_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LEASE_S", 0.2)
    st = mk(tmp_path)

    def stolen(ctx):
        c = db.connect(st.db_path)
        c.execute("UPDATE jobs SET lease_owner='intruder' WHERE id=?", (ctx.job["id"],))
        c.commit()
        c.close()
        time.sleep(0.15)
        return {"ok": 1}
    w = JobWorker(st, {"backup": stolen}, heartbeat_s=0.05)
    job, _ = st.create("backup", {}, {})
    asyncio.run(w.run_once())
    assert st.get(job["id"])["state"] == "running"        # the new owner's job is untouched


def test_heartbeat_requires_owner(tmp_path):
    st = mk(tmp_path)
    job, _ = st.create("backup", {}, {})
    st.claim("me")
    assert st.heartbeat(job["id"], "me") is True
    assert st.heartbeat(job["id"], "someone") is False
