"""B-g. Rooms do not share captions, backlog, or listener slots."""

import pytest

from tests.sim import (
    SCALE,
    Listener,
    TextAsr,
    VirtualHost,
    export_json,
    open_room,
    post_segment,
    serving,
    sim_settings,
)
from tests.test_round2 import auth


@pytest.mark.anyio
async def test_two_rooms_parallel_100_each():
    """B-g1."""
    import asyncio

    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        await open_room(client, token, "room2")
        async with Listener(app, "class") as left, Listener(app, "room2") as right:
            host_a = VirtualHost(client, token, "class", "s")
            host_b = VirtualHost(client, token, "room2", "s")
            await asyncio.gather(host_a.run(100), host_b.run(100))
            assert await left.wait_for(lambda: len({m.get("seq") for m in left.captions()}) >= 100, 30)
            assert await right.wait_for(lambda: len({m.get("seq") for m in right.captions()}) >= 100, 30)
            assert {m["id"].split(":")[0] for m in left.captions()} == {"class"}
            assert {m["id"].split(":")[0] for m in right.captions()} == {"room2"}
        for room in ("class", "room2"):
            rows = await export_json(client, token, room)
            assert [row["seq"] for row in rows] == list(range(1, 101))
            assert {row["room_id"] for row in rows} == {room}


@pytest.mark.anyio
async def test_backlog_in_one_room_keeps_other_correct():
    """B-g2. One ASR worker. room2 stays ordered. Its latency is logged, not gated."""
    import asyncio

    class SplitAsr(TextAsr):
        def transcribe(self, wav, prompt=""):
            text = wav.read_bytes().decode()
            delay = 9.0 if text.startswith("慢") else 1.5
            self.delay_v = delay
            return super().transcribe(wav, prompt)

    async with serving(asr=SplitAsr(0), settings=sim_settings(translate=False, asr_workers=1)) as (app, client, token):
        await open_room(client, token, "class")
        await open_room(client, token, "room2")
        async with Listener(app, "room2") as listener:
            slow = VirtualHost(client, token, "class", "s")
            fast = VirtualHost(client, token, "room2", "s")
            await asyncio.gather(
                slow.run(12, text_of=lambda i: f"慢{i}"),
                fast.run(12, text_of=lambda i: f"快{i}"),
            )
            assert await listener.wait_for(lambda: len(listener.captions()) >= 12, 30)
        rows = await export_json(client, token, "room2")
        assert [row["zh"] for row in rows] == [f"快{i}" for i in range(1, 13)]
        delays = []
        for msg in listener.messages:
            if msg.get("status") == "zh_ready" and msg.get("seq") is not None:
                seq = int(msg["seq"])
                delays.append((msg["_recv_mono"] - fast.segment_end_mono[seq]) / SCALE)
        if delays:
            print(f"B-g2 room2 zh latency virtual seconds p50={sorted(delays)[len(delays)//2]:.2f} max={max(delays):.2f}")


@pytest.mark.anyio
async def test_close_delete_reopen_one_room_other_unaffected():
    """B-g3."""
    import asyncio

    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        await open_room(client, token, "room2")
        async with Listener(app, "room2") as listener:
            async def keep_pushing():
                for seq in range(1, 16):
                    resp = await post_segment(client, token, "room2", "s", seq, f"乙{seq}".encode(), (seq - 1) * 6000, seq * 6000)
                    assert resp.status_code == 200, resp.text
                    await asyncio.sleep(0.01)

            pushed = asyncio.create_task(keep_pushing())
            await asyncio.sleep(0.02)
            await client.post("/api/rooms/close", json={"room_id": "class"}, headers={**auth(token), "content-type": "application/json"})
            deleted = await client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
            assert deleted.status_code == 200
            await open_room(client, token, "class")
            await pushed
            assert listener.closed is False
            assert await listener.wait_for(lambda: len({m.get("seq") for m in listener.captions()}) >= 15, 10)
        rows = await export_json(client, token, "room2")
        assert [row["seq"] for row in rows] == list(range(1, 16))


@pytest.mark.anyio
async def test_listener_full_rejected():
    """B-g4."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False, max_listeners=2)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as first, Listener(app, "class") as second:
            third = Listener(app, "class")
            await third.__aenter__()
            try:
                assert await third.wait_for(lambda: any(m.get("type") == "room_unavailable" for m in third.messages), 5)
                note = next(m for m in third.messages if m.get("type") == "room_unavailable")
                assert note["reason"] == "full"
                assert await third.wait_for(lambda: third.closed, 5)
                assert third.close_code == 1013
                resp = await post_segment(client, token, "class", "s", 1, "第1句".encode(), 0, 6000)
                assert resp.status_code == 200, resp.text
                assert await first.wait_for(lambda: any(m.get("zh") for m in first.messages), 5)
                assert await second.wait_for(lambda: any(m.get("zh") for m in second.messages), 5)
            finally:
                await third.close()


@pytest.mark.anyio
async def test_unknown_room_listener_rejected_without_creating_room():
    """B-g5."""
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        before = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
        listener = Listener(app, "nosuchroom")
        await listener.__aenter__()
        try:
            assert await listener.wait_for(lambda: any(m.get("type") == "room_unavailable" for m in listener.messages), 5)
            note = next(m for m in listener.messages if m.get("type") == "room_unavailable")
            assert note["reason"] == "unknown_or_ended"
            assert await listener.wait_for(lambda: listener.closed, 5)
            assert listener.close_code == 4404
        finally:
            await listener.close()
        after = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
        assert after == before
        assert "nosuchroom" not in app.state.rooms
