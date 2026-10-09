"""B-b. Slow English must not block Chinese.

Every push here sends the host page opt-in: form wait_translation=0 and header
x-breeze-async-translation: 1. The default /api/push still waits for English
(tests/test_round2.py). This branch drops the oldest queued translation
(skipped_backlog) instead of failing the new line with queue_full.
"""

import threading

import pytest

from app.translate import TranslateResult
from tests.sim import (
    SCALE,
    HttpPlanTranslator,
    Listener,
    ScriptedTranslator,
    TextAsr,
    VirtualHost,
    caption_rows,
    export_json,
    http_error,
    open_room,
    post_segment,
    serving,
    sim_settings,
    vlimit,
)


def _seq(zh: str) -> int:
    digits = "".join(ch for ch in zh if ch.isdigit())
    return int(digits or "0")


@pytest.mark.anyio
async def test_push_returns_before_slow_translation():
    """B-b1. Opt-in push returns at Chinese. English arrives later on the listener.

    The translation takes the spec's 40 virtual s. translate_timeout_s is raised to 60
    virtual s for this test only: with the default 40 s timeout a 40 s translation
    races its own timeout and English legitimately ends as "timeout" on a slow runner.
    """
    translator = ScriptedTranslator(lambda zh: ("ok", 40.0))
    settings = sim_settings(translate_timeout_s=60 * SCALE)
    async with serving(settings=settings, asr=TextAsr(1.5), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            started = __import__("time").monotonic()
            resp = await post_segment(client, token, "class", "s", 1, "第1句".encode(), 0, 6000)
            elapsed_v = (__import__("time").monotonic() - started) / SCALE
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "第1句"
            assert elapsed_v <= vlimit(2.5)
            assert await listener.wait_for(
                lambda: any(m.get("status") == "ready" and m.get("en") for m in listener.messages),
                vlimit(40),
            )
            versions = [int(m.get("version") or 0) for m in listener.messages if m.get("id") and m.get("status") == "ready"]
            assert versions
            chinese = [int(m.get("version") or 0) for m in listener.messages if m.get("status") == "zh_ready"]
            assert chinese and versions[0] > chinese[0]


@pytest.mark.anyio
async def test_slow_translation_does_not_pause_recorder():
    """B-b2. Thirty segments, 40s English each. The recorder never waits and never sees 429."""
    translator = ScriptedTranslator(lambda zh: ("ok", 40.0))
    async with serving(asr=TextAsr(1.5), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "slow")
            await host.run(30)
            assert host.waiting_v_total == 0
            assert host.retries == []
            # The last push can return before the listener pump has read its zh_ready.
            assert await listener.wait_for(
                lambda: len({m.get("seq") for m in listener.messages if m.get("status") == "zh_ready"}) >= 30,
                vlimit(15),
            )
            zh_at = {}
            for msg in listener.messages:
                if msg.get("status") == "zh_ready" and msg.get("seq") is not None:
                    zh_at.setdefault(int(msg["seq"]), msg["_recv_mono"])
            delays = [(zh_at[seq] - host.segment_end_mono[seq]) / SCALE for seq in range(1, 31)]
            ordered = sorted(delays)
            p95 = ordered[int(0.95 * (len(ordered) - 1))]
            assert p95 <= vlimit(15)
        rows = await export_json(client, token, "class")
        assert len(rows) == 30
        assert all(row.get("zh") for row in rows)


@pytest.mark.anyio
async def test_translation_blackhole_queue_full_keeps_chinese():
    """B-b3. A translator that never returns used to fail the new line with queue_full.

    This branch keeps Chinese, drops the oldest queued line as skipped_backlog, and
    times out the one a worker already started. No segment disappears and none is queue_full.
    """
    gate = threading.Event()
    translator = ScriptedTranslator(lambda zh: ("block", gate))
    async with serving(asr=TextAsr(0), translator=translator, settings=sim_settings()) as (app, client, token):
        try:
            await open_room(client, token, "class")
            async with Listener(app, "class") as listener:
                host = VirtualHost(client, token, "class", "hole", period_v=6.0)
                await host.run(20, pace=False)
                def terminal():
                    rows = caption_rows(app, "class")
                    if len(rows) < 20:
                        return False
                    return all(row.get("translate_status") in {"timeout", "skipped", "skipped_backlog"} for row in rows)

                assert await listener.wait_for(terminal, 600)
                rows = {row["seq"]: row for row in caption_rows(app, "class")}
                assert set(rows) == set(range(1, 21))
                for row in rows.values():
                    assert row.get("zh")
                    assert row.get("translate_status") != "queue_full"
                    assert row.get("status") != "missing"
                zh_ready = {msg.get("seq") for msg in listener.messages if msg.get("status") == "zh_ready"}
                assert zh_ready == set(range(1, 21))
            exported = await export_json(client, token, "class")
            assert len(exported) == 20
        finally:
            # Unblock the scripted translator before the app shuts down. On a build
            # where push waits for English, an assertion failure used to leave that
            # thread parked and stop() never returned.
            gate.set()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "status", "error", "sleeps"),
    [
        ("rate", "rate", "英譯太頻繁，中文仍保留", 2),
        ("quota", "quota", "英譯額度不足，中文仍保留", 0),
        ("http500", "http", "英譯服務回應 500，中文仍保留", 2),
        ("http503", "http", "英譯服務回應 503，中文仍保留", 2),
        ("http408", "http", "英譯服務回應 408，中文仍保留", 2),
        ("timeout", "timeout", "英譯逾時，不假設沒有計費。中文仍保留", None),
        ("network", "network", "英譯沒有網路，中文仍保留", None),
        ("badjson", "bad_response", "英譯回應無法讀取，中文仍保留", 0),
        ("null", "bad_response", "英譯回應無法讀取，中文仍保留", 0),
    ],
)
async def test_provider_errors_keep_chinese(kind, status, error, sleeps):
    """B-b4. Provider failures keep the Chinese line and do not publish the same version twice."""
    import urllib.error

    slept: list[float] = []

    def responses():
        if kind == "rate":
            return [lambda: http_error(429, b"slow down", retry_after="1") for _ in range(3)]
        if kind == "quota":
            body = b'{"error":{"code":"insufficient_quota"}}'
            return [lambda: http_error(429, body) for _ in range(3)]
        if kind == "http500":
            return [lambda: http_error(500, b"no", retry_after="1") for _ in range(3)]
        if kind == "http503":
            return [lambda: http_error(503, b"no", retry_after="1") for _ in range(3)]
        if kind == "http408":
            return [lambda: http_error(408, b"no", retry_after="1") for _ in range(3)]
        if kind == "timeout":
            return [TimeoutError("timed out") for _ in range(3)]
        if kind == "network":
            return [urllib.error.URLError("offline") for _ in range(3)]
        if kind == "badjson":
            return [b"not-json" for _ in range(3)]
        return [b'{"choices":[{"message":{"content":null}}]}' for _ in range(3)]

    translator = HttpPlanTranslator(responses(), slept)
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            resp = await post_segment(client, token, "class", "s", 1, "第1句".encode(), 0, 6000)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "第1句"
            assert await listener.wait_for(
                lambda: any(m.get("status") == "translate_failed" for m in listener.messages),
                20,
            )
        row = next(item for item in caption_rows(app, "class") if item["seq"] == 1)
        assert row["zh"] == "第1句"
        assert row["status"] == "translate_failed"
        assert row["translate_status"] == status
        assert row["error"] == error
        if sleeps is not None:
            assert len(slept) == sleeps
        versions = [int(m["version"]) for m in listener.messages if m.get("id")]
        assert len(versions) == len(set(versions))


