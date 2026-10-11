"""QA 全站測試長 D1 (delete never reached the ledger), D2 (cross-room stale skip),
D3 (merge carry survives delete/clear)."""
import asyncio
import sqlite3
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.admin import db, search
from tests.test_pipeline_repair import app_for, auth, push, settings_with, stop, token_of
from tests.test_translate_backpressure import Gate


def rows(path, sql, args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


async def _ledger_app(monkeypatch, tmp_path, **kw):
    path = tmp_path / "zen.sqlite3"
    monkeypatch.setenv("ZEN_LEDGER", "1")
    monkeypatch.setenv("ZEN_DB_PATH", str(path))
    app = app_for(settings=settings_with(allow_testclient=True, data_path=""), **kw)
    return app, path


@pytest.mark.anyio
@pytest.mark.parametrize("scope", ["single", "room"])
async def test_host_delete_reaches_ledger(monkeypatch, tmp_path, scope):
    app, path = await _ledger_app(monkeypatch, tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            assert (await push(client, token, "class", "s", 1, "秘密內容".encode())).status_code == 200
            assert (await push(client, token, "class", "s", 2, "留下來".encode())).status_code == 200
            assert await asyncio.to_thread(app.state.ledger.wait_idle, 5)
            params = {"room_id": "class", "id": "class:s:1"} if scope == "single" else {"room_id": "class"}
            r = await client.delete("/api/captions", params=params, headers=auth(token))
            assert r.status_code == 200, r.text
            assert await asyncio.to_thread(app.state.ledger.wait_idle, 5)
    finally:
        await stop(app)
    status = dict(rows(path, "SELECT seq, status FROM segments"))
    assert status[1] == "deleted"
    assert status[2] == ("deleted" if scope == "room" else status[2])
    if scope == "single":
        assert status[2] != "deleted"
    c = db.connect(path)
    try:
        hits, _ = search.search_transcripts(c, "秘密內容")
        assert hits == []
        # the text itself is gone from the ledger and the FTS index, not just hidden
        assert c.execute("SELECT COUNT(*) FROM transcripts WHERE text LIKE '%秘密%'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM transcripts_fts WHERE transcripts_fts MATCH '\"秘密內容\"'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM transcripts_fts_uni WHERE transcripts_fts_uni MATCH '\"秘密內容\"'").fetchone()[0] == 0
        assert all(v == "ok" for v in db.fts_integrity(c).values())
        kept = c.execute("SELECT COUNT(*) FROM transcripts WHERE text LIKE '%留下來%'").fetchone()[0]
        assert kept == (0 if scope == "room" else 1)
    finally:
        c.close()


def test_late_update_never_resurrects_deleted(tmp_path):
    from app.ledger import ledger_from_env
    path = tmp_path / "zen.sqlite3"
    led = ledger_from_env({"ZEN_LEDGER": "1", "ZEN_DB_PATH": str(path)})
    base = {"type": "caption", "room_id": "r", "session_id": "s", "seq": 1, "id": "r:s:1", "t0_ms": 0, "t1_ms": 1}
    led.submit({**base, "zh": "第一版", "status": "ready"})
    led.submit({"type": "caption_deleted", "room_id": "r", "id": "r:s:1"})
    led.submit({**base, "zh": "第一版", "en": "late EN", "status": "ready"})
    assert led.wait_idle(5)
    led.close()
    assert rows(path, "SELECT status FROM segments")[0][0] == "deleted"
    assert rows(path, "SELECT count(*) FROM translations")[0][0] == 0


@pytest.mark.anyio
async def test_cross_room_queue_does_not_skip_last_line(monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_S", "0.2")
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_POLICY", "skip")
    tr = Gate()
    app = app_for(translator=tr, settings=settings_with(allow_testclient=True, translate_workers=1,
                                                        translate_queue=4, translate_timeout_s=5))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, "rooma", "s", 1, "甲一".encode(),
                                             wait_translation="0", async_header=True))
            assert await asyncio.to_thread(tr.started.wait, 2)
            assert (await push(client, token, "rooma", "s", 2, "甲二".encode(), wait_translation="0",
                               async_header=True)).status_code == 200
            assert (await push(client, token, "roomb", "s", 1, "乙一".encode(), wait_translation="0",
                               async_header=True)).status_code == 200
            await asyncio.sleep(0.35)
            tr.release.set()
            await asyncio.wait_for(first, 3)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                a = {i["seq"]: i for i in app.state.bus.caption_state("rooma")}
                if a.get(2, {}).get("en") or a.get(2, {}).get("translate_status") == "skipped_backlog":
                    break
                await asyncio.sleep(0.02)
            assert a[2].get("translate_status") != "skipped_backlog", a[2]
            assert a[2].get("en") == "EN 甲二"
    finally:
        tr.release.set()
        await stop(app)


def test_delete_and_clear_drop_merge_carry():
    app = app_for()
    p = app.state.pipeline
    p._merge_carry[("class", "s")] = (1, "私密", 1)
    p._merge_carry[("other", "s")] = (1, "別房", 1)
    p.delete_segment("class", "s", 1)
    assert ("class", "s") not in p._merge_carry and ("other", "s") in p._merge_carry
    p._merge_carry[("class", "s")] = (1, "私密", 1)
    p.invalidate_room("class")
    assert ("class", "s") not in p._merge_carry
