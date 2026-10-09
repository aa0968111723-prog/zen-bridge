import asyncio
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.pipeline import Segment
from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import app_for, auth, push, settings_with, stop, token_of


class HoldEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline, cancel
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        return TranslateResult("EN " + zh, "ok")


@pytest.mark.anyio
async def test_async_retry_duplicate_returns_at_zh():
    """A recorder retry of the same digest must honour wait_translation=0 while English is stuck."""
    translator = HoldEnglish()
    app = app_for(
        translator=translator,
        settings=settings_with(
            allow_testclient=True,
            translate_workers=1,
            translate_timeout_s=5,
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await asyncio.wait_for(
                push(client, token, "class", "s", 1, "新".encode(), wait_translation="0", async_header=True),
                2,
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "zh_ready"
            assert await asyncio.to_thread(translator.started.wait, 2)
            started = time.monotonic()
            again = await asyncio.wait_for(
                push(
                    client, token, "class", "s", 1, "新".encode(),
                    wait_translation="0", async_header=True, retry=True,
                ),
                0.5,
            )
            assert time.monotonic() - started < 0.5
            assert again.status_code == 200, again.text
            body = again.json()
            assert body["status"] == "zh_ready"
            assert body["zh"] == "新"
            assert not body.get("en")
            blocked = asyncio.create_task(
                push(client, token, "class", "s", 1, "新".encode(), retry=True),
            )
            await asyncio.sleep(0.3)
            assert not blocked.done()
            translator.release.set()
            synced = await asyncio.wait_for(blocked, 3)
            assert synced.status_code == 200, synced.text
            assert synced.json()["status"] == "ready"
            assert synced.json()["en"] == "EN 新"
    finally:
        translator.release.set()
        await stop(app)


@pytest.mark.anyio
async def test_full_queue_skips_oldest_not_the_new_segment():
    translator = HoldEnglish()
    app = app_for(
        translator=translator,
        settings=settings_with(
            allow_testclient=True,
            translate_workers=1,
            translate_queue=1,
            translate_timeout_s=3,
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(
                client, token, "class", "s", 1, "新".encode(),
                wait_translation="0", async_header=True,
            ))
            assert await asyncio.to_thread(translator.started.wait, 2)
            pipe = app.state.pipeline
            extra = Segment(room_id="class", session_id="s", seq=9, zh="重", status="zh_ready", version=1)
            pipe._stamp_gen(extra)
            pipe.results[extra.key] = extra
            pipe._emitted_segs.add(extra.key)
            pipe._put_translation(extra)
            pipe._put_translation(extra)
            queued = [item for item in pipe._translate_q._queue if item[2].key == extra.key]
            assert len(queued) == 1
            pipe._drop_queued(extra.key)
            second = await push(
                client, token, "class", "s", 2, "舊".encode(),
                wait_translation="0", async_header=True,
            )
            third = await push(
                client, token, "class", "s", 3, "最新".encode(),
                wait_translation="0", async_header=True,
            )
            assert second.status_code == 200 and third.status_code == 200
            assert third.json()["translate_status"] != "queue_full"
            await asyncio.sleep(0.05)
            assert pipe.translate_skipped >= 1
            skipped = [
                item for item in app.state.bus.caption_state("class")
                if item.get("translate_status") == "skipped_backlog"
            ]
            assert skipped
            assert skipped[0]["zh"] == "舊"
            assert skipped[0]["status"] == "translate_failed"
            assert skipped[0]["en"] == ""
            translator.release.set()
            await asyncio.wait_for(first, 2)
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                latest = {item["seq"]: item for item in app.state.bus.caption_state("class")}
                if latest.get(3, {}).get("en") == "EN 最新":
                    break
                await asyncio.sleep(0.02)
            latest = {item["seq"]: item for item in app.state.bus.caption_state("class")}
            assert latest[3]["zh"] == "最新"
            assert latest[3]["en"] == "EN 最新"
            assert latest[3]["translate_status"] != "queue_full"
            missing = await client.post(
                "/api/segment/missing",
                json={"room_id": "class", "session_id": "s", "seq": 4, "reason": "主持端放棄這段"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert missing.status_code == 200
            revived = await client.post(
                "/api/segment/retranslate",
                json={"room_id": "class", "session_id": "s", "seq": 4, "zh": "不該復活"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert revived.status_code == 409
            assert "不能重譯" in revived.text
            kept = {item["seq"]: item for item in app.state.bus.caption_state("class")}
            assert kept[4]["status"] == "missing"
            assert kept[4]["zh"] != "不該復活"
    finally:
        translator.release.set()
        await stop(app)
