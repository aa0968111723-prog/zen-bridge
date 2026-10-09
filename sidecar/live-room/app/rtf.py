"""Bounded recognition real-time factor (RTF = asr_ms / audio_ms).

A class is about 1000 slices. The recent window keeps 200 samples. Each
(room, session) keeps its own capped series, so two rooms that take turns
do not wipe each other. A new session in the same room replaces only that
room, including a session that has only been silence so far. ASR timeouts
and ASR errors are counted and are not samples.

Percentile summaries are cached per bucket and recomputed only when that
bucket receives a sample. Callers receive a copy, so editing a snapshot
cannot change the cache. Waiting audio, the in-flight recognizer age,
timeouts, and the skip counters are read live so a metrics poll still sees them.

``last_process_ms`` and ``process_ms`` are not stored here. The pipeline's
``last_process_ms`` is wall time from the start of a slice until recognition
returns, including the wait for a free recognizer. ``process_ms`` is decode
plus recognition only.
"""
from __future__ import annotations

import copy
import time
from collections import deque

WINDOW_LIMIT = 200
# Above one 100-minute class (~1000 slices) plus retries, still fixed.
SESSION_LIMIT = 4096
# One live session per room. Well above the default room cap, still fixed.
SESSION_ROOM_CAP = 32


def percentile(values: list[float], p: float) -> float:
    """Linear rank, same shape as the simulation helper. Empty input is refused."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _num(value: float) -> int | float:
    number = round(float(value), 6)
    if number == int(number) and abs(number) < 10**12:
        return int(number)
    return number


def _in_flight_age(seconds: float) -> float:
    """Seconds an in-flight recognizer has run.

    ``round(x, 3)`` of a sub-millisecond age is 0.0, which looks idle. Publish
    microsecond precision and never 0 while recognition is actually running.
    """
    number = round(max(float(seconds), 0.0), 6)
    return number if number > 0 else 0.000001


def _stat(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "max": None}
    return {
        "p50": _num(percentile(values, 0.50)),
        "p95": _num(percentile(values, 0.95)),
        "max": _num(max(values)),
    }


def _column(samples: list[tuple], index: int) -> list[float]:
    if not samples:
        return []
    if len(samples[0]) <= index:
        return [0.0] * len(samples) if index >= 3 else []
    return [row[index] for row in samples]


def summarize(samples: list[tuple], limit: int) -> dict:
    return {
        "count": len(samples),
        "limit": limit,
        "asr_ms": _stat(_column(samples, 0)),
        "audio_ms": _stat(_column(samples, 1)),
        "rtf": _stat(_column(samples, 2)),
        "decode_ms": _stat(_column(samples, 3)),
        "asr_wait_ms": _stat(_column(samples, 4)),
    }


class RtfMeter:
    """Recent-window and per-room session samples, plus two backlog clocks.

    ``backlog_s`` counts from upload (an estimate until the WAVE duration is
    known) and is removed when recognition starts. ``backlog_audio_s`` counts
    only real decoded audio, including the slice currently being recognized,
    and is removed when recognition finishes. Each room is separate.
    """

    def __init__(self) -> None:
        self._window: deque[tuple] = deque(maxlen=WINDOW_LIMIT)
        self._window_rev = 0
        self._window_cache: tuple[int, dict] | None = None
        # Insertion order is least-recently updated first. Touching a session moves it to the end.
        self._sessions: dict[tuple[str, str], deque] = {}
        self._bucket_rev: dict[tuple[str, str], int] = {}
        self._bucket_cache: dict[tuple[str, str], tuple[int, dict]] = {}
        self._timeouts: dict[tuple[str, str], int] = {}
        self._silent: dict[tuple[str, str], int] = {}
        self._empty: dict[tuple[str, str], int] = {}
        self._errors: dict[tuple[str, str], int] = {}
        self._latest: tuple[str, str] | None = None
        # key -> (seconds, estimated). Upload estimate, cleared when ASR starts.
        self._waiting: dict[tuple, tuple[float, bool]] = {}
        # key -> real WAVE seconds. Set after decode, cleared when ASR finishes.
        self._decoded: dict[tuple, float] = {}
        # key -> (perf_counter start, room id) while transcribe is in flight.
        self._asr_active: dict[tuple, tuple[float, str]] = {}
        self._asr_timeouts = 0
        self._silent_skipped = 0
        self._asr_empty = 0
        self._asr_errors = 0
        self._last_rtf: float | None = None

    def note_waiting(self, key: tuple, seconds: float, estimated: bool = False) -> None:
        if seconds and seconds > 0:
            self._waiting[key] = (float(seconds), bool(estimated))

    def clear_waiting(self, key: tuple) -> None:
        self._waiting.pop(key, None)

    def note_decoded(self, key: tuple, seconds: float) -> None:
        """Real WAVE length. Stays until recognition of this segment finishes."""
        if seconds and seconds > 0:
            self._decoded[key] = float(seconds)

    def clear_decoded(self, key: tuple) -> None:
        self._decoded.pop(key, None)

    def note_asr_active(self, key: tuple, room: str) -> None:
        """Mark one segment as inside recognition. Age is computed at read time."""
        self._asr_active[key] = (time.perf_counter(), str(room or ""))

    def clear_asr_active(self, key: tuple) -> None:
        self._asr_active.pop(key, None)

    def note_timeout(self, session: tuple[str, str] | None = None) -> None:
        """Count one ASR timeout. Do not append an RTF sample."""
        self._asr_timeouts += 1
        if session is None:
            return
        self._bucket(session)
        self._timeouts[session] = self._timeouts.get(session, 0) + 1
        self._latest = session

    def note_silent_skip(self, session: tuple[str, str] | None = None) -> None:
        """Silence gate skipped ASR. Not an RTF sample and not an empty result.

        Registering the session evicts that room's previous one, same as a
        timeout, so a silence-only new session cannot keep the old p95.
        """
        self._silent_skipped += 1
        if session is None:
            return
        self._bucket(session)
        self._silent[session] = self._silent.get(session, 0) + 1
        self._latest = session

    def note_empty(self, session: tuple[str, str] | None = None) -> None:
        """ASR returned no words. Not a timeout and not a silence-gate skip."""
        self._asr_empty += 1
        if session is None:
            return
        self._bucket(session)
        self._empty[session] = self._empty.get(session, 0) + 1
        self._latest = session

    def note_error(self, session: tuple[str, str] | None = None) -> None:
        """ASR returned ok=False or raised. Not an RTF sample and not a timeout."""
        self._asr_errors += 1
        if session is None:
            return
        self._bucket(session)
        self._errors[session] = self._errors.get(session, 0) + 1
        self._latest = session

    def _forget_session(self, key: tuple[str, str]) -> None:
        self._sessions.pop(key, None)
        self._timeouts.pop(key, None)
        self._silent.pop(key, None)
        self._empty.pop(key, None)
        self._errors.pop(key, None)
        self._bucket_rev.pop(key, None)
        self._bucket_cache.pop(key, None)

    def drop_room(self, room_id: str) -> None:
        """Forget one ended room's samples. Process-wide counters stay."""
        stale = [key for key in self._sessions if key[0] == room_id]
        for key in stale:
            self._forget_session(key)
        for store in (self._timeouts, self._silent, self._empty, self._errors):
            for key in [key for key in store if key[0] == room_id]:
                store.pop(key, None)
        for store in (self._waiting, self._decoded, self._asr_active):
            stale_keys = [key for key in store if isinstance(key, tuple) and key and key[0] == room_id]
            for key in stale_keys:
                store.pop(key, None)
        if self._latest is not None and self._latest[0] == room_id:
            self._latest = next(reversed(self._sessions), None)

    def record(self, asr_s: float, audio_s: float, session: tuple[str, str] | None = None, decode_s: float = 0.0, wait_s: float = 0.0) -> None:
        self.record_ms(
            int(round(float(asr_s) * 1000)),
            int(round(float(audio_s) * 1000)),
            session,
            decode_ms=int(round(float(decode_s) * 1000)),
            wait_ms=int(round(float(wait_s) * 1000)),
        )

    def record_ms(self, asr_ms: int, audio_ms: int, session: tuple[str, str] | None = None, decode_ms: int = 0, wait_ms: int = 0) -> None:
        if audio_ms <= 0:
            return
        asr_value = max(0, int(asr_ms))
        audio_value = int(audio_ms)
        sample = (
            asr_value,
            audio_value,
            asr_value / audio_value,
            max(0, int(decode_ms)),
            max(0, int(wait_ms)),
        )
        self._window.append(sample)
        self._window_rev += 1
        self._last_rtf = sample[2]
        key = session if session is not None else self._latest
        if key is None:
            key = ("", "")
        self._bucket(key).append(sample)
        self._bucket_rev[key] = self._bucket_rev.get(key, 0) + 1
        self._latest = key

    def _bucket(self, session: tuple[str, str]) -> deque:
        """One live session per room. A different session replaces only that room."""
        room = session[0]
        if room:
            stale = [key for key in self._sessions if key[0] == room and key != session]
            for key in stale:
                self._forget_session(key)
        bucket = self._sessions.pop(session, None)
        if bucket is None:
            bucket = deque(maxlen=SESSION_LIMIT)
        self._sessions[session] = bucket
        while len(self._sessions) > SESSION_ROOM_CAP:
            old = next(iter(self._sessions))
            if old == session:
                break
            self._forget_session(old)
        return bucket

    def _summary_for(self, key: tuple[str, str], rows: deque) -> dict:
        rev = self._bucket_rev.get(key, 0)
        cached = self._bucket_cache.get(key)
        if cached is not None and cached[0] == rev:
            return cached[1]
        samples = list(rows)
        summary = summarize(samples, SESSION_LIMIT)
        summary["recent"] = summarize(samples[-WINDOW_LIMIT:], WINDOW_LIMIT)
        self._bucket_cache[key] = (rev, summary)
        return summary

    def _window_summary(self) -> dict:
        cached = self._window_cache
        if cached is not None and cached[0] == self._window_rev:
            return cached[1]
        summary = summarize(list(self._window), WINDOW_LIMIT)
        self._window_cache = (self._window_rev, summary)
        return summary

    def _backlog(self) -> tuple[float, bool, dict[str, float]]:
        raw: dict[str, float] = {}
        estimated = False
        total = 0.0
        for key, item in self._waiting.items():
            seconds, is_estimate = item
            total += seconds
            estimated = estimated or bool(is_estimate)
            room = key[0] if isinstance(key, tuple) and key else ""
            raw[room] = raw.get(room, 0.0) + seconds
        by_room = {room: round(value, 3) for room, value in raw.items() if value > 0}
        if not self._waiting:
            return 0, False, {}
        return round(total, 3), estimated, by_room

    def _decoded_total(self) -> int | float:
        """Idle matches the historical ``round(sum({}.values()), 3)`` integer 0."""
        if not self._decoded:
            return 0
        return round(sum(self._decoded.values()), 3)

    def _active_age(self) -> tuple[int | float, dict[str, float]]:
        if not self._asr_active:
            return 0, {}
        now = time.perf_counter()
        by_room: dict[str, float] = {}
        longest = 0.0
        for _key, (started, room) in self._asr_active.items():
            elapsed = max(0.0, now - float(started))
            longest = max(longest, elapsed)
            by_room[room] = max(by_room.get(room, 0.0), elapsed)
        published = {room: _in_flight_age(value) for room, value in by_room.items() if value > 0 or room}
        return _in_flight_age(longest), published

    def snapshot(self, session: tuple[str, str] | None = None) -> dict:
        """``rtf.session`` is the requested session, or the one updated most recently.

        ``asr_rtf_p50`` / ``asr_rtf_p95`` are the process-wide recent window.
        Each ``rtf.sessions`` row also has ``recent`` (that session's last 200).
        ``backlog_audio_s`` is decoded audio still queued or being recognized.
        ``backlog_s`` is audio received but not yet inside recognition.
        ``asr_active_s`` is how long the oldest in-flight recognition has run.
        Idle is integer 0; an in-flight age is always positive.
        """
        chosen = session if session is not None else self._latest
        window = self._window_summary()
        sessions = []
        chosen_summary = summarize([], SESSION_LIMIT)
        chosen_summary["recent"] = summarize([], WINDOW_LIMIT)
        for key, rows in self._sessions.items():
            summary = self._summary_for(key, rows)
            if key == chosen:
                chosen_summary = summary
            item = dict(summary)
            item["room_id"] = key[0]
            item["session_id"] = key[1]
            item["asr_timeouts"] = int(self._timeouts.get(key, 0))
            item["silent_skipped"] = int(self._silent.get(key, 0))
            item["asr_empty"] = int(self._empty.get(key, 0))
            item["asr_errors"] = int(self._errors.get(key, 0))
            sessions.append(_published(item))
        if chosen is not None and chosen not in self._sessions:
            chosen_summary = summarize([], SESSION_LIMIT)
            chosen_summary["recent"] = summarize([], WINDOW_LIMIT)
        backlog_s, backlog_estimated, by_room = self._backlog()
        active_s, active_by_room = self._active_age()
        rtf_block = window["rtf"]
        return {
            "backlog_audio_s": self._decoded_total(),
            "backlog_s": backlog_s,
            "backlog_estimated": backlog_estimated,
            "backlog_by_room": by_room,
            "asr_active_s": active_s,
            "asr_active_by_room": active_by_room,
            "asr_timeouts": self._asr_timeouts,
            "silent_skipped": self._silent_skipped,
            "asr_empty": self._asr_empty,
            "asr_errors": self._asr_errors,
            "asr_rtf_last": None if self._last_rtf is None else _num(self._last_rtf),
            "asr_rtf_p50": rtf_block["p50"],
            "asr_rtf_p95": rtf_block["p95"],
            "asr_ms_p50": window["asr_ms"]["p50"],
            "asr_ms_p95": window["asr_ms"]["p95"],
            "asr_wait_ms_p50": window["asr_wait_ms"]["p50"],
            "asr_wait_ms_p95": window["asr_wait_ms"]["p95"],
            "decode_ms_p50": window["decode_ms"]["p50"],
            "decode_ms_p95": window["decode_ms"]["p95"],
            "asr_samples": window["count"],
            "rtf": {
                "window": _published(window),
                "session": _published(chosen_summary),
                "sessions": sessions,
            },
        }


def _published(summary: dict) -> dict:
    """Deep copy so a caller cannot write through into the percentile cache."""
    return copy.deepcopy(summary)
