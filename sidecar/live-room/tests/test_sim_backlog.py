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
    """B-a1. ASR 1.5s and a 6s period stay inside maxInflight=2.

    Every blocking wait is counted, including those under one virtual second.
    Their sum stays under one virtual second. Segments 600-615 are rejected once
    each and then accepted on the host's single retry. Pending is sampled while
    a slot is held, not only after the upload returns.
    """
    assert report.waiting_v_total < 1, report.waiting
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


def _window_failure_detail(export_rows, memory_rows) -> dict:
    """Counts and seq lists for segments 300-330. Printed when the check fails."""
    slow = [row for row in export_rows if 300 <= int(row.get("seq") or 0) <= 330]
    lost = [int(row.get("seq") or 0) for row in slow if not row.get("en")]
    by_status: dict[str, list[int]] = {}
    for row in memory_rows:
        seq = int(row.get("seq") or 0)
        if not 300 <= seq <= 330:
            continue
        status = str(row.get("translate_status") or "")
        by_status.setdefault(status, []).append(seq)
    return {
        "export_rows": len(slow),
        "lost": lost,
        "counts": {status: len(seqs) for status, seqs in sorted(by_status.items())},
        "seqs": by_status,
    }


def assert_slow_translation_window(export_rows, memory_rows, *, queued: int, busy: int) -> None:
    """Seq 300-330: no timeout, no stale queue drop, backlog skips only, lost <= 25.

    Missing English must be a published failure status. The threshold 25 is unchanged.
    ``queued`` and ``busy`` are the translate queue and in-flight workers just
    before export; both have to be zero or a blank row is still in flight.
    """
    assert queued == 0, queued
    assert busy == 0, busy
    detail = _window_failure_detail(export_rows, memory_rows)
    assert detail["export_rows"] == 31, detail
    lost = detail["lost"]
    by_status = detail["seqs"]
    timeouts = by_status.get("timeout", [])
    stale = by_status.get("skipped", [])
    backlog = by_status.get("skipped_backlog", [])
    errors = by_status.get("error", [])
    waiter = by_status.get("waiter_timeout", [])
    fail = set(timeouts) | set(stale) | set(backlog) | set(errors) | set(waiter)
    unaccounted = [seq for seq in lost if seq not in fail]
    mem_lost = []
    for row in memory_rows:
        seq = int(row.get("seq") or 0)
        if 300 <= seq <= 330 and not row.get("has_en"):
            mem_lost.append(seq)
    assert not unaccounted, detail
    assert set(lost) == fail, detail
    assert set(mem_lost) == set(lost), detail
    assert timeouts == [], detail
    assert stale == [], detail
    assert errors == [], detail
    assert waiter == [], detail
    assert 0 < len(backlog), detail
    assert 0 < len(lost) <= 25, detail


def test_100min_slow_translation_catches_up(report):
    """Segments 300-330 take almost the whole translate budget, then English catches up.

    The slow window may drop English while the queue is full. That is
    skipped_backlog, counted in translate_skipped only when the row is written.
    Timeouts and stale queue drops in the window must be zero. Missing English
    stays at or under 25. The queue is empty and no translation is in flight
    before the export those rows come from.
    """
    assert 0 < report.translate_skipped <= 25
    assert report.final_metrics["translate_queued"] == 0
    assert report.final_metrics["translate_timeouts"] == 0, report.final_metrics
    assert report.final_metrics["translate_stale"] == 0, report.final_metrics
    assert report.final_metrics["translate_errors"] == 0, report.final_metrics
    assert report.final_metrics["translate_waiter_timeouts"] == 0, report.final_metrics
    written = [row for row in report.translate_rows if row["translate_status"] == "skipped_backlog"]
    assert report.translate_skipped == len(written), (report.translate_skipped, len(written))
    assert_slow_translation_window(
        report.export_json,
        report.translate_rows,
        queued=report.translate_queued_at_export,
        busy=report.translate_busy_at_export,
    )
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


