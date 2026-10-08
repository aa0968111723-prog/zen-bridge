"""B-f. Stopping the session waits for audio already in ASR, not for English.

VirtualHost.stop posts /api/session/end the way host.html does: after uploads drain,
with no flush=0. sim_settings sets stop_flush_s to 2 virtual seconds so a 40s translation
does not hold the stop button. B-f2 raises that budget so the gated ASR can finish.
"""

import threading

import pytest

from tests.sim import (
    SCALE,
    Listener,
    caption_rows,
    ScriptedTranslator,
    TextAsr,
    VirtualHost,
    export_json,
    open_room,
    post_segment,
    serving,
    sim_settings,
    vlimit,
)
from tests.test_round2 import auth, breaking_decoder


@pytest.mark.anyio
async def test_stop_latency_not_bound_by_translation():
    """B-f1. Opt-in push returns at Chinese, so stop is ASR plus the short flush grace, not 40s of English.

    stop() is pressed right after the fifth slice is handed to upload (drain=False), so
    the measured time includes settling that last upload, as host.html does before
    /api/session/end. On main that upload waits for English (about 40 virtual s).
    """
    translator = ScriptedTranslator(lambda zh: ("ok", 40.0))
    async with serving(asr=TextAsr(1.5), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        host = VirtualHost(client, token, "class", "s")
        await host.run(5, drain=False)
        resp = await host.stop()
        assert resp.status_code == 200, resp.text
        assert host.stop_elapsed_v <= vlimit(3.5)


@pytest.mark.anyio
async def test_session_end_while_last_segment_in_asr_not_missing():
    """B-f2. session/end while ASR is in _active must not mark that segment missing."""
    import asyncio

    gate = {"started": threading.Event(), "release": threading.Event()}

    def pick(text):
        if text == "第2句":
            return gate
        return None

    settings = sim_settings(translate=False, stop_flush_s=2)
    async with serving(settings=settings, asr=TextAsr(0, gate=pick)) as (app, client, token):
        await open_room(client, token, "class")
        first = await post_segment(client, token, "class", "s", 1, "第1句".encode(), 0, 6000)
        assert first.status_code == 200, first.text
        second = asyncio.create_task(post_segment(client, token, "class", "s", 2, "第2句".encode(), 6000, 12000))
        assert await asyncio.to_thread(gate["started"].wait, 3)
        end = asyncio.create_task(client.post(
            "/api/session/end",
            json={"room_id": "class", "session_id": "s"},
            headers={**auth(token), "content-type": "application/json"},
        ))
        await asyncio.sleep(0.05)
        gate["release"].set()
        resp = await second
        ended = await end
        assert resp.status_code == 200, resp.text
        assert ended.status_code == 200, ended.text
        row = next(item for item in caption_rows(app, "class") if item["seq"] == 2)
        assert row["status"] in {"zh_ready", "ready"}


@pytest.mark.anyio
async def test_push_after_session_end_is_409_and_not_exported():
    """B-f3."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        host = VirtualHost(client, token, "class", "s")
        await host.run(2, pace=False)
        ended = await host.stop()
        assert ended.status_code == 200, ended.text
        before = await export_json(client, token, "class")
        late = await post_segment(client, token, "class", "s", 3, "第3句".encode(), 12000, 18000)
        assert late.status_code == 409
        assert late.json()["detail"] == "這個會話已結束"
        after = await export_json(client, token, "class")
        assert len(after) == len(before)


@pytest.mark.anyio
async def test_pagehide_without_session_end_fills_missing():
    """B-f4. Seq 3 is never uploaded. The gap loop arms on one tick, then waits gap_wait_s.
    Both are 3 virtual seconds at this scale, so the missing line shows up within that sum plus tolerance.
    """
    import time

    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            for seq in (1, 2, 4):
                resp = await post_segment(client, token, "class", "s", seq, f"第{seq}句".encode(), (seq - 1) * 6000, seq * 6000)
                assert resp.status_code == 200, resp.text
            marked = time.monotonic()

            def seen():
                return any(m.get("seq") == 3 and m.get("status") == "missing" for m in listener.messages)

            assert await listener.wait_for(seen, 20)
            missing = next(m for m in listener.messages if m.get("seq") == 3 and m.get("status") == "missing")
            assert (missing["_recv_mono"] - marked) / SCALE <= vlimit(6)
            # Seq 4 is released right after the missing line; the pump may not have read it yet.
            assert await listener.wait_for(
                lambda: any(m.get("seq") == 4 and m.get("zh") for m in listener.messages), 5,
            )


@pytest.mark.anyio
async def test_upload_failure_marks_missing_not_stall():
    """B-f5. A broken container becomes status error and does not stall later seqs.

    JSON export omits a text-less error row (the same filter as missing). The line is still
    on the listener and in pipeline state; it is not dropped from the room, and seq 4 follows.
    """
    async with serving(
        asr=TextAsr(0),
        settings=sim_settings(translate=False),
        decoder=breaking_decoder,
    ) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            for seq, payload in ((1, "第1句"), (2, "第2句")):
                resp = await post_segment(client, token, "class", "s", seq, payload.encode(), (seq - 1) * 6000, seq * 6000)
                assert resp.status_code == 200, resp.text
            bad = await post_segment(client, token, "class", "s", 3, b"bad", 12000, 18000)
            assert bad.status_code == 422
            assert bad.json()["status"] == "error"
            nxt = await post_segment(client, token, "class", "s", 4, "第4句".encode(), 18000, 24000)
            assert nxt.status_code == 200, nxt.text
            assert await listener.wait_for(lambda: any(m.get("seq") == 4 and m.get("zh") for m in listener.messages), 10)
            assert any(m.get("seq") == 3 and m.get("status") == "error" for m in listener.messages)
        rows = await export_json(client, token, "class")
        assert [row["seq"] for row in rows] == [1, 2, 4]
        assert app.state.pipeline.get("class", "s", 3).status == "error"
