import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.store import CaptionStore
from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import app_for, auth, push, settings_with, stop, token_of


def _event(seq: int, zh: str = "字") -> dict:
    return {
        "id": f"r:s:{seq}",
        "room_id": "r",
        "session_id": "s",
        "seq": seq,
        "version": 1,
        "zh": zh,
        "status": "ready",
        "t0_ms": 0,
        "t1_ms": 1000,
    }


@pytest.mark.anyio
async def test_store_save_runs_off_event_loop(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    seen = {}
    loop_ident = threading.current_thread().ident
    original = app.state.store._write_save

    def wrapped(event):
        seen["name"] = threading.current_thread().name
        seen["ident"] = threading.current_thread().ident
        return original(event)

    app.state.store._write_save = wrapped
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(client, token, "class", "s", 1, "字".encode(), async_header=True, wait_translation="0")
            assert resp.status_code == 200, resp.text
            app.state.store.flush()
        assert seen["name"].startswith("breeze-store")
        assert seen["ident"] != loop_ident
        assert app.state.store.room_rows("class")[0]["zh"] == "字"
    finally:
        await stop(app)


def test_store_writes_preserve_order(tmp_path):
    store = CaptionStore(tmp_path / "captions.sqlite3")
    order = []
    original = store._write_save

    def wrapped(event):
        order.append(event["id"])
        time.sleep(0.02)
        return original(event)

    store._write_save = wrapped
    try:
        store.submit_save(_event(1, "一"))
        store.submit_save(_event(2, "二"))
        store.flush()
        assert order == ["r:s:1", "r:s:2"]
        assert [row["zh"] for row in store.room_rows("r")] == ["一", "二"]
    finally:
        store.close()


def test_delete_room_ordered_after_pending_saves(tmp_path):
    store = CaptionStore(tmp_path / "captions.sqlite3")
    order = []
    original = store._write_save
    original_delete = store._delete_room_now

    def wrapped(event):
        order.append("save")
        time.sleep(0.04)
        return original(event)

    def wrapped_delete(room_id):
        order.append("delete")
        return original_delete(room_id)

    store._write_save = wrapped
    store._delete_room_now = wrapped_delete
    try:
        store.submit_save(_event(1))
        store.submit_save(_event(2))
        removed = store.enqueue_delete_room("r").result(timeout=3)
        assert removed == 2
        assert order == ["save", "save", "delete"]
        assert store.room_rows("r") == []
        store.save(_event(3, "新"))
        assert [row["seq"] for row in store.room_rows("r")] == [3]
    finally:
        store.close()


def test_shutdown_flushes_pending_saves(tmp_path):
    path = tmp_path / "captions.sqlite3"
    store = CaptionStore(path)
    started = threading.Event()
    release = threading.Event()
    original = store._write_save

    def wrapped(event):
        started.set()
        assert release.wait(3)
        return original(event)

    store._write_save = wrapped
    store.submit_save(_event(1, "存下"))
    assert started.wait(2)

    def let_go():
        time.sleep(0.05)
        release.set()

    threading.Thread(target=let_go, daemon=True).start()
    store.close()
    again = CaptionStore(path)
    try:
        rows = again.room_rows("r")
        assert [row["zh"] for row in rows] == ["存下"]
    finally:
        again.close()


@pytest.mark.anyio
async def test_store_error_logged_and_pipeline_not_wedged(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    original = app.state.store._write_save
    state = {"first": True}

    def wrapped(event):
        if state["first"] and int(event.get("seq") or 0) == 1:
            state["first"] = False
            raise RuntimeError("disk full")
        return original(event)

    app.state.store._write_save = wrapped
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await push(client, token, "class", "s", 1, "第一".encode(), async_header=True, wait_translation="0")
            second = await push(client, token, "class", "s", 2, "第二".encode(), async_header=True, wait_translation="0")
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            app.state.store.flush()
            assert app.state.store.errors >= 1
            rows = app.state.store.room_rows("class")
            assert any(row["zh"] == "第二" for row in rows)
            assert any(item.get("zh") == "第二" for item in app.state.bus.caption_state("class"))
    finally:
        await stop(app)


class _GateTranslator(Translator):
    """Blocks inside translate() until the test opens the gate."""

    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.gate = threading.Event()
        self.started = threading.Event()

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline, cancel
        self.started.set()
        self.gate.wait(10)
        return TranslateResult("EN:" + zh, "ok")


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780")


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.anyio
async def test_room_delete_success_releases_held_push(tmp_path):
    """Seq 2 is held for seq 1. Deleting the room must answer that push, not leave it waiting.

    The room is gone, so the answer is a cancelled result rather than a caption.
    """
    app = app_for(settings=settings_with(
        allow_testclient=True,
        translate=False,
        gap_wait_s=30.0,
        data_path=str(tmp_path / "captions.sqlite3"),
    ))
    task = None
    try:
        async with _client(app) as client:
            token = await token_of(app, client)
            task = asyncio.create_task(push(
                client, token, "class", "s", 2, "第二".encode(),
                async_header=True, wait_translation="0",
            ))
            await asyncio.sleep(0.5)
            assert not task.done(), "seq 2 should still be held behind the missing seq 1"
            deleted = await client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
            assert deleted.status_code == 200, deleted.text
            done, _pending = await asyncio.wait({task}, timeout=1.0)
            assert done, "held /api/push did not return within 1s after the room delete committed"
            resp = task.result()
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["status"] == "cancelled"
            assert body["ok"] is False
            assert body.get("detail")
    finally:
        await _cancel(task)
        await stop(app)


@pytest.mark.anyio
async def test_delete_503_during_translation_still_translates_and_wakes(tmp_path):
    """A single-caption delete that fails must not strand the English already in flight.

    After the 503, releasing the translator has to finish the default push with
    status ready and a non-empty English line.
    """
    translator = _GateTranslator()
    app = app_for(
        settings=settings_with(
            allow_testclient=True,
            translate=True,
            data_path=str(tmp_path / "captions.sqlite3"),
        ),
        translator=translator,
    )
    task = None
    try:
        async with _client(app) as client:
            token = await token_of(app, client)
            task = asyncio.create_task(push(client, token, "class", "s", 1, "第一".encode()))
            for _ in range(200):
                if translator.started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert translator.started.is_set(), "translation never started"

            def boom(*_args, **_kwargs):
                raise sqlite3.OperationalError("disk full")

            app.state.store._delete_id_now = boom
            denied = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:1"},
                headers=auth(token),
            )
            assert denied.status_code == 503, denied.text
            translator.gate.set()
            done, _pending = await asyncio.wait({task}, timeout=1.0)
            assert done, "default /api/push did not return within 1s after the failed delete"
            resp = task.result()
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "ready"
            assert body["en"]
            segment = app.state.pipeline.get("class", "s", 1)
            assert segment is not None
            assert segment.status == "ready"
            assert segment.en
            assert segment.translate_queued is False
    finally:
        translator.gate.set()
        await _cancel(task)
        await stop(app)


@pytest.mark.anyio
async def test_room_delete_503_keeps_captions_finalized_during_window(tmp_path):
    """A caption finalized while a room delete is in flight must survive a 503.

    The push has to return, and both the bus and the store must keep seq 2.
    """
    app = app_for(settings=settings_with(
        allow_testclient=True,
        translate=False,
        data_path=str(tmp_path / "captions.sqlite3"),
    ))
    delete_task = None
    push_task = None
    release = threading.Event()
    try:
        async with _client(app) as client:
            token = await token_of(app, client)
            first = await push(
                client, token, "class", "s", 1, "一".encode(),
                async_header=True, wait_translation="0",
            )
            assert first.status_code == 200, first.text
            app.state.store.flush()

            def slow_boom(*_args, **_kwargs):
                assert release.wait(3), "room delete was not released"
                raise sqlite3.OperationalError("disk full")

            app.state.store._delete_room_now = slow_boom
            delete_task = asyncio.create_task(client.delete(
                "/api/captions", params={"room_id": "class"}, headers=auth(token),
            ))
            await asyncio.sleep(0.05)
            assert "class" in app.state.pipeline._muted
            push_task = asyncio.create_task(push(
                client, token, "class", "s", 2, "二".encode(),
                async_header=True, wait_translation="0",
            ))
            for _ in range(100):
                if ("class", "s", 2) in app.state.pipeline._emit_waiters:
                    break
                await asyncio.sleep(0.01)
            assert ("class", "s", 2) in app.state.pipeline._emit_waiters
            assert not push_task.done()
            assert all(row.get("seq") != 2 for row in app.state.bus.caption_state("class"))
            release.set()
            denied = await delete_task
            delete_task = None
            assert denied.status_code == 503, denied.text
            done, _pending = await asyncio.wait({push_task}, timeout=1.0)
            assert done, "seq 2 /api/push did not return within 1s after the failed room delete"
            resp = push_task.result()
            push_task = None
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "二"
            bus = {row["seq"]: row for row in app.state.bus.caption_state("class")}
            assert 2 in bus
            assert bus[2]["zh"] == "二"
            app.state.store.flush()
            stored = {row["seq"]: row for row in app.state.store.room_rows("class")}
            assert 2 in stored
            assert stored[2]["zh"] == "二"
    finally:
        release.set()
        await _cancel(delete_task)
        await _cancel(push_task)
        await stop(app)


@pytest.mark.anyio
async def test_sweep_loop_survives_purge_error(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    calls = {"n": 0}

    def boom(_ttl):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("purge broke")
        return 0

    app.state.store.purge_expired = boom
    try:
        await app.state.sweep_once()
        await app.state.sweep_once()
        assert calls["n"] == 2
    finally:
        await stop(app)