@pytest.mark.anyio
async def test_translation_recovers_after_outage():
    """B-b5. The first calls block the two workers. The queue keeps the newest lines,
    so segments 11-14 still receive English within translate_timeout + 3 virtual seconds
    once the outage ends. Older lines may be skipped_backlog; that is this branch's backlog policy.
    """
    import time

    gate = threading.Event()

    def plan(zh: str):
        if _seq(zh) <= 10:
            return ("block", gate)
        return ("ok", 2.0)

    translator = ScriptedTranslator(plan)
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        try:
            await open_room(client, token, "class")
            async with Listener(app, "class") as listener:
                host = VirtualHost(client, token, "class", "out")
                await host.run(14, pace=False)
                gate.set()
                limit_v = 40 + 3

                def ready():
                    got = {}
                    for msg in listener.messages:
                        if msg.get("en") and msg.get("seq") is not None:
                            got[int(msg["seq"])] = msg["_recv_mono"]
                    return all(seq in got for seq in (11, 12, 13, 14))

                assert await listener.wait_for(ready, vlimit(limit_v))
                ends = {seq: host.segment_end_mono[seq] for seq in (11, 12, 13, 14)}
                for msg in listener.messages:
                    seq = msg.get("seq")
                    if seq in ends and msg.get("en"):
                        assert (msg["_recv_mono"] - ends[int(seq)]) / SCALE <= vlimit(limit_v)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and app.state.pipeline.stats()["translate_queued"] != 0:
                    await __import__("asyncio").sleep(0.01)
                assert app.state.pipeline.stats()["translate_queued"] == 0
        finally:
            gate.set()
