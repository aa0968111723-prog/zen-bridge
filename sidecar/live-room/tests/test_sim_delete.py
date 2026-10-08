"""B-d. Delete clears store, bus, export, and listeners, and does not resurrect.

The clear event is type captions_cleared. The cursor is not rewound; a listener that
kept the pre-delete cursor is backfilled with the clear plus the new segments.
"""

import asyncio
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.server import create_app
from tests.sim import (
    SCALE,
    Listener,
    ScriptedTranslator,
    TextAsr,
    VirtualHost,
    copy_decoder,
    export_json,
    export_srt,
    open_room,
    post_segment,
    serving,
    sim_settings,
    vlimit,
)
from tests.test_round2 import Socket, auth, stop, token_of


async def _delete(client, token, room):
    return await client.delete("/api/captions", params={"room_id": room}, headers=auth(token))


@pytest.mark.anyio
@pytest.mark.parametrize("storage", [False, True])
async def test_delete_clears_store_bus_and_export(storage, tmp_path):
    """B-d1."""
    path = str(tmp_path / "d.sqlite3") if storage else ""
    async with serving(settings=sim_settings(data_path=path, translate=False), asr=TextAsr(0)) as (app, client, token):
        await open_room(client, token, "class")
        host = VirtualHost(client, token, "class", "s")
        await host.run(20, pace=False)
        resp = await _delete(client, token, "class")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["deleted"] == (20 if storage else 0)
        assert await export_json(client, token, "class") == []
        assert (await export_srt(client, token, "class")) == ""
        assert app.state.bus.history("class") == []


@pytest.mark.anyio
async def test_delete_notifies_listeners():
    """B-d2. The listener sees captions_cleared. One virtual second is plenty; the event is on the delete itself."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "s")
            await host.run(3, pace=False)
            delete_at = time.monotonic()
            deleted = await _delete(client, token, "class")
            assert deleted.status_code == 200, deleted.text
            assert await listener.wait_for(
                lambda: any(m.get("type") == "captions_cleared" for m in listener.messages),
                1,
            )
            cleared = next(m for m in listener.messages if m.get("type") == "captions_cleared")
            # The clear is published inside DELETE. Bound the listener's receipt, not the poll deadline.
            assert (cleared["_recv_mono"] - delete_at) / SCALE <= vlimit(1)


@pytest.mark.anyio
async def test_inflight_translation_does_not_resurrect():
    """B-d3. English that finishes after the delete must not come back.

    The three uploads are left running (drain=False) and the room is deleted as soon
    as all three zh_ready lines are on the listener, while their 20 virtual s English
    is still in flight. Then every translation is allowed to finish and the test waits
    a further 30 virtual s before checking store, bus, export and the listener.
    """
    import asyncio
    import time

    translator = ScriptedTranslator(lambda zh: ("ok", 20.0))
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "s")
            await host.run(3, pace=False, drain=False)
            assert await listener.wait_for(
                lambda: len({m.get("seq") for m in listener.messages if m.get("status") == "zh_ready"}) >= 3,
                10,
            )
            before = len(listener.messages)
            assert len(translator.finished) < 3, "delete must happen while English is still in flight"
            await _delete(client, token, "class")
            if host._tasks:
                await asyncio.wait(host._tasks)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(translator.finished) < len(translator.started):
                await asyncio.sleep(0.01)
            await asyncio.sleep(30 * SCALE)
            ids = {"class:s:1", "class:s:2", "class:s:3"}
            later = [m for m in listener.messages[before:] if m.get("id") in ids and m.get("type") != "captions_cleared"]
            assert later == []
        flush = getattr(app.state.store, "flush", None)
        if flush is not None:
            await asyncio.to_thread(flush)
        assert await export_json(client, token, "class") == []
        assert app.state.store.room_rows("class") == []
        assert app.state.bus.history("class") == []


@pytest.mark.anyio
async def test_retranslate_after_delete_is_404():
    """B-d4."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        resp = await __import__("tests.sim", fromlist=["post_segment"]).post_segment(
            client, token, "class", "s", 1, "第1句".encode(), 0, 6000,
        )
        assert resp.status_code == 200, resp.text
        await _delete(client, token, "class")
        again = await client.post(
            "/api/segment/retranslate",
            json={"room_id": "class", "session_id": "s", "seq": 1},
            headers={**auth(token), "content-type": "application/json"},
        )
        assert again.status_code == 404


