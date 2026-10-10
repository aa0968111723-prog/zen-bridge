"""Two hours of three-second slices, without models, sockets, or wall-clock pacing.

Run with LIVE_ROOM_SOAK_SLICES=9600 for a longer laptop soak (default: 2400
per target). Draft ASR and target selection are not wired into Pipeline yet:
drafts use its proposed interface, and each fake MT is bound to ONE target.
The pipeline's legacy ``en`` field carries that target's translation.
"""

import asyncio
import contextlib
import gc
import io
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tracemalloc
import wave

import pytest

from app.asr import AsrResult
from app.dispatch import RoomBus
from app.draft_asr import DraftPartial
from app.pipeline import Pipeline, Segment
from app.rooms import RoomBook
from app.rtf import SESSION_LIMIT, WINDOW_LIMIT
from app.settings import Settings
from app.store import CaptionStore
from app.translate import TranslateResult


class FakeAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav, prompt):
        with wave.open(str(wav), "rb") as audio:
            assert audio.getnframes() == 48000
            assert audio.getframerate() == 16000
        self.calls += 1
        return AsrResult(ok=True, text=f"第{self.calls}段課堂內容，" + "這是持續講課的字幕。" * 8)


class FakeMt:
    def __init__(self, target):
        self.target = target
        self.calls = 0

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        assert len(context) <= 4
        assert not glossary
        assert deadline is not None and not cancel.is_set()
        self.calls += 1
        prefix = "Lecture translation: " if self.target == "en" else "授業の翻訳："
        return TranslateResult(prefix + zh, "ok")


class FakeDraft:
    """Draft-interface fake; retains neither PCM nor prior partials."""

    def __init__(self):
        self.samples = 0

    def feed(self, pcm16):
        self.samples += len(pcm16) // 2
        t_ms = self.samples * 1000 // 16000
        return [DraftPartial("課堂草稿", False, t_ms - 1500),
                DraftPartial("課堂草稿內容", True, t_ms)]

    def finish(self):
        return []


def copy_decoder(src, work):
    wav = work / "audio.wav"
    shutil.copyfile(src, wav)
    return wav


def assert_stable(pipe, bus, room, settings):
    stats = pipe.stats()
    assert json.loads(json.dumps(stats, allow_nan=False)) == stats
    for name in ("pending", "inflight", "held", "translate_queued", "translate_busy",
                 "backlog_s", "backlog_audio_s", "asr_active_s"):
        assert stats[name] == 0, (name, stats)
    for name in ("rejected", "missing", "translate_skipped", "translate_timeouts",
                 "translate_errors", "translate_waiter_timeouts", "asr_errors", "asr_timeouts"):
        assert type(stats[name]) is int and stats[name] == 0
    for name in ("last_process_ms", "process_ms", "oldest_wait_ms", "asr_samples"):
        assert type(stats[name]) is int and stats[name] >= 0
    assert len(pipe.results) <= settings.max_results
    assert len(pipe.events) <= settings.history_limit * 2
    assert len(pipe._seen_order) == len(pipe._seen_versions) <= settings.max_results * 4
    for container in (pipe._index, pipe._hashes, pipe._emitted_segs, pipe._tr_epoch):
        assert len(container) <= settings.room_caption_cap
    for container in (pipe._active, pipe._flight, pipe._waiters, pipe._emit_waiters,
                      pipe._reserved, pipe._gap_since, pipe._cancel,
                      pipe._merge_carry, pipe._merge_seqs):
        assert not container
    assert pipe._bytes == pipe.inflight == 0
    assert all(not seg.translate_queued for seg in pipe.results.values())
    assert sum(len(rows) for rows in pipe._held.values()) == 0
    assert len(pipe._recent_zh) == 1
    assert all(len(rows) <= 8 and rows.maxlen == 8 for rows in pipe._recent_zh.values())
    assert len(pipe._rtf._window) <= WINDOW_LIMIT
    assert len(pipe._rtf._sessions) == 1
    assert all(len(rows) <= SESSION_LIMIT for rows in pipe._rtf._sessions.values())
    assert not pipe._rtf._waiting and not pipe._rtf._decoded and not pipe._rtf._asr_active
    assert len(pipe._tasks) == settings.translate_workers + 1
    assert all(not task.done() for task in pipe._tasks)
    assert pipe._translate_q.maxsize == settings.translate_queue
    assert pipe._translate_q._unfinished_tasks == 0
    assert len(room["history"]) <= settings.history_limit
    for container in (bus.by_room, bus._log):
        assert len(container["soak"]) <= settings.history_limit
    for container in (bus._state, bus._state_order, bus._ver, bus._caption_at):
        assert len(container["soak"]) <= settings.room_caption_cap
    assert stats["rtf"]["window"]["count"] <= WINDOW_LIMIT
    assert stats["rtf"]["session"]["count"] <= SESSION_LIMIT
    assert stats["rtf"]["session"]["audio_ms"]["p50"] == 3000


