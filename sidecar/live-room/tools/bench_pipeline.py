#!/usr/bin/env python3
"""In-process capture/translation benchmark.

Drives the real ASGI app (create_app + httpx) the way the host page records:
a chunk every period, at most two uploads in flight, and no recording while
both slots are busy. Time is compressed by --scale; printed times are converted
back to real seconds (wall / scale).

The client always sends the async-translation opt-in (form wait_translation=0
and header x-breeze-async-translation: 1). Original servers ignore both, so a
run against unmodified code measures today's blocking /api/push.

Usage (from the repo root):
  python tools/bench_pipeline.py --scenario A --json /tmp/bench_A.json
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.asr import AsrResult  # noqa: E402
from app.server import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.translate import TranslateResult  # noqa: E402

PRESETS = {
    "A": {
        "segments": 40,
        "period_s": 6.0,
        "rtf": 0.6,
        "tr_ms": 2000.0,
        "tr_jitter": 0.5,
        "slow_from": 10,
        "slow_to": 20,
        "slow_ms": 15000.0,
        "scale": 0.05,
    },
    "B": {
        "segments": 40,
        "period_s": 6.0,
        "rtf": 1.3,
        "tr_ms": 2000.0,
        "tr_jitter": 0.5,
        "slow_from": 0,
        "slow_to": 0,
        "slow_ms": 15000.0,
        "scale": 0.05,
    },
    "soak": {
        "segments": 1000,
        "period_s": 6.0,
        "rtf": 0.6,
        "tr_ms": 2000.0,
        "tr_jitter": 0.5,
        "slow_from": 300,
        "slow_to": 330,
        "slow_ms": 15000.0,
        "scale": 0.01,
    },
}

FAIL_TRANSLATE = {"queue_full", "timeout", "error", "skipped", "skipped_backlog"}
_KNOWN_TRANSLATE_FAILS = ("queue_full", "timeout", "error", "skipped")


def classify_translate_failures(rows: list[dict]) -> dict:
    """Count Chinese lines that never received English.

    ``skipped_backlog`` is the drop-oldest outcome. It is part of ``skipped``
    and is also returned on its own, so it is not an unexplained ``other``
    and it is not added into the total twice.
    """
    counts = {name: 0 for name in _KNOWN_TRANSLATE_FAILS}
    skipped_backlog = 0
    other = 0
    considered = 0
    for row in rows:
        if row.get("en"):
            considered += 1
            continue
        considered += 1
        status = str(row.get("translate_status") or "")
        if status == "skipped_backlog":
            counts["skipped"] += 1
            skipped_backlog += 1
            continue
        if status in counts:
            counts[status] += 1
            continue
        if status or row.get("status") == "translate_failed":
            other += 1
    total = sum(counts.values()) + other
    return {
        **counts,
        "skipped_backlog": skipped_backlog,
        "other": other,
        "total": total,
        "rate": (total / considered) if considered else 0.0,
    }
SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def _session_counts(segments: int, sessions: int) -> list[int]:
    sessions = max(1, min(int(sessions), max(1, int(segments))))
    base, extra = divmod(int(segments), sessions)
    return [base + (1 if index < extra else 0) for index in range(sessions)]


def _flag_given(name: str) -> bool:
    for arg in sys.argv[1:]:
        if arg == name or arg.startswith(name + "="):
            return True
    return False


def _rss_bytes() -> int:
    """Resident set size in bytes.

    Linux reads /proc/self/statm. Other Unix platforms fall back to resource.getrusage.
    Windows has neither; return 0 so a soak run still finishes and the report stays printable.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident = int(handle.read().split()[1])
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        return resident * int(page)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes.
        if sys.platform == "darwin":
            return int(usage)
        return int(usage) * 1024
    except (ImportError, AttributeError, OSError, ValueError):
        return 0


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _round(value, digits: int = 3):
    if value is None:
        return None
    return round(float(value), digits)


def _write_wav(path: Path, seconds: float = 0.1) -> None:
    """16 kHz mono PCM with a non-silent square wave. Duration is wall audio, not the scenario period."""
    rate = 16000
    count = max(1, int(rate * seconds))
    frame = bytearray()
    for index in range(count):
        sample = 8000 if (index // 20) % 2 == 0 else -8000
        frame += int(sample).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frame))


