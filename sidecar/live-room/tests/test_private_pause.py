"""round3 §1.3-1 private pause: queued + in-flight slices are dropped (never published, never in
the export/ledger), uploads during pause are not decoded, listeners see paused/resumed, and a
slice captured before resume + grace is dropped too."""
import asyncio
import json
import threading

import pytest


@pytest.mark.anyio
async def test_reopening_a_paused_room_reports_pause_until_explicit_resume():
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, 'class')
        paused = await client.post('/api/rooms/class/pause', headers=auth(token))
        assert paused.status_code == 200
        reopened = await client.post('/api/rooms/open', headers=auth(token), json={'room_id': 'class'})
        assert reopened.status_code == 200 and reopened.json()['paused'] is True
        resumed = await client.post('/api/rooms/class/resume', headers=auth(token))
        assert resumed.status_code == 200
        reopened = await client.post('/api/rooms/open', headers=auth(token), json={'room_id': 'class'})
        assert reopened.json()['paused'] is False

from app.pipeline import Pipeline
from tests.sim import Listener, ScriptedTranslator, TextAsr, export_json, post_segment, serving, sim_settings
from tests.test_round2 import auth, open_room


def _gate():
    return {"started": threading.Event(), "release": threading.Event()}


async def _wait(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


@pytest.mark.anyio
async def test_pause_drops_inflight_and_queued_and_refuses_new(monkeypatch):
    monkeypatch.setattr(Pipeline, "PAUSE_RESUME_GRACE_S", 0.05)
    gate = _gate()
    asr = TextAsr(0, gate=lambda text: gate if text.startswith("秘密二") else None)
    async with serving(asr=asr, settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as ear:
            r1 = await post_segment(client, token, "class", "s", 1, "第一句".encode(), 0, 6000)
            assert r1.status_code == 200, r1.text
            t2 = asyncio.create_task(post_segment(client, token, "class", "s", 2, "秘密二".encode(), 6000, 12000))
            assert await asyncio.to_thread(gate["started"].wait, 5)
            t3 = asyncio.create_task(post_segment(client, token, "class", "s", 3, "秘密三".encode(), 12000, 18000))
            await asyncio.sleep(0.05)
            p = await client.post("/api/rooms/class/pause", headers=auth(token))
            assert p.status_code == 200 and p.json()["paused"] is True and p.json()["voided"] >= 1
            gate["release"].set()
            r2, r3 = await asyncio.gather(t2, t3)
            assert r2.status_code in (200, 409) and r3.status_code in (200, 409)
            for r in (r2, r3):
                if r.status_code == 200:
                    assert r.json().get("status") == "cancelled" and not r.json().get("zh")
            r4 = await post_segment(client, token, "class", "s", 4, "秘密四".encode(), 18000, 24000)
            assert r4.status_code == 409 and "私密暫停" in r4.text
            assert "秘密四" not in asr.seen                       # never decoded
            q = await client.post("/api/rooms/class/resume", headers=auth(token))
            assert q.status_code == 200 and q.json()["paused"] is False
            r5 = await post_segment(client, token, "class", "s", 5, "秘密五".encode(), 24000, 30000)
            assert r5.status_code == 409                           # 6 s slice began before resume
            await asyncio.sleep(0.1)
            r6 = await post_segment(client, token, "class", "s", 6, "恢復後".encode(), 30000, 30010)
            assert r6.status_code == 200, r6.text
            assert await _wait(lambda: any(m.get("zh") == "恢復後" for m in ear.messages))
            kinds = [m.get("type") for m in ear.messages]
            assert "paused" in kinds and "resumed" in kinds and kinds.index("paused") < kinds.index("resumed")
            shown = json.dumps(ear.messages, ensure_ascii=False)
            assert "秘密" not in shown and "第一句" in shown
        exported = json.dumps(await export_json(client, token, "class"), ensure_ascii=False)
        assert "秘密" not in exported and "第一句" in exported and "恢復後" in exported


@pytest.mark.anyio
async def test_pause_drops_pending_english_of_shown_lines(monkeypatch):
    block = threading.Event()
    tr = ScriptedTranslator(lambda zh: ("block", block))
    async with serving(translator=tr, settings=sim_settings(translate=True)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as ear:
            r1 = await post_segment(client, token, "class", "s", 1, "中文已顯示".encode(), 0, 6000)
            assert r1.status_code == 200
            assert await _wait(lambda: tr.started)
            p = await client.post("/api/rooms/class/pause", headers=auth(token))
            assert p.json()["english_dropped"] == 1
            block.set()
            await asyncio.sleep(0.2)
            assert not any(m.get("en") for m in ear.messages)      # the English never lands


@pytest.mark.anyio
async def test_pause_requires_host_token():
    async with serving() as (app, client, token):
        for path in ("/api/rooms/class/pause", "/api/rooms/class/resume"):
            r = await client.post(path, headers={"origin": "http://127.0.0.1"})
            assert r.status_code in (401, 403)
