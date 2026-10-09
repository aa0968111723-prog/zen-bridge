"""B-a. A paced class must not stall the recorder, and latency must not drift.

The shared run is the host page's opt-in (wait_translation=0, x-breeze-async-translation: 1).
Latency is per segment: listener zh_ready wall time minus the moment that segment's
recording ended, converted with SCALE. It is not "virtual now minus ideal t1", which
would accumulate asyncio delay across 1000 segments.
"""

import pytest

from tests.sim import SEGMENTS, latencies, percentile, run_100min, vlimit


@pytest.fixture(scope="module")
def report():
    return run_100min()


def test_100min_session_never_waits(report):
    """B-a1. ASR 1.5s and a 6s period stay inside maxInflight=2, so the host never waits.

    Segments 600-615 are rejected once each and then accepted on the host's single
    retry. Pending is sampled while a slot is held, not only after the upload returns.
    """
    assert report.waiting_v_total == 0, report.waiting
    assert report.metrics, "expected a metrics snapshot at least every 100 segments"
    assert report.storm_rejects == 16
    assert 1 <= report.pending_peak <= 2
    assert report.retries == list(range(600, 616))
    for snap in report.metrics:
        assert snap["pending"] <= 2
        assert snap["oldest_wait_ms"] / 1000 / report_scale() <= vlimit(15)
        if int(snap["seq"]) < 600:
            assert snap["rejected"] == 0
        elif int(snap["seq"]) >= 700:
            assert snap["rejected"] == report.storm_rejects
        else:
            assert 1 <= snap["rejected"] <= report.storm_rejects
    assert report.final_metrics["missing"] == 0
    assert report.final_metrics["pending"] == 0
    assert report.final_metrics["rejected"] == report.storm_rejects
    assert report.missing == 0
    assert [int(row["seq"]) for row in report.export_json] == list(range(1, SEGMENTS + 1))


def report_scale():
    from tests.sim import SCALE

    return SCALE


def test_100min_latency_no_drift(report):
    """B-a2. Thresholds are the spec's 10/15/20/3. Windows may add 20% and nothing else."""
    values = latencies(report)
    assert len(values) == SEGMENTS
    first = values[:100]
    last = values[-100:]
    p50 = percentile(values, 0.50)
    p95 = percentile(values, 0.95)
    drift = percentile(last, 0.50) - percentile(first, 0.50)
    assert p50 <= vlimit(10)
    assert p95 <= vlimit(15)
    assert max(values) <= vlimit(20)
    assert drift <= vlimit(3)


def test_100min_slow_translation_catches_up(report):
    """Segments 300-330 take the full translate budget. English is caught up after that."""
    assert report.translate_skipped > 0
    assert report.final_metrics["translate_queued"] == 0
    slow = [row for row in report.export_json if 300 <= int(row.get("seq") or 0) <= 330]
    assert len(slow) == 31
    later = [row for row in report.export_json if int(row.get("seq") or 0) >= 450]
    assert len(later) == SEGMENTS - 449
    assert all(row.get("en") for row in later)


def test_100min_structures_bounded(report):
    """B-a3. After segment 600 the live windows stay at their defaults."""
    later = [snap for snap in report.metrics if snap["seq"] >= 600]
    assert later
    for snap in later:
        assert snap["results"] <= 500
        assert snap["held"] <= 32
        assert snap["translate_queued"] <= 4
        assert snap["log"] <= 200
        assert snap["by_room"] <= 200
    at_500 = report.results_at.get(500, report.results_at.get(600, 0))
    for seq, count in report.results_at.items():
        if seq >= 500:
            assert count <= 500
            assert count <= max(at_500, 500)
    assert report.bus_log <= 200
    assert report.bus_by_room <= 200
    # App heap after a collection with GC on, at 500 / 750 / 1000. RSS for those
    # same points is on the report; the client keeps its own buffers, so the bound
    # is the server heap. Growth must flatten, or stay under 5 MB across the last 500.
    assert report.tracemalloc_500 > 0
    early = report.tracemalloc_750 - report.tracemalloc_500
    late = report.tracemalloc_1000 - report.tracemalloc_750
    growth = report.tracemalloc_1000 - report.tracemalloc_500
    assert late <= max(early, 0) / 2 or growth < 5 * 1024 * 1024, (early, late, growth)


def test_emitted_segs_bounded(report):
    """B-a4. _emitted_segs is trimmed with the room caption index (default 5000),
    which is what retranslate still looks up. A 1000-segment class is under that cap,
    so the class keeps one emitted key and one caption-state row per segment.
    The spec's 536 (max_results + held + queue) would drop keys retranslate needs;
    that cap is a product choice, pinned by test_emitted_segment_index_respects_caption_cap.
    """
    assert report.state_count == SEGMENTS
    assert report.emitted == SEGMENTS


@pytest.mark.anyio
async def test_sample_stall_does_not_drop_english_in_flight():
    """A loop hold longer than the scaled translate budget must not drop English.

    The paced run samples the server heap at slice boundaries. On Python 3.11
    that walk is longer than 40s of virtual time. A line the worker has already
    started still has to come back in English; the sample waits for it first.
    """
    import time

    from tests.sim import (
        Listener,
        ScriptedTranslator,
        TextAsr,
        VirtualHost,
        export_json,
        open_room,
        sample_after_translations,
        serving,
    )

    def plan(zh: str):
        # Longer than the 6s slice gap, shorter than the 40s budget, so the
        # next boundary still finds this line in flight.
        del zh
        return ("ok", 8.0)

    async with serving(asr=TextAsr(0.2), translator=ScriptedTranslator(plan)) as (app, client, token):
        await open_room(client, token, "class")

        async def before(seq: int) -> None:
            if seq != 8:
                return
            await sample_after_translations(app.state.pipeline, lambda: time.sleep(0.6))

        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "sample")
            await host.run(12, pace=True, before_slice=before)
            assert await listener.wait_for(
                lambda: len({m.get("seq") for m in listener.captions() if m.get("en")}) >= 12,
                30,
            )
        rows = await export_json(client, token, "class")
        assert [int(row["seq"]) for row in rows] == list(range(1, 13))
        missing = [int(row["seq"]) for row in rows if not row.get("en")]
        assert missing == [], missing


