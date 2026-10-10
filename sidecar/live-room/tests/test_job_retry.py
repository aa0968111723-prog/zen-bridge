"""QA 後端 B4: transient job failures retry with backoff until max_attempts; others fail at once."""
import asyncio
import urllib.error

import pytest

from app.admin import db
from app.admin.jobs import JobRetryable, JobStore, JobWorker, is_retryable, retry_delay_s
from app.admin.live_client import LiveDown


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def store(tmp_path):
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    clk = Clock()
    return JobStore(path, clock=clk), clk


def test_backoff_schedule():
    assert [retry_delay_s(a, jitter=0) for a in (1, 2, 3, 10)] == [5.0, 10.0, 20.0, 300.0]


@pytest.mark.parametrize("exc,ok", [(JobRetryable("x"), True), (LiveDown("x"), True), (TimeoutError(), True),
                                    (urllib.error.URLError("x"), True), (ConnectionRefusedError(), True),
                                    (RuntimeError("bug"), False), (ValueError("bad"), False)])
def test_is_retryable(exc, ok):
    assert is_retryable(exc) is ok


def test_transient_failure_retries_then_fails(tmp_path):
    st, clk = store(tmp_path)
    calls = []

    def flaky(ctx):
        calls.append(1)
        raise LiveDown("down")
    w = JobWorker(st, {"backup": flaky})
    job, _ = st.create("backup", {}, {}, max_attempts=3)
    for _ in range(3):
        assert asyncio.run(w.run_once()) is True
        got = st.get(job["id"])
        if got["state"] == "queued":
            assert asyncio.run(w.run_once()) is False        # not due yet: backoff respected
            clk.t += 400
    assert len(calls) == 3 and st.get(job["id"])["state"] == "failed"


def test_transient_then_success(tmp_path):
    st, clk = store(tmp_path)
    seq = [LiveDown("down"), None]

    def h(ctx):
        e = seq.pop(0)
        if e:
            raise e
        return {"ok": 1}
    w = JobWorker(st, {"backup": h})
    job, _ = st.create("backup", {}, {})
    asyncio.run(w.run_once())
    assert st.get(job["id"])["state"] == "queued"
    clk.t += 400
    asyncio.run(w.run_once())
    assert st.get(job["id"])["state"] == "succeeded"


def test_bug_fails_immediately(tmp_path):
    st, _ = store(tmp_path)
    calls = []

    def bug(ctx):
        calls.append(1)
        raise RuntimeError("transient?")
    w = JobWorker(st, {"backup": bug})
    job, _ = st.create("backup", {}, {})
    asyncio.run(w.run_once())
    assert st.get(job["id"])["state"] == "failed" and len(calls) == 1
