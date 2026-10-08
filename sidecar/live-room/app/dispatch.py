from __future__ import annotations

import asyncio
import secrets
import time
from typing import Awaitable, Callable

from app.aio import cancellation_pending, wait_bounded

# Stored captions, host export, and host HTTP bodies. zh_raw is the recognition
# text from before a host edit, and any partial text left when recognition failed.
_PASS = (
    "type", "id", "room_id", "session_id", "session_ord", "seq", "version",
    "zh", "en", "status", "translate_status", "error", "t0_ms", "t1_ms", "zh_raw",
    "epoch",
)
# Audience sockets. Same caption fields, without zh_raw. A host token or listen
# key is not a caption field and must not be added here.
_LISTENER_PASS = (
    "type", "id", "room_id", "session_id", "session_ord", "seq", "version",
    "zh", "en", "status", "translate_status", "error", "t0_ms", "t1_ms",
    "epoch", "cursor",
)
_AUDIENCE_DENY = frozenset({
    "zh_raw", "host_token", "token", "listen_key", "listen_url", "authorization",
})
_CONTROL = {"caption_deleted", "captions_cleared"}


def for_listener(event: dict) -> dict:
    """Copy one caption onto the audience whitelist.

    Storage and host export keep zh_raw. This is the view a listener socket may see.
    """
    if not isinstance(event, dict):
        return {}
    return {key: event[key] for key in _LISTENER_PASS if key in event and key not in _AUDIENCE_DENY}