@pytest.mark.anyio
async def test_forced_backlog_reports_waiting_and_recovers():
    """B-a5. ASR slower than the 6s cut produces a waiting interval, then every line is exported."""
    from tests.sim import Listener, ScriptedTranslator, TextAsr, VirtualHost, export_json, open_room, serving

    async with serving(asr=TextAsr(9.0), translator=ScriptedTranslator(lambda zh: ("ok", 0.0))) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "backlog")
            await host.run(50)
            assert host.waiting_v_total > 0
            assert host.max_posts <= 2
            assert await listener.wait_for(lambda: len({m.get("seq") for m in listener.captions()}) >= 50, 30)
        rows = await export_json(client, token, "class")
        seqs = [row["seq"] for row in rows]
        assert seqs == list(range(1, 51))
        for snap_pending in (app.state.pipeline.stats()["pending"],):
            assert snap_pending <= 2
        assert app.state.pipeline.stats()["pending"] == 0
        for start, end in host.waiting:
            for row in rows:
                t0 = (row.get("t0_ms") or 0) / 1000
                t1 = (row.get("t1_ms") or 0) / 1000
                overlaps = t0 < end and t1 > start
                assert not overlaps


@pytest.mark.anyio
async def test_429_retry_lands_once():
    """B-a6. max_queue=1. A 4s ASR does not overlap a 6s period, so this host uses a 3.5s
    period: the next upload starts while the previous slot is still held, the retry 0.8s
    later lands, and each seq is exported once.

    Opt-in matches host.html (wait_translation=0, x-breeze-async-translation: 1). On this
    branch the admit slot is released at Chinese, not after English. hold_for_retry
    keeps the recorder from starting another slice while the one 800ms retry is
    in flight, or that slice would take the only queue slot. The 100-minute host
    does not set it: maxInflight is what host.html waits on.
    """
    from tests.sim import Listener, TextAsr, VirtualHost, export_json, open_room, serving, sim_settings

    async with serving(settings=sim_settings(max_queue=1, translate=False), asr=TextAsr(4.0)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "retry", period_v=3.5, hold_for_retry=True)
            await host.run(30)
            assert host.retries, "expected at least one 429"
            assert await listener.wait_for(
                lambda: len({m.get("seq") for m in listener.captions() if m.get("zh")}) >= 30,
                40,
            )
            heard = {m.get("seq") for m in listener.captions()}
            assert set(host.retries) <= heard
        rows = await export_json(client, token, "class")
        seqs = [row["seq"] for row in rows]
        assert seqs == list(range(1, 31))
        assert len(seqs) == len(set(seqs))


@pytest.mark.anyio
async def test_traced_upload_stays_inside_one_slice():
    """A traced multipart upload has to finish inside one compressed slice.

    The 100-minute run traces every allocation and compresses a 6s slice to
    60ms. Starlette's per-byte multipart parser then blocks the event loop
    for most of that slice, and the host books the overrun as recorder wait
    and as the same stretch on the last cue. One slice-sized body must come
    back well inside 60ms while tracing is on, and the audio bytes must survive.
    The bulk of the body is an unused field: the audio itself stays one byte,
    so the silence scan is not what the clock is measuring.
    """
    import time
    import tracemalloc

    from fastapi import Request

    from tests.sim import TextAsr, open_room, post_segment, serving, sim_settings
    from tests.test_round2 import auth

    weird = "甲\r\n--not-the-boundary\r\n乙".encode()
    # A few hundred kilobytes, the size of one noisy spoken slice. Kept out of
    # the audio part so the silence scan is not what the clock measures.
    pad = "x" * (512 * 1024)
    original_form = Request.form

    async def per_byte_parser(self, *args, **kwargs):
        raise RuntimeError("multipart must not use the per-byte form parser")

    # Starlette walks multipart one byte at a time on the event loop. Under the
    # allocation tracer that is most of a compressed slice, so a push that still
    # calls Request.form() fails here instead of only on a lucky slow run.
    Request.form = per_byte_parser
    tracemalloc.start(1)
    try:
        async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
            del app
            await open_room(client, token, "class")
            warm = await post_segment(client, token, "class", "s", 1, b"warm", 0, 6000)
            assert warm.status_code == 200, warm.text
            odd = await post_segment(client, token, "class", "s", 2, weird, 6000, 12000)
            assert odd.status_code == 200, odd.text
            assert odd.json().get("zh") == weird.decode()
            started = time.perf_counter()
            resp = await client.post(
                "/api/push",
                params={"room_id": "class", "session_id": "s", "seq": "3"},
                data={
                    "room_id": "class",
                    "session_id": "s",
                    "seq": "3",
                    "t0_ms": "12000",
                    "t1_ms": "18000",
                    "wait_translation": "0",
                    "pad": pad,
                },
                files={"audio": ("a.webm", b"x", "audio/webm")},
                headers={**auth(token), "x-breeze-async-translation": "1"},
            )
            elapsed = time.perf_counter() - started
        assert resp.status_code == 200, resp.text
        assert resp.json().get("zh") == "x"
        assert elapsed < 0.06, elapsed
    finally:
        Request.form = original_form
        tracemalloc.stop()