def _parse_seq(text: str) -> int:
    match = re.search(r"第(\d+)段", text or "")
    if not match:
        return 0
    return int(match.group(1))


class FakeAsr:
    """Sleeps RTF * real period * scale, then returns Chinese that names the segment."""

    def __init__(self, rtf: float, period_s: float, scale: float):
        self.rtf = rtf
        self.period_s = period_s
        self.scale = scale
        self.durations: list[float] = []
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del prompt
        started = time.monotonic()
        time.sleep(self.rtf * self.period_s * self.scale)
        self.durations.append(time.monotonic() - started)
        self.calls += 1
        try:
            seq = (wav.parent / "seg.txt").read_text(encoding="utf-8").strip()
        except OSError:
            seq = "0"
        return AsrResult(ok=True, text=f"第{seq}段中文")


class SlowTranslator:
    """Sleeps a (possibly slow-window) translation delay. Duck-types Translator for metrics."""

    def __init__(self, tr_ms: float, jitter: float, slow_from: int, slow_to: int, slow_ms: float, scale: float, rng: random.Random):
        self.tr_ms = tr_ms
        self.jitter = jitter
        self.slow_from = slow_from
        self.slow_to = slow_to
        self.slow_ms = slow_ms
        self.scale = scale
        self.rng = rng
        self.key = "bench-key"
        self.tokens_used = 0
        self.calls = 0
        self.durations: list[float] = []

    def status_label(self) -> str:
        return "benchmark"

    def price_note(self):
        return None

    def translate(self, text: str, glossary=None, context=None, **kwargs) -> TranslateResult:
        del glossary, context, kwargs
        seq = _parse_seq(text)
        mean = self.slow_ms if self.slow_from and self.slow_from <= seq <= self.slow_to else self.tr_ms
        spread = abs(mean * self.jitter / 3.0) if self.jitter else 0.0
        delay_ms = mean if spread <= 0 else max(0.0, self.rng.gauss(mean, spread))
        started = time.monotonic()
        time.sleep(delay_ms / 1000.0 * self.scale)
        self.durations.append(time.monotonic() - started)
        self.calls += 1
        self.tokens_used += 1
        return TranslateResult(text=f"EN {text}", status="ok")


def _decoder(src: Path, work: Path) -> Path:
    raw = src.read_bytes()
    match = re.search(br"SEQ(\d+)", raw)
    seq = match.group(1).decode("ascii") if match else "0"
    (work / "seg.txt").write_text(seq, encoding="utf-8")
    wav = work / "audio.wav"
    _write_wav(wav, 0.1)
    return wav


def _ms_to_stamp(ms: int) -> tuple[int, int, int, int]:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return hours, minutes, seconds, millis


def _parse_srt(payload: str) -> list[dict]:
    text = payload.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            continue
        index = None
        time_line = ""
        body_from = 0
        if lines[0].strip().isdigit() and SRT_TIME.search(lines[1] if len(lines) > 1 else ""):
            index = int(lines[0].strip())
            time_line = lines[1]
            body_from = 2
        else:
            for offset, line in enumerate(lines):
                if SRT_TIME.search(line):
                    time_line = line
                    body_from = offset + 1
                    break
        match = SRT_TIME.search(time_line)
        if not match:
            cues.append({"index": index, "ok_time": False, "text": "\n".join(lines[body_from:])})
            continue
        parts = [int(item) for item in match.groups()]
        start = ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * 1000 + parts[3]
        end = ((parts[4] * 60 + parts[5]) * 60 + parts[6]) * 1000 + parts[7]
        cues.append({
            "index": index,
            "ok_time": True,
            "start": start,
            "end": end,
            "stamp": time_line.strip(),
            "text": "\n".join(lines[body_from:]).strip(),
        })
    return cues


