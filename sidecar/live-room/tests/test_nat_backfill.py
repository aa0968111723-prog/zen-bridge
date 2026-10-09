"""Classroom NAT: many listeners on one public IP all get the restart backfill.

A pure per-IP cap of 8 left everyone after the eighth phone with a hello that
had no backfill and no reason. The client then cleared the screen. Denied
replays are explicit (backfill_deferred + retry_after) and do not include
another room's captions.
"""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.settings import Settings
from app.translate import Translator
from tests.test_listen_key import _hello, _zh, drive
from tests.test_round2 import app_for, open_room, push, stop, token_of


LINES = ("甲", "乙", "丙", "丁")


async def _class_with_lines(tmp_path, **settings):
    path = tmp_path / "captions.sqlite3"
    base = dict(allow_testclient=True, translate=False, data_path=str(path), max_listeners=80)
    base.update(settings)
    app = app_for(settings=Settings(**base), translator=Translator(enabled=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        await open_room(client, token, "class")
        await open_room(client, token, "other")
        for seq, text in enumerate(LINES, start=1):
            made = await push(client, token, "class", "s", seq, text.encode(), t0_ms=(seq - 1) * 1000, t1_ms=seq * 1000)
            assert made.status_code == 200, made.text
        other = await push(client, token, "other", "s", 1, "別班".encode(), t0_ms=0, t1_ms=1000)
        assert other.status_code == 200, other.text
    await stop(app)
    return path


@pytest.mark.anyio
async def test_sixty_listeners_same_ip_get_full_backfill_after_restart(tmp_path):
    path = await _class_with_lines(tmp_path)
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, data_path=str(path), max_listeners=80),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "other")

            async def one(index):
                # Stale cursor from before the restart. Same public IP, own client id.
                return await drive(
                    app,
                    f"/ws/listen?room_id=class&cursor=5000&cid=phone{index}",
                    client=("203.0.113.50", 10000 + index),
                )

            messages = await asyncio.gather(*(one(index) for index in range(60)))
            assert len(messages) == 60
            for item in messages:
                hello = _hello(item)
                assert hello.get("backfill_deferred") is not True
                assert _zh(hello.get("backfill")) == list(LINES)
                blob = str(hello)
                assert "別班" not in blob

            foreign = _hello(await drive(
                app,
                "/ws/listen?room_id=other&cursor=5000&cid=phone-other",
                client=("203.0.113.50", 20000),
            ))
            assert _zh(foreign.get("backfill")) == ["別班"]
            assert not any(text in _zh(foreign.get("backfill")) for text in LINES)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_deferred_backfill_names_the_wait_and_hides_captions():
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=1,
            replay_client_per_minute=8,
            history_limit=1,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            made = await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)
            assert made.status_code == 200, made.text
            first = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=phone-a", client=("198.51.100.9", 5000),
            ))
            assert _zh(first.get("backfill")) == ["甲", "乙"]
            second = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=phone-b", client=("198.51.100.9", 5001),
            ))
            assert second.get("backfill_deferred") is True
            assert "backfill" not in second
            assert int(second.get("retry_after_ms") or 0) >= 250
            assert int(second.get("retry_after") or 0) == int(second["retry_after_ms"])
            # The live window may still carry the newest line. The older line lives
            # only in the full backfill, and that list is not sent.
            assert "甲" not in str(second)
            assert "乙" in _zh(second.get("history"))
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_replay_limit_is_per_client_as_well_as_per_ip():
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=180,
            replay_client_per_minute=1,
            history_limit=1,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            made = await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)
            assert made.status_code == 200, made.text
            first = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=same-phone", client=("198.51.100.10", 5000),
            ))
            assert _zh(first.get("backfill")) == ["甲", "乙"]
            again = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=same-phone", client=("198.51.100.10", 5001),
            ))
            assert again.get("backfill_deferred") is True
            assert "甲" not in str(again)
            other = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=other-phone", client=("198.51.100.10", 5002),
            ))
            assert _zh(other.get("backfill")) == ["甲", "乙"]
    finally:
        await stop(app)


