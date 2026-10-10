"""round4 #5: draft captions over /ws/draft -> listeners; final replaces by id; pause drops drafts."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.draft_asr import DraftPartial, NullDraft
from app.draft_hub import DraftHub
from app.settings import Settings
from app.server import create_app
from tests.test_pipeline_repair import EchoAsr, copy_decoder
from app.translate import Translator
from tests.test_round2 import Socket, auth, open_room, push, stop, token_of


class ScriptDraft:
    """Each 100 ms packet adds one character; a packet starting with b'E' is an endpoint."""
    def __init__(self):
        self.text = ""

    def feed(self, pcm):
        if pcm[:1] == b"E":
            done, self.text = self.text, ""
            return [DraftPartial(done, True, 0)]
        self.text += "字"
        return [DraftPartial(self.text, False, 0)]

    def finish(self):
        return []

    def reset(self):
        self.text = ""


def hub(published, paused=lambda r: False):
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]
    return DraftHub(ScriptDraft, publish=published.append, is_paused=paused, clock=clock)


def test_draft_seq_follows_last_push_and_resets():
    out = []
    h = hub(out)
    s = h.open("r", "s")
    assert s.seq == 1
    h.feed_and_emit(s, b"x")
    assert out[-1]["id"] == "r:s:1" and out[-1]["zh"] == "字" and out[-1]["type"] == "draft"
    h.note_pushed("r", "s", 1)
    h.feed_and_emit(s, b"x")
    assert out[-1]["seq"] == 2 and out[-1]["zh"] == "字字", "engine keeps streaming; seq moved on"
    h.feed_and_emit(s, b"E")       # endpoint commits text inside the same seq
    h.feed_and_emit(s, b"x")
    assert out[-1]["zh"].startswith("字字")
    h.note_pushed("r", "s", 1)   # stale/duplicate push does not move seq back
    assert s.seq == 2


def test_paused_room_publishes_nothing_and_drops_text():
    out = []
    paused = {"r": True}
    h = hub(out, paused=lambda r: paused.get(r, False))
    s = h.open("r", "s")
    assert h.feed_and_emit(s, b"x") is None and out == []
    assert h.stats()["draft_dropped_paused"] == 1


def test_oversized_packet_refused_and_null_engine_means_off():
    h = hub([])
    s = h.open("r", "s")
    with pytest.raises(ValueError):
        h.feed(s, b"\0" * 40000)
    off = DraftHub(NullDraft, publish=lambda e: None)
    assert off.available() is False and off.open("r", "s") is None


def test_default_factory_never_loads_a_model_to_probe(monkeypatch):
    monkeypatch.setenv("BREEZE_DRAFT_ASR", "off")
    assert DraftHub(publish=lambda e: None).available() is False
    monkeypatch.setenv("BREEZE_DRAFT_ASR", "xasr")
    assert DraftHub(publish=lambda e: None).available() is True


def _app(factory=ScriptDraft):
    return create_app(Settings(allow_testclient=True), asr=EchoAsr(), translator=Translator(enabled=True, key=""),
                      decoder=copy_decoder, draft_factory=factory)


async def _drain_until(sock, kind, tries=10):
    for _ in range(tries):
        msg = await sock.recv()
        if msg.get("type") == kind:
            return msg
    raise AssertionError(f"no {kind}")


@pytest.mark.anyio
async def test_draft_ws_end_to_end_then_final_same_id():
    app = _app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            setup = (await client.get("/api/setup?room_id=class", headers=auth(token))).json()
            assert setup["draft_available"] is True
            async with Socket(app, "/ws/listen?room_id=class") as listener:
                await _drain_until(listener, "hello")
                async with Socket(app, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
                    await host.inc.put({"type": "websocket.receive", "text": json.dumps({"token": token})})
                    on = await host.recv()
                    assert on == {"type": "draft_on", "seq": 1}
                    await host.inc.put({"type": "websocket.receive", "bytes": b"\1\0" * 1600})
                    d = await _drain_until(listener, "draft")
                    assert d["id"] == "class:s1:1" and d["zh"] == "字" and "en" not in d
                    resp = await push(client, token, "class", "s1", 1, "正式字幕")
                    assert resp.status_code == 200, resp.text
                    final = await _drain_until(listener, "caption")
                    assert final["id"] == d["id"]
                    # drafts are not history: a new listener's hello carries the final only
                async with Socket(app, "/ws/listen?room_id=class") as late:
                    hello = await _drain_until(late, "hello")
                    assert all(row.get("type") != "draft" for row in hello.get("history", []))
                    assert not any(row.get("status") == "draft" for row in hello.get("history", []))
            exported = await client.get("/api/export?room_id=class&kind=srt", headers=auth(token))
            assert "字" not in exported.text.replace("正式字幕", "")
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_draft_ws_rejects_bad_token_and_reports_off():
    app = _app()
    off = _app(NullDraft)
    try:
        async with Socket(app, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
            await host.inc.put({"type": "websocket.receive", "text": json.dumps({"token": "nope"})})
            msg = await host.recv()
            assert msg["type"] == "websocket.close" and msg["code"] == 1008
        async with AsyncClient(transport=ASGITransport(app=off), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(off, client)
            assert (await client.get("/api/setup", headers=auth(token))).json()["draft_available"] is False
            async with Socket(off, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
                await host.inc.put({"type": "websocket.receive", "text": json.dumps({"token": token})})
                assert (await host.recv())["type"] == "draft_off"
    finally:
        await stop(app)
        await stop(off)
