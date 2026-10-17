"""Live server（主持頁）用的訓練資料開關：/api/training/class*。主持人 token 驗證，和主持頁其他 API 一樣。

讀寫 training.sqlite3 都在 thread 裡；沒有 training.sqlite3（還沒登記講者）時只回 available=false。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.training import capture, harvest, registry
from app.training import db as tdb


def make_router(require_host, check_room) -> APIRouter:
    r = APIRouter()

    def _with_db(fn, write=False):
        root = tdb.training_dir()
        if not tdb.db_path(root).exists():
            return None
        c = tdb.connect(root)
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c)
                if write:
                    c.execute("COMMIT")
                return out
            except BaseException:
                if write:
                    c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    @r.get("/api/training/class")
    async def class_state(request: Request, room_id: str = "class") -> dict:
        require_host(request)
        room = check_room(room_id)

        def f(c):
            return {"lecturers": harvest.lecturers(c), "room": harvest.room_setting(c, room)}
        data = await asyncio.to_thread(_with_db, f)
        if data is None:
            return {"available": False, "message": "還沒有登記任何講者。到後台「訓練資料」快速登記（約 1 分鐘）。"}
        return {"available": True, **data, "status": capture.status(room), "capture": capture.stats()}

    @r.post("/api/training/class")
    async def class_set(request: Request) -> dict:
        require_host(request)
        b = await request.json()
        room = check_room(str(b.get("room_id") or "class"))

        def f(c):
            return harvest.set_room(c, room_id=room, speaker_id=b.get("speaker_id") or None,
                                    collect=b.get("collect") is True, by="host")
        try:
            out = await asyncio.to_thread(_with_db, f, True)
        except registry.RegistryError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if out is None:
            raise HTTPException(status_code=409, detail="還沒有登記任何講者")
        capture.refresh()
        return {"room": out}

    @r.post("/api/training/class/mark")
    async def class_mark(request: Request) -> dict:
        require_host(request)
        b = await request.json()
        room = check_room(str(b.get("room_id") or "class"))
        kind = b.get("kind") or "question"
        if kind != "question":
            raise HTTPException(status_code=422, detail="kind 只支援 question")
        wall = b.get("wall_ms")
        capture.mark(room, kind, bool(b.get("on")), int(wall) if isinstance(wall, (int, float)) else None)
        return {"ok": True}

    return r
