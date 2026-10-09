from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from app.aio import cancellation_pending, wait_bounded
from app.asr import AsrResult
from app.audio import AudioError, wav_duration_seconds, wav_rms
from app.settings import Settings
from app.textutil import annotate_question
from app.translate import TranslateResult, Translator

FAILURES = {"error", "missing", "timeout", "cancelled"}
TERMINAL = FAILURES | {"ready", "translate_failed", "silent", "zh_ready"}


class PipelineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Segment:
    room_id: str
    session_id: str
    seq: int
    zh: str = ""
    zh_raw: str = ""
    en: str = ""
    status: str = "queued"
    translate_status: str = ""
    error: str = ""
    version: int = 1
    session_ord: int = 0
    t0_ms: int | None = None
    t1_ms: int | None = None
    received_at: float = field(default_factory=time.time)
    cursor: int = 0
    translate_queued: bool = False
    room_gen: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.room_id, self.session_id, self.seq)

    @property
    def id(self) -> str:
        return f"{self.room_id}:{self.session_id}:{self.seq}"

    def public(self) -> dict:
        return {
            "type": "final" if self.status in {"ready", "silent"} else "update",
            "id": self.id,
            "room_id": self.room_id,
            "session_id": self.session_id,
            "session_ord": self.session_ord,
            "seq": self.seq,
            "version": self.version,
            "zh": self.zh,
            "zh_raw": self.zh_raw,
            "en": self.en,
            "status": self.status,
            "translate_status": self.translate_status,
            "error": self.error,
            "cursor": self.cursor,
            "t0_ms": self.t0_ms,
            "t1_ms": self.t1_ms,
        }


