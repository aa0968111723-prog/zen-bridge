"""Scaled headless stand-in for the host page, the audience socket, and a 100-minute class.

VirtualHost posts the same opt-in the host page sends (form wait_translation=0 and
header x-breeze-async-translation: 1). The default /api/push still waits for English;
tests/test_round2.py covers that and is left unchanged.

Times on the subtitle clock are scheduled (one period per segment, plus time spent
waiting on maxInflight). They are not taken from the wall clock, so a few milliseconds
of asyncio delay cannot stretch a 1000-segment SRT by minutes.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.error
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.server import create_app, rss_bytes
from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, auth, copy_decoder, open_room, stop, token_of

# 0.01 rather than the spec's 0.005: a 6s slice is then 60ms, so a few milliseconds of
# ASGI overhead cannot fill maxInflight and look like the recorder paused. Windows
# uses 0.02: its default timer tick is about 15.6ms, so at 0.01 one virtual second is
# shorter than one clock tick and a push that sleeps through four ticks (ASR thread,
# decode, loop wakeups) already exceeds a 6s slice. Thresholds stay in virtual
# seconds either way; BREEZE_SIM_SCALE overrides the default.
_DEFAULT_SCALE = "0.02" if sys.platform == "win32" else "0.01"
SCALE = float(os.getenv("BREEZE_SIM_SCALE", _DEFAULT_SCALE))
SEGMENTS = int(os.getenv("BREEZE_SIM_SEGMENTS", "1000"))

_RUN_CACHE: dict[tuple, "SimReport"] = {}


def vms(real_s: float) -> float:
    """Real seconds to virtual milliseconds."""
    return real_s / SCALE * 1000


def virtual_s(real_s: float) -> float:
    return real_s / SCALE


@contextmanager
def no_gc_pause():
    """Collect, then disable automatic GC for one measured window.

    A full collection is a stop-the-world pause. The 100-minute class freezes
    and disables GC for the same reason. This shorter window does not freeze.
    The enabled state from before the window is restored on the way out.
    """
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@contextmanager
def watch_full_gc():
    """Record generation-2 collections that start inside the block.

    gc.callbacks runs on 3.11 and 3.13 before each collection. An empty list
    means no full GC. The callback is removed even when the block fails.
    """
    seen: list[int] = []

    def _on_gc(phase, info):
        if phase == "start" and int(info.get("generation", -1)) >= 2:
            seen.append(int(info["generation"]))

    gc.callbacks.append(_on_gc)
    try:
        yield seen
    finally:
        try:
            gc.callbacks.remove(_on_gc)
        except ValueError:
            pass


def vlimit(limit: float) -> float:
    """Spec threshold in virtual seconds.

    Windows is the only platform that gets any slack, and only 20%. Its timer
    tick is about 15.6 ms, which at the Windows sim scale is a large fraction
    of a short limit. A zero-wait check and the SRT end bound do not go through
    here: one Windows 3.11 run booked a 109 ms stall as 5.45 virtual seconds
    of recorder pause (scale 0.02) and the same stretch on the last cue. That
    is a simulator stall while a slot was still held, not a wider spec limit.
    """
    if sys.platform == "win32":
        return limit * 1.2
    return limit


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def sim_settings(**over) -> Settings:
    # stop_flush_s / shutdown_flush_s exist on this branch (short stop and shutdown).
    # The spec's main Settings does not have them. Drop unknown fields so the same
    # suite can run there; on this branch every key below is a real field.
    base = dict(
        allow_testclient=True,
        translate_timeout_s=40 * SCALE,
        gap_wait_s=3 * SCALE,
        heartbeat_s=0.05,
        idle_timeout_s=5,
        # 8 virtual seconds, the product default (BREEZE_STOP_FLUSH=8). B-f1's cap
        # stays vlimit(3.5). A 2v flush does not catch a stop that sits out the
        # window: on the Windows scale that stop was measured under 4.2, so it
        # passed; at 8v the same stop is about 10v and fails. Stricter, not looser.
        stop_flush_s=8 * SCALE,
        shutdown_flush_s=0.2,
    )
    base.update(over)
    fields = getattr(Settings, "__dataclass_fields__", None)
    if fields is not None:
        base = {key: value for key, value in base.items() if key in fields}
    return Settings(**base)


class TextAsr:
    """Audio bytes are the Chinese line. delay_v is virtual seconds."""

    def __init__(self, delay_v: float = 1.5, gate=None, on_start=None):
        self.delay_v = delay_v
        self.gate = gate
        self.on_start = on_start
        self.calls = 0
        self.done_at: list[float] = []
        self.seen: list[str] = []

    def transcribe(self, wav: Path, prompt: str = "") -> AsrResult:
        del prompt
        # The admit slot is still held here. Callers use this to observe pending.
        if self.on_start is not None:
            self.on_start()
        text = wav.read_bytes().decode()
        self.seen.append(text)
        self.calls += 1
        gate = self.gate(text) if callable(self.gate) else self.gate
        if isinstance(gate, dict):
            gate["started"].set()
            if not gate["release"].wait(5):
                raise TimeoutError("ASR gate was not released")
        time.sleep(max(0.0, self.delay_v) * SCALE)
        self.done_at.append(time.monotonic())
        return AsrResult(ok=True, text=text)


def _sleep_cancel(delay_s: float, cancel) -> bool:
    """Sleep delay_s. Return True if cancel fired first."""
    if delay_s <= 0:
        return bool(cancel is not None and cancel.is_set())
    if cancel is None:
        time.sleep(delay_s)
        return False
    return cancel.wait(delay_s)


def _deadline_margin(room_s: float) -> float:
    """Real seconds to finish before the caller's translate deadline.

    Strictest of the three reviews of the Windows 3.12 failure (run
    37580495810): at least 50 ms, at least a tenth of the time still left,
    and at least 20 ms plus three monotonic ticks. A fixed 20 ms early wake
    was eaten by two 15.6 ms ticks (thread wait lands on the next tick,
    asyncio.timeout fires one tick early). The scripted line still occupies
    the worker until this margin before the deadline.
    """
    tick = time.get_clock_info("monotonic").resolution
    return max(0.05, float(room_s) * 0.1, 0.02 + 3 * tick)


class ScriptedTranslator(Translator):
    """plan(zh) -> ("ok", delay_v) | ("raise", exc) | ("block", event)."""

    def __init__(self, plan):
        super().__init__(enabled=True, key="test-key")
        self.plan = plan
        self.started: list[tuple[str, float]] = []
        self.finished: list[tuple[str, float]] = []

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context
        self.calls += 1
        self.started.append((zh, time.monotonic()))
        try:
            action = self.plan(zh) if callable(self.plan) else self.plan
            kind = action[0]
            if kind == "ok":
                delay = float(action[1]) * SCALE
                if deadline is not None:
                    # Finish inside the caller's timeout. A sleep equal to the deadline
                    # races asyncio.wait_for and comes back as "timeout" instead of English.
                    # The margin has to cover a coarse monotonic clock, not a fixed 20 ms.
                    room = deadline - time.monotonic()
                    delay = min(delay, max(0.0, room - _deadline_margin(room)))
                if _sleep_cancel(delay, cancel):
                    return _timeout_result()
                return _ok_result(zh)
            if kind == "raise":
                raise action[1]
            if kind == "block":
                event = action[1]
                while not event.is_set():
                    if cancel is not None and cancel.is_set():
                        return _timeout_result()
                    if deadline is not None and time.monotonic() >= deadline:
                        return _timeout_result()
                    if event.wait(0.01):
                        break
                if cancel is not None and cancel.is_set():
                    return _timeout_result()
                return _ok_result(zh)
            raise RuntimeError(f"unknown plan {kind}")
        finally:
            self.finished.append((zh, time.monotonic()))


def _ok_result(zh: str):
    from app.translate import TranslateResult

    return TranslateResult("EN " + zh, "ok")


def _timeout_result():
    from app.translate import TranslateResult

    return TranslateResult("", "timeout", "英譯逾時，不假設沒有計費。中文仍保留")


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, req, timeout=None):
        del req, timeout
        if not self.responses:
            raise AssertionError("FakeOpener ran out of responses")
        item = self.responses.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return _Body(item)


class HttpPlanTranslator(Translator):
    def __init__(self, responses, slept: list):
        super().__init__(enabled=True, key="test-key", opener=FakeOpener(responses), sleeper=slept.append)


def http_error(code: int, body: bytes = b"", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions",
        code,
        "err",
        headers,
        io.BytesIO(body),
    )


class Listener:
    """Socket plus a pump that records messages and answers ping."""

    def __init__(self, app, room: str, cursor: int = 0):
        query = f"/ws/listen?room_id={room}&cursor={cursor}"
        self.sock = Socket(app, query)
        self.messages: list[dict] = []
        self.closed = False
        self.close_code = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        await self.sock.__aenter__()
        self._task = asyncio.create_task(self._pump())
        return self

    async def _pump(self) -> None:
        while True:
            # A plain get, not wait_for(get(), timeout): on Python 3.11 wait_for can
            # swallow a cancel that lands as the inner get completes, and close()
            # would then wait forever on a pump that keeps looping.
            try:
                msg = await self.sock.out.get()
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            if msg.get("type") == "websocket.send":
                data = json.loads(msg.get("text") or "{}")
                if isinstance(data, dict):
                    data["_recv_mono"] = now
                    self.messages.append(data)
                    if data.get("type") == "ping":
                        await self.sock.inc.put({
                            "type": "websocket.receive",
                            "text": json.dumps({"type": "pong"}),
                        })
                continue
            if msg.get("type") == "websocket.close":
                self.closed = True
                self.close_code = msg.get("code")
                self.messages.append({"type": "websocket.close", "code": msg.get("code"), "_recv_mono": now})
                return

    def captions(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for msg in self.messages:
            ident = msg.get("id")
            if not ident or msg.get("type") in {"ping", "hello", "room_unavailable", "captions_cleared", "websocket.close"}:
                continue
            prev = latest.get(ident)
            if prev is None or int(msg.get("version") or 1) >= int(prev.get("version") or 1):
                latest[ident] = msg
        return list(latest.values())

    async def wait_for(self, pred, timeout_v: float) -> bool:
        # Floor the wait in real time so a 5 ms virtual budget cannot flake the poll,
        # but callers assert on recorded _recv_mono, not on this deadline.
        deadline = time.monotonic() + max(timeout_v * SCALE, 1.0)
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(0.005)
        return bool(pred())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.sock.close()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False


def _body_json(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cached_push_rows(rows: list[dict]) -> list[dict]:
    """Seq and status only. The httpx Response and its body stay out of the cache."""
    return [{"seq": int(row["seq"]), "status": int(row["status"])} for row in rows]


async def post_segment(client, token, room, session, seq, payload: bytes, t0_ms: int, t1_ms: int, *, retry: bool = False):
    """Host-page opt-in: wait_translation=0 and x-breeze-async-translation: 1."""
    headers = {**auth(token), "x-breeze-async-translation": "1"}
    data = {
        "room_id": room,
        "session_id": session,
        "seq": str(seq),
        "t0_ms": str(t0_ms),
        "t1_ms": str(t1_ms),
        "wait_translation": "0",
    }
    if retry:
        data["retry"] = "1"
        headers["x-breeze-retry"] = "1"
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data=data,
        files={"audio": ("a.webm", payload, "audio/webm")},
        headers=headers,
    )


class VirtualHost:
    """recorder_machine.js with the host page's async-translation opt-in."""

    def __init__(
        self, client, token, room, session, scale=None, max_inflight=2, period_v=6.0, retry_429_v=0.8,
        hold_for_retry: bool = False,
    ):
        self.client = client
        self.token = token
        self.room = room
        self.session = session
        self.scale = SCALE if scale is None else scale
        self.max_inflight = max_inflight
        self.period_v = period_v
        self.retry_429_v = retry_429_v
        # host.html retries a 429 inside the upload and keeps recording until
        # maxInflight uploads are out. Only a one-slot queue must hold the
        # recorder for that retry, or the next slice takes the slot.
        self.hold_for_retry = hold_for_retry
        # Cleared only around a stop-the-world sample so that sample is not
        # inside the upload whose latency we measure. Set means slices may start.
        self._release_slice = asyncio.Event()
        self._release_slice.set()
        self.waiting: list[tuple[float, float]] = []
        self.responses: list[dict] = []
        self.retries: list[int] = []
        self.segment_end_mono: dict[int, float] = {}
        self.clock_ms = 0
        self.stop_elapsed_v = None
        self._tasks: set[asyncio.Task] = set()
        self._retry_tasks: set[asyncio.Task] = set()
        self._all: list[asyncio.Task] = []
        self._active_posts = 0
        self.max_posts = 0

    def _active_tasks(self) -> set[asyncio.Task]:
        self._tasks = {task for task in self._tasks if not task.done()}
        return set(self._tasks)

    def hold_new_slices(self) -> None:
        """Park uploads that have not started. Already-running slices are left alone."""
        self._release_slice.clear()

    def release_new_slices(self) -> None:
        self._release_slice.set()

    async def upload_one(self, seq: int, t0_ms: int, t1_ms: int, text: str):
        # Event.wait() returns immediately when the event is set, without yielding.
        # A cleared event parks this slice until the heap sample is done, and
        # segment_end is taken after that so the sample is not latency.
        await self._release_slice.wait()
        payload = text.encode()
        # Same clock as _recv_mono. Do not switch this one to perf_counter.
        self.segment_end_mono[seq] = time.monotonic()
        # perf_counter, not monotonic: Windows 3.11 monotonic steps by ~15.6 ms.
        started = time.perf_counter()
        self._active_posts += 1
        self.max_posts = max(self.max_posts, self._active_posts)
        try:
            resp = await post_segment(self.client, self.token, self.room, self.session, seq, payload, t0_ms, t1_ms)
            if resp.status_code == 429:
                # host.html sleeps 800ms and posts this same upload once more.
                # With max_queue=1 that retry has to wait until the other upload
                # releases the only slot, and the recorder must not start a slice
                # that would take it. A wider queue retries immediately.
                self.retries.append(seq)
                current = asyncio.current_task()
                if current is not None and self.hold_for_retry:
                    self._retry_tasks.add(current)
                try:
                    if self.hold_for_retry:
                        holders = [task for task in self._active_tasks() if task not in self._retry_tasks]
                        if holders:
                            await asyncio.wait(holders)
                    await asyncio.sleep(self.retry_429_v * self.scale)
                    resp = await post_segment(
                        self.client, self.token, self.room, self.session, seq, payload, t0_ms, t1_ms, retry=True,
                    )
                finally:
                    if current is not None:
                        self._retry_tasks.discard(current)
        finally:
            self._active_posts -= 1
        elapsed_v = (time.perf_counter() - started) / self.scale
        self.responses.append({
            "seq": seq,
            "status": resp.status_code,
            "elapsed_v": elapsed_v,
            "body": _body_json(resp),
            "t0_ms": t0_ms,
            "t1_ms": t1_ms,
        })
        return resp

    async def _wait_slot(self) -> None:
        while True:
            active = self._active_tasks()
            if not self._retry_tasks and len(active) < self.max_inflight:
                return
            if not active:
                return
            start_ms = self.clock_ms
            # perf_counter, not monotonic: Windows 3.11 monotonic steps by ~15.6 ms.
            real = time.perf_counter()
            await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            # Book every real pause, including those under one virtual second.
            # An immediate return (a free slot) never reaches this wait.
            # Round-to-zero is the scheduler, not a pause the clock can see.
            spent_ms = int(round((time.perf_counter() - real) / self.scale * 1000))
            if spent_ms > 0:
                self.clock_ms += spent_ms
                self.waiting.append((start_ms / 1000, self.clock_ms / 1000))

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(
        self, count: int, text_of=None, pace: bool = True, on_each=None, drain: bool = True, before_slice=None,
    ) -> None:
        """Record ``count`` slices. drain=False returns once the last slice is handed to
        upload, like pressing stop right after speaking; stop() then settles uploads.
        before_slice(seq) runs before the slot check, outside any measured wait.
        An async hook is awaited; a hook that returns without awaiting does not
        yield, so arming a 429 storm still beats the upload created last slice."""
        text_of = text_of or (lambda i: f"第{i}句")
        for seq in range(1, count + 1):
            if before_slice is not None:
                hooked = before_slice(seq)
                if asyncio.iscoroutine(hooked):
                    await hooked
            await self._wait_slot()
            if pace:
                await asyncio.sleep(self.period_v * self.scale)
                t0 = self.clock_ms
                self.clock_ms += int(round(self.period_v * 1000))
                t1 = self.clock_ms
            else:
                t0 = int(round((seq - 1) * self.period_v * 1000))
                t1 = int(round(seq * self.period_v * 1000))
            text = text_of(seq)
            task = asyncio.create_task(self.upload_one(seq, t0, t1, text))
            self._all.append(task)
            self._track(task)
            if on_each is not None:
                task.add_done_callback(lambda done, seq=seq: on_each(seq))
        if not drain:
            return
        if self._tasks:
            await asyncio.wait(self._tasks)
        for task in self._all:
            task.result()

    async def stop(self):
        """Wait for uploads already started, then POST /api/session/end. No flush=0."""
        # Same clock as the upload wait. monotonic() on Windows 3.11 is one tick wide.
        real = time.perf_counter()
        if self._tasks:
            await asyncio.wait(self._tasks)
        resp = await self.client.post(
            "/api/session/end",
            json={
                "room_id": self.room,
                "session_id": self.session,
                "last_seq": len(self._all),
            },
            headers={**auth(self.token), "content-type": "application/json"},
        )
        self.stop_elapsed_v = (time.perf_counter() - real) / self.scale
        return resp

    def release_upload_results(self) -> None:
        """Drop push rows and finished tasks so their httpx responses can be freed."""
        self.responses.clear()
        self._all.clear()
        self._tasks.clear()
        self._retry_tasks.clear()

    @property
    def waiting_v_total(self) -> float:
        return sum(end - start for start, end in self.waiting)