def test_100min_memory_flattens():
    """App heap for the traced class. Not the latency report.

    The latency run leaves tracemalloc off. This one turns it on and checks the
    server heap only. Early growth is traced bytes from segment 500 to 750;
    late growth is traced bytes from 750 to 1000. The class passes when
    late/early <= 1/2, or when that late growth is under 1 MiB (1024*1024).
    Either condition is enough. early <= 0 does not divide, so the ratio is
    unset and only the 1 MiB hatch can pass. This is tighter than main, whose
    escape was total growth under 5 MB; the ratio stays 1/2. RSS is not judged
    here.
    """
    report = run_100min(trace=True)
    assert report.tracemalloc_500 > 0
    early = report.tracemalloc_750 - report.tracemalloc_500
    late = report.tracemalloc_1000 - report.tracemalloc_750
    # early <= 0 has no ratio. Do not treat that as <= 1/2.
    ratio = late / early if early > 0 else None
    detail: dict = {"early": early, "late": late, "ratio": ratio}
    rss_names = ("rss_0", "rss_250", "rss_500", "rss_750", "rss_1000")
    if any(hasattr(report, name) for name in rss_names):
        detail["rss"] = {name: getattr(report, name) for name in rss_names if hasattr(report, name)}
    assert (ratio is not None and ratio <= 1 / 2) or late < 1024 * 1024, detail


def test_100min_rss_bounded(report):
    """RSS on the latency run, with tracemalloc off.

    Real growth is about 24 KB per segment. Each 250-segment window stays under
    10 MB, and the whole 1000-segment class stays under 40 MB. A traced run
    cannot host this check: its RSS is mostly the tracer.
    """
    windows = (
        report.rss_250 - report.rss_0,
        report.rss_500 - report.rss_250,
        report.rss_750 - report.rss_500,
        report.rss_1000 - report.rss_750,
    )
    assert all(growth < 10 * 1024 * 1024 for growth in windows), (
        windows,
        report.rss_0,
        report.rss_250,
        report.rss_500,
        report.rss_750,
        report.rss_1000,
    )
    assert report.rss_1000 - report.rss_0 < 40 * 1024 * 1024, (
        report.rss_0,
        report.rss_1000,
    )
    assert windows[3] <= max(windows[:3]) + 2 * 1024 * 1024, windows


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
async def test_upload_latency_stays_inside_one_slice():
    """Untraced slice upload: end-to-end post time only, under 60 ms.

    This run measures one 512 KiB audio upload with tracing off. It does not
    measure loop gaps; that probe is on the 900 KiB runs below. The clock is
    perf_counter around the post, strictly under 60 ms. The bulk is the audio
    part (a text field over 64 KB is rejected). The silence scan stays off, so
    it is not what the clock measures, and the audio bytes have to come back intact.
    """
    import hashlib
    import time
    import tracemalloc

    from app.asr import AsrResult

    from tests.sim import TextAsr, open_room, post_segment, serving, sim_settings

    assert not tracemalloc.is_tracing()
    weird = "甲\r\n--not-the-boundary\r\n乙".encode()
    payload = b"x" * (512 * 1024)
    digest = hashlib.sha256(payload).digest()

    class SliceAsr(TextAsr):
        def __init__(self):
            super().__init__(0)
            self.intact = False

        def transcribe(self, wav, prompt=""):
            data = wav.read_bytes()
            if hashlib.sha256(data).digest() == digest:
                self.intact = True
                return AsrResult(ok=True, text="x")
            return super().transcribe(wav, prompt)

    asr = SliceAsr()
    async with serving(asr=asr, settings=sim_settings(translate=False)) as (app, client, token):
        del app
        await open_room(client, token, "class")
        odd = await post_segment(client, token, "class", "s", 1, weird, 0, 6000)
        assert odd.status_code == 200, odd.text
        assert odd.json().get("zh") == weird.decode()
        warm = await post_segment(client, token, "class", "s", 2, b"warm", 6000, 12000)
        assert warm.status_code == 200, warm.text
        started = time.perf_counter()
        resp = await post_segment(client, token, "class", "s", 3, payload, 12000, 18000)
        elapsed = time.perf_counter() - started
    assert resp.status_code == 200, resp.text
    assert resp.json().get("zh") == "x"
    assert asr.intact
    assert elapsed < 0.06, elapsed
    assert not tracemalloc.is_tracing()


def _boundary_mark() -> str:
    return "\r\n--not-the-boundary\r\n"