class RoomBus:
    """Per-room event log. A publish never lands in another room's history.

    The live window (history / cursor resume) stays at `limit`. A separate caption
    state keeps one row per segment for export and for backfill after a long gap.
    The per-room cursor counter is not restarted by drop or clear.
    """

    def __init__(self, limit: int = 200, caption_cap: int = 5000, epoch: int | None = None):
        self.limit = limit
        self.caption_cap = max(1, int(caption_cap))
        self.by_room: dict[str, list[dict]] = {}
        self._log: dict[str, list[dict]] = {}
        self._ver: dict[str, dict[str, int]] = {}
        self._cursor: dict[str, int] = {}
        self._epoch: dict[str, int] = {}
        # One value per process. A restarted server does not reuse it, so clients
        # can tell that cursors and versions belong to a new process.
        self._boot = int(epoch) if epoch is not None else secrets.randbelow(2_000_000_000) + 1
        self._state: dict[str, dict[str, dict]] = {}
        self._state_order: dict[str, list[str]] = {}
        self._state_at: dict[str, float] = {}
        self._caption_at: dict[str, dict[str, float]] = {}
        self._tomb: dict[str, set[str]] = {}

    def epoch(self, room_id: str) -> int:
        return int(self._epoch.get(room_id, self._boot))

    def _alloc_cursor(self, room: str) -> int:
        nxt = int(self._cursor.get(room, 0)) + 1
        self._cursor[room] = nxt
        return nxt

    def publish(self, event: dict) -> dict | None:
        room = str(event.get("room_id") or "")
        raw_updated = event.get("updated_at")
        snap = {key: event.get(key) for key in _PASS}
        snap["type"] = snap.get("type") or "caption"
        snap["room_id"] = room
        self._epoch.setdefault(room, self._boot)
        if snap.get("epoch") is None:
            snap["epoch"] = self._epoch[room]
        seg_id = str(snap.get("id") or "")
        if seg_id:
            snap["id"] = seg_id
        version = int(snap.get("version") or 1)
        snap["version"] = version
        kind = str(snap["type"])
        if kind not in _CONTROL and seg_id and seg_id in self._tomb.get(room, ()):
            return None
        if kind not in _CONTROL and seg_id:
            best = self._ver.get(room, {}).get(seg_id)
            if best is not None and version <= best:
                return None
            self._ver.setdefault(room, {})[seg_id] = version
        snap["cursor"] = self._alloc_cursor(room)
        stored = dict(snap)
        log = self._log.setdefault(room, [])
        log.append(stored)
        if len(log) > self.limit:
            del log[: len(log) - self.limit]
        if kind == "captions_cleared":
            return dict(stored)
        if kind == "caption_deleted":
            self._remove_id(room, seg_id)
            return dict(stored)
        self._remember_state(room, stored, raw_updated)
        rows = self.by_room.setdefault(room, [])
        replaced = False
        for index, item in enumerate(rows):
            if seg_id and str(item.get("id") or "") == seg_id:
                if version >= int(item.get("version") or 0):
                    rows[index] = stored
                replaced = True
                break
        if not replaced:
            rows.append(stored)
        rows.sort(key=lambda item: (int(item.get("session_ord") or 0), int(item.get("seq") or 0), int(item.get("cursor") or 0)))
        if len(rows) > self.limit:
            del rows[: len(rows) - self.limit]
        self.by_room[room] = rows
        return dict(stored)

    def _remember_state(self, room: str, stored: dict, updated_at=None) -> None:
        seg_id = str(stored.get("id") or "")
        if not seg_id:
            return
        bucket = self._state.setdefault(room, {})
        order = self._state_order.setdefault(room, [])
        previous = bucket.get(seg_id)
        if previous is not None and int(stored.get("version") or 1) < int(previous.get("version") or 1):
            return
        if seg_id not in bucket:
            order.append(seg_id)
        bucket[seg_id] = dict(stored)
        stamp = time.time()
        if updated_at not in (None, ""):
            try:
                parsed = float(updated_at)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed > 0:
                stamp = parsed
        self._caption_at.setdefault(room, {})[seg_id] = stamp
        self._state_at[room] = max(self._state_at.get(room, 0.0), stamp)
        extra = len(order) - self.caption_cap
        if extra > 0:
            versions = self._ver.get(room)
            stamps = self._caption_at.get(room)
            for old in order[:extra]:
                bucket.pop(old, None)
                if versions is not None:
                    versions.pop(old, None)
                if stamps is not None:
                    stamps.pop(old, None)
            del order[:extra]

    def _remove_id(self, room: str, seg_id: str) -> None:
        if not seg_id:
            return
        bucket = self._state.get(room)
        if bucket is not None:
            bucket.pop(seg_id, None)
        order = self._state_order.get(room)
        if order is not None:
            self._state_order[room] = [item for item in order if item != seg_id]
        rows = self.by_room.get(room)
        if rows is not None:
            self.by_room[room] = [item for item in rows if str(item.get("id") or "") != seg_id]
        log = self._log.get(room)
        if log is not None:
            self._log[room] = [
                item for item in log
                if str(item.get("id") or "") != seg_id or item.get("type") == "caption_deleted"
            ]
        stamps = self._caption_at.get(room)
        if stamps is not None:
            stamps.pop(seg_id, None)
            if not stamps:
                self._caption_at.pop(room, None)

    def history(self, room_id: str) -> list[dict]:
        return [dict(item) for item in self.by_room.get(room_id, [])]

    def caption_state(self, room_id: str) -> list[dict]:
        rows = [dict(item) for item in self._state.get(room_id, {}).values()]
        rows.sort(key=lambda item: (int(item.get("session_ord") or 0), int(item.get("seq") or 0), int(item.get("cursor") or 0)))
        return rows

    def has_captions(self, room_id: str) -> bool:
        return bool(self._state.get(room_id))

    def has_caption(self, room_id: str, seg_id: str) -> bool:
        seg_id = str(seg_id or "")
        if not seg_id:
            return False
        if seg_id in self._state.get(room_id, {}):
            return True
        for item in self.by_room.get(room_id, ()):
            if str(item.get("id") or "") == seg_id and item.get("type") not in _CONTROL:
                return True
        return False

    def caption_age(self, room_id: str, now: float | None = None) -> float:
        stamp = self._state_at.get(room_id)
        if stamp is None:
            return 0.0
        current = time.time() if now is None else now
        return current - stamp

    def prune_expired(self, ttl_s: float, now: float | None = None) -> list[tuple[str, str]]:
        """Drop captions older than ttl, including ones in a room that is still open.

        SQLite purge deletes each row by its own updated_at. The room's newest
        caption must not keep an older line alive.
        """
        current = time.time() if now is None else float(now)
        cutoff = current - float(ttl_s)
        removed: list[tuple[str, str]] = []
        for room, stamps in list(self._caption_at.items()):
            stale = [seg_id for seg_id, stamp in list(stamps.items()) if float(stamp) < cutoff]
            for seg_id in stale:
                self._remove_id(room, seg_id)
                removed.append((room, seg_id))
            if not self._state.get(room):
                self._state_at.pop(room, None)
        return removed

    def since(self, room_id: str, cursor: int) -> dict:
        log = self._log.get(room_id, [])
        latest = int(self._cursor.get(room_id, 0))
        if not log:
            # An old client cursor that this process never issued is a gap, not silence.
            gap = cursor > 0 and cursor != latest
            return {
                "events": [],
                "gap": gap,
                "oldest_cursor": 0,
                "latest_cursor": latest,
                "backfill": self.caption_state(room_id) if gap else [],
            }
        oldest = int(log[0]["cursor"])
        latest = max(latest, int(log[-1]["cursor"]))
        ahead = cursor > latest
        hole = oldest > cursor + 1
        gap = cursor > 0 and (hole or ahead)
        events = [dict(item) for item in log if int(item["cursor"]) > cursor]
        return {
            "events": events,
            "gap": gap,
            "oldest_cursor": oldest,
            "latest_cursor": latest,
            "backfill": self.caption_state(room_id) if gap else [],
        }

    def hydrate(self, room_id: str, rows: list[dict]) -> None:
        """Replay saved captions into this room. Same id and version is ignored."""
        self._epoch.setdefault(room_id, self._boot)
        for row in rows or []:
            if not row.get("id"):
                continue
            event = {key: row.get(key) for key in _PASS}
            event["type"] = "caption"
            event["room_id"] = room_id
            event["epoch"] = self.epoch(room_id)
            if row.get("updated_at") not in (None, ""):
                event["updated_at"] = row.get("updated_at")
            self.publish(event)

    def latest_cursor(self, room_id: str) -> int:
        return int(self._cursor.get(room_id, 0))

    def clear_room(self, room_id: str) -> dict:
        """Forget captions but keep the cursor. Listeners learn from captions_cleared."""
        tomb = self._tomb.setdefault(room_id, set())
        for seg_id in list(self._state.get(room_id, {})):
            tomb.add(seg_id)
        for item in self.by_room.get(room_id, []):
            if item.get("id"):
                tomb.add(str(item["id"]))
        self._state.pop(room_id, None)
        self._state_order.pop(room_id, None)
        self._state_at.pop(room_id, None)
        self._caption_at.pop(room_id, None)
        self._ver.pop(room_id, None)
        self.by_room.pop(room_id, None)
        self._log[room_id] = []
        self._epoch[room_id] = self.epoch(room_id) + 1
        published = self.publish({
            "type": "captions_cleared",
            "room_id": room_id,
            "epoch": self._epoch[room_id],
        })
        return published or {"type": "captions_cleared", "room_id": room_id, "epoch": self.epoch(room_id)}

    def delete_caption(self, room_id: str, seg_id: str, session_id: str = "", seq: int = 0) -> dict:
        seg_id = str(seg_id or "")
        self._tomb.setdefault(room_id, set()).add(seg_id)
        versions = self._ver.get(room_id)
        if versions is not None:
            versions.pop(seg_id, None)
        self._remove_id(room_id, seg_id)
        published = self.publish({
            "type": "caption_deleted",
            "room_id": room_id,
            "id": seg_id,
            "session_id": session_id,
            "seq": seq,
        })
        return published or {"type": "caption_deleted", "room_id": room_id, "id": seg_id}

    def retire(self, room_id: str) -> None:
        """Drop the live window. Export state and the cursor counter stay."""
        self.by_room.pop(room_id, None)
        self._log.pop(room_id, None)

    def drop(self, room_id: str) -> None:
        self.by_room.pop(room_id, None)
        self._log.pop(room_id, None)
        self._ver.pop(room_id, None)
        self._state.pop(room_id, None)
        self._state_order.pop(room_id, None)
        self._state_at.pop(room_id, None)
        self._caption_at.pop(room_id, None)
        self._tomb.pop(room_id, None)
        self._epoch[room_id] = self.epoch(room_id) + 1