def _check_srt(payload: str, segments_with_text: int, require_hour: bool) -> dict:
    cues = _parse_srt(payload)
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    indices = [cue.get("index") for cue in cues]
    expected = list(range(1, len(cues) + 1))
    add("indices_1_to_n", indices == expected, f"got {indices[:8]}{'...' if len(indices) > 8 else ''} ({len(cues)} cues)")

    bad_time = [cue for cue in cues if not cue.get("ok_time")]
    add("timestamps_hhmmss_mmm", not bad_time and all(cues), f"unparsed {len(bad_time)} of {len(cues)}")

    hour = [cue for cue in cues if cue.get("ok_time") and cue["start"] >= 3_600_000]
    if require_hour:
        sample = hour[0]["stamp"] if hour else ""
        add("timestamp_at_least_one_hour", bool(hour), sample or "no cue starts at or after 01:00:00,000")
    else:
        add("timestamp_at_least_one_hour", True, "not required for this scenario")

    starts = [cue["start"] for cue in cues if cue.get("ok_time")]
    monotonic = all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1))
    add("start_monotonic_non_decreasing", monotonic, f"{len(starts)} starts")

    overlap = []
    ordered = [cue for cue in cues if cue.get("ok_time")]
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev["end"] > nxt["start"]:
            overlap.append((prev.get("index"), nxt.get("index"), prev["end"], nxt["start"]))
    add("no_overlap_end_le_next_start", not overlap, f"overlaps {overlap[:3]}")

    backwards = [cue.get("index") for cue in ordered if cue["end"] <= cue["start"]]
    add("end_after_start", not backwards, f"bad {backwards[:6]}")

    add(
        "cue_count_vs_segments_with_text",
        len(cues) == segments_with_text,
        f"cues {len(cues)} vs segments_with_text {segments_with_text}",
    )
    return {"checks": checks, "cues": len(cues), "pass": all(item["ok"] for item in checks)}


class Recorder:
    """Host capture loop: full-period chunks, maxInflight=2, waiting records nothing.

    Mirrors recorder_machine.js backpressure (beginSegment waits while two uploads
    are in flight) while keeping each captured chunk one period long, which is what
    the benchmark knobs mean by "a chunk every period".
    """

    def __init__(self, segments: int, period_s: float, scale: float, max_inflight: int = 2, sessions: int = 1):
        self.segments = segments
        self.period_s = period_s
        self.scale = scale
        self.max_inflight = max_inflight
        self.sessions = max(1, int(sessions))
        self.paused_wall = 0.0
        self.chunks: list[dict] = []

    async def run(self, upload, on_session_end=None) -> None:
        """Split the run into K sessions. Each session restarts seq and t0 at 0, like the browser."""
        inflight: set[asyncio.Task] = set()
        counts = _session_counts(self.segments, self.sessions)
        global_seq = 0

        for index, count in enumerate(counts):
            session_id = f"s{index + 1}"
            session_start = time.monotonic()

            def rel_ms(mark: float, origin: float = session_start) -> int:
                return int(round((mark - origin) / self.scale * 1000.0))

            for seq in range(1, count + 1):
                if len(inflight) >= self.max_inflight:
                    paused_at = time.monotonic()
                    while len(inflight) >= self.max_inflight:
                        await asyncio.wait(set(inflight), return_when=asyncio.FIRST_COMPLETED)
                    self.paused_wall += time.monotonic() - paused_at
                t0 = time.monotonic()
                await asyncio.sleep(self.period_s * self.scale)
                t1 = time.monotonic()
                global_seq += 1
                chunk = {
                    "seq": seq,
                    "global_seq": global_seq,
                    "session_id": session_id,
                    "t0_ms": rel_ms(t0),
                    "t1_ms": rel_ms(t1),
                    "end_mono": t1,
                }
                self.chunks.append(chunk)
                task = asyncio.create_task(upload(chunk))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
                inflight.clear()
            if on_session_end is not None:
                await on_session_end(session_id)