def _heavy_audio() -> bytes:
    """900 KiB audio part, inside the 512–900 KB band the duration cap still accepts.

    The boundary-like line sits in the part body. It is not the multipart boundary,
    so the parser has to keep scanning. A 512 KB *field* is the wrong payload:
    it is rejected at 64 KB and the clock would time a 400, not a parse.
    The inserted line is padded to a multiple of 3 so the surrounding "句"
    bytes stay valid UTF-8.
    """
    mark = _boundary_mark().encode()
    pad = b" " * ((3 - (len(mark) % 3)) % 3)
    blob = mark + pad
    audio = ("句" * (900 * 1024 // 3)).encode()
    cut = 300
    audio = audio[:cut] + blob + audio[cut + len(blob):]
    assert len(blob) % 3 == 0
    assert len(audio) == 900 * 1024
    assert 512 * 1024 <= len(audio) <= 900 * 1024
    assert mark in audio
    audio.decode()
    return audio


def _sixty_kb_field() -> str:
    """Text field of 60 KiB, under the 64 KB part limit."""
    mark = _boundary_mark()
    pad = ("x" * (60 * 1024 - len(mark))) + mark
    raw = pad.encode()
    assert len(raw) == 60 * 1024
    assert len(raw) < 64 * 1024
    assert mark in pad
    return pad


async def _post_heavy(client, token, seq: int, audio: bytes, pad: str):
    from tests.test_round2 import auth

    headers = {**auth(token), "x-breeze-async-translation": "1"}
    return await client.post(
        "/api/push",
        params={"room_id": "class", "session_id": "s", "seq": str(seq)},
        data={
            "room_id": "class",
            "session_id": "s",
            "seq": str(seq),
            "t0_ms": str((seq - 1) * 6000),
            "t1_ms": str(seq * 6000),
            "wait_translation": "0",
            "pad": pad,
        },
        files={"audio": ("a.webm", audio, "audio/webm")},
        headers=headers,
    )


@pytest.mark.anyio
async def test_heavy_multipart_upload_does_not_stall_the_loop():
    """Untraced 900 KiB upload: post under 60 ms, and the loop gap under 20 ms.

    Tracing stays off. This run measures loop stall, not only the post clock:
    the body is a 900 KiB audio part plus a 60 KiB text field (under the 64 KB
    field cap) and boundary-like bytes inside both. perf_counter around the
    post stays strictly under 60 ms. A 1 ms timer records the longest stretch
    the loop did not run; that gap stays at or under 20 ms, tighter than the
    post bound so a stall inside a fast post is still visible. Automatic GC is
    off for the measured post so a collection is not booked as parse time.
    """
    import asyncio
    import time
    import tracemalloc

    from tests.sim import TextAsr, no_gc_pause, open_room, post_segment, serving, sim_settings

    assert not tracemalloc.is_tracing()
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        del app
        await open_room(client, token, "class")
        warm = await post_segment(client, token, "class", "s", 1, b"warm", 0, 6000)
        assert warm.status_code == 200, warm.text
        audio = _heavy_audio()
        pad = _sixty_kb_field()
        gaps: list[float] = []
        stop = asyncio.Event()

        async def tick():
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        with no_gc_pause():
            beater = asyncio.create_task(tick())
            await asyncio.sleep(0.005)
            try:
                started = time.perf_counter()
                resp = await _post_heavy(client, token, 2, audio, pad)
                elapsed = time.perf_counter() - started
            finally:
                stop.set()
                await beater
    assert resp.status_code == 200, resp.text
    assert resp.json().get("zh") == audio.decode()
    assert elapsed < 0.06, elapsed
    assert gaps, "the 1 ms timer did not run"
    assert max(gaps) <= 0.020, max(gaps)
    assert not tracemalloc.is_tracing()


@pytest.mark.anyio
async def test_traced_upload_stays_inside_one_slice():
    """Traced parse of the same 900 KiB body: post under 60 ms, loop gap under 20 ms.

    The untraced slice run above measures end-to-end post time of 512 KiB with
    tracing off. The untraced 900 KiB run measures loop stall without the
    tracer. This run turns tracing on and measures both: the post clock and
    the same 1 ms loop probe. Bytes that look like a multipart boundary stay
    inside the audio part, and a slice-sized file comes back as Chinese.
    Automatic GC is off around the heavy post so a collection is not the gap.
    """
    import asyncio
    import time
    import tracemalloc

    from tests.sim import TextAsr, no_gc_pause, open_room, post_segment, serving, sim_settings

    weird = "甲\r\n--not-the-boundary\r\n乙".encode()
    bulky = ("句" * 20000).encode()
    tracemalloc.start(1)
    try:
        async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
            del app
            await open_room(client, token, "class")
            odd = await post_segment(client, token, "class", "s", 1, weird, 0, 6000)
            assert odd.status_code == 200, odd.text
            assert odd.json().get("zh") == weird.decode()
            resp = await post_segment(client, token, "class", "s", 2, bulky, 6000, 12000)
            assert resp.status_code == 200, resp.text
            assert resp.json().get("zh") == bulky.decode()
            audio = _heavy_audio()
            pad = _sixty_kb_field()
            gaps: list[float] = []
            stop_tick = asyncio.Event()

            async def tick():
                last = time.perf_counter()
                while not stop_tick.is_set():
                    await asyncio.sleep(0.001)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            with no_gc_pause():
                beater = asyncio.create_task(tick())
                await asyncio.sleep(0.005)
                try:
                    started = time.perf_counter()
                    heavy = await _post_heavy(client, token, 3, audio, pad)
                    elapsed = time.perf_counter() - started
                finally:
                    stop_tick.set()
                    await beater
            assert heavy.status_code == 200, heavy.text
            assert heavy.json().get("zh") == audio.decode()
            assert elapsed < 0.06, elapsed
            assert gaps, "the 1 ms timer did not run"
            assert max(gaps) <= 0.020, max(gaps)
            assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_deadline_margin_covers_three_coarse_ticks():
    """The early wake is the strictest of 50 ms, 10% of the time left, and 3 ticks."""
    import time

    from tests.sim import _deadline_margin

    tick = time.get_clock_info("monotonic").resolution
    room = 0.8
    margin = _deadline_margin(room)
    assert margin >= 0.05
    assert margin >= room * 0.1
    assert margin >= 0.02 + 3 * tick


def test_slow_window_check_fails_when_every_line_times_out():
    """Forcing all 31 slow lines to time out must fail the check. The cap stays 25.

    This is the reverse of the paced class: lost=31, every status timeout.
    If the timeouts assertion or the cap is removed, this test goes red.
    """
    export = [{"seq": seq, "en": "", "zh": "句"} for seq in range(300, 331)]
    memory = [
        {"seq": seq, "en": "", "translate_status": "timeout", "has_en": False}
        for seq in range(300, 331)
    ]
    assert len([row for row in export if not row["en"]]) == 31
    with pytest.raises(AssertionError) as caught:
        assert_slow_translation_window(export, memory, queued=0, busy=0)
    text = str(caught.value)
    assert "timeout" in text
    assert "31" in text or "300" in text


@pytest.mark.anyio
async def test_forced_translation_timeout_makes_the_slow_window_fail():
    """A real pipeline where every seq 300-330 translation times out fails the same check.

    The translator sleeps past the deadline and does not use the early-wake
    margin. lost is 31. The assertion must raise. Queue settings stay at the
    product default, so some rows are backlog skips and some are timeouts;
    either way every row is missing English and the check is red.
    """
    import asyncio
    import time

    from app.translate import TranslateResult, Translator

    from tests.sim import TextAsr, export_json, open_room, post_segment, serving, sim_settings

    class NeverOnTime(Translator):
        def __init__(self):
            super().__init__(enabled=True, key="test-key")

        def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
            del zh, glossary, context, deadline
            if cancel is not None and cancel.wait(5):
                return TranslateResult("", "timeout", "forced")
            time.sleep(5)
            return TranslateResult("", "timeout", "forced")

    settings = sim_settings(translate_timeout_s=0.2, translate_workers=2, translate_queue=4)
    async with serving(asr=TextAsr(0), translator=NeverOnTime(), settings=settings) as (app, client, token):
        await open_room(client, token, "class")
        for seq in range(300, 331):
            resp = await post_segment(
                client, token, "class", "s", seq, f"第{seq}句".encode(),
                (seq - 1) * 6000, seq * 6000,
            )
            assert resp.status_code == 200, resp.text
        pipe = app.state.pipeline
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            queued = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
            if queued == 0 and pipe._translate_busy == 0:
                await asyncio.sleep(0.05)
                queued = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
                if queued == 0 and pipe._translate_busy == 0:
                    break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("translations did not drain")
        queued = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
        busy = int(pipe._translate_busy)
        exported = await export_json(client, token, "class")
        memory = [
            {
                "seq": int(row.get("seq") or 0),
                "translate_status": str(row.get("translate_status") or ""),
                "has_en": bool(row.get("en")),
            }
            for row in app.state.bus.caption_state("class")
        ]
    lost = [row for row in exported if 300 <= int(row.get("seq") or 0) <= 330 and not row.get("en")]
    assert len(lost) == 31, [row.get("seq") for row in exported if row.get("en")]
    with pytest.raises(AssertionError) as caught:
        assert_slow_translation_window(exported, memory, queued=queued, busy=busy)
    assert "timeout" in str(caught.value) or "31" in str(caught.value)
    # The synthetic case above is all timeouts. Here the product path must
    # have published at least one timeout, not only backlog skips.
    statuses = {row["translate_status"] for row in memory if 300 <= row["seq"] <= 330}
    assert "timeout" in statuses, statuses
