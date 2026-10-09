from __future__ import annotations

import re
import secrets
import time

from fastapi import HTTPException

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RoomIdError(ValueError):
    pass


def validate_room_id(raw: str) -> str:
    room_id = (raw or "").strip()
    if not ROOM_RE.fullmatch(room_id):
        raise RoomIdError("房間代號只能用英文、數字、底線和減號，長度 1 到 64。不接受空白、中文或過長代號。")
    return room_id


def validate_session_id(raw: str) -> str:
    session_id = (raw or "").strip()
    if not SESSION_RE.fullmatch(session_id):
        raise RoomIdError("會話代號只能用英文、數字、底線和減號，長度 1 到 64。")
    return session_id


class RoomBook:
    """Host-opened rooms only. Anonymous listeners cannot create a permanent room."""

    def __init__(self, max_rooms: int, idle_s: float):
        self.max_rooms = max_rooms
        self.idle_s = idle_s
        self.rooms: dict[str, dict] = {}

    def _active_count(self) -> int:
        return sum(1 for room in self.rooms.values() if not room.get("ended"))

    def open(self, room_id: str) -> dict:
        current = self.rooms.get(room_id)
        if current and not current.get("ended"):
            current["last_active"] = time.monotonic()
            return current
        if self._active_count() >= self.max_rooms:
            raise HTTPException(status_code=429, detail="房間數已達上限")
        room = {
            "listeners": set(),
            "history": [],
            "created": time.monotonic(),
            "last_active": time.monotonic(),
            "ended": False,
            "session_active": False,
            # New secret every open. An old QR cannot read the next class.
            "listen_key": secrets.token_urlsafe(16),
        }
        self.rooms[room_id] = room
        return room

    def get(self, room_id: str) -> dict | None:
        room = self.rooms.get(room_id)
        if not room or room.get("ended"):
            return None
        return room

    def touch(self, room_id: str) -> None:
        room = self.get(room_id)
        if room:
            room["last_active"] = time.monotonic()

    def set_session_active(self, room_id: str, active: bool) -> None:
        room = self.get(room_id)
        if room:
            room["session_active"] = active
            room["last_active"] = time.monotonic()

    def close(self, room_id: str) -> dict | None:
        room = self.rooms.get(room_id)
        if not room:
            return None
        room["ended"] = True
        room["session_active"] = False
        return room

    def drop(self, room_id: str) -> None:
        self.rooms.pop(room_id, None)

    def sweep(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        stale = []
        for room_id, room in self.rooms.items():
            if room.get("session_active"):
                continue
            if room.get("listeners"):
                continue
            if room.get("ended") or now - room["last_active"] >= self.idle_s:
                stale.append(room_id)
        for room_id in stale:
            self.rooms.pop(room_id, None)
        return stale