@pytest.mark.anyio
async def test_listener_backfills_after_delete():
    """B-d5. Cursor stays high across captions_cleared. Reconnecting with it returns
    the post-delete segments once, not the deleted ones, and not a blank gap.
    """
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        listener = Listener(app, "class")
        await listener.__aenter__()
        try:
            host = VirtualHost(client, token, "class", "old")
            await host.run(30, pace=False)
            assert await listener.wait_for(lambda: any(int(m.get("cursor") or 0) >= 30 for m in listener.messages), 10)
            saved = max(int(m.get("cursor") or 0) for m in listener.messages)
            await _delete(client, token, "class")
            assert await listener.wait_for(
                lambda: any(m.get("type") == "captions_cleared" for m in listener.messages),
                5,
            )
            nxt = VirtualHost(client, token, "class", "new")
            await nxt.run(5, pace=False)
        finally:
            await listener.close()
        for seq in range(6, 9):
            resp = await post_segment(
                client, token, "class", "new", seq, f"第{seq}句".encode(), (seq - 1) * 6000, seq * 6000,
            )
            assert resp.status_code == 200, resp.text
        async with Listener(app, "class", cursor=saved) as again:
            assert await again.wait_for(lambda: any(m.get("type") == "hello" for m in again.messages), 5)
            hello = next(m for m in again.messages if m.get("type") == "hello")
            nested = [*(hello.get("events") or []), *(hello.get("backfill") or []), *(hello.get("history") or [])]
            saw_clear = any(m.get("type") == "captions_cleared" for m in again.messages) or any(
                item.get("type") == "captions_cleared" for item in nested
            )
            # The pre-delete cursor is still inside the new log, so gap stays false
            # and backfill is empty. captions_cleared is in the live log; the cursor
            # is not rewound. Each id+version is delivered once.
            assert saw_clear is True
            assert hello.get("gap") is False
            assert list(hello.get("backfill") or []) == []
            history_ids = [item["id"] for item in (hello.get("history") or []) if item.get("id")]
            assert history_ids == []
            events = list(hello.get("events") or [])
            seen_version: dict[tuple, int] = {}
            first_ids: list[str] = []
            for item in events:
                ident = item.get("id")
                if not ident:
                    continue
                if ident not in first_ids:
                    first_ids.append(ident)
                key = (ident, int(item.get("version") or 1))
                seen_version[key] = seen_version.get(key, 0) + 1
            assert first_ids == [f"class:new:{seq}" for seq in range(1, 9)]
            assert all(count == 1 for count in seen_version.values()), seen_version
            assert not any(str(item).startswith("class:old:") for item in first_ids)


@pytest.mark.anyio
async def test_delete_is_room_scoped():
    """B-d6."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        await open_room(client, token, "room2")
        async with Listener(app, "room2") as other:
            left = VirtualHost(client, token, "class", "s")
            right = VirtualHost(client, token, "room2", "s")
            await left.run(10, pace=False)
            await right.run(10, pace=False)
            before = len(other.messages)
            await _delete(client, token, "class")
            assert not any(m.get("type") == "captions_cleared" for m in other.messages[before:])
        rows = await export_json(client, token, "room2")
        assert [row["seq"] for row in rows] == list(range(1, 11))
        assert {row["room_id"] for row in rows} == {"room2"}


@pytest.mark.anyio
async def test_deleted_inflight_translation_stays_gone_after_restart(tmp_path):
    """Delete while English is still in flight, then restart. The deleted lines stay gone.

    Store, export, and the replay backfill on the new process must not contain them.
    """
    path = tmp_path / "gone.sqlite3"
    gate = threading.Event()
    translator = ScriptedTranslator(lambda zh: ("block", gate))
    deleted = [f"class:s:{n}" for n in range(1, 4)]
    app1 = create_app(
        sim_settings(data_path=str(path), translate_timeout_s=30),
        asr=TextAsr(0),
        translator=translator,
        decoder=copy_decoder,
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app1, client)
            await open_room(client, token, "class")
            async with Listener(app1, "class") as listener:
                host = VirtualHost(client, token, "class", "s")
                await host.run(3, pace=False, drain=False)
                assert await listener.wait_for(
                    lambda: len({m.get("seq") for m in listener.messages if m.get("status") == "zh_ready"}) >= 3,
                    10,
                )
                assert await listener.wait_for(lambda: len(translator.started) >= 1, 10)
                assert len(translator.finished) < len(translator.started)
                deleted_resp = await _delete(client, token, "class")
                assert deleted_resp.status_code == 200, deleted_resp.text
            gate.set()
            if host._tasks:
                await asyncio.wait(host._tasks)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(translator.finished) < len(translator.started):
                await asyncio.sleep(0.01)
            assert len(translator.finished) >= len(translator.started)
            flush = getattr(app1.state.store, "flush", None)
            if flush is not None:
                await asyncio.to_thread(flush)
            assert app1.state.store.room_rows("class") == []
            assert await export_json(client, token, "class") == []
    finally:
        gate.set()
        await stop(app1)

    app2 = create_app(
        sim_settings(data_path=str(path)),
        asr=TextAsr(0),
        translator=ScriptedTranslator(lambda zh: ("ok", 0.0)),
        decoder=copy_decoder,
    )
    try:
        async with app2.router.lifespan_context(app2):
            async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(app2, client)
                await open_room(client, token, "class")
                assert app2.state.store.room_rows("class") == []
                resp = await post_segment(client, token, "class", "s2", 1, "新的一句".encode(), 0, 6000)
                assert resp.status_code == 200, resp.text
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not app2.state.store.room_rows("class"):
                    await asyncio.sleep(0.01)
                flush = getattr(app2.state.store, "flush", None)
                if flush is not None:
                    await asyncio.to_thread(flush)
                rows = await export_json(client, token, "class")
                assert [row["id"] for row in rows] == ["class:s2:1"]
                async with Socket(app2, "/ws/listen?room_id=class&cursor=0&replay=1") as ws:
                    hello = await ws.recv()
                backfill_ids = [item["id"] for item in (hello.get("backfill") or []) if item.get("id")]
                history_ids = [item["id"] for item in (hello.get("history") or []) if item.get("id")]
                event_ids = [item["id"] for item in (hello.get("events") or []) if item.get("id")]
                assert backfill_ids == ["class:s2:1"]
                stored = app2.state.store.room_rows("class")
                assert [row["id"] for row in stored] == ["class:s2:1"]
                for old in deleted:
                    assert old not in backfill_ids
                    assert old not in history_ids
                    assert old not in event_ids
                    assert old not in [row["id"] for row in rows]
                    assert old not in [row["id"] for row in stored]
    finally:
        await stop(app2)