class ListenerSlot:
    """Bounded per-connection send queue. offer() never waits on the socket."""

    def __init__(self, sender: Callable[[dict], Awaitable[None]], maxsize: int = 32, send_timeout: float = 2.0):
        self.sender = sender
        self.q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.send_timeout = send_timeout
        self.task: asyncio.Task | None = None
        self.alive = True
        self.dropped = 0
        self.last_pong = 0.0

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while self.alive:
                if cancellation_pending():
                    raise asyncio.CancelledError()
                msg = await self.q.get()
                if msg is None or not self.alive:
                    return
                if cancellation_pending():
                    raise asyncio.CancelledError()
                try:
                    await wait_bounded(self.sender(msg), self.send_timeout)
                except asyncio.TimeoutError:
                    self.alive = False
                    return
                if cancellation_pending():
                    raise asyncio.CancelledError()
                if not self.alive:
                    return
        except asyncio.CancelledError:
            self.alive = False
            raise
        except Exception:
            self.alive = False
        finally:
            self.alive = False

    def offer(self, msg: dict) -> bool:
        if not self.alive:
            return False
        try:
            self.q.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self.alive = False
            return False

    async def close(self) -> None:
        self.alive = False
        try:
            self.q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        task = self.task
        if task is None:
            return
        try:
            await wait_bounded(task, 0.2)
        except asyncio.CancelledError:
            # Awaiting an already-cancelled sender raises CancelledError here
            # even when close() itself was not cancelled.
            if not task.done():
                task.cancel()
            if cancellation_pending():
                raise
        except Exception:
            if not task.done():
                task.cancel()