_CUE_RE = re.compile(
    r"^(\d+)\n(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})\n(.+)$",
    re.S,
)


def _stamp_ms(stamp: str) -> int:
    hours, minutes, rest = stamp.split(":")
    seconds, millis = rest.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def parse_srt(text: str) -> list[tuple[int, int, int, str]]:
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        return []
    blocks = re.split(r"\n[ \t]*\n", body)
    cues = []
    for block in blocks:
        piece = block.strip("\n")
        match = _CUE_RE.fullmatch(piece)
        if not match:
            raise ValueError(f"SRT cue does not match the strict pattern: {piece!r}")
        cues.append((int(match.group(1)), _stamp_ms(match.group(2)), _stamp_ms(match.group(3)), match.group(4)))
    return cues


@dataclass
class SimReport:
    segments: int
    room: str
    session: str
    db_path: str
    srt: str
    export_json: list
    metrics: list[dict] = field(default_factory=list)
    waiting: list[tuple[float, float]] = field(default_factory=list)
    waiting_v_total: float = 0.0
    segment_end_mono: dict[int, float] = field(default_factory=dict)
    zh_ready_mono: dict[int, float] = field(default_factory=dict)
    # seq and status only. Push bodies and httpx Response objects stay out of _RUN_CACHE.
    responses: list[dict] = field(default_factory=list)
    final_metrics: dict = field(default_factory=dict)
    results_at: dict[int, int] = field(default_factory=dict)
    emitted: int = 0
    bus_log: int = 0
    bus_by_room: int = 0
    state_count: int = 0
    missing: int = 0
    silent: int = 0
    tracemalloc_500: int = 0
    tracemalloc_750: int = 0
    tracemalloc_1000: int = 0
    rss_0: int = 0
    rss_250: int = 0
    rss_500: int = 0
    rss_750: int = 0
    rss_1000: int = 0
    pending_peak: int = 0
    storm_rejects: int = 0
    retries: list[int] = field(default_factory=list)
    translate_skipped: int = 0
    # seq, translate_status, has_en. From caption state, not the export file.
    translate_rows: list = field(default_factory=list)
    translate_queued_at_export: int = 0
    translate_busy_at_export: int = 0