def _wire_bytes(rows) -> int:
    return len(json.dumps(list(rows), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _publish_lines(app, room, count, zh_for):
    bus = app.state.bus
    for seq in range(1, count + 1):
        bus.publish({
            "type": "caption",
            "id": f"{room}:s:{seq}",
            "room_id": room,
            "session_id": "s",
            "session_ord": 1,
            "seq": seq,
            "version": 1,
            "zh": zh_for(seq),
            "en": "",
            "status": "ready",
            "t0_ms": (seq - 1) * 1000,
            "t1_ms": seq * 1000,
        })


@pytest.mark.anyio
async def test_backfill_is_last_200_and_cid_cannot_raise_the_quota():
    """One replay is the last 200 rows and at most 100 KB.

    A same-epoch gap that is deferred does not include the older rows, and the
    next allowed replay is still that 200-row tail. Omitting the client id, or
    sending one that fails the id shape, shares one per-address bucket, so it
    cannot multiply the per-client quota. Extra well-formed ids cannot pass
    the per-address quota either.
    """
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=3,
            replay_client_per_minute=1,
            history_limit=50,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "wide")
            _publish_lines(app, "class", 210, lambda seq: f"{seq:04d}")
            _publish_lines(app, "wide", 30, lambda seq: ("W" if seq == 30 else "Q") + ("x" * 5000))

            ip = ("203.0.113.40", 4100)
            first = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=phone", client=ip,
            ))
            backfill = first.get("backfill") or []
            assert [item.get("seq") for item in backfill] == list(range(11, 211))
            assert len(backfill) == 200
            assert _wire_bytes(backfill) <= 100 * 1024
            assert "0001" not in _zh(backfill)
            assert backfill[-1]["zh"] == "0210"

            # Same epoch, cursor ahead of the room: still only the tail, then deferred.
            gapped = _hello(await drive(
                app,
                "/ws/listen?room_id=class&cursor=999999999&cid=gap-phone",
                client=("203.0.113.40", 4101),
            ))
            assert gapped.get("gap") is True
            assert gapped.get("backfill_deferred") is not True
            assert [item.get("seq") for item in gapped.get("backfill") or []] == list(range(11, 211))
            assert "0001" not in _zh(gapped.get("backfill"))
            assert "0001" not in _zh(gapped.get("history"))
            assert "0001" not in _zh(gapped.get("events"))

            deferred = _hello(await drive(
                app,
                "/ws/listen?room_id=class&cursor=999999999&cid=gap-phone",
                client=("203.0.113.40", 4102),
            ))
            assert deferred.get("backfill_deferred") is True
            assert "backfill" not in deferred
            assert "0001" not in _zh(deferred.get("history"))
            assert "0001" not in _zh(deferred.get("events"))
            assert int(deferred.get("retry_after_ms") or 0) >= 250

            # Third id on this address spends the last per-address slot.
            third = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=forged-1", client=("203.0.113.40", 4103),
            ))
            assert [item.get("seq") for item in third.get("backfill") or []] == list(range(11, 211))
            blocked = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=forged-2", client=("203.0.113.40", 4104),
            ))
            assert blocked.get("backfill_deferred") is True
            assert "backfill" not in blocked

            # A different address: omitted and malformed ids share one client bucket.
            anon_ip = "198.51.100.40"
            omitted = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1", client=(anon_ip, 4200),
            ))
            assert len(omitted.get("backfill") or []) == 200
            again = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1", client=(anon_ip, 4201),
            ))
            assert again.get("backfill_deferred") is True
            forged = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=bad%20cid", client=(anon_ip, 4202),
            ))
            assert forged.get("backfill_deferred") is True
            long_cid = "a" * 80
            too_long = _hello(await drive(
                app, f"/ws/listen?room_id=class&replay=1&cid={long_cid}", client=(anon_ip, 4203),
            ))
            assert too_long.get("backfill_deferred") is True
            # A real phone id is its own bucket and still cannot be blocked by the anon ones,
            # but the address ceiling remains.
            phone = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=real-phone", client=(anon_ip, 4204),
            ))
            assert len(phone.get("backfill") or []) == 200
            # Anon used one address slot. Two more well-formed ids fill the ceiling of 3.
            phone2 = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=real-phone-2", client=(anon_ip, 4205),
            ))
            assert len(phone2.get("backfill") or []) == 200
            over = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=real-phone-3", client=(anon_ip, 4206),
            ))
            assert over.get("backfill_deferred") is True

            fat = _hello(await drive(
                app, "/ws/listen?room_id=wide&replay=1&cid=fat", client=("203.0.113.77", 4300),
            ))
            fat_rows = fat.get("backfill") or []
            assert fat.get("backfill_deferred") is not True
            assert 1 <= len(fat_rows) < 30
            assert len(fat_rows) <= 200
            assert fat_rows[-1]["zh"].startswith("W")
            assert all(not item["zh"].startswith("Q") or item["seq"] != 1 for item in fat_rows)
            assert fat_rows[0]["seq"] != 1
            assert _wire_bytes(fat_rows) <= 100 * 1024
            one = _wire_bytes(fat_rows[-1:])
            assert one * 30 > 100 * 1024
    finally:
        await stop(app)
