"""B-e. Restart, backfill, and room close.

Backfill past the 200-event window is the branch behavior: hello.gap is still true and
events is still the live window, but backfill/caption_state has every segment once.
A room_unavailable reason of ended closes the socket (unknown_or_ended still retries).
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.server import create_app
from app.translate import Translator
from tests.sim import (
    Listener,
    ScriptedTranslator,
    TextAsr,
    VirtualHost,
    caption_rows,
    copy_decoder,
    export_json,
    open_room,
    post_segment,
    serving,
    sim_settings,
    token_of,
)
from tests.test_round2 import auth, stop


@pytest.mark.anyio
async def test_restart_old_cursor_gets_new_events(tmp_path):
    """B-e1. After restart the cursor space starts over, so an old cursor is ahead of
    latest_cursor. The hello backfill has the replayed lines and the 5 new ones, once each.
    """
    path = tmp_path / "restart.sqlite3"
    translator = ScriptedTranslator(lambda zh: ("ok", 0.0))
    app1 = create_app(sim_settings(data_path=str(path)), asr=TextAsr(0), translator=translator, decoder=copy_decoder)
    saved = 0
    try:
        async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app1, client)
            await open_room(client, token, "class")
            async with Listener(app1, "class") as listener:
                host = VirtualHost(client, token, "class", "s")
                await host.run(50, pace=False)
                assert await listener.wait_for(lambda: any(m.get("status") == "ready" for m in listener.messages), 20)
                saved = max(int(m.get("cursor") or 0) for m in listener.messages)
            # Main's store writes each row in save(); flush is this branch's async store.
            flush = getattr(app1.state.store, "flush", None)
            if flush is not None:
                await __import__("asyncio").to_thread(flush)
    finally:
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
                host = VirtualHost(client, token, "class", "s2")
                await host.run(5, pace=False)
                async with Listener(app2, "class", cursor=saved) as listener:
                    assert await listener.wait_for(lambda: any(m.get("type") == "hello" for m in listener.messages), 5)
                    hello = next(m for m in listener.messages if m.get("type") == "hello")
                    assert hello["latest_cursor"] < saved
                    backfill_ids = [item["id"] for item in (hello.get("backfill") or []) if item.get("id")]
                    expected = [f"class:s:{n}" for n in range(1, 51)] + [f"class:s2:{n}" for n in range(1, 6)]
                    assert backfill_ids == expected
    finally:
        await stop(app2)


@pytest.mark.anyio
async def test_short_disconnect_backfill_100_segments():
    """B-e2. Disconnect between segments 40 and 60. The 200-event window still covers
    100 segments × 2 versions, so gap stays false and each id arrives once at its final version.
    """
    translator = ScriptedTranslator(lambda zh: ("ok", 0.0))
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        listener = Listener(app, "class")
        await listener.__aenter__()
        host = VirtualHost(client, token, "class", "s")
        await host.run(50, pace=False)
        assert await listener.wait_for(lambda: len(translator.finished) >= 50, 20)
        cursor = max(int(m.get("cursor") or 0) for m in listener.messages)
        await listener.close()
        for seq in range(51, 101):
            resp = await post_segment(client, token, "class", "s", seq, f"第{seq}句".encode(), (seq - 1) * 6000, seq * 6000)
            assert resp.status_code == 200, resp.text
        for _ in range(400):
            if len(translator.finished) >= 100 and app.state.pipeline.stats()["translate_queued"] == 0:
                break
            await __import__("asyncio").sleep(0.01)
        async with Listener(app, "class", cursor=cursor) as again:
            assert await again.wait_for(lambda: any(m.get("type") == "hello" for m in again.messages), 5)
            hello = next(m for m in again.messages if m.get("type") == "hello")
            assert hello["gap"] is False
        state = {item["id"]: item for item in caption_rows(app, "class")}
        assert len(state) == 100
        heard = {}
        async with Listener(app, "class", cursor=0) as full:
            assert await full.wait_for(lambda: any(m.get("type") == "hello" for m in full.messages), 5)
            hello = next(m for m in full.messages if m.get("type") == "hello")
            # cursor 0 history is the live by_room window, capped at 200 rows but one per segment.
            for item in hello.get("history") or []:
                if item.get("id"):
                    heard[item["id"]] = item
        # 100 segments fit in the window. Final versions match the server.
        assert len(heard) == 100
        for ident, item in heard.items():
            assert int(item["version"]) == int(state[ident]["version"])


@pytest.mark.anyio
async def test_default_window_exceeded_reports_gap():
    """B-e3. The live event window is still 200. This branch also backfills every segment once
    when the cursor is older than that window, instead of leaving the client with ~100 segments.
    """
    translator = ScriptedTranslator(lambda zh: ("ok", 0.0))
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        listener = Listener(app, "class")
        await listener.__aenter__()
        host = VirtualHost(client, token, "class", "s")
        await host.run(10, pace=False)
        assert await listener.wait_for(lambda: len(translator.finished) >= 10, 10)
        cursor = max(int(m.get("cursor") or 0) for m in listener.messages)
        await listener.close()
        for seq in range(11, 161):
            resp = await post_segment(client, token, "class", "s", seq, f"第{seq}句".encode(), 0, 6000)
            assert resp.status_code == 200, resp.text
        for _ in range(400):
            if len(translator.finished) >= 160 and app.state.pipeline.stats()["translate_queued"] == 0:
                break
            await __import__("asyncio").sleep(0.005)
        async with Listener(app, "class", cursor=cursor) as again:
            assert await again.wait_for(lambda: any(m.get("type") == "hello" for m in again.messages), 5)
            hello = next(m for m in again.messages if m.get("type") == "hello")
            assert hello["gap"] is True
            assert len(hello["events"]) == 200
            assert hello["oldest_cursor"] > cursor + 1
            backfill = hello.get("backfill") or []
            ids = [item["id"] for item in backfill if item.get("id")]
            assert len(ids) == len(set(ids)) == 160
        async with Listener(app, "class", cursor=0) as fresh:
            assert await fresh.wait_for(lambda: any(m.get("type") == "hello" for m in fresh.messages), 5)
            hello = next(m for m in fresh.messages if m.get("type") == "hello")
            # 160 segments fit under the 200-row by_room cap, so history is the whole class, not 200.
            assert len(hello["history"]) == 160


@pytest.mark.anyio
async def test_room_close_closes_listener_socket():
    """B-e4."""
    import time
    from tests.sim import SCALE

    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            assert await listener.wait_for(lambda: any(m.get("type") == "hello" for m in listener.messages), 5)
            started = time.monotonic()
            resp = await client.post(
                "/api/rooms/close",
                json={"room_id": "class"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert resp.status_code == 200, resp.text
            assert await listener.wait_for(lambda: any(m.get("type") == "room_unavailable" for m in listener.messages), 5)
            note = next(m for m in listener.messages if m.get("type") == "room_unavailable")
            assert note["reason"] == "ended"
            assert (note["_recv_mono"] - started) / SCALE <= __import__("tests.sim", fromlist=["vlimit"]).vlimit(1)
            assert await listener.wait_for(lambda: listener.closed, 5)
            close = next(m for m in listener.messages if m.get("type") == "websocket.close")
            assert (close["_recv_mono"] - started) / SCALE <= __import__("tests.sim", fromlist=["vlimit"]).vlimit(2)


@pytest.mark.anyio
async def test_reopen_same_room_old_listener_gets_new_or_closed():
    """B-e5. The old listener is closed. It must not sit open and silent."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        listener = Listener(app, "class")
        await listener.__aenter__()
        try:
            await listener.wait_for(lambda: any(m.get("type") == "hello" for m in listener.messages), 5)
            await client.post(
                "/api/rooms/close",
                json={"room_id": "class"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert await listener.wait_for(lambda: listener.closed, 5)
            await open_room(client, token, "class")
            host = VirtualHost(client, token, "class", "s")
            await host.run(3, pace=False)
            assert listener.closed is True
        finally:
            await listener.close()


@pytest.mark.anyio
async def test_restart_rejects_old_token(tmp_path):
    """B-e6."""
    path = tmp_path / "token.sqlite3"
    app1 = create_app(sim_settings(data_path=str(path), translate=False), asr=TextAsr(0), translator=Translator(enabled=False), decoder=copy_decoder)
    try:
        async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://127.0.0.1:8780") as client:
            old = await token_of(app1, client)
    finally:
        await stop(app1)
    app2 = create_app(sim_settings(data_path=str(path), translate=False), asr=TextAsr(0), translator=Translator(enabled=False), decoder=copy_decoder)
    try:
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://127.0.0.1:8780") as client:
            resp = await post_segment(client, old, "class", "s", 1, "第1句".encode(), 0, 6000)
            assert resp.status_code == 401
    finally:
        await stop(app2)
