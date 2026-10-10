"""全站 D4: a merged MT line is marked (event en_merged_from -> translations.status='merged') and a
correction on it never goes straight into TM."""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app import feedback
from app.admin import db
from app.ledger import Ledger
from tests.test_corrections_stick import cap
from tests.test_pipeline_repair import app_for, push, settings_with, stop, token_of
from tests.test_translate_backpressure import Gate


@pytest.mark.anyio
async def test_merged_translation_event_names_merged_seqs(monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE_LATE_POLICY", "publish")   # tests the dequeue rule only
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
            for seq, zh in ((2, "甲句"), (3, "乙句")):
                await push(client, token, "class", "s", seq, zh.encode(), wait_translation="0", async_header=True)
            await asyncio.sleep(0.35)
            tr.release.set()
            await asyncio.wait_for(first, 3)
            loop = asyncio.get_running_loop(); end = loop.time() + 3
            while loop.time() < end:
                evs = [e for e in app.state.pipeline.events if e.get("seq") == 3 and e.get("en")]
                if evs:
                    break
                await asyncio.sleep(0.02)
    finally:
        tr.release.set()
        await stop(app)
    assert evs and evs[-1].get("en_merged_from") == [2], evs
    plain = [e for e in app.state.pipeline.events if e.get("seq") == 1 and e.get("en")]
    assert plain and "en_merged_from" not in plain[-1]


def test_ledger_marks_merged_and_correction_waits_for_review(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(cap(seq=3, zh="乙句", status="ready", en="EN 甲句，乙句", translate_status="ok", en_merged_from=[2]))
    led.submit(cap(seq=4, zh="丙句", status="ready", en="EN 丙句", translate_status="ok"))
    assert led.wait_idle(5)
    led.close()
    c = db.connect(path)
    st = dict(c.execute("SELECT segment_id, status FROM translations WHERE is_current=1").fetchall())
    assert st == {"class:s1:3": "merged", "class:s1:4": "ok"}
    for seg in ("class:s1:3", "class:s1:4"):
        corr = feedback.record_correction(c, segment_id=seg, target_type="translation", text="Fixed line.")
        out = feedback.promote_correction(c, corr["correction_id"], reviewed=True)
        q = c.execute("SELECT quality FROM tm_units WHERE id=?", (out["tm_id"],)).fetchone()[0]
        assert q == (feedback.TM_PENDING if seg.endswith(":3") else feedback.TM_LIVE), seg
    c.close()
