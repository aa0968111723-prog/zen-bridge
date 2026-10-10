"""示意圖 V2: trigger / cooldown / de-dupe / channel / router. Fake LLM + fake clock only.

No model, no network: every LLM here is an in-process fake.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import visual as V
from app.visual_routes import (
    CHANNEL_TYPES,
    VisualChannel,
    VisualHub,
    build_visual_router,
)

pytestmark = pytest.mark.anyio

GLOSSARY = [
    {"zh": "般若", "en": "prajna", "locked": True},
    {"zh": "菩薩", "en": "bodhisattva", "locked": False},
]

GOOD_JSON = json.dumps({
    "kind": "mermaid_flow",
    "title_zh": "般若與菩薩",
    "title_en": "Prajna and the bodhisattva",
    "body_zh": "般若引導菩薩行。",
    "body_en": "Prajna guides the bodhisattva path.",
    "mermaid": "flowchart LR\n  A[般若] --> B[菩薩]",
    "terms": [{"zh": "般若", "en": "wisdom"}, {"zh": "空性", "en": "emptiness"}],
}, ensure_ascii=False)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms


class FakeLLM:
    def __init__(self, reply=GOOD_JSON, *, error: Exception | None = None, delay_s: float = 0.0):
        self.reply = reply
        self.error = error
        self.delay_s = delay_s
        self.calls: list[list[dict]] = []

    async def complete(self, messages, *, timeout_s):
        self.calls.append(messages)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self.reply


def cfg(**kw) -> V.VisualConfig:
    base = dict(min_window_ms=30_000, max_window_ms=60_000, cooldown_ms=30_000, timeout_s=1.0,
                dedupe_window_ms=600_000, queue_size=4)
    base.update(kw)
    return V.VisualConfig(**base)


def final(room: str, seq: int, zh: str, t0: int, t1: int, *, kind: str = "final", session: str = "s1") -> dict:
    return {"type": kind, "id": f"{room}:{session}:{seq}", "room_id": room, "zh": zh,
            "en": f"en {seq}", "t0_ms": t0, "t1_ms": t1}


def window(room: str, start_seq: int, base_ms: int, text: str = "般若引導菩薩行") -> list[dict]:
    """Four finals spanning 32 s: enough for one auto trigger at min_window 30 s."""
    return [final(room, start_seq + i, f"{text}{i}", base_ms + i * 8_000, base_ms + i * 8_000 + 8_000)
            for i in range(4)]


def make_hub(llm=None, *, clock=None, config=None, glossary=GLOSSARY, channel=None) -> VisualHub:
    return VisualHub(llm if llm is not None else FakeLLM(), config=config or cfg(), clock=clock or FakeClock(),
                     glossary_provider=(lambda room: glossary), channel=channel)


async def feed_all(hub: VisualHub, events) -> None:
    for ev in events:
        hub.feed(ev)
    await asyncio.sleep(0)
    await hub.drain()


# ---------------------------------------------------------------------------
# Trigger (pure, fake clock)
# ---------------------------------------------------------------------------

def test_trigger_fires_only_after_min_window_of_finals():
    trig = V.VisualTrigger("r1", cfg(), FakeClock())
    jobs = []
    for ev in window("r1", 0, 0):
        line = V.caption_from_event(ev)
        jobs.append(trig.add(line))
    assert jobs[:3] == [None, None, None]
    job = jobs[3]
    assert job is not None and job.room_id == "r1" and not job.manual
    assert job.source_ids == ["r1:s1:0", "r1:s1:1", "r1:s1:2", "r1:s1:3"]
    assert (job.t0_ms, job.t1_ms) == (0, 32_000)


def test_non_final_and_malformed_events_are_ignored():
    assert V.caption_from_event(final("r1", 0, "般若", 0, 1000, kind="update")) is None
    assert V.caption_from_event({"type": "final", "id": "x", "zh": "", "t0_ms": 0, "t1_ms": 1}) is None
    assert V.caption_from_event({"type": "final", "id": "x", "zh": "a", "t0_ms": 5, "t1_ms": 1}) is None
    assert V.caption_from_event({"type": "final", "id": "x", "zh": "a", "t0_ms": True, "t1_ms": 1}) is None


def test_cooldown_blocks_then_releases_with_fake_clock():
    clock = FakeClock()
    trig = V.VisualTrigger("r1", cfg(cooldown_ms=30_000), clock)
    assert [trig.add(V.caption_from_event(e)) for e in window("r1", 0, 0)][-1] is not None
    clock.advance(10_000)
    assert [trig.add(V.caption_from_event(e)) for e in window("r1", 10, 40_000, "第二段講菩薩")][-1] is None
    assert trig.skipped["cooldown"] >= 1
    clock.advance(25_000)  # 35 s after the first fire
    assert trig.add(V.caption_from_event(final("r1", 20, "第三段般若", 80_000, 82_000))) is not None


def test_dedup_uses_normalized_hash_within_window_only():
    clock = FakeClock()
    trig = V.VisualTrigger("r1", cfg(cooldown_ms=1, dedupe_window_ms=600_000), clock)
    lines_a = [V.CaptionLine(f"a{i}", f"般若，引導 菩薩行{i}。", "", 0, 1000) for i in range(3)]
    lines_b = [V.CaptionLine(f"b{i}", f"般若引導菩薩行{i}", "", 0, 1000) for i in range(3)]  # same text, other ids/punct
    lines_c = [V.CaptionLine(f"c{i}", f"ＡＢＣ般若引導菩薩行{i}!!", "", 0, 1000) for i in range(3)]
    assert V.normalize_for_hash("ＡＢＣ 般若，") == V.normalize_for_hash("abc般若")
    for line in lines_a:
        trig._recent.append(line)
    assert trig.manual() is not None
    clock.advance(1_000)
    trig._recent.clear()
    trig._recent.extend(lines_b)
    assert trig.manual() is None and trig.skipped["duplicate"] == 1
    clock.advance(600_001)  # window expired: same content may fire again
    assert trig.manual() is not None
    clock.advance(1_000)
    trig._recent.clear()
    trig._recent.extend(lines_c)  # "ABC" prefix makes it different content
    assert trig.manual() is not None


def test_manual_force_bypasses_cooldown_and_dedupe():
    clock = FakeClock()
    trig = V.VisualTrigger("r1", cfg(), clock)
    trig._recent.append(V.CaptionLine("a", "般若", "", 0, 10))
    assert trig.manual() is not None
    assert trig.manual() is None
    assert trig.manual(force=True) is not None


# ---------------------------------------------------------------------------
# Hub + channel (real loop, fake LLM, fake clock)
# ---------------------------------------------------------------------------

async def test_hub_broadcasts_llm_draft_to_room_subscriber():
    llm = FakeLLM()
    hub = make_hub(llm)
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    assert len(llm.calls) == 1
    msg = sub.get_nowait()
    assert msg["type"] == "visual_draft" and msg["room_id"] == "r1" and msg["origin"] == "llm"
    assert msg["kind"] == "mermaid_flow" and msg["mermaid"].startswith("flowchart")
    # glossary wins over the model's term
    assert {"zh": "般若", "en": "prajna", "locked": True} in msg["terms"]
    assert hub.channel.history("r1")[-1]["id"] == msg["id"]
    await hub.stop()


async def test_per_room_cooldown_and_room_isolation():
    clock = FakeClock()
    llm = FakeLLM()
    hub = make_hub(llm, clock=clock)
    await hub.start()
    sub_a, sub_b = hub.channel.subscribe("roomA"), hub.channel.subscribe("roomB")
    await feed_all(hub, window("roomA", 0, 0))
    assert sub_a.pending == 1 and sub_b.pending == 0
    # roomA is cooling down, roomB is not affected by it
    await feed_all(hub, window("roomA", 10, 40_000, "甲房第二段"))
    await feed_all(hub, window("roomB", 0, 0, "乙房講菩薩"))
    assert sub_a.pending == 1
    assert sub_b.pending == 1
    assert sub_b.get_nowait()["room_id"] == "roomB"
    assert all(d["room_id"] == "roomA" for d in hub.channel.history("roomA"))
    assert hub.stats("roomA")["skipped"]["cooldown"] >= 1
    assert hub.stats("roomB")["skipped"]["cooldown"] == 0
    await hub.stop()


async def test_hub_ignores_non_final_and_bad_room_ids():
    llm = FakeLLM()
    hub = make_hub(llm)
    await hub.start()
    assert hub.feed(final("r1", 0, "般若", 0, 40_000, kind="update")) is False
    assert hub.feed(final("bad room!", 0, "般若", 0, 40_000)) is False
    assert hub.feed({"type": "captions_cleared", "room_id": "r1"}) is False
    await hub.drain()
    assert llm.calls == []
    await hub.stop()


async def test_disabled_hub_without_llm_does_nothing():
    hub = VisualHub(None, config=cfg())
    assert hub.enabled is False
    assert hub.feed(window("r1", 0, 0)[-1]) is False
    assert hub.trigger("r1") is False


def test_channel_is_separate_from_captions():
    channel = VisualChannel()
    sub = channel.subscribe("r1")
    for bad in ({"type": "final", "zh": "字幕"}, {"type": "update"}, {"type": "room"}, "final", None):
        with pytest.raises(ValueError):
            channel.publish("r1", bad)
    with pytest.raises(ValueError):
        channel.publish("r1", {"type": "visual_draft", "room_id": "r2"})
    assert sub.pending == 0 and channel.rejected == 6
    assert CHANNEL_TYPES == {"visual_draft", "visual_hello"}
    # router does not touch the caption endpoints
    router = build_visual_router(VisualHub(None))
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/ws/visual" in paths and "/ws/listen" not in paths and "/ws/host" not in paths


def test_slow_subscriber_drops_oldest_and_does_not_block_others():
    channel = VisualChannel(queue_size=3)
    slow, fast = channel.subscribe("r1"), channel.subscribe("r1")
    got_fast = []
    for i in range(10):
        assert channel.publish("r1", {"type": "visual_draft", "room_id": "r1", "id": f"d{i}"}) == 2
        got_fast.append(fast.get_nowait()["id"])  # fast reader keeps up
    assert got_fast == [f"d{i}" for i in range(10)] and fast.dropped == 0
    assert slow.pending == 3 and slow.dropped == 7
    assert [slow.get_nowait()["id"] for _ in range(3)] == ["d7", "d8", "d9"]
    # published payloads are detached copies
    src = {"type": "visual_draft", "room_id": "r1", "id": "x", "terms": [{"zh": "a"}]}
    channel.publish("r1", src)
    src["terms"][0]["zh"] = "mutated"
    assert fast.get_nowait()["terms"][0]["zh"] == "a"


async def test_subscriber_get_wakes_on_offer_and_on_close():
    channel = VisualChannel()
    sub = channel.subscribe("r1")
    waiter = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    channel.publish("r1", {"type": "visual_draft", "room_id": "r1", "id": "d1"})
    assert (await asyncio.wait_for(waiter, 1))["id"] == "d1"
    waiter = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    channel.unsubscribe(sub)
    assert await asyncio.wait_for(waiter, 1) is None
    assert channel.subscriber_count("r1") == 0


async def test_llm_error_falls_back_to_glossary_card():
    hub = make_hub(FakeLLM(error=RuntimeError("boom")))
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    msg = sub.get_nowait()
    assert msg["origin"] == "fallback" and msg["kind"] == "concept_card"
    assert [t["zh"] for t in msg["terms"]] == ["般若", "菩薩"]
    await hub.stop()


async def test_llm_timeout_falls_back_quickly():
    hub = make_hub(FakeLLM(delay_s=30.0), config=cfg(timeout_s=0.05))
    await hub.start()
    sub = hub.channel.subscribe("r1")
    loop = asyncio.get_running_loop()
    started = loop.time()
    await feed_all(hub, window("r1", 0, 0))
    assert loop.time() - started < 5.0
    assert sub.get_nowait()["origin"] == "fallback"
    await hub.stop()


async def test_llm_failure_without_glossary_hits_broadcasts_nothing():
    hub = make_hub(FakeLLM(error=ValueError("bad")), glossary=[])
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    assert sub.pending == 0 and hub.suppressed == 1 and hub.channel.history("r1") == []
    await hub.stop()


@pytest.mark.parametrize("reply", [
    "not json at all",
    "",
    json.dumps({"kind": "concept_card", "title_zh": "", "body_zh": "  ", "body_en": "", "terms": []}),
    json.dumps({"kind": "concept_card", "title_zh": "。。。", "body_zh": "……", "terms": "x"}),
    "<think>只有推理，沒有答案 {\"kind\":\"concept_card\",\"title_zh\":\"x\",\"body_zh\":\"y\"}",
    "[1, 2, 3]",
])
async def test_garbage_llm_output_is_never_broadcast(reply):
    hub = make_hub(FakeLLM(reply), glossary=[])
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    assert sub.pending == 0
    assert hub.suppressed == 1
    await hub.stop()


async def test_think_reasoning_is_stripped():
    reply = ("<think>先想想：講者提到般若……\n{\"title_zh\": \"錯的\"}</think>\n"
             "```json\n" + json.dumps({"kind": "concept_card", "title_zh": "<think>x</think>般若要點",
                                       "body_zh": "般若是智慧。", "body_en": "Prajna is wisdom.",
                                       "mermaid": "", "terms": []}, ensure_ascii=False) + "\n```")
    hub = make_hub(FakeLLM(reply))
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    msg = sub.get_nowait()
    assert msg["origin"] == "llm" and msg["title_zh"] == "般若要點" and msg["body_zh"] == "般若是智慧。"
    assert "think" not in json.dumps(msg, ensure_ascii=False).lower()
    await hub.stop()


def test_strip_think_variants():
    assert V.strip_think("<think>a</think>b") == "b"
    assert V.strip_think("<THINK type='x'>a\nb</THINK >\n{}") == "{}"
    assert V.strip_think("reasoning only</think>{\"a\":1}") == "{\"a\":1}"
    assert V.strip_think("{\"a\":1}<think>never closed") == "{\"a\":1}"
    assert V.strip_think("<think>never closed {\"a\":1}") == ""
    assert V.strip_think(None) == ""


async def test_unsafe_mermaid_is_downgraded_not_broadcast_raw():
    reply = json.dumps({"kind": "mermaid_flow", "title_zh": "般若", "body_zh": "般若與菩薩",
                        "mermaid": "flowchart LR\n A --> B\n click A \"javascript:alert(1)\""})
    hub = make_hub(FakeLLM(reply))
    await hub.start()
    sub = hub.channel.subscribe("r1")
    await feed_all(hub, window("r1", 0, 0))
    msg = sub.get_nowait()
    assert msg["kind"] == "concept_card" and msg["mermaid"] == ""
    await hub.stop()


async def test_feed_from_another_thread_is_marshalled_to_the_loop():
    llm = FakeLLM()
    hub = make_hub(llm)
    await hub.start()
    sub = hub.channel.subscribe("r1")
    events = window("r1", 0, 0)
    worker = threading.Thread(target=lambda: [hub.feed(e) for e in events])
    worker.start()
    worker.join()
    for _ in range(50):
        await asyncio.sleep(0.01)
        await hub.drain()
        if sub.pending:
            break
    assert sub.get_nowait()["room_id"] == "r1"
    await hub.stop()


# ---------------------------------------------------------------------------
# Default backend is local-only (no network: constructing the client opens nothing)
# ---------------------------------------------------------------------------

def test_default_llm_is_local_only_and_off_by_default():
    assert V.build_llm_from_env({}) is None
    assert V.build_llm_from_env({V.ENV_BASE_URL: "http://127.0.0.1:11434/v1"}) is None  # no model
    remote = {V.ENV_BASE_URL: "https://api.example.com/v1", V.ENV_MODEL: "m"}
    assert V.build_llm_from_env(remote) is None
    assert V.build_llm_from_env({V.ENV_BASE_URL: "http://localhost.evil.com/v1", V.ENV_MODEL: "m"}) is None
    local = V.build_llm_from_env({V.ENV_BASE_URL: "http://127.0.0.1:11434/v1", V.ENV_MODEL: "qwen"})
    assert isinstance(local, V.OpenAICompatLLM) and local._executor is None
    assert VisualHub.from_env({}).enabled is False


# ---------------------------------------------------------------------------
# Router: viewer page, state, trigger, WebSocket channel
# ---------------------------------------------------------------------------

def make_app(hub: VisualHub, **kw) -> FastAPI:
    app = FastAPI()
    app.include_router(build_visual_router(hub, **kw))
    return app


def test_viewer_page_and_script_are_served():
    # CISO P1: the HTTP history is guarded; this test opens it explicitly (server.py passes the listen-key guard)
    with TestClient(make_app(VisualHub(None), http_guard=lambda r, room: True)) as client:
        page = client.get("/visual?room=r1")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "/visual/visual.js" in page.text and "示意圖" in page.text
        js = client.get("/visual/visual.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert "/ws/visual" in js.text and "/ws/listen" not in js.text and "innerHTML" not in js.text
        assert "export " in js.text and 'type="module"' in page.text  # static/*.js are ES modules
        state = client.get("/api/visual/r1").json()
        assert state["enabled"] is False and state["history"] == []
        assert client.get("/api/visual/bad%20room").status_code == 400


def test_websocket_receives_hello_then_live_drafts_for_its_room_only():
    hub = make_hub(FakeLLM())
    with TestClient(make_app(hub, http_guard=lambda r, room: True)) as client:
        with client.websocket_connect("/ws/visual?room_id=r1") as ws_a, \
                client.websocket_connect("/ws/visual?room_id=r2") as ws_b:
            hello = ws_a.receive_json()
            assert hello == {"type": "visual_hello", "room_id": "r1", "enabled": True, "history": []}
            assert ws_b.receive_json()["room_id"] == "r2"
            for ev in window("r1", 0, 0):
                hub.feed(ev)  # called from the test thread, like a pipeline worker
            draft = ws_a.receive_json()
            assert draft["type"] == "visual_draft" and draft["room_id"] == "r1"
            assert hub.channel.subscriber_count("r2") == 1 and hub.channel.history("r2") == []
        # history replay for a late viewer
        with client.websocket_connect("/ws/visual?room_id=r1") as late:
            assert [d["id"] for d in late.receive_json()["history"]] == [draft["id"]]
        assert client.get("/api/visual/r1").json()["history"][0]["id"] == draft["id"]


def test_websocket_rejects_bad_room_and_guard():
    from starlette.websockets import WebSocketDisconnect

    app = make_app(VisualHub(None), ws_guard=lambda ws, room: room != "secret")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/visual?room_id=bad room") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/visual?room_id=secret") as ws:
                ws.receive_json()


def test_manual_trigger_endpoint_and_default_local_only_guard():
    hub = make_hub(FakeLLM())
    with TestClient(make_app(hub, host_guard=None)) as client:
        assert client.post("/api/visual/r1/trigger").json()["accepted"] is False  # nothing heard yet
        hub.feed(final("r1", 0, "般若引導菩薩行", 0, 2_000))
        res = client.post("/api/visual/r1/trigger")
        assert res.status_code == 200 and res.json()["accepted"] is True
        assert client.post("/api/visual/r1/trigger").json()["accepted"] is False  # cooldown
        assert client.post("/api/visual/r1/trigger?force=1").json()["accepted"] is True
    remote = TestClient(make_app(make_hub(FakeLLM())), client=("10.1.2.3", 5000))
    with remote:
        assert remote.post("/api/visual/r1/trigger").status_code == 403
    local = TestClient(make_app(make_hub(FakeLLM())), client=("127.0.0.1", 5000))
    with local:
        assert local.post("/api/visual/r1/trigger").status_code == 200


def test_http_history_is_refused_without_a_guard_for_remote_clients():
    with TestClient(make_app(VisualHub(None))) as client:          # TestClient peer is "testclient", not loopback
        assert client.get("/api/visual/r1").status_code == 403
    with TestClient(make_app(VisualHub(None), http_guard=lambda r, room: False)) as client:
        assert client.get("/api/visual/r1").status_code == 403