@pytest.mark.parametrize("target", ["en", "ja"])
def test_pipeline_soak(tmp_path, monkeypatch, caplog, target):
    slices = int(os.environ.get("LIVE_ROOM_SOAK_SLICES", "2400"))
    assert slices >= 4, "LIVE_ROOM_SOAK_SLICES must be at least 4"

    def forbidden(*args, **kwargs):
        raise AssertionError("soak must not use network or subprocesses")

    runner = asyncio.Runner()
    runner.get_loop()
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    caplog.set_level(logging.WARNING)

    async def simulate():
        # Smaller retention caps fill before the 25% baseline on the default run,
        # exercising eviction repeatedly rather than hiding growth below defaults.
        settings = Settings(max_results=128, history_limit=64, room_caption_cap=256,
                            translate_workers=1, translate_queue=4, segment_ms=3000,
                            gap_wait_s=0.003)
        asr, mt, draft = FakeAsr(), FakeMt(target), FakeDraft()
        store = CaptionStore(tmp_path / "captions.sqlite3")
        book = RoomBook(settings.max_rooms, settings.room_idle_s)
        room = book.open("soak")
        book.set_session_active("soak", True)
        bus = RoomBus(settings.history_limit, settings.room_caption_cap)
        counts = {"zh_ready": 0, "ready": 0, "draft": 0}

        def on_event(event):
            status = event["status"]
            counts[status] = counts.get(status, 0) + 1
            store.submit_save(event)
            published = bus.publish(event)
            room["history"] = bus.history("soak")
            book.touch("soak")
            return published

        pipe = Pipeline(asr, mt, "", tmp_path / "audio", settings, on_event)
        pcm = b"\xe8\x03" * 48000
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(pcm)
        payload = buffer.getvalue()
        baseline = None
        owns_trace = not tracemalloc.is_tracing()
        if owns_trace:
            tracemalloc.start()

        async def push(seq):
            partials = draft.feed(pcm)
            assert len(partials) == 2 and all(not part.final for part in partials)
            assert partials[-1].is_endpoint and partials[-1].t_ms == seq * 3000
            counts["draft"] += len(partials)
            assert pipe.try_admit()
            result = await pipe.submit(
                Segment("soak", f"session-{target}", seq,
                        t0_ms=(seq - 1) * 3000, t1_ms=seq * 3000),
                payload, copy_decoder, slot_held=True,
            )
            assert result.status == "ready" and result.translate_status == "ok"
            assert result.zh and result.en.startswith(
                "Lecture translation: " if target == "en" else "授業の翻訳："
            )
            assert not result.error

        try:
            # Four concurrent uploads exercise admission, ordered release, waiters,
            # and the real translation executor without deliberately dropping speech.
            done = 0
            quarter = slices // 4
            while done < slices:
                end = min(done + 4, slices)
                if done < quarter < end:
                    end = quarter
                await asyncio.wait_for(
                    asyncio.gather(*(push(seq) for seq in range(done + 1, end + 1))), 10
                )
                await asyncio.wait_for(pipe._translate_q.join(), 10)
                done = end
                if done == quarter or done == slices or done % 32 == 0:
                    # Drain asynchronous SQLite saves before sampling retained memory.
                    await asyncio.to_thread(store.flush)
                    assert_stable(pipe, bus, room, settings)
                    assert not any(pipe.tmp.iterdir())
                    if done == quarter:
                        gc.collect()
                        baseline = tracemalloc.get_traced_memory()[0]
            assert all(not part.final for part in draft.finish())
            await asyncio.to_thread(store.flush)
            assert_stable(pipe, bus, room, settings)
            gc.collect()
            growth = tracemalloc.get_traced_memory()[0] - baseline
            # 4 MiB allows the intentionally retained RTF session to grow up to
            # 4096 tuples plus Python/SQLite/executor allocator noise on Windows.
            # Audio/history caps are already full at baseline; they must not scale
            # with lecture length. This checks live allocations, not peak usage/RSS.
            assert growth < 4 * 1024 * 1024, f"retained allocation growth: {growth} bytes"
            assert asr.calls == mt.calls == slices
            assert counts == {"zh_ready": slices, "ready": slices, "draft": slices * 2}
            assert store.path == tmp_path / "captions.sqlite3"
            assert store.enabled and store.errors == 0
            with contextlib.closing(sqlite3.connect(store.path)) as conn:
                assert conn.execute("select count(*) from captions").fetchone()[0] == slices
                assert conn.execute(
                    "select count(*) from captions where status != 'ready' or en = ''"
                ).fetchone()[0] == 0
                assert conn.execute(
                    "select distinct session_id from captions"
                ).fetchall() == [(f"session-{target}",)]
        finally:
            await pipe.aclose()
            await asyncio.to_thread(store.close)
            if owns_trace:
                tracemalloc.stop()
        assert not pipe._tasks and not pipe._flight and not pipe._waiters
        assert not pipe._emit_waiters and pipe._translate_pool is None
        assert not any(record.exc_info or record.levelno >= logging.ERROR for record in caplog.records)

    with runner:
        runner.run(simulate())
