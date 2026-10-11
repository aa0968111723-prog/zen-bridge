"""QA 效能長 P1-15: chained stale lines under the merge policy must all reach the translator."""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_pipeline_repair import app_for, push, settings_with, stop, token_of
from tests.test_translate_backpressure import Gate


@pytest.mark.anyio
async def test_merge_chain_keeps_every_line(monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_S", "0.2")
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_POLICY", "merge")
    tr = Gate()
    app = app_for(translator=tr, settings=settings_with(allow_testclient=True, translate_workers=1,
                                                        translate_queue=4, translate_timeout_s=5))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, "class", "s", 1, "一".encode(), wait_translation="0", async_header=True))
            assert await asyncio.to_thread(tr.started.wait, 2)
            for seq, zh in ((2, "甲句"), (3, "乙句"), (4, "丙句")):
                assert (await push(client, token, "class", "s", seq, zh.encode(), wait_translation="0", async_header=True)).status_code == 200
            await asyncio.sleep(0.35)
            tr.release.set()
            await asyncio.wait_for(first, 3)
            loop = asyncio.get_running_loop(); end = loop.time() + 3
            while loop.time() < end:
                rows = {i["seq"]: i for i in app.state.bus.caption_state("class")}
                if rows.get(4, {}).get("en"):
                    break
                await asyncio.sleep(0.02)
            joined = " ".join(tr.seen)
            assert "甲句" in joined, "seq 2 was merged away and then overwritten by seq 3: its text never reached the translator"
            # every line marked as merged must have been sent somewhere
            for seq, zh in ((2, "甲句"), (3, "乙句"), (4, "丙句")):
                status = rows.get(seq, {}).get("translate_status")
                if status == "skipped_backlog" and "併入" in str(rows.get(seq, {}).get("translate_error") or rows.get(seq, {})):
                    assert zh in joined, (seq, tr.seen)
            assert "丙句" in joined
    finally:
        tr.release.set()
        await stop(app)