class Pipeline:
    def __init__(self, asr, translator: Translator, prompt: str, tmp: Path, settings: Settings | None = None, on_event=None):
        self.asr = asr
        self.translator = translator
        self.prompt = prompt
        self.tmp = tmp
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings()
        self.on_event = on_event
        self._asr_slots = asyncio.Semaphore(self.settings.asr_workers)
        self.results: dict[tuple[str, str, int], Segment] = {}
        self._hashes: dict[tuple[str, str, int], str] = {}
        self._waiters: dict[tuple[str, str, int], list[asyncio.Future]] = {}
        self.events: list[dict] = []
        self.broadcasts = self.events
        self._held: dict[tuple[str, str], dict[int, Segment]] = {}
        self._next: dict[tuple[str, str], int] = {}
        self._max_seq: dict[tuple[str, str], int] = {}
        self._gap_since: dict[tuple[str, str, int], float] = {}
        self._active: set[tuple[str, str, int]] = set()
        self._closed: set[tuple[str, str]] = set()
        self._session_ord: dict[tuple[str, str], int] = {}
        self._room_sessions: dict[str, int] = {}
        self._seen_versions: set[tuple] = set()
        self._seen_order: deque = deque()
        self._emitted_segs: set[tuple[str, str, int]] = set()
        self._emit_waiters: dict[tuple[str, str, int], list[asyncio.Future]] = {}
        self._slots = 0
        self._bytes = 0
        self._reserved: set[tuple[str, str, int]] = set()
        self._flight: dict[tuple[str, str, int], asyncio.Future] = {}
        self._cancel: set[tuple[str, str, int]] = set()
        self._room_gen: dict[str, int] = {}
        self._index: dict[tuple[str, str, int], dict] = {}
        self._sealed: dict[str, set[str]] = {}
        self._braced: dict[str, set[str]] = {}
        self._braced_dropped: dict[str, dict] = {}
        self._muted: set[str] = set()
        self._mute_backlog: dict[str, list[Segment]] = {}
        self.inflight = 0
        self.rejected = 0
        self.missing_count = 0
        self.oldest_wait_started: float | None = None
        self.last_process_s: float | None = None
        self._translate_q: asyncio.Queue | None = None
        self._translate_pool: ThreadPoolExecutor | None = None
        self._tr_epoch: dict[tuple[str, str, int], int] = {}
        self._version_floor: dict[tuple[str, str, int], int] = {}
        self._seeded: set[str] = set()
        self._flushing: set[tuple[str, str]] = set()
        self.translate_skipped = 0
        self._translate_busy = 0
        self._recent_zh: dict[tuple[str, str], deque] = {}
        self._tasks: list[asyncio.Task] = []
        self._workers = False
        self.glossary: dict[tuple[str, str], list[dict]] = {}

    def get(self, room_id: str, session_id: str, seq: int) -> Segment | None:
        return self.results.get((room_id, session_id, seq))

    def try_admit(self, limit: int | None = None) -> bool:
        return self.try_admit_count() if limit is None else self._admit_with_limit(limit)

    def _admit_with_limit(self, limit: int) -> bool:
        if self._slots >= limit:
            self.rejected += 1
            return False
        self._take_slot()
        return True

    def try_admit_count(self) -> bool:
        if self._slots >= self.settings.max_queue:
            self.rejected += 1
            return False
        if self._bytes >= self.settings.max_inflight_bytes:
            self.rejected += 1
            return False
        self._take_slot()
        return True

    def _take_slot(self) -> None:
        self._slots += 1
        self.inflight = self._slots
        if self.oldest_wait_started is None:
            self.oldest_wait_started = time.monotonic()

    def release_admit(self) -> None:
        self.release_slot()

    def release_slot(self, nbytes: int = 0) -> None:
        self._slots = max(0, self._slots - 1)
        if nbytes:
            self._bytes = max(0, self._bytes - nbytes)
        self.inflight = self._slots
        if self._slots == 0:
            self.oldest_wait_started = None

    def joinable_without_slot(self, key: tuple[str, str, int]) -> bool:
        flight = self._flight.get(key)
        if flight is not None and not flight.done():
            return True
        return key in self._active or key in self._reserved or key in self.results

    def note_reserved(self, key: tuple[str, str, int]) -> None:
        self._reserved.add(key)

    def clear_reserved(self, key: tuple[str, str, int]) -> None:
        self._reserved.discard(key)

    def stats(self) -> dict:
        oldest = 0
        if self._slots and self.oldest_wait_started is not None:
            oldest = int((time.monotonic() - self.oldest_wait_started) * 1000)
        return {
            "pending": self._slots,
            "inflight": len(self._active),
            "oldest_wait_ms": oldest,
            "last_process_ms": None if self.last_process_s is None else int(self.last_process_s * 1000),
            "rejected": self.rejected,
            "missing": self.missing_count,
            "held": sum(len(rows) for rows in self._held.values()),
            "results": len(self.results),
            "translate_queued": 0 if self._translate_q is None else self._translate_q.qsize(),
            "translate_skipped": self.translate_skipped,
        }

    def ensure_workers(self) -> None:
        if self._workers:
            return
        self._workers = True
        workers = max(1, int(self.settings.translate_workers))
        # The configured queue is the bound. When it is full the oldest waiting
        # line is skipped so the newest speech still gets English.
        backlog = max(1, int(self.settings.translate_queue))
        self._translate_q = asyncio.Queue(maxsize=backlog)
        # Own pool: translation must not occupy the default executor that decode and ASR share.
        self._translate_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="breeze-translate")
        for _ in range(workers):
            self._tasks.append(asyncio.create_task(self._translate_loop()))
        self._tasks.append(asyncio.create_task(self._gap_loop()))

    async def aclose(self) -> None:
        # Let queued translations finish, but never wait past the shutdown budget.
        # A translator that ignores cancel can still outlive this; the pool is
        # then shut down without waiting on it.
        deadline = time.monotonic() + max(0.0, float(getattr(self.settings, "shutdown_flush_s", 2.0)))
        while time.monotonic() < deadline:
            queued = self._translate_q is not None and not self._translate_q.empty()
            if self._translate_busy <= 0 and not queued:
                break
            await asyncio.sleep(0.01)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._workers = False
        pool = self._translate_pool
        self._translate_pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        for waiters in list(self._waiters.values()):
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._waiters.clear()
        for waiters in list(self._emit_waiters.values()):
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._emit_waiters.clear()
        for fut in list(self._flight.values()):
            if not fut.done():
                fut.cancel()
        self._flight.clear()

    def drop_room(self, room_id: str) -> None:
        """Forget one ended room. A later host open uses a new generation, so late jobs cannot refill it."""
        self._room_gen[room_id] = self._room_gen.get(room_id, 1) + 1
        keys = {key for key in self.results if key[0] == room_id}
        keys.update(key for key in self._flight if key[0] == room_id)
        keys.update(key for key in self._active if key[0] == room_id)
        keys.update(key for key in self._reserved if key[0] == room_id)
        keys.update(key for key in self._hashes if key[0] == room_id)
        keys.update(key for key in self._waiters if key[0] == room_id)
        keys.update(key for key in self._emit_waiters if key[0] == room_id)
        keys.update(key for key in self._emitted_segs if key[0] == room_id)
        keys.update(key for key in self._cancel if key[0] == room_id)
        for key in keys:
            self.results.pop(key, None)
            self._hashes.pop(key, None)
            self._active.discard(key)
            self._reserved.discard(key)
            self._emitted_segs.discard(key)
            self._cancel.discard(key)
            for fut in self._waiters.pop(key, []):
                if not fut.done():
                    fut.cancel()
            for fut in self._emit_waiters.pop(key, []):
                if not fut.done():
                    fut.cancel()
            flight = self._flight.pop(key, None)
            if flight is not None and not flight.done():
                flight.cancel()
        groups = {group for group in self._held if group[0] == room_id}
        groups.update(group for group in self._next if group[0] == room_id)
        groups.update(group for group in self._max_seq if group[0] == room_id)
        groups.update(group for group in self._closed if group[0] == room_id)
        groups.update(group for group in self._session_ord if group[0] == room_id)
        groups.update(group for group in self.glossary if group[0] == room_id)
        for group in groups:
            self._held.pop(group, None)
            self._next.pop(group, None)
            self._max_seq.pop(group, None)
            self._closed.discard(group)
            self._session_ord.pop(group, None)
            self.glossary.pop(group, None)
        self._room_sessions.pop(room_id, None)
        for stamp in [stamp for stamp in self._gap_since if stamp[0] == room_id]:
            self._gap_since.pop(stamp, None)
        for key in [key for key in self._tr_epoch if key[0] == room_id]:
            self._tr_epoch.pop(key, None)
        self._flushing = {group for group in self._flushing if group[0] != room_id}
        for group in [group for group in self._recent_zh if group[0] == room_id]:
            self._recent_zh.pop(group, None)
        for key in [key for key in self._index if key[0] == room_id]:
            self._index.pop(key, None)
        self._sealed.pop(room_id, None)
        self._braced.pop(room_id, None)
        self._muted.discard(room_id)
        self._mute_backlog.pop(room_id, None)
        prefix = room_id + ":"
        for ident in [ident for ident in self._braced_dropped if ident.startswith(prefix)]:
            self._braced_dropped.pop(ident, None)

    def note_retained_order(self, room_id: str, rows: list[dict]) -> None:
        """Reseed session ordinals after a close that kept the room's captions.

        drop_room forgets the counter. The room stays hydrated, so the next
        session would start at 1 and sort beside the retained lines.
        """
        max_ord = 0
        for row in rows or []:
            session_id = str(row.get("session_id") or "")
            try:
                session_ord = int(row.get("session_ord") or 0)
            except (TypeError, ValueError):
                continue
            if not session_id or session_ord < 1:
                continue
            key = (room_id, session_id)
            self._session_ord[key] = max(int(self._session_ord.get(key, 0)), session_ord)
            max_ord = max(max_ord, session_ord)
        if max_ord:
            self._room_sessions[room_id] = max(int(self._room_sessions.get(room_id, 0)), max_ord)

    def _ord(self, room_id: str, session_id: str) -> int:
        key = (room_id, session_id)
        if key not in self._session_ord:
            count = self._room_sessions.get(room_id, 0) + 1
            self._room_sessions[room_id] = count
            self._session_ord[key] = count
        return self._session_ord[key]

    def _stamp_gen(self, segment: Segment) -> None:
        if not segment.room_gen:
            segment.room_gen = self._room_gen.setdefault(segment.room_id, 1)

    def _stale(self, segment: Segment) -> bool:
        # Generation is stamped once. drop_room bumps it; a late job must not adopt the new one.
        if not segment.room_gen:
            return False
        return segment.room_gen != self._room_gen.get(segment.room_id)

    def _note(self, segment: Segment) -> None:
        group = (segment.room_id, segment.session_id)
        self._stamp_gen(segment)
        if self._stale(segment):
            return
        segment.session_ord = self._ord(segment.room_id, segment.session_id)
        self._max_seq[group] = max(self._max_seq.get(group, 0), segment.seq)

    def _mark_emitted(self, segment: Segment) -> None:
        self._emitted_segs.add(segment.key)
        self._release_emit_waiters(segment.key)

    def _release_emit_waiters(self, key: tuple[str, str, int]) -> None:
        """Unblock a push waiting to publish. Does not count the caption as emitted."""
        for fut in self._emit_waiters.pop(key, []):
            if not fut.done():
                fut.set_result(True)

    def _discard_emit_waiter(self, key: tuple[str, str, int], fut: asyncio.Future) -> None:
        waiters = self._emit_waiters.get(key)
        if not waiters:
            return
        remaining = [item for item in waiters if item is not fut]
        if remaining:
            self._emit_waiters[key] = remaining
        else:
            self._emit_waiters.pop(key, None)

    def _emit_wait_s(self) -> float:
        """Long enough for a gap fill and a session flush. Not long enough to hang a push."""
        return max(0.0, float(self.settings.gap_wait_s)) + max(0.0, float(self.settings.stop_flush_s)) + 5.0

    def _result_wait_s(self) -> float:
        """Bound for a push waiting on decode, recognition, ordered emit, or English."""
        settings = self.settings
        return (
            self._emit_wait_s()
            + max(0.0, float(settings.decode_timeout_s))
            + max(0.0, float(settings.asr_timeout_s))
            + max(0.0, float(settings.translate_timeout_s)) * 2
            + 5.0
        )

    async def _wait_emitted(self, segment: Segment) -> None:
        if segment.key in self._emitted_segs:
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._emit_waiters.setdefault(segment.key, []).append(fut)
        try:
            await wait_bounded(fut, self._emit_wait_s())
        except asyncio.TimeoutError:
            self._discard_emit_waiter(segment.key, fut)
        except asyncio.CancelledError:
            self._discard_emit_waiter(segment.key, fut)
            raise

    def _voided(self, segment: Segment) -> bool:
        return segment.id in self._sealed.get(segment.room_id, ())

    def _abandon(self, segment: Segment) -> Segment:
        segment.zh = ""
        segment.en = ""
        segment.zh_raw = ""
        segment.translate_queued = False
        segment.status = "cancelled"
        segment.translate_status = ""
        segment.error = "已清除"
        return segment

    def _remember_index(self, segment: Segment) -> None:
        if self._stale(segment) or self._voided(segment):
            return
        self._index[segment.key] = {
            "hash": self._hashes.get(segment.key),
            "version": segment.version,
            "status": segment.status,
            "zh": segment.zh,
            "zh_raw": segment.zh_raw,
            "en": segment.en,
            "t0_ms": segment.t0_ms,
            "t1_ms": segment.t1_ms,
            "session_ord": segment.session_ord,
            "error": segment.error,
            "translate_status": segment.translate_status,
            "room_gen": segment.room_gen,
        }
        self._trim_index(segment.room_id)

    def _trim_index(self, room_id: str) -> None:
        cap = max(1, int(getattr(self.settings, "room_caption_cap", 5000)))
        keys = [key for key in self._index if key[0] == room_id]
        if len(keys) <= cap:
            return
        keys.sort(key=lambda key: (int(self._index[key].get("session_ord") or 0), key[1], key[2]))
        for key in keys[: len(keys) - cap]:
            self._index.pop(key, None)
            self._hashes.pop(key, None)
            # Same cap as the compact index. The set used to live until drop_room,
            # so a long class kept one tuple per segment forever.
            self._emitted_segs.discard(key)

    def _rehydrate(self, key: tuple[str, str, int]) -> Segment | None:
        row = self._index.get(key)
        if not row:
            return None
        segment = Segment(
            room_id=key[0],
            session_id=key[1],
            seq=key[2],
            zh=row.get("zh") or "",
            zh_raw=row.get("zh_raw") or "",
            en=row.get("en") or "",
            status=row.get("status") or "ready",
            translate_status=row.get("translate_status") or "",
            error=row.get("error") or "",
            version=int(row.get("version") or 1),
            session_ord=int(row.get("session_ord") or 0),
            t0_ms=row.get("t0_ms"),
            t1_ms=row.get("t1_ms"),
            room_gen=int(row.get("room_gen") or 0) or self._room_gen.get(key[0], 1),
        )
        if row.get("hash"):
            self._hashes.setdefault(key, row["hash"])
        self.results[key] = segment
        return segment

    def invalidate_room(self, room_id: str) -> None:
        """Drop this room's captions. The live session keeps its seq counter and can continue."""
        self._room_gen[room_id] = self._room_gen.get(room_id, 1) + 1
        sealed = self._sealed.setdefault(room_id, set())
        for (rid, sid), max_seq in list(self._max_seq.items()):
            if rid != room_id:
                continue
            group = (rid, sid)
            self._held.pop(group, None)
            self._next[group] = max(self._next.get(group, 1), int(max_seq) + 1)
            for seq in range(1, int(max_seq) + 1):
                sealed.add(f"{rid}:{sid}:{seq}")
                self._gap_since.pop((rid, sid, seq), None)
        for key in list(self._flight):
            if key[0] == room_id:
                sealed.add(f"{key[0]}:{key[1]}:{key[2]}")
        for key in list(self.results):
            if key[0] == room_id:
                sealed.add(f"{key[0]}:{key[1]}:{key[2]}")
        for key in list(self._index):
            if key[0] == room_id:
                sealed.add(f"{key[0]}:{key[1]}:{key[2]}")
                self._index.pop(key, None)
        for key in [key for key in self._hashes if key[0] == room_id]:
            if f"{key[0]}:{key[1]}:{key[2]}" in sealed:
                self._hashes.pop(key, None)
        for key in [key for key in self.results if key[0] == room_id]:
            segment = self.results.pop(key)
            self._emitted_segs.discard(key)
            self._abandon(segment)
            self._wake(key, segment)
        for key in [key for key in self._waiters if key[0] == room_id]:
            blank = Segment(
                room_id=key[0], session_id=key[1], seq=key[2], status="cancelled", error="已清除",
                room_gen=self._room_gen.get(room_id, 0),
            )
            self._wake(key, blank)
        for group in [group for group in self._held if group[0] == room_id]:
            self._held.pop(group, None)
        for key in [key for key in self._tr_epoch if key[0] == room_id]:
            self._tr_epoch[key] = self._tr_epoch.get(key, 0) + 1
        # A single-caption delete still in flight must not unseal what this room delete sealed.
        self._braced.pop(room_id, None)
        self._mute_backlog.pop(room_id, None)
        prefix = room_id + ":"
        for ident in [ident for ident in self._braced_dropped if ident.startswith(prefix)]:
            self._braced_dropped.pop(ident, None)
        for key in [key for key in self._emit_waiters if key[0] == room_id]:
            self._release_emit_waiters(key)

    def caption_known(self, room_id: str, session_id: str, seq: int) -> bool:
        """True when this seq is still in pipeline state. A sealed id that was removed is not known."""
        key = (room_id, session_id, seq)
        if key in self.results or key in self._index or key in self._active or key in self._reserved:
            return True
        flight = self._flight.get(key)
        if flight is not None and not flight.done():
            return True
        held = self._held.get((room_id, session_id))
        return bool(held and seq in held)

    def forget_expired(self, room_id: str, session_id: str, seq: int) -> None:
        """Drop one caption that aged out. Do not seal it; that seq can be uploaded again."""
        key = (room_id, session_id, seq)
        ident = f"{room_id}:{session_id}:{seq}"
        if ident in self._sealed.get(room_id, ()):
            return
        if key in self._active:
            return
        flight = self._flight.get(key)
        if flight is not None and not flight.done():
            return
        self._tr_epoch[key] = self._tr_epoch.get(key, 0) + 1
        self.results.pop(key, None)
        self._index.pop(key, None)
        self._hashes.pop(key, None)
        self._emitted_segs.discard(key)
        held = self._held.get((room_id, session_id))
        if held is not None:
            held.pop(seq, None)

    def mute_room(self, room_id: str) -> None:
        """Hold publishes while a room delete is waiting on the store."""
        self._muted.add(room_id)

    def unmute_room(self, room_id: str, *, abort: bool = False) -> None:
        """End the mute. A failed delete republishes captions finalized during the window."""
        self._muted.discard(room_id)
        backlog = self._mute_backlog.pop(room_id, [])
        if not abort:
            return
        for segment in backlog:
            try:
                self._emit(segment)
            except Exception:
                logging.getLogger("breeze.pipeline").exception("muted caption replay failed")
                self._release_emit_waiters(segment.key)

    def _park_muted(self, segment: Segment) -> None:
        rows = self._mute_backlog.setdefault(segment.room_id, [])
        for index, item in enumerate(rows):
            if item.key == segment.key:
                rows[index] = segment
                return
        rows.append(segment)

    def brace_delete(self, room_id: str, session_id: str, seq: int) -> None:
        """Stop a later emit from saving this id before the store delete settles.

        The caption stays in memory. abort_delete undoes a seal this call added.
        A sealed id is already void, so this does not bump the translation epoch.
        """
        ident = f"{room_id}:{session_id}:{seq}"
        already = ident in self._sealed.get(room_id, ())
        self._sealed.setdefault(room_id, set()).add(ident)
        if not already:
            self._braced.setdefault(room_id, set()).add(ident)

    def abort_delete(self, room_id: str, session_id: str, seq: int) -> None:
        ident = f"{room_id}:{session_id}:{seq}"
        key = (room_id, session_id, seq)
        braced = self._braced.get(room_id)
        if braced is None or ident not in braced:
            return
        braced.discard(ident)
        sealed = self._sealed.get(room_id)
        if sealed is not None:
            sealed.discard(ident)
        dropped = self._braced_dropped.pop(ident, None) or {}
        segment = self.results.get(key)
        if segment is None and key in self._index:
            segment = self._rehydrate(key)
        result = dropped.get("result")
        if (
            result is not None
            and segment is not None
            and segment.status == "zh_ready"
            and segment.zh == dropped.get("zh")
            and not self._stale(segment)
        ):
            self._finish_translation(segment, result, None, segment.zh)
            self._wake(key, self.results.get(key, segment))
            return
        if (
            dropped.get("requeue")
            and segment is not None
            and segment.status == "zh_ready"
            and not self._stale(segment)
        ):
            self.ensure_workers()
            if self._translate_q is not None:
                self._put_translation(segment)
            return
        emit_seg = dropped.get("emit")
        if emit_seg is not None:
            self._emit(emit_seg)
        if segment is not None and not segment.translate_queued:
            self._wake(key, self.results.get(key, segment))

    def delete_segment(self, room_id: str, session_id: str, seq: int) -> None:
        key = (room_id, session_id, seq)
        ident = f"{room_id}:{session_id}:{seq}"
        braced = self._braced.get(room_id)
        if braced is not None:
            braced.discard(ident)
        self._sealed.setdefault(room_id, set()).add(ident)
        self._braced_dropped.pop(ident, None)
        self._tr_epoch[key] = self._tr_epoch.get(key, 0) + 1
        self._index.pop(key, None)
        self._hashes.pop(key, None)
        self._emitted_segs.discard(key)
        held = self._held.get((room_id, session_id))
        if held is not None:
            held.pop(seq, None)
        segment = self.results.pop(key, None)
        if segment is None:
            segment = Segment(room_id=room_id, session_id=session_id, seq=seq, status="cancelled", error="已刪除")
        else:
            self._abandon(segment)
        self._wake(key, segment)
        if self._next.get((room_id, session_id), 1) == seq:
            self._next[(room_id, session_id)] = seq + 1
            self._gap_since.pop((room_id, session_id, seq), None)
            self._drain((room_id, session_id))
        self._release_emit_waiters(key)

    def _emit(self, segment: Segment) -> None:
        if segment.room_id in self._muted and not self._stale(segment) and not self._voided(segment):
            # _drain already advanced _next. Keep the caption and publish it if the delete fails.
            self._park_muted(segment)
            return
        if self._voided(segment) and segment.id in self._braced.get(segment.room_id, ()) and not self._stale(segment):
            # The seal is temporary. Remember the version so a 503 can publish it.
            self._braced_dropped.setdefault(segment.id, {})["emit"] = segment
            return
        if self._stale(segment) or self._voided(segment):
            # Not published, so it must not count as emitted. The waiting push still has to return.
            self._release_emit_waiters(segment.key)
            return
        marker = (*segment.key, segment.version)
        if marker in self._seen_versions:
            self._mark_emitted(segment)
            return
        self._remember_zh(segment)
        self._seen_versions.add(marker)
        self._seen_order.append(marker)
        while len(self._seen_order) > self.settings.max_results * 4:
            self._seen_versions.discard(self._seen_order.popleft())
        event = {
            "type": "caption",
            "id": segment.id,
            "room_id": segment.room_id,
            "session_id": segment.session_id,
            "session_ord": segment.session_ord,
            "seq": segment.seq,
            "version": segment.version,
            "zh": segment.zh,
            "zh_raw": segment.zh_raw,
            "en": segment.en,
            "status": segment.status,
            "translate_status": segment.translate_status,
            "error": segment.error,
            "t0_ms": segment.t0_ms,
            "t1_ms": segment.t1_ms,
        }
        self.events.append(dict(event))
        if len(self.events) > self.settings.history_limit * 2:
            del self.events[: len(self.events) - self.settings.history_limit * 2]
        self._mark_emitted(segment)
        self._remember_index(segment)
        if segment.status == "zh_ready" and not segment.translate_queued:
            self._queue_translate(segment)
        if self.on_event:
            try:
                delivered = self.on_event(dict(event))
            except Exception:
                logging.getLogger("breeze.pipeline").exception("caption listener failed")
                delivered = None
            if isinstance(delivered, dict) and delivered.get("cursor"):
                segment.cursor = int(delivered["cursor"])
                event["cursor"] = segment.cursor
                indexed = self._index.get(segment.key)
                if indexed is not None and int(indexed.get("version") or 0) == segment.version:
                    indexed["cursor"] = segment.cursor

    def _skip_void(self, segment: Segment) -> None:
        """A deleted or stale seq must not pin the ordered release of later ones."""
        group = (segment.room_id, segment.session_id)
        held = self._held.get(group)
        if held is not None:
            held.pop(segment.seq, None)
        if self._next.get(group, 1) != segment.seq:
            return
        self._next[group] = segment.seq + 1
        self._gap_since.pop((*group, segment.seq), None)
        self._drain(group)

    def _release(self, segment: Segment) -> None:
        self._note(segment)
        if self._stale(segment) or self._voided(segment):
            self._skip_void(segment)
            self._wake(segment.key, segment)
            return
        group = (segment.room_id, segment.session_id)
        self.results[segment.key] = segment
        nxt = self._next.get(group, 1)
        if segment.seq < nxt:
            # Already ordered. Retry must broadcast this version now; parking it in
            # _held would never drain, and zh_ready would wait for English forever.
            held = self._held.get(group)
            if held is not None:
                held.pop(segment.seq, None)
            self._emit(segment)
            return
        held = self._held.setdefault(group, {})
        held[segment.seq] = segment
        self._drain(group)
        self._force_held_cap(group)

    def _drain(self, group: tuple[str, str]) -> None:
        held = self._held.setdefault(group, {})
        nxt = self._next.get(group, 1)
        while nxt in held:
            item = held.pop(nxt)
            nxt += 1
            self._next[group] = nxt
            self._gap_since.pop((*group, nxt), None)
            try:
                self._emit(item)
            except Exception:
                logging.getLogger("breeze.pipeline").exception("caption emit failed")

    def _force_held_cap(self, group: tuple[str, str]) -> None:
        held = self._held.get(group, {})
        guard = 0
        while len(held) > self.settings.max_held and guard < self.settings.max_held + 2:
            guard += 1
            nxt = self._next.get(group, 1)
            if nxt in held:
                self._drain(group)
                continue
            self.mark_missing(group[0], group[1], nxt, "缺段堆積已達上限，先跳過這個序號")
            held = self._held.get(group, {})

    def mark_missing(self, room_id: str, session_id: str, seq: int, reason: str) -> Segment:
        key = (room_id, session_id, seq)
        if f"{room_id}:{session_id}:{seq}" in self._sealed.get(room_id, ()):
            if self._next.get((room_id, session_id), 1) == seq:
                self._next[(room_id, session_id)] = seq + 1
                self._gap_since.pop((room_id, session_id, seq), None)
                self._drain((room_id, session_id))
            existing = self.results.get(key)
            return existing if existing is not None else Segment(
                room_id=room_id, session_id=session_id, seq=seq, status="cancelled", error="已刪除",
            )
        existing = self.results.get(key)
        if key in self._active:
            return existing if existing is not None else Segment(
                room_id=room_id, session_id=session_id, seq=seq, status="transcribing"
            )
        if existing and existing.status not in {"queued"} and key not in self._held.get((room_id, session_id), {}):
            self._drain((room_id, session_id))
            return existing
        if existing and existing.status not in {"queued", "decoding", "transcribing"} and key not in self._active:
            if existing.seq in self._held.get((room_id, session_id), {}):
                self._drain((room_id, session_id))
            return existing
        segment = Segment(
            room_id=room_id,
            session_id=session_id,
            seq=seq,
            status="missing",
            error=reason,
            version=1,
        )
        self.results[key] = segment
        self.missing_count += 1
        self._active.discard(key)
        self._release(segment)
        self._wake(key, segment)
        return segment

    def _session_busy(self, group: tuple[str, str]) -> bool:
        room_id, session_id = group
        for key in self._active:
            if key[0] == room_id and key[1] == session_id:
                return True
        for key, flight in self._flight.items():
            if key[0] == room_id and key[1] == session_id and not flight.done():
                return True
        for key in self._reserved:
            if key[0] == room_id and key[1] == session_id:
                return True
        for key, segment in self.results.items():
            if key[0] == room_id and key[1] == session_id and segment.translate_queued:
                return True
        if self._translate_q is not None:
            for item in list(self._translate_q._queue):
                segment = item[2]
                if segment.room_id == room_id and segment.session_id == session_id:
                    return True
        return False

    def _fill_session_holes(self, room_id: str, session_id: str) -> None:
        """Mark seqs that never arrived. Leave in-flight and already-saved seqs alone."""
        group = (room_id, session_id)
        max_seq = self._max_seq.get(group, 0)
        seq = 1
        while seq <= max_seq:
            key = (room_id, session_id, seq)
            if key in self._active or key in self._reserved:
                seq += 1
                continue
            flight = self._flight.get(key)
            if flight is not None and not flight.done():
                seq += 1
                continue
            current = self.results.get(key)
            if current is None and key in self._index:
                current = self._rehydrate(key)
            if current is None or current.status == "queued":
                self.mark_missing(room_id, session_id, seq, "會話結束，這段沒有收到")
            seq += 1
        self._drain(group)

    def _seq_settled(self, key: tuple[str, str, int]) -> bool:
        """True once this seq has arrived and left decoding.

        English may still be queued. The host already opted out of waiting for
        it on upload, and stop should not sit out the flush window for a translation.
        """
        if key in self._active or key in self._reserved:
            return False
        flight = self._flight.get(key)
        if flight is not None and not flight.done():
            return False
        current = self.results.get(key)
        if current is None and key in self._index:
            current = self._rehydrate(key)
        if current is None or current.status in {"queued", "decoding", "transcribing"}:
            return False
        return True

    def _through_seq_settled(self, group: tuple[str, str], last_seq: int) -> bool:
        if last_seq <= 0:
            return not self._session_busy(group)
        room_id, session_id = group
        for seq in range(1, last_seq + 1):
            if not self._seq_settled((room_id, session_id, seq)):
                return False
        return True

    async def end_session(
        self,
        room_id: str,
        session_id: str,
        *,
        flush_s: float | None = None,
        last_seq: int | None = None,
    ) -> None:
        """Finish in-flight audio, then close the session.

        Chunks are admitted until this returns. With ``last_seq`` (the host
        page sends the last seq it produced) the wait ends as soon as every
        seq up to that number has arrived and settled, or the flush deadline
        passes. Without it, old clients stay open for the whole flush window
        unless audio or English was already in flight — that work is finished
        and the call returns, so a stop is not pinned to the deadline. Holes
        are filled only after admission closes.
        """
        group = (room_id, session_id)
        if group in self._closed and group not in self._flushing:
            return
        timeout = self.settings.stop_flush_s if flush_s is None else max(0.0, float(flush_s))
        expected: int | None
        if last_seq is None:
            expected = None
        else:
            try:
                expected = int(last_seq)
            except (TypeError, ValueError):
                expected = None
            if expected is not None and expected < 0:
                expected = None
        deadline = time.monotonic() + timeout
        # In-flight work at the start of stop (a translation already running)
        # is flushed, then the call returns. Sitting out the rest of the window
        # would fail callers that bound the stop by stop_flush_s. If nothing is
        # in flight, hold the whole window: the next chunk may still be on the
        # wire, and closing after the old 0.2s grace answered it 409.
        busy_at_start = self._session_busy(group)
        self._flushing.add(group)
        try:
            while time.monotonic() < deadline:
                if expected is not None:
                    if self._through_seq_settled(group, expected):
                        break
                elif busy_at_start and not self._session_busy(group):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.02, remaining))
        finally:
            self._flushing.discard(group)
            self._closed.add(group)
            self._fill_session_holes(room_id, session_id)

    def fail_received(self, segment: Segment, detail: str, status: str = "error") -> Segment:
        segment.status = status
        segment.error = detail[:180]
        segment.version = max(segment.version, 1)
        self._active.discard(segment.key)
        self._release(segment)
        self._wake(segment.key, segment)
        return segment

    def request_cancel(self, room_id: str, session_id: str, seq: int) -> Segment:
        key = (room_id, session_id, seq)
        current = self.results.get(key)
        if current and current.status in {"ready", "silent", "translate_failed", "zh_ready"} and key not in self._active:
            raise PipelineError(409, "這段已經送出")
        self._cancel.add(key)
        if key in self._active:
            return current or Segment(room_id=room_id, session_id=session_id, seq=seq, status="transcribing")
        return self.fail_received(
            Segment(room_id=room_id, session_id=session_id, seq=seq),
            "主持端取消這段",
            status="cancelled",
        )

    async def submit(self, segment: Segment, audio: bytes, decoder, *, slot_held: bool, retry: bool = False, owner: bool = False, wait_translation: bool = True) -> Segment:
        del owner  # Admission is synchronous; the flight future replaces the old owner spin.
        self.ensure_workers()
        if segment.id in self._sealed.get(segment.room_id, ()):
            if slot_held:
                self.release_slot()
            raise PipelineError(409, "這段已刪除")
        group = (segment.room_id, segment.session_id)
        if group in self._closed and group not in self._flushing:
            if slot_held:
                self.release_slot()
            raise PipelineError(409, "這個會話已結束")
        digest = hashlib.sha256(audio).hexdigest()
        # No await before the flight is registered, so two tasks cannot both become the owner.
        inflight = self._flight.get(segment.key)
        if inflight is not None and not inflight.done():
            stored = self._hashes.get(segment.key)
            if stored and stored != digest:
                if slot_held:
                    self.release_slot()
                raise PipelineError(409, "同一段的內容不同，已拒絕替換")
            if slot_held:
                self.release_slot()
            return await inflight
        existing = self.results.get(segment.key)
        if existing is None and segment.key in self._index:
            existing = self._rehydrate(segment.key)
        stored = self._hashes.get(segment.key)
        replace_missing = (
            group in self._flushing
            and existing is not None
            and existing.status == "missing"
            and segment.key not in self._active
        )
        if existing and stored == digest:
            reprocess = (retry or replace_missing) and existing.status in FAILURES and segment.key not in self._active
            if not reprocess:
                if slot_held:
                    self.release_slot()
                return await self._wait_result(existing, wait_translation=wait_translation)
            segment.version = max(existing.version + 1, 1)
        elif existing and stored != digest:
            # Restored rows have no audio hash. Returning the saved caption
            # keeps a restart from rejecting the same seq or rewriting it.
            salvage = (retry or replace_missing) and existing.status in FAILURES and segment.key not in self._active
            if stored is None and not salvage:
                if slot_held:
                    self.release_slot()
                floor = self._version_floor.get(segment.key, 0)
                if floor and existing.version < floor:
                    existing.version = floor
                return await self._wait_result(existing, wait_translation=wait_translation)
            if not salvage:
                if slot_held:
                    self.release_slot()
                raise PipelineError(409, "同一段的內容不同，已拒絕替換")
            segment.version = max(existing.version + 1, self._version_floor.get(segment.key, 0) + 1, 1)
        if (retry or replace_missing) and (existing is None or (existing.status in FAILURES and segment.key not in self._active)):
            self._cancel.discard(segment.key)
        floor = self._version_floor.get(segment.key, 0)
        if floor and segment.version <= floor and (existing is None or segment.version != existing.version):
            segment.version = max(segment.version, floor + 1)
        if not slot_held:
            if not self.try_admit_count():
                raise PipelineError(429, "辨識佇列已滿，請稍後再送")
            slot_held = True
        if self._bytes + len(audio) > self.settings.max_inflight_bytes:
            self.release_slot()
            self.rejected += 1
            raise PipelineError(429, "在途音訊太多，請稍後再送")
        self._bytes += len(audio)
        self._hashes[segment.key] = digest
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._flight[segment.key] = fut
        holder = {"held": slot_held, "bytes": len(audio), "wait_translation": wait_translation}
        try:
            result = await self._process(segment, audio, decoder, holder)
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if holder["held"]:
                self.release_slot(holder["bytes"])
                holder["held"] = False
            if not fut.done():
                if isinstance(exc, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(exc)
                    fut.exception()
            raise
        finally:
            if self._flight.get(segment.key) is fut:
                self._flight.pop(segment.key, None)

    def _future(self, segment: Segment) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if segment.room_gen:
            setattr(fut, "room_gen", segment.room_gen)
        return fut

    def _result_ready(self, segment: Segment, *, wait_translation: bool = True) -> bool:
        if self._stale(segment):
            return True
        if segment.status in TERMINAL and segment.status != "zh_ready":
            return True
        if segment.status != "zh_ready" or segment.key not in self._emitted_segs:
            return False
        # Async opt-in returns as soon as ordered Chinese is published.
        return (not wait_translation) or (not segment.translate_queued)

    async def _wait_result(self, segment: Segment, *, wait_translation: bool = True) -> Segment:
        if self._result_ready(segment, wait_translation=wait_translation):
            return segment
        if not wait_translation:
            try:
                if segment.key not in self._emitted_segs:
                    await self._wait_emitted(segment)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            current = self.results.get(segment.key)
            current = current if current is not None else segment
            if self._result_ready(current, wait_translation=False):
                return current
            if self._stale(current) or current.status in TERMINAL:
                return current
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        try:
            return await wait_bounded(fut, self._result_wait_s())
        except asyncio.TimeoutError:
            self._discard_waiter(segment.key, fut)
            if not fut.done():
                fut.cancel()
            current = self.results.get(segment.key)
            return current if current is not None else segment
        except asyncio.CancelledError:
            self._discard_waiter(segment.key, fut)
            raise

    def _discard_waiter(self, key: tuple[str, str, int], fut: asyncio.Future) -> None:
        waiters = self._waiters.get(key)
        if not waiters:
            return
        remaining = [item for item in waiters if item is not fut]
        if remaining:
            self._waiters[key] = remaining
        else:
            self._waiters.pop(key, None)

    def _wake(self, key: tuple[str, str, int], segment: Segment) -> None:
        gen = segment.room_gen
        matched: list[asyncio.Future] = []
        keep: list[asyncio.Future] = []
        for fut in self._waiters.pop(key, []):
            fut_gen = getattr(fut, "room_gen", None)
            if fut_gen is not None and gen and fut_gen != gen:
                keep.append(fut)
            else:
                matched.append(fut)
        if keep:
            self._waiters[key] = keep
        for fut in matched:
            if not fut.done():
                fut.set_result(segment)

    def _release_holder(self, holder: dict) -> None:
        if holder["held"]:
            self.release_slot(holder["bytes"])
            holder["held"] = False

    async def _process(self, segment: Segment, audio: bytes, decoder, holder: dict) -> Segment:
        self._note(segment)
        if not self._stale(segment):
            self.results[segment.key] = segment
            self._active.add(segment.key)
        self._reserved.discard(segment.key)
        work = self.tmp / uuid.uuid4().hex
        started = time.monotonic()
        try:
            segment.status = "decoding"
            try:
                wav = await wait_bounded(
                    asyncio.to_thread(self._decode_sync, work, audio, decoder),
                    timeout=self.settings.decode_timeout_s,
                )
            except asyncio.TimeoutError:
                self.fail_received(segment, "轉檔逾時", status="timeout")
                return segment
            except AudioError as exc:
                self.fail_received(segment, exc.detail, status="error")
                raise
            except Exception as exc:
                self.fail_received(segment, str(exc)[:180] or "解碼失敗", status="error")
                return segment
            if segment.key in self._cancel:
                self.fail_received(segment, "主持端取消這段", status="cancelled")
                return segment
            seconds = wav_duration_seconds(wav)
            if seconds is not None and seconds > self.settings.max_audio_seconds:
                self.fail_received(segment, f"音訊長於 {self.settings.max_audio_seconds} 秒，已拒絕")
                raise AudioError(413, segment.error)
            rms = wav_rms(wav)
            if (
                self.settings.silence_rms > 0
                and rms is not None
                and rms < self.settings.silence_rms
                and (seconds or 0) >= 0.3
            ):
                segment.status = "silent"
                segment.error = "這段太安靜，沒有送去辨識"
                segment.zh = ""
                self._release(segment)
                return segment
            segment.status = "transcribing"
            try:
                async with self._asr_slots:
                    asr: AsrResult = await wait_bounded(
                        asyncio.to_thread(self.asr.transcribe, wav, self.prompt),
                        timeout=self.settings.asr_timeout_s,
                    )
            except asyncio.TimeoutError:
                self.fail_received(segment, "辨識逾時", status="timeout")
                return segment
            except Exception as exc:
                self.fail_received(segment, str(exc)[:180] or "辨識失敗", status="error")
                return segment
            self.last_process_s = time.monotonic() - started
            if segment.key in self._cancel:
                self.fail_received(segment, "主持端取消這段", status="cancelled")
                return segment
            text = (asr.text or "").strip()
            if not asr.ok or not text:
                if asr.ok or not asr.error:
                    segment.status = "silent"
                    segment.error = asr.error or "這段沒聽到話"
                    segment.zh_raw = text
                    segment.zh = ""
                    self._release(segment)
                    return segment
                segment.status = "error"
                segment.error = asr.error or "辨識失敗"
                segment.zh_raw = text
                segment.zh = ""
                self._release(segment)
                return segment
            segment.zh_raw = text
            segment.zh = annotate_question(text)
            segment.status = "zh_ready"
            # Retry sets version to max(existing + 1, 1). Keep it; do not hard-reset to 1.
            segment.version = max(segment.version, 1)
            segment.error = ""
            self._release(segment)
            if self._stale(segment):
                return segment
        finally:
            self._active.discard(segment.key)
            self._release_holder(holder)
            shutil.rmtree(work, ignore_errors=True)
            if segment.status not in TERMINAL:
                if self._stale(segment):
                    self._wake(segment.key, segment)
                else:
                    task = asyncio.current_task()
                    cancelling = task is not None and task.cancelling()
                    self.fail_received(
                        segment,
                        "主持端取消這段" if cancelling else "辨識中斷",
                        status="cancelled" if cancelling else "error",
                    )
            elif segment.status != "zh_ready":
                self._wake(segment.key, segment)
            self._trim_results()
            if self._stale(segment) or self._voided(segment):
                self._abandon(segment)
        if segment.status != "zh_ready" or self._stale(segment) or self._voided(segment):
            return segment
        if not holder.get("wait_translation", True):
            try:
                if segment.key not in self._emitted_segs:
                    await self._wait_emitted(segment)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                if self._stale(segment):
                    return segment
                raise
            if self._stale(segment) or self._voided(segment):
                return self._abandon(segment)
            return self.results.get(segment.key, segment)
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        try:
            if segment.key not in self._emitted_segs:
                await self._wait_emitted(segment)
            if self._stale(segment) or self._voided(segment) or not segment.translate_queued:
                self._discard_waiter(segment.key, fut)
                if self._stale(segment) or self._voided(segment):
                    return self._abandon(segment)
                return segment
            if not fut.done():
                await wait_bounded(fut, self._result_wait_s())
        except asyncio.TimeoutError:
            self._discard_waiter(segment.key, fut)
            if not fut.done():
                fut.cancel()
            if self._stale(segment) or self._voided(segment):
                return self._abandon(segment)
            return self.results.get(segment.key, segment)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if self._stale(segment) or self._voided(segment):
                return self._abandon(segment)
            raise
        if self._stale(segment) or self._voided(segment):
            return self._abandon(segment)
        return self.results.get(segment.key, segment)

    def _epoch_current(self, segment: Segment, epoch: int) -> bool:
        return (
            not self._stale(segment)
            and not self._voided(segment)
            and self._tr_epoch.get(segment.key) == epoch
        )

    def _drop_queued(self, key: tuple[str, str, int]) -> None:
        """Remove a waiting attempt for this segment. Do not count it as skipped."""
        queue = self._translate_q
        if queue is None:
            return
        pending = queue._queue
        kept: deque = deque()
        removed = 0
        while pending:
            item = pending.popleft()
            if item[2].key == key:
                removed += 1
            else:
                kept.append(item)
        pending.extend(kept)
        for _ in range(removed):
            queue.task_done()

    def _put_translation(self, segment: Segment) -> None:
        assert self._translate_q is not None
        self._drop_queued(segment.key)
        epoch = self._tr_epoch.get(segment.key, 0) + 1
        self._tr_epoch[segment.key] = epoch
        item = (epoch, time.monotonic(), segment)
        # Epoch travels with the queue item. The segment object is shared, so a
        # later retranslate must not change which attempt a worker already holds.
        segment.translate_queued = True
        self._offer_translation(item)

    def _offer_translation(self, item: tuple, deferred: bool = False) -> None:
        """Queue one attempt. A full queue drops the oldest waiting line, not the new one.

        Idle workers are woken with call_soon, so a burst can see a full queue before
        they take a slot. Drop only on the deferred attempt, after that turn.
        """
        assert self._translate_q is not None
        epoch, _enqueued_at, segment = item
        if self._tr_epoch.get(segment.key) != epoch:
            return
        try:
            self._translate_q.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        loop = asyncio.get_running_loop()
        if not deferred:
            loop.call_soon(partial(self._offer_translation, item, True))
            return
        try:
            old_epoch, _old_at, old = self._translate_q.get_nowait()
        except asyncio.QueueEmpty:
            self.translate_skipped += 1
            loop.call_soon(self._skip_backlog, segment, epoch)
            return
        self._translate_q.task_done()
        self.translate_skipped += 1
        loop.call_soon(self._skip_backlog, old, old_epoch)
        try:
            self._translate_q.put_nowait(item)
        except asyncio.QueueFull:
            self.translate_skipped += 1
            loop.call_soon(self._skip_backlog, segment, epoch)

    def _skip_backlog(self, segment: Segment, epoch: int) -> None:
        """Oldest queued line lost its slot. Publish that once, after the outer emit."""
        if self._tr_epoch.get(segment.key) != epoch:
            return
        if self._stale(segment) or self._voided(segment) or segment.key not in self._emitted_segs:
            segment.translate_queued = False
            self._wake(segment.key, segment)
            return
        if segment.status != "zh_ready":
            self._wake(segment.key, segment)
            return
        self._fail_translation(segment, "skipped_backlog", "英譯積壓，略過較舊的段落，中文仍保留")
        self._wake(segment.key, segment)

    def _fail_translation(self, segment: Segment, translate_status: str, error: str) -> None:
        if self._stale(segment) or segment.key not in self._emitted_segs:
            segment.translate_queued = False
            return
        segment.translate_queued = False
        segment.en = ""
        segment.translate_status = translate_status
        segment.error = error
        segment.status = "translate_failed"
        segment.version += 1
        self.results[segment.key] = segment
        self._emit(segment)

    def _queue_translate(self, segment: Segment) -> None:
        if self._stale(segment) or self._voided(segment):
            return
        self.ensure_workers()
        self._put_translation(segment)

    def _decode_sync(self, work: Path, audio: bytes, decoder):
        work.mkdir(parents=True, exist_ok=True)
        src = work / "in.bin"
        src.write_bytes(audio)
        return decoder(src, work)

    async def _enqueue_translation(self, segment: Segment) -> None:
        if self._stale(segment) or self._voided(segment):
            return
        if segment.key not in self._emitted_segs:
            await self._wait_emitted(segment)
        if self._stale(segment) or self._voided(segment) or segment.key not in self._emitted_segs:
            return
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        self._put_translation(segment)
        try:
            await wait_bounded(fut, timeout=max(0.1, float(self.settings.translate_timeout_s) + 1.0))
        except asyncio.TimeoutError:
            self._discard_waiter(segment.key, fut)
            if segment.translate_queued and segment.status == "zh_ready":
                segment.translate_queued = False
                self._fail_translation(segment, "timeout", "英譯逾時，不假設沒有計費。中文仍保留")
                self._wake(segment.key, segment)

    async def _translate_loop(self) -> None:
        assert self._translate_q is not None
        while True:
            if cancellation_pending():
                raise asyncio.CancelledError()
            try:
                epoch, enqueued_at, segment = await self._translate_q.get()
            except asyncio.CancelledError:
                raise
            self._translate_busy += 1
            try:
                try:
                    await self._apply_translation(segment, epoch, enqueued_at)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger("breeze.pipeline").exception("translation worker failed")
                    try:
                        if self._epoch_current(segment, epoch) and segment.key in self._emitted_segs:
                            self._fail_translation(segment, "error", "英譯失敗，中文仍保留")
                    except Exception:
                        logging.getLogger("breeze.pipeline").exception("translation failure could not be published")
                try:
                    if self._epoch_current(segment, epoch):
                        self._wake(segment.key, segment)
                except Exception:
                    logging.getLogger("breeze.pipeline").exception("translation waiter wake failed")
            finally:
                self._translate_busy = max(0, self._translate_busy - 1)
                self._translate_q.task_done()

    def _suppress_attempt(self, segment: Segment, epoch: int, result: TranslateResult | None) -> None:
        """Remember work hidden by a temporary seal. A real delete or a newer epoch owns the flag."""
        if self._stale(segment):
            return
        if segment.id not in self._braced.get(segment.room_id, ()):
            return
        if self._tr_epoch.get(segment.key) != epoch:
            return
        slot = self._braced_dropped.setdefault(segment.id, {})
        if result is not None:
            slot["result"] = result
            slot["zh"] = segment.zh
            slot.pop("requeue", None)
            return
        if "result" not in slot:
            slot["requeue"] = True

    def _finish_translation(
        self,
        segment: Segment,
        translated: TranslateResult,
        epoch: int | None,
        zh_snapshot: str | None,
    ) -> bool:
        """Publish one finished attempt. Return False when this attempt must not land.

        Pass epoch=None only after abort_delete has lifted the seal.
        """
        if epoch is not None and not self._epoch_current(segment, epoch):
            # A newer attempt owns translate_queued. Do not clear it.
            # A brace is not a newer attempt: keep the result for abort_delete.
            self._suppress_attempt(segment, epoch, translated)
            return False
        if zh_snapshot is not None and segment.zh != zh_snapshot:
            segment.translate_queued = False
            return False
        segment.en = translated.text or ""
        segment.translate_status = translated.status
        segment.error = translated.detail
        segment.status = "ready" if translated.status in {"ok", "off", "no_key"} else "translate_failed"
        segment.translate_queued = False
        segment.version = segment.version + 1
        if epoch is not None and not self._epoch_current(segment, epoch):
            segment.en = ""
            return False
        if zh_snapshot is not None and segment.zh != zh_snapshot:
            segment.en = ""
            segment.translate_queued = False
            return False
        self.results[segment.key] = segment
        self._emit(segment)
        return True

    async def _apply_translation(self, segment: Segment, epoch: int, enqueued_at: float) -> None:
        if not self._epoch_current(segment, epoch):
            # A newer attempt owns translate_queued. Do not clear it.
            self._suppress_attempt(segment, epoch, None)
            return
        if segment.key not in self._emitted_segs:
            segment.translate_queued = False
            return
        if time.monotonic() - enqueued_at > self.settings.translate_timeout_s:
            self._fail_translation(segment, "skipped", "英譯排隊太久，中文仍保留")
            return
        zh_snapshot = segment.zh
        glossary = self.glossary.get((segment.room_id, segment.session_id), [])
        context = self._context(segment)
        kwargs = {}
        params = inspect.signature(self.translator.translate).parameters
        if "glossary" in params:
            kwargs["glossary"] = glossary
        if "context" in params:
            kwargs["context"] = context
        if "deadline" in params:
            kwargs["deadline"] = time.monotonic() + self.settings.translate_timeout_s
        cancel = None
        if "cancel" in params:
            cancel = threading.Event()
            kwargs["cancel"] = cancel
        assert self._translate_pool is not None
        cfut = self._translate_pool.submit(partial(self.translator.translate, zh_snapshot, **kwargs))
        try:
            translated: TranslateResult = await wait_bounded(asyncio.wrap_future(cfut), timeout=self.settings.translate_timeout_s)
        except asyncio.TimeoutError:
            if cancel is not None:
                cancel.set()
            cfut.cancel()
            if self._epoch_current(segment, epoch):
                self._fail_translation(segment, "timeout", "英譯逾時，不假設沒有計費。中文仍保留")
            else:
                self._suppress_attempt(segment, epoch, None)
            return
        except asyncio.CancelledError:
            if cancel is not None:
                cancel.set()
            cfut.cancel()
            raise
        except Exception:
            if cancel is not None:
                cancel.set()
            if self._epoch_current(segment, epoch):
                self._fail_translation(segment, "error", "英譯失敗，中文仍保留")
            else:
                self._suppress_attempt(segment, epoch, None)
            return
        self._finish_translation(segment, translated, epoch, zh_snapshot)

    def _remember_zh(self, segment: Segment) -> None:
        if not segment.zh or self._stale(segment):
            return
        group = (segment.room_id, segment.session_id)
        rows = self._recent_zh.setdefault(group, deque(maxlen=8))
        for index, (seq, _text) in enumerate(rows):
            if seq == segment.seq:
                rows[index] = (segment.seq, segment.zh)
                return
        if rows and segment.seq < rows[0][0]:
            return
        for index, (seq, _text) in enumerate(rows):
            if segment.seq < seq:
                rows.insert(index, (segment.seq, segment.zh))
                return
        rows.append((segment.seq, segment.zh))

    def _context(self, segment: Segment) -> list[str]:
        rows = self._recent_zh.get((segment.room_id, segment.session_id), ())
        return [text for seq, text in rows if seq < segment.seq][-4:]

    async def _gap_loop(self) -> None:
        tick = min(0.05, max(self.settings.gap_wait_s, 0.01))
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            for group, max_seq in list(self._max_seq.items()):
                if group in self._closed or group in self._flushing:
                    continue
                nxt = self._next.get(group, 1)
                if nxt > max_seq or nxt in self._held.get(group, {}):
                    continue
                if (group[0], group[1], nxt) in self._active:
                    continue
                stamp = (*group, nxt)
                started = self._gap_since.get(stamp)
                if started is None:
                    self._gap_since[stamp] = now
                    continue
                if now - started >= self.settings.gap_wait_s:
                    self._gap_since.pop(stamp, None)
                    self.mark_missing(group[0], group[1], nxt, "缺段：等待上限已到，後面的字幕繼續")

    def _trim_results(self) -> None:
        if len(self.results) <= self.settings.max_results:
            return
        victims = []
        for key, segment in self.results.items():
            group = (key[0], key[1])
            if key[2] < self._next.get(group, 1) and key not in self._active and segment.status not in {"queued", "decoding", "transcribing", "zh_ready"}:
                victims.append((segment.received_at, key))
        victims.sort()
        extra = len(self.results) - self.settings.max_results
        for _, key in victims[:extra]:
            # Keep the compact index and the audio hash so a later retry is still deduped.
            self.results.pop(key, None)

    def seed_from_store(self, room_id: str, rows: list[dict]) -> None:
        """Restore per-session progress from saved captions. Safe to call once per room."""
        if room_id in self._seeded:
            return
        self._seeded.add(room_id)
        sessions: dict[str, dict] = {}
        for row in rows or []:
            session_id = str(row.get("session_id") or "")
            try:
                seq = int(row.get("seq") or 0)
                version = max(1, int(row.get("version") or 1))
                session_ord = int(row.get("session_ord") or 0)
            except (TypeError, ValueError):
                continue
            if not session_id or seq < 1:
                continue
            key = (room_id, session_id, seq)
            self._version_floor[key] = max(self._version_floor.get(key, 0), version)
            segment = Segment(
                room_id=room_id,
                session_id=session_id,
                seq=seq,
                zh=row.get("zh") or "",
                zh_raw=row.get("zh_raw") or "",
                en=row.get("en") or "",
                status=row.get("status") or "ready",
                translate_status=row.get("translate_status") or "",
                error=row.get("error") or "",
                version=version,
                session_ord=session_ord,
                t0_ms=row.get("t0_ms"),
                t1_ms=row.get("t1_ms"),
            )
            self._stamp_gen(segment)
            self.results[key] = segment
            self._emitted_segs.add(key)
            self._remember_index(segment)
            bucket = sessions.setdefault(session_id, {"ord": 0, "max_seq": 0, "seqs": set()})
            bucket["ord"] = max(int(bucket["ord"]), session_ord)
            bucket["max_seq"] = max(int(bucket["max_seq"]), seq)
            bucket["seqs"].add(seq)
        max_ord = 0
        for session_id, info in sessions.items():
            group = (room_id, session_id)
            if info["ord"]:
                self._session_ord[group] = int(info["ord"])
                max_ord = max(max_ord, int(info["ord"]))
            self._max_seq[group] = int(info["max_seq"])
            # Past the saved seqs, so a continuing session does not invent 1..N gaps.
            self._next[group] = int(info["max_seq"]) + 1
            # Seq below the oldest saved caption were expired or purged, not missing.
            # Filling 1..max_seq would resurrect them as blank captions with a new TTL.
            oldest = min(info["seqs"]) if info["seqs"] else 1
            for seq in range(oldest, int(info["max_seq"]) + 1):
                if seq not in info["seqs"]:
                    self.mark_missing(room_id, session_id, seq, "缺段：這段沒有留在逐字稿裡")
        if max_ord:
            self._room_sessions[room_id] = max(self._room_sessions.get(room_id, 0), max_ord)

    async def retranslate(self, room_id: str, session_id: str, seq: int, zh: str | None = None) -> Segment:
        self.ensure_workers()
        if f"{room_id}:{session_id}:{seq}" in self._sealed.get(room_id, ()):
            raise PipelineError(404, "找不到這段字幕")
        segment = self.results.get((room_id, session_id, seq))
        if segment is None:
            segment = self._rehydrate((room_id, session_id, seq))
        if segment is None or not (segment.zh or zh):
            raise PipelineError(404, "找不到這段字幕")
        if segment.status in {"cancelled", "missing"}:
            raise PipelineError(409, "這段已取消或缺少，不能重譯")
        if zh is not None:
            segment.zh_raw = segment.zh_raw or segment.zh
            segment.zh = annotate_question(zh.strip())
            self._remember_zh(segment)
        # A second retranslate replaces the queued attempt instead of stacking one.
        segment.status = "zh_ready"
        await self._enqueue_translation(segment)
        return self.results.get((room_id, session_id, seq), segment)
