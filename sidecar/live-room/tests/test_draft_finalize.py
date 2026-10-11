"""grok/fast-draft-asr: endpoint-final mode. Draft -> server-cut utterance -> Breeze final, same id.

* drafts arrive while speaking; at a pause the server hands that utterance to the normal pipeline
  under the draft's seq, so the final caption replaces the grey draft in place;
* only finals are translated (drafts never carry ``en``);
* the next utterance gets seq + 1; flush on disconnect finalises the last open sentence;
* off unless BREEZE_ENDPOINT_FINAL=1 *and* the host asked for it (current behaviour unchanged).
"""
import asyncio
import json
import wave
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.draft_asr import DraftPartial
from app.draft_hub import DraftHub
from app.endpointer import EndpointConfig, Endpointer
from app.server import create_app
from app.settings import Settings
from app.translate import TranslateResult, Translator
from tests.test_endpointer import silence, tone
from tests.test_pipeline_repair import copy_decoder
from tests.test_round2 import Socket, auth, open_room, stop, token_of


class LoudDraft:
    """One '字' per loud packet; reset clears it (like a sherpa stream restart)."""
    def __init__(self):
        self.text = ""

    def feed(self, pcm):
        import array
        a = array.array("h")
        a.frombytes(pcm)
        if a and max(abs(x) for x in a) > 2000:
            self.text += "字"
            return [DraftPartial(self.text, False, 0)]
        return []

    def finish(self):
        return []

    def reset(self):
        self.text = ""


class WavAsr:
    """Final text = duration of the utterance it was given (proves it got the cut audio)."""
    def __init__(self):
        self.calls = []

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        with wave.open(str(wav)) as w:
            ms = w.getnframes() * 1000 // w.getframerate()
        self.calls.append(ms)
        return AsrResult(ok=True, text=f"定稿{len(self.calls)}")


class English(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.seen = []

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        self.seen.append(zh)
        return TranslateResult("EN " + zh, "ok")


def test_hub_cuts_seq_and_restarts_engine():
    hub = DraftHub(LoudDraft, publish=lambda e: None, clock=iter(range(1000)).__next__)
    sess = hub.open("r", "s", endpointer=Endpointer(EndpointConfig()), t0_offset_ms=1000)
    finals = []
    pcm = silence(0.3) + tone(1.2) + silence(0.6) + tone(1.0) + silence(0.6)
    for i in range(0, len(pcm), 3200):
        ev, rows = hub.feed_full(sess, pcm[i:i + 3200])
        finals += rows
        if ev:
            assert ev["status"] == "draft" and "en" not in ev
    assert [f.seq for f in finals] == [1, 2]
    assert finals[0].t0_ms >= 1000, "host t0 offset applied"
    assert sess.seq == 3 and sess.partial == "" and sess.engine.text == ""
    assert hub.flush(sess) == []
    assert hub.stats()["draft_finalized"] == 2


def test_note_pushed_is_monotonic_with_server_cuts():
    hub = DraftHub(LoudDraft, publish=lambda e: None)
    sess = hub.open("r", "s", endpointer=Endpointer())
    for i, chunk in enumerate([tone(1.2), silence(0.6)]):
        hub.feed_full(sess, chunk[:32000])
        hub.feed_full(sess, chunk[32000:])
    assert sess.seq == 2
    hub.note_pushed("r", "s", 1)          # a late host push for seq 1 does not move seq back
    assert sess.seq == 2


def _app(asr, translator):
    return create_app(Settings(allow_testclient=True, gap_wait_s=0.2), asr=asr, translator=translator,
                      decoder=copy_decoder, draft_factory=LoudDraft)


async def _drain(sock, kinds, tries=40, identifier=None):
    for _ in range(tries):
        msg = await sock.recv(timeout=5)
        if msg.get("type") in kinds and (identifier is None or msg.get("id")==identifier):
            return msg
    raise AssertionError(f"no {kinds}")


async def _send_pcm(host, pcm):
    for i in range(0, len(pcm), 3200):
        await host.inc.put({"type": "websocket.receive", "bytes": pcm[i:i + 3200]})


@pytest.mark.anyio
async def test_endpoint_final_end_to_end_replaces_draft_and_translates_finals_only(monkeypatch):
    monkeypatch.setenv("BREEZE_ENDPOINT_FINAL", "1")
    asr, en = WavAsr(), English()
    app = _app(asr, en)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            setup = (await client.get("/api/setup?room_id=class", headers=auth(token))).json()
            assert setup["draft_available"] is True and setup["endpoint_final"] is True
            async with Socket(app, "/ws/listen?room_id=class") as listener:
                await _drain(listener, {"hello"})
                async with Socket(app, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
                    await host.inc.put({"type": "websocket.receive",
                                        "text": json.dumps({"token": token, "finalize": True, "t0_ms": 500})})
                    assert await host.recv() == {"type": "draft_on", "seq": 1, "finalize": True}
                    await _send_pcm(host, silence(0.3) + tone(1.2))
                    d = await _drain(listener, {"draft"})
                    assert d["id"] == "class:s1:1" and "en" not in d
                    await _send_pcm(host, silence(0.6))
                    final = await _drain(listener, {"caption"})
                    note = await host.recv(timeout=5)
                    assert note["type"] == "final_seq" and note["seq"] == 1 and note["t0_ms"] >= 500
                    assert final["id"] == d["id"] and final["zh"].startswith("定稿1")
                    # next sentence is seq 2, and its draft is a new id
                    await _send_pcm(host, tone(1.0))
                    d2 = await _drain(listener, {"draft"})
                    assert d2["id"] == "class:s1:2"
                    await host.inc.put({"type": "websocket.receive", "text": '{"type":"flush"}'})
                    assert (await _drain(host, {"final_seq"}))["seq"] == 2
                    assert (await _drain(host, {"flushed"}))["seq"] == 2, "flush ack comes after the last final_seq"
                    # Translation for seq 1 may arrive after the draft for seq 2.
                    # Match the required caption, rather than relying on thread timing.
                    final2 = await _drain(listener, {"caption"}, identifier="class:s1:2")
                    assert final2["id"] == "class:s1:2"
                # translations: finals only; let the async translate queue run
                for _ in range(40):
                    if len(en.seen) >= 2:
                        break
                    await asyncio.sleep(0.05)
                assert all(z.startswith("定稿") for z in en.seen) and len(en.seen) >= 2
                assert all("字" not in z for z in en.seen), "drafts are never translated"
            # each Breeze call got one short utterance, not a 6 s slice
            assert asr.calls and max(asr.calls) <= 4500
            exported = await client.get("/api/export?room_id=class&kind=srt", headers=auth(token))
            assert "定稿1" in exported.text and "字字" not in exported.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_finalize_needs_both_server_flag_and_host_opt_in(monkeypatch):
    monkeypatch.delenv("BREEZE_ENDPOINT_FINAL", raising=False)
    app = _app(WavAsr(), English())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            setup = (await client.get("/api/setup?room_id=class", headers=auth(token))).json()
            assert setup["endpoint_final"] is False
            async with Socket(app, "/ws/draft?room_id=class&session_id=s1", with_key=False) as host:
                await host.inc.put({"type": "websocket.receive", "text": json.dumps({"token": token, "finalize": True})})
                assert await host.recv() == {"type": "draft_on", "seq": 1}, "server flag off: legacy drafts"
    finally:
        await stop(app)