def _zh_ready_times(listeners: list[Listener]) -> dict[int, float]:
    found: dict[int, float] = {}
    for listener in listeners:
        for msg in listener.messages:
            if msg.get("status") != "zh_ready" or msg.get("seq") is None:
                continue
            seq = int(msg["seq"])
            mono = float(msg.get("_recv_mono") or 0)
            if seq not in found or mono < found[seq]:
                found[seq] = mono
    return found


def caption_rows(app, room: str) -> list[dict]:
    """This branch's caption_state. The spec's main only keeps the live by_room list."""
    bus = app.state.bus
    state = getattr(bus, "caption_state", None)
    if state is not None:
        return list(state(room))
    return [dict(item) for item in bus.by_room.get(room, [])]


def _translations_idle(pipe) -> bool:
    queued = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
    return pipe._translate_busy <= 0 and queued == 0


async def _wait_translate_idle(pipe, timeout: float = 5.0) -> None:
    """Wait until no English is queued or in a worker.

    The admit slot is already released at Chinese, so this is not a recorder
    pause. Callers that then hold the loop (gc, tracemalloc) need the wait:
    the scaled translate budget is a few hundred real milliseconds, and a
    longer hold stores that line as a timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if _translations_idle(pipe):
            # A deferred queue offer is a call_soon. Let it land, then recheck.
            await asyncio.sleep(0)
            if _translations_idle(pipe):
                return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.005)


async def sample_after_translations(pipe, sample):
    """Run sample() only after in-flight English has landed.

    sample() may stop the loop. On Python 3.11 a tracemalloc walk of this
    process is longer than the scaled 40s translate budget, so a line the
    worker has already started would be published with no English.
    """
    await _wait_translate_idle(pipe)
    return sample()


async def _wait_translations(app, translator, count: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        stats = app.state.pipeline.stats()
        done = len(getattr(translator, "finished", []))
        # _translate_busy is this branch's worker counter. Main has no such attribute;
        # the queue size is enough to know the scripted translator has finished.
        busy = getattr(app.state.pipeline, "_translate_busy", 0)
        # A full translate queue drops the oldest line. That line never calls
        # the translator, so it is not in ``finished``; it still left the queue.
        skipped = int(getattr(app.state.pipeline, "translate_skipped", 0) or 0)
        if done + skipped >= count and stats.get("translate_queued", 0) == 0 and busy <= 0:
            return
        await asyncio.sleep(0.01)


def _seq_of(zh: str) -> int:
    digits = "".join(ch for ch in zh if ch.isdigit())
    return int(digits) if digits else 0


def _app_traced_bytes() -> int:
    """Live bytes allocated from app/. The sim client is not included."""
    if not tracemalloc.is_tracing():
        return 0
    root = (Path(__file__).resolve().parents[1] / "app").resolve()
    total = 0
    for stat in tracemalloc.take_snapshot().statistics("filename"):
        raw = stat.traceback[0].filename
        if not raw or raw.startswith("<"):
            continue
        filename = Path(raw).resolve()
        if filename == root or root in filename.parents:
            total += stat.size
    return total


def _sample_server_memory() -> tuple[int, int]:
    """One collection with GC enabled, then server heap and process RSS.

    The paced run leaves automatic GC off so a collection cannot be booked as
    a recorder pause. This turns it on for the sample only.
    """
    was = gc.isenabled()
    gc.enable()
    gc.collect()
    traced = _app_traced_bytes()
    rss = rss_bytes()
    if not was:
        gc.disable()
    return traced, rss


def _class_plan(zh: str):
    """2s English, except segments 300-330, which take almost the whole 40s budget.

    Slow is not a timeout. The translator finishes one deadline margin early,
    so those lines come back in English unless the queue drops them. A timeout
    in that window is a harness failure, not the backlog the test is measuring.
    """
    seq = _seq_of(zh)
    if 300 <= seq <= 330:
        return ("ok", 40.0)
    return ("ok", 2.0)


async def _run_100min_async(*, trace: bool) -> SimReport:
    room = "class"
    session = "sim100"
    root = Path(tempfile.mkdtemp(prefix="breeze-sim-"))
    db_path = root / "class.sqlite3"
    translator = ScriptedTranslator(_class_plan)
    observed = {"pipe": None, "pending": 0}

    def on_start() -> None:
        pipe = observed["pipe"]
        if pipe is None:
            return
        pending = int(pipe.stats()["pending"])
        if pending > observed["pending"]:
            observed["pending"] = pending

    asr = TextAsr(1.5, on_start=on_start)
    settings = sim_settings(data_path=str(db_path))
    app = create_app(settings, asr=asr, translator=translator, decoder=copy_decoder)
    observed["pipe"] = app.state.pipeline
    # At segment 600 the next 16 admits fail once each. The following admit
    # (the host's single retry) is let through. rejected counts those 429s.
    storm = {"rounds": 0, "let_pass": False, "rejects": 0}
    pipe = app.state.pipeline
    orig_admit = pipe.try_admit_count

    def storm_admit() -> bool:
        if storm["let_pass"]:
            storm["let_pass"] = False
            return orig_admit()
        if storm["rounds"] > 0:
            storm["rounds"] -= 1
            storm["let_pass"] = True
            pipe.rejected += 1
            storm["rejects"] += 1
            return False
        return orig_admit()

    pipe.try_admit_count = storm_admit
    # tracemalloc walks the whole process. On a 100-minute class that walk is
    # the stall, so the latency run leaves it off. The memory run opts in.
    started_trace = False
    if trace and not tracemalloc.is_tracing():
        tracemalloc.start()
        started_trace = True
    # Host, server and both listeners share one process here, and every real
    # millisecond is 1/SCALE virtual milliseconds. A full GC pass is a 30-90 ms
    # stop-the-world pause on a runner (measured on 3.11), which the recorder model
    # would book as a multi-second recorder pause that a browser plus a separate
    # server never see. Freeze what exists before the class, turn automatic GC off
    # for the paced run, and collect at slice boundaries before the slot check,
    # where a pause is not inside any measured wait.
    # A Windows 3.11 run booked 109 real ms (5.45 virtual s at scale 0.02) as
    # waiting and as the same SRT end drift. That is a stall while a slot was
    # still held, not a recorder backlog. Collecting before the slot check keeps
    # the pause out of the measured wait; the zero-wait and ±2s SRT bounds stay exact.
    # The same hold is longer than the scaled translate budget, so the sample
    # runs only after in-flight English has landed (sample_after_translations).
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.freeze()
    gc.disable()
    mem_at: dict[int, tuple[int, int]] = {}

    def collect_between_slices(seq: int):
        # The upload for seq-1 is created at the end of the previous slice and
        # admits on the next yield. Arming here makes that upload the first 429.
        # A plain return does not yield, so the upload cannot admit first.
        if seq == 601:
            storm["rounds"] = 16
        # 250 is an RSS point on the latency run only. The traced run still
        # collects here, but does not snapshot: that walk is the stall.
        rss_points = (250, 500, 750) if not trace else (500, 750)
        if seq not in rss_points and seq % 50 != 0:
            return None
        # The upload created at the end of the previous slice has not run yet.
        # Park it so the sample is not inside its Chinese latency, then let any
        # English already in flight land before the loop is held.

        async def _pause_for_sample() -> None:
            assert host is not None
            host.hold_new_slices()
            try:
                if seq in rss_points:
                    mem_at[seq] = await sample_after_translations(pipe, _sample_server_memory)
                else:
                    await sample_after_translations(pipe, gc.collect)
            finally:
                host.release_new_slices()

        return _pause_for_sample()
    snapshots: list[dict] = []
    listeners: list[Listener] = []
    host: VirtualHost | None = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=30) as client:
            token = await token_of(app, client)
            await open_room(client, token, room)
            for _ in range(2):
                listener = Listener(app, room)
                await listener.__aenter__()
                listeners.append(listener)
            # Warm the push path once in a separate room before the class: first form
            # parse, decode and ASR threads, translate pool, store writer. That one-off
            # cost is tens of real ms on a CI runner (3.11 showed a 2.35 virtual s wait
            # at the very first slice only); a real server is warm long before the first
            # 6 s slice, and the scaled clock would magnify it 1/SCALE times.
            await open_room(client, token, "warmup")
            warm = await post_segment(client, token, "warmup", "warmup", 1, "預熱".encode(), 0, 6000)
            assert warm.status_code == 200, warm.text
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(getattr(translator, "finished", [])) < 1:
                await asyncio.sleep(0.01)
            # Baseline RSS for the latency run, after warmup, before segment 1.
            # The traced run does not sample here: a snapshot walk is the stall.
            if not trace:
                mem_at[0] = await sample_after_translations(pipe, _sample_server_memory)
            host = VirtualHost(client, token, room, session)
            pending_snaps: list[asyncio.Task] = []

            def on_each(seq: int) -> None:
                if seq % 100 != 0:
                    return
                pending_snaps.append(asyncio.create_task(_snapshot(app, client, token, seq, snapshots)))

            await host.run(SEGMENTS, pace=True, on_each=on_each, before_slice=collect_between_slices)
            if pending_snaps:
                await asyncio.gather(*pending_snaps)
            await _snapshot(app, client, token, SEGMENTS, snapshots)
            # Settle English before the end sample. The sample blocks this thread
            # on a collection and a tracemalloc snapshot; doing that while the last
            # line is still queued trips the translate timeout (40s virtual).
            await _wait_translations(app, translator, SEGMENTS + 1)
            mem_at[1000] = await sample_after_translations(app.state.pipeline, _sample_server_memory)
            if gc_was_enabled:
                gc.enable()
            # flush is this branch's async store. Main writes each row before publish returns.
            flush = getattr(app.state.store, "flush", None)
            if flush is not None:
                await asyncio.to_thread(flush)
            # Sample the queue before export. A line still in flight would be
            # missing English in the file without a finished status yet.
            pipe = app.state.pipeline
            queued_at_export = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
            busy_at_export = int(pipe._translate_busy)
            srt_resp = await client.get("/api/export", params={"room_id": room, "kind": "srt"}, headers=auth(token))
            json_resp = await client.get("/api/export", params={"room_id": room, "kind": "json"}, headers=auth(token))
            assert srt_resp.status_code == 200, srt_resp.text
            assert json_resp.status_code == 200, json_resp.text
            final = (await client.get("/api/metrics", headers=auth(token))).json()
            state = caption_rows(app, room)
            translate_rows = [
                {
                    "seq": int(row.get("seq") or 0),
                    "translate_status": str(row.get("translate_status") or ""),
                    "has_en": bool(row.get("en")),
                }
                for row in state
            ]
            pipe = app.state.pipeline
            _, rss_0 = mem_at.get(0, (0, 0))
            _, rss_250 = mem_at.get(250, (0, 0))
            traced_500, rss_500 = mem_at.get(500, (0, 0))
            traced_750, rss_750 = mem_at.get(750, (0, 0))
            traced_1000, rss_1000 = mem_at.get(1000, (0, 0))
            report = SimReport(
                segments=SEGMENTS,
                room=room,
                session=session,
                db_path=str(db_path),
                srt=srt_resp.text,
                export_json=json_resp.json(),
                metrics=list(snapshots),
                waiting=list(host.waiting),
                waiting_v_total=host.waiting_v_total,
                segment_end_mono=dict(host.segment_end_mono),
                zh_ready_mono=_zh_ready_times(listeners),
                responses=_cached_push_rows(host.responses),
                final_metrics=final,
                results_at={int(item["seq"]): int(item["results"]) for item in snapshots},
                emitted=sum(1 for key in pipe._emitted_segs if key[0] == room),  # the warm-up room is not the class
                bus_log=len(app.state.bus._log.get(room, [])),
                bus_by_room=len(app.state.bus.by_room.get(room, [])),
                state_count=len(state),
                missing=sum(1 for row in state if row.get("status") == "missing"),
                silent=sum(1 for row in state if row.get("status") == "silent"),
                tracemalloc_500=traced_500,
                tracemalloc_750=traced_750,
                tracemalloc_1000=traced_1000,
                rss_0=rss_0,
                rss_250=rss_250,
                rss_500=rss_500,
                rss_750=rss_750,
                rss_1000=rss_1000,
                pending_peak=int(observed["pending"]),
                storm_rejects=int(storm["rejects"]),
                retries=list(host.retries),
                translate_skipped=int(getattr(pipe, "translate_skipped", 0) or 0),
                translate_rows=translate_rows,
                translate_queued_at_export=queued_at_export,
                translate_busy_at_export=busy_at_export,
            )
            await host.stop()
    finally:
        for listener in listeners:
            await listener.close()
        await stop(app)
        if started_trace and tracemalloc.is_tracing():
            tracemalloc.stop()
        if gc_was_enabled:
            gc.enable()
        if host is not None:
            host.release_upload_results()
        # The report already copied the fields tests read. Drop the class,
        # including upload tasks and their httpx responses, before collecting.
        host = None
        app = None
        translator = None
        asr = None
        pipe = None
        observed = None
        orig_admit = None
        storm_admit = None
        collect_between_slices = None
        on_start = None
        on_each = None
        listeners = []
        client = None
        warm = None
        srt_resp = None
        json_resp = None
        final = None
        state = None
        snapshots = None
        pending_snaps = None
        gc.unfreeze()
        # Outside the paced class. Frees what the report did not keep and
        # resets gen2 before the next test's measured window.
        gc.collect()
    return report


async def _snapshot(app, client, token, seq: int, snapshots: list[dict]) -> None:
    resp = await client.get("/api/metrics", headers=auth(token))
    data = resp.json()
    current = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
    data = dict(data)
    data["seq"] = seq
    data["traced"] = current
    data["results"] = len(app.state.pipeline.results)
    data["log"] = len(app.state.bus._log.get("class", []))
    data["by_room"] = len(app.state.bus.by_room.get("class", []))
    snapshots.append(data)


def run_100min(*, trace: bool = False) -> SimReport:
    """One paced 100-minute class per process. Latency callers leave tracing off.

    trace=True is the memory run: same scale and the same class, plus a heap
    snapshot. It is not the report latency tests read.
    """
    key = (SEGMENTS, SCALE, "100min-v3", bool(trace))
    cached = _RUN_CACHE.get(key)
    if cached is not None:
        return cached
    report = asyncio.run(_run_100min_async(trace=bool(trace)))
    _RUN_CACHE[key] = report
    return report


@asynccontextmanager
async def serving(settings=None, asr=None, translator=None, decoder=None):
    app = create_app(
        settings or sim_settings(),
        asr=asr or TextAsr(0),
        translator=translator if translator is not None else Translator(enabled=False),
        decoder=decoder or copy_decoder,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=30) as client:
        token = await token_of(app, client)
        try:
            yield app, client, token
        finally:
            await stop(app)


def latencies(report: SimReport) -> list[float]:
    values = []
    for seq, end in report.segment_end_mono.items():
        recv = report.zh_ready_mono.get(seq)
        if recv is None:
            continue
        values.append((recv - end) / SCALE)
    return values


async def export_json(client, token, room: str):
    resp = await client.get("/api/export", params={"room_id": room, "kind": "json"}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def export_srt(client, token, room: str) -> str:
    resp = await client.get("/api/export", params={"room_id": room, "kind": "srt"}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.text
