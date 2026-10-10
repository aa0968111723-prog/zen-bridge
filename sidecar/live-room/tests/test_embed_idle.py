"""Embeddings run only when the live pipeline is idle or not running (hardware.md §5.1-3)."""
import asyncio
import struct

import pytest

from app import embed
from app.admin import db
from app.admin.jobs import JobStore, JobWorker
from tests.local_fakes import FakeOpener, migrated_db, seed_segment


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


IDLE = {"pending": 0, "inflight": 0, "translate_queued": 0, "translate_busy": 0, "backlog_s": 0.0}


def test_gate_live_down_is_ready():
    gate = embed.IdleGate(probe=lambda: None, idle_s=10)
    assert gate.check() == (True, "live_down")


def test_gate_probe_error_counts_as_live_down():
    def boom():
        raise OSError("refused")
    assert embed.IdleGate(probe=boom).check()[0] is True


@pytest.mark.parametrize("key", ["pending", "inflight", "translate_queued", "translate_busy"])
def test_gate_busy_blocks(key):
    clock = Clock()
    gate = embed.IdleGate(probe=lambda: {**IDLE, key: 1}, idle_s=10, clock=clock)
    clock.t = 100
    assert gate.check() == (False, "asr_or_translate_busy")


def test_gate_backlog_blocks():
    gate = embed.IdleGate(probe=lambda: {**IDLE, "backlog_s": 1.5}, idle_s=0)
    assert gate.check()[0] is False


def test_gate_needs_full_quiet_window_and_resets_on_busy():
    clock = Clock()
    state = {"m": dict(IDLE)}
    gate = embed.IdleGate(probe=lambda: state["m"], idle_s=10, clock=clock)
    assert gate.check()[0] is False          # first look starts the window
    clock.t = 9.9
    assert gate.check()[0] is False
    clock.t = 10.0
    assert gate.check() == (True, "idle")
    state["m"] = {**IDLE, "pending": 1}
    clock.t = 11
    assert gate.check()[0] is False
    state["m"] = dict(IDLE)
    clock.t = 20
    assert gate.check()[0] is False          # 9 s since busy
    clock.t = 21
    assert gate.check()[0] is True


def test_gate_from_env():
    assert embed.gate_from_env(lambda: None, {"ZEN_EMBED_IDLE_S": "3"}).idle_s == 3.0
    assert embed.gate_from_env(lambda: None, {}).idle_s == 10.0


def test_embedder_request_shape_and_loopback_only():
    opener = FakeOpener({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    e = embed.embedder_from_env({}, opener=opener)
    assert e.model == "qwen3-embedding:0.6b"
    assert e.embed(["甲", "乙"]) == [[0.1, 0.2], [0.3, 0.4]]
    req = opener.requests[0]
    assert req["url"] == "http://127.0.0.1:11434/api/embed"
    assert req["body"]["options"]["num_thread"] == 2
    assert req["body"]["input"] == ["甲", "乙"]
    from app.translate_config import TranslateConfigError
    with pytest.raises(TranslateConfigError):
        embed.embedder_from_env({"ZEN_EMBED_BASE_URL": "http://10.1.1.1:11434"})


def test_embedder_shape_error():
    e = embed.OllamaEmbedder(opener=FakeOpener({"embeddings": [[0.1]]}))
    with pytest.raises(embed.EmbedError):
        e.embed(["a", "b"])


def _setup(tmp_path, n=3):
    path = migrated_db(tmp_path)
    c = db.connect(path)
    for i in range(1, n + 1):
        seed_segment(c, seg=f"s1-{i}", seq=i, zh=f"第{i}句話")
    c.close()
    return path


def _run_job(path, handler):
    store = JobStore(path)
    job, _ = store.create("embed_backfill", {}, {"model": "m"})
    worker = JobWorker(store, {"embed_backfill": handler})
    asyncio.run(worker.run_once())
    return store, store.get(job["id"])


def test_backfill_defers_when_busy_without_spending_attempt(tmp_path):
    path = _setup(tmp_path)
    opener = FakeOpener({"embeddings": [[1.0, 2.0]] * 3})
    gate = embed.IdleGate(probe=lambda: {**IDLE, "pending": 2}, idle_s=0)
    handler = embed.backfill_handler(lambda: db.connect(path), embed.OllamaEmbedder(opener=opener, model="m"), gate,
                                     defer_s=30)
    store, job = _run_job(path, handler)
    assert job["state"] == "queued" and job["attempt"] == 0
    assert job["progress"]["stage"] == "waiting"
    assert opener.requests == []                       # no embedding call while ASR is busy


def test_backfill_runs_after_session_when_live_down(tmp_path):
    path = _setup(tmp_path, n=3)
    opener = FakeOpener({"embeddings": [[1.0, 2.0], [3.0, 4.0]]}, {"embeddings": [[5.0, 6.0]]})
    gate = embed.IdleGate(probe=lambda: None)
    handler = embed.backfill_handler(lambda: db.connect(path), embed.OllamaEmbedder(opener=opener, model="m"), gate,
                                     batch=2)
    store, job = _run_job(path, handler)
    assert job["state"] == "succeeded", job
    assert job["result"]["embedded"] == 3
    c = db.connect(path)
    rows = c.execute("SELECT owner_id, dim, vector FROM embeddings ORDER BY id").fetchall()
    assert [r[1] for r in rows] == [2, 2, 2]
    assert struct.unpack("<2f", rows[0][2]) == (1.0, 2.0)
    assert embed.pending_transcripts(c, "m", 10) == []
    c.close()
    assert len(opener.requests) == 2 and len(opener.requests[0]["body"]["input"]) == 2