async def _run(args) -> dict:
    scale = args.scale
    if scale <= 0:
        raise SystemExit("--scale must be > 0")
    rng = random.Random(args.seed)
    asr = FakeAsr(args.rtf, args.period_s, scale)
    translator = SlowTranslator(
        args.tr_ms, args.tr_jitter, args.slow_from, args.slow_to, args.slow_ms, scale, rng,
    )
    slow_real = (args.slow_ms / 1000.0) if args.slow_from else 0.0
    translate_timeout_s = max(40.0 * scale, (max(args.tr_ms, args.slow_ms) / 1000.0) * scale * 4.0, 0.05)
    asr_timeout_s = max(120.0 * scale, args.rtf * args.period_s * scale * 8.0, 1.0)
    gap_wait_s = max(3.0 * scale, args.rtf * args.period_s * scale * 10.0, 0.25)
    settings_kwargs = dict(
        allow_testclient=True,
        silence_rms=0.0,
        translate_timeout_s=translate_timeout_s,
        asr_timeout_s=asr_timeout_s,
        decode_timeout_s=max(40.0 * scale, 5.0),
        gap_wait_s=gap_wait_s,
        max_queue=64,
        translate_queue=4,
        history_limit=200,
        max_results=500,
    )
    data_path = "" if args.no_store else (args.data_path or "").strip()
    if data_path:
        path = Path(data_path)
        if path.exists():
            path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(path) + suffix)
            if extra.exists():
                extra.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        settings_kwargs["data_path"] = str(path)

    app = create_app(settings=Settings(**settings_kwargs), asr=asr, translator=translator, decoder=_decoder)
    zh_at: dict[int, float] = {}
    en_at: dict[int, float] = {}
    translate_status: dict[int, str] = {}
    versions: dict[int, int] = {}

    bus = app.state.bus
    original_publish = bus.publish
    by_segment: dict[tuple[str, int], int] = {}

    def publish(event: dict):
        now = time.monotonic()
        local_seq = int(event.get("seq") or 0)
        seq = by_segment.get((str(event.get("session_id") or ""), local_seq), local_seq)
        if seq:
            zh = event.get("zh") or ""
            en = event.get("en") or ""
            if zh and seq not in zh_at:
                zh_at[seq] = now
            if en and seq not in en_at:
                en_at[seq] = now
            status = str(event.get("translate_status") or "")
            if status:
                translate_status[seq] = status
            if event.get("version") is not None:
                versions[seq] = int(event.get("version") or 0)
        return original_publish(event)

    bus.publish = publish

    series: list[dict] = []
    client_inflight = {"n": 0}
    stop_sample = asyncio.Event()

    async def sampler(origin: float) -> None:
        # One real second == `scale` wall seconds.
        step = scale
        tick = 0
        while not stop_sample.is_set():
            target = origin + tick * step
            delay = target - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_sample.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass
            stats = app.state.pipeline.stats()
            series.append({
                "t_real_s": round(tick * 1.0, 3),
                "t_wall_s": round(time.monotonic() - origin, 3),
                "pending": stats["pending"],
                "inflight": stats["inflight"],
                "translate_queued": stats["translate_queued"],
                "held": stats["held"],
                "oldest_wait_ms": round(stats["oldest_wait_ms"] / scale, 3),
                "client_inflight": client_inflight["n"],
                "threads": threading.active_count(),
                "rss_bytes": _rss_bytes(),
            })
            tick += 1

    pushes: dict[int, dict] = {}

    async def upload(client, token: str, chunk: dict) -> None:
        client_inflight["n"] += 1
        started = time.monotonic()
        try:
            response = await client.post(
                "/api/push",
                data={
                    "room_id": "bench",
                    "session_id": chunk["session_id"],
                    "seq": str(chunk["seq"]),
                    "t0_ms": str(chunk["t0_ms"]),
                    "t1_ms": str(chunk["t1_ms"]),
                    "wait_translation": "0",
                },
                files={"audio": ("segment.webm", f"SEQ{chunk['global_seq']}".encode("ascii"), "audio/webm")},
                headers={
                    "authorization": f"Bearer {token}",
                    "x-breeze-async-translation": "1",
                },
            )
            body = {}
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:200]}
            pushes[chunk["global_seq"]] = {
                "http": response.status_code,
                "status": body.get("status"),
                "zh": body.get("zh") or "",
                "en": body.get("en") or "",
                "version": body.get("version"),
                "translate_status": body.get("translate_status") or "",
                "wait_wall_s": time.monotonic() - started,
            }
        except Exception as exc:
            pushes[chunk["global_seq"]] = {
                "http": 0,
                "status": "client_error",
                "zh": "",
                "en": "",
                "error": str(exc)[:200],
                "wait_wall_s": time.monotonic() - started,
            }
        finally:
            client_inflight["n"] = max(0, client_inflight["n"] - 1)

    wall_start = time.monotonic()
    import httpx
    bus_history = 0
    store_rows = 0

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8780", timeout=120.0) as client:
            token = app.state.token
            opened = await client.post(
                "/api/rooms/open",
                json={"room_id": "bench"},
                headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            )
            if opened.status_code != 200:
                raise SystemExit(f"open room failed: {opened.status_code} {opened.text[:200]}")
            sample_task = asyncio.create_task(sampler(wall_start))
            recorder = Recorder(args.segments, args.period_s, scale, sessions=args.sessions)
            capture_started = time.monotonic()

            async def do_upload(chunk: dict) -> None:
                by_segment[(chunk["session_id"], chunk["seq"])] = chunk["global_seq"]
                await upload(client, token, chunk)

            async def end_session(session_id: str) -> None:
                await client.post(
                    "/api/session/end",
                    json={"room_id": "bench", "session_id": session_id},
                    headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                )

            await recorder.run(do_upload, on_session_end=end_session)
            capture_wall = time.monotonic() - capture_started
            last_end = max((chunk["end_mono"] for chunk in recorder.chunks), default=time.monotonic())

            drain_deadline = time.monotonic() + max(30.0, args.segments * max(args.tr_ms, args.slow_ms) / 1000.0 * scale + 15.0)

            def unresolved() -> list[int]:
                waiting = []
                for chunk in recorder.chunks:
                    seq = chunk["global_seq"]
                    pushed = pushes.get(seq) or {}
                    if pushed.get("http") not in (200, None) and not pushed.get("zh") and seq not in zh_at:
                        continue
                    if seq in en_at:
                        continue
                    status = translate_status.get(seq) or pushed.get("translate_status") or ""
                    if status in FAIL_TRANSLATE:
                        continue
                    if pushed.get("status") in {"silent", "error", "missing", "timeout", "cancelled", "translate_failed"}:
                        continue
                    waiting.append(seq)
                return waiting

            while time.monotonic() < drain_deadline:
                queued = app.state.pipeline.stats().get("translate_queued") or 0
                if not unresolved() and queued == 0 and client_inflight["n"] == 0:
                    await asyncio.sleep(0.05)
                    if not unresolved() and (app.state.pipeline.stats().get("translate_queued") or 0) == 0:
                        break
                await asyncio.sleep(min(0.05, scale))
            stop_sample.set()
            await sample_task

            srt_payload = ""
            export_status = None
            if args.scenario == "soak" or args.check_srt or data_path:
                exported = await client.get(
                    "/api/export",
                    params={"room_id": "bench", "kind": "srt"},
                    headers={"authorization": f"Bearer {token}"},
                )
                export_status = exported.status_code
                srt_payload = exported.text if exported.status_code == 200 else ""
            # Shutdown closes the writer. Count rows first; main's store also accepts this call.
            bus_history = len(app.state.bus.history("bench"))
            if getattr(app.state, "store", None) is not None and app.state.store.enabled:
                store_rows = len(app.state.store.room_rows("bench"))

    wall_s = time.monotonic() - wall_start
    real = lambda wall: wall / scale

    zh_latency = []
    en_latency = []
    by_seq_zh = {}
    by_seq_en = {}
    for chunk in recorder.chunks:
        seq = chunk["global_seq"]
        if seq in zh_at:
            value = real(zh_at[seq] - chunk["end_mono"])
            zh_latency.append(value)
            by_seq_zh[seq] = value
        if seq in en_at:
            value = real(en_at[seq] - chunk["end_mono"])
            en_latency.append(value)
            by_seq_en[seq] = value

    produced = [seq for seq, row in pushes.items() if row.get("http") == 200 and (row.get("zh") or seq in zh_at)]
    segments_with_text = sorted(seq for seq in produced if (pushes.get(seq) or {}).get("zh") or seq in zh_at)
    # Prefer event zh, which is what listeners stored.
    segments_with_text = sorted(set(segments_with_text) | set(zh_at))

    failure_rows = []
    for seq in segments_with_text:
        status = translate_status.get(seq) or (pushes.get(seq) or {}).get("translate_status") or ""
        failure_rows.append({
            "en": "1" if seq in en_at else "",
            "translate_status": status,
            "status": (pushes.get(seq) or {}).get("status") or "",
        })
    classified = classify_translate_failures(failure_rows)
    failure_counts = {name: classified[name] for name in ("queue_full", "timeout", "error", "skipped")}
    other_failures = classified["other"]
    failure_rate = classified["rate"]

    last_en = max(en_at.values(), default=None)
    catch_up = real(last_en - last_end) if last_en is not None else None

    def deciles(table: dict[int, float]) -> list[dict]:
        seqs = sorted(table)
        if not seqs:
            return []
        buckets = []
        count = len(seqs)
        for index in range(10):
            start = count * index // 10
            stop = count * (index + 1) // 10
            group = [table[seq] for seq in seqs[start:stop]]
            buckets.append({
                "decile": index + 1,
                "n": len(group),
                "seq_from": seqs[start] if group else None,
                "seq_to": seqs[stop - 1] if group else None,
                "p50_s": _round(_percentile(group, 50)),
            })
        return buckets

    def edge_p50(table: dict[int, float]) -> dict:
        seqs = sorted(table)
        if not seqs:
            return {"first10_p50_s": None, "last10_p50_s": None, "delta_s": None, "n_first": 0, "n_last": 0}
        count = max(1, len(seqs) // 10)
        first = [table[seq] for seq in seqs[:count]]
        last = [table[seq] for seq in seqs[-count:]]
        first_p = _percentile(first, 50)
        last_p = _percentile(last, 50)
        delta = None if first_p is None or last_p is None else last_p - first_p
        return {
            "first10_p50_s": _round(first_p),
            "last10_p50_s": _round(last_p),
            "delta_s": _round(delta),
            "n_first": len(first),
            "n_last": len(last),
        }

    def peak(name: str):
        if not series:
            return None
        return max(point[name] for point in series)

    final = series[-1] if series else {}
    observed_rtf = None
    if asr.durations:
        observed_rtf = statistics.mean(asr.durations) / scale / args.period_s

    http_counts: dict[str, int] = {}
    for row in pushes.values():
        key = str(row.get("http"))
        http_counts[key] = http_counts.get(key, 0) + 1

    srt = None
    if srt_payload or args.scenario == "soak" or args.check_srt:
        srt = _check_srt(srt_payload, len(segments_with_text), require_hour=args.scenario == "soak" or args.segments * args.period_s >= 3600)
        srt["export_http"] = export_status
        srt["bus_history_len"] = bus_history
        srt["store_rows"] = store_rows
        srt["history_limit"] = app.state.settings.history_limit
        srt["export_uses"] = "sqlite" if data_path else "memory"

    report = {
        "scenario": args.scenario or None,
        "args": {
            "segments": args.segments,
            "period_s": args.period_s,
            "rtf": args.rtf,
            "tr_ms": args.tr_ms,
            "tr_jitter": args.tr_jitter,
            "slow_from": args.slow_from,
            "slow_to": args.slow_to,
            "slow_ms": args.slow_ms,
            "scale": scale,
            "seed": args.seed,
            "data_path": data_path or None,
            "sessions": args.sessions,
            "no_store": bool(args.no_store),
            "async_opt_in": True,
        },
        "timeouts_wall_s": {
            "translate": translate_timeout_s,
            "asr": asr_timeout_s,
            "gap_wait": gap_wait_s,
        },
        "audio_real_s": args.segments * args.period_s,
        "wall_s": _round(wall_s),
        "capture_wall_s": _round(capture_wall),
        "recording_paused_s": _round(real(recorder.paused_wall)),
        "recording_paused_pct": _round(100.0 * recorder.paused_wall / capture_wall if capture_wall else 0.0, 2),
        "segments_requested": args.segments,
        "segments_produced": len(produced),
        "segments_with_text": len(segments_with_text),
        "segments_with_en": len(en_latency),
        "zh_latency_s": {
            "p50": _round(_percentile(zh_latency, 50)),
            "p95": _round(_percentile(zh_latency, 95)),
            "max": _round(max(zh_latency) if zh_latency else None),
            "n": len(zh_latency),
        },
        "en_latency_s": {
            "p50": _round(_percentile(en_latency, 50)),
            "p95": _round(_percentile(en_latency, 95)),
            "max": _round(max(en_latency) if en_latency else None),
            "n": len(en_latency),
        },
        "translate_failures": {
            **failure_counts,
            "skipped_backlog": classified["skipped_backlog"],
            "other": other_failures,
            "rate": _round(failure_rate, 4),
        },
        "catch_up_s": _round(catch_up),
        "rtf_configured": args.rtf,
        "rtf_observed": _round(observed_rtf, 3),
        "queue_max": {
            "pending": peak("pending"),
            "inflight": peak("inflight"),
            "translate_queued": peak("translate_queued"),
            "held": peak("held"),
            "oldest_wait_ms": peak("oldest_wait_ms"),
            "client_inflight": peak("client_inflight"),
        },
        "queue_final": {
            "pending": final.get("pending"),
            "inflight": final.get("inflight"),
            "translate_queued": final.get("translate_queued"),
            "held": final.get("held"),
            "oldest_wait_ms": final.get("oldest_wait_ms"),
            "client_inflight": final.get("client_inflight"),
        },
        "threads": {"max": peak("threads"), "final": final.get("threads")},
        "rss_bytes": {"max": peak("rss_bytes"), "final": final.get("rss_bytes")},
        "drift": {"zh": edge_p50(by_seq_zh), "en": edge_p50(by_seq_en)},
        "deciles": {"zh": deciles(by_seq_zh), "en": deciles(by_seq_en)},
        "http_counts": http_counts,
        "bus_history_len": bus_history,
        "store_rows": store_rows,
        "asr_calls": asr.calls,
        "translate_calls": translator.calls,
        "unresolved_after_drain": len(unresolved()) if False else None,
        "series": series,
        "srt": srt,
        "slow_real_s": slow_real,
    }
    # unresolved() closed over state; recompute plainly for the report.
    still = []
    for chunk in recorder.chunks:
        seq = chunk["global_seq"]
        if seq in en_at:
            continue
        status = translate_status.get(seq) or (pushes.get(seq) or {}).get("translate_status") or ""
        pushed = pushes.get(seq) or {}
        if status in FAIL_TRANSLATE or pushed.get("status") in {"silent", "error", "missing", "timeout", "cancelled", "translate_failed"}:
            continue
        if pushed.get("http") not in (200, None) and seq not in zh_at:
            continue
        still.append(seq)
    report["unresolved_after_drain"] = still[:20]
    report["unresolved_count"] = len(still)
    return report


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _print_report(report: dict) -> None:
    args = report["args"]
    slow = "off"
    if args["slow_from"]:
        slow = f"{args['slow_from']}-{args['slow_to']} @ {args['slow_ms']:.0f} ms"
    print(f"scenario          {report['scenario'] or '(custom)'}")
    print(
        f"config            segments={args['segments']} period={args['period_s']}s "
        f"rtf={args['rtf']} tr={args['tr_ms']:.0f}ms jitter={args['tr_jitter']} "
        f"slow={slow} scale={args['scale']} seed={args['seed']}"
    )
    print("async opt-in      wait_translation=0 + x-breeze-async-translation: 1")
    print(f"audio             {report['audio_real_s']:.1f} real s    wall {report['wall_s']} s    capture wall {report['capture_wall_s']} s")
    print(f"recording paused  {_fmt(report['recording_paused_s'])} real s   ({_fmt(report['recording_paused_pct'], 2)}% of capture)")
    print(
        f"segments          requested {report['segments_requested']}  produced {report['segments_produced']}  "
        f"with_text {report['segments_with_text']}  with_en {report['segments_with_en']}"
    )
    zh = report["zh_latency_s"]
    en = report["en_latency_s"]
    print(f"zh latency real s p50 {_fmt(zh['p50'])}  p95 {_fmt(zh['p95'])}  max {_fmt(zh['max'])}  n {zh['n']}")
    print(f"en latency real s p50 {_fmt(en['p50'])}  p95 {_fmt(en['p95'])}  max {_fmt(en['max'])}  n {en['n']}")
    fails = report["translate_failures"]
    print(
        f"translate fails   queue_full={fails['queue_full']} timeout={fails['timeout']} "
        f"error={fails['error']} skipped={fails['skipped']} "
        f"skipped_backlog={fails.get('skipped_backlog', 0)} other={fails['other']} rate={fails['rate']}"
    )
    print(f"catch-up          {_fmt(report['catch_up_s'])} real s   (last chunk end → last English; negative means English finished during capture)")
    print(f"rtf               configured {report['rtf_configured']}  observed { _fmt(report['rtf_observed']) }")
    qmax = report["queue_max"]
    qfin = report["queue_final"]
    print(
        "queue max         pending={pending} inflight={inflight} translate_queued={translate_queued} "
        "held={held} oldest_wait_ms={oldest_wait_ms} client_inflight={client_inflight}".format(**qmax)
    )
    print(
        "queue final       pending={pending} inflight={inflight} translate_queued={translate_queued} "
        "held={held} oldest_wait_ms={oldest_wait_ms} client_inflight={client_inflight}".format(**qfin)
    )
    rss = report["rss_bytes"]
    print(
        f"threads           max {report['threads']['max']}  final {report['threads']['final']}    "
        f"rss MiB max {_fmt((rss['max'] or 0) / 1048576, 1)}  final {_fmt((rss['final'] or 0) / 1048576, 1)}"
    )
    drift_zh = report["drift"]["zh"]
    drift_en = report["drift"]["en"]
    print(
        f"drift zh p50      first10% {_fmt(drift_zh['first10_p50_s'])} s  last10% {_fmt(drift_zh['last10_p50_s'])} s  "
        f"delta {_fmt(drift_zh['delta_s'])} s"
    )
    print(
        f"drift en p50      first10% {_fmt(drift_en['first10_p50_s'])} s  last10% {_fmt(drift_en['last10_p50_s'])} s  "
        f"delta {_fmt(drift_en['delta_s'])} s"
    )
    print(f"http              {report['http_counts']}   asr_calls {report['asr_calls']}  translate_calls {report['translate_calls']}")
    print(f"bus history       {report['bus_history_len']}   store rows {report['store_rows']}   unresolved {report['unresolved_count']}")
    if report.get("srt"):
        srt = report["srt"]
        print(
            f"srt export        http {srt.get('export_http')} via {srt.get('export_uses')}  "
            f"cues {srt.get('cues')}  bus_history {srt.get('bus_history_len')}  store_rows {srt.get('store_rows')}  "
            f"history_limit {srt.get('history_limit')}"
        )
        for check in srt["checks"]:
            mark = "PASS" if check["ok"] else "FAIL"
            print(f"  srt {mark:4}  {check['name']}: {check['detail']}")
        print(f"srt overall       {'PASS' if srt['pass'] else 'FAIL'}")
    print("times             all latency, pause, and catch-up figures are real seconds (wall / scale)")
    print("queue series      oldest_wait_ms in the JSON series is real ms (wall ms / scale); one sample per real second")


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Benchmark caption capture backlog and translation lag")
    parser.add_argument("--segments", type=int, default=40)
    parser.add_argument("--period-s", type=float, default=6.0, dest="period_s")
    parser.add_argument("--rtf", type=float, default=0.6)
    parser.add_argument("--tr-ms", type=float, default=2000.0, dest="tr_ms")
    parser.add_argument("--tr-jitter", type=float, default=0.5, dest="tr_jitter")
    parser.add_argument("--slow-from", type=int, default=0, dest="slow_from")
    parser.add_argument("--slow-to", type=int, default=0, dest="slow_to")
    parser.add_argument("--slow-ms", type=float, default=15000.0, dest="slow_ms")
    parser.add_argument("--scale", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--scenario", choices=sorted(PRESETS), default="")
    parser.add_argument("--data-path", default="", dest="data_path")
    parser.add_argument("--no-store", action="store_true", dest="no_store", help="Disable SQLite even if --data-path is set")
    parser.add_argument("--sessions", type=int, default=1, help="Split the run into K sessions in one room; each restarts t0 at 0")
    parser.add_argument("--check-srt", action="store_true", dest="check_srt")
    args = parser.parse_args()
    if args.scenario:
        for key, value in PRESETS[args.scenario].items():
            flag = "--" + key.replace("_", "-")
            if not _flag_given(flag):
                setattr(args, key, value)
    report = asyncio.run(_run(args))
    _print_report(report)
    if args.json_path:
        import json
        destination = Path(args.json_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"json              {destination}")


if __name__ == "__main__":
    main()
