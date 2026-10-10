"""CTO M-03: A7 (ASR done -> first listener write) and B1 (first draft packet -> first draft char)."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.draft_hub import DraftHub
from app.latency import StageLatency
from tests.test_round2 import Socket, app_for, auth, push, stop, token_of


def test_marks_close_once_and_are_bounded():
    lat = StageLatency()
    lat.mark("A7", "r:s:1", at=10.0)
    lat.mark("A7", "r:s:1", at=11.0)          # first start wins
    assert lat.finish("A7", "r:s:1", at=10.25) == pytest.approx(250.0)
    assert lat.finish("A7", "r:s:1", at=12.0) is None
    assert lat.snapshot()["A7"]["n"] == 1 and lat.snapshot()["A7"]["name"] == "push"
    for i in range(5000):
        lat.mark("B1", f"k{i}", at=0.0)
    assert len(lat._marks["B1"]) <= 1024
    assert lat.finish("B1", "k0") is None and lat.finish("B1", "k4999", at=0.5) == pytest.approx(500.0)


class Part:
    def __init__(self, text, end=False):
        self.text, self.is_endpoint = text, end


class Engine:
    def __init__(self):
        self.n = 0

    def feed(self, pcm):
        self.n += 1
        return [] if self.n < 3 else [Part("今天")]

    def reset(self):
        self.n = 0


def test_b1_first_packet_to_first_draft_write():
    t = [100.0]
    lat = StageLatency()
    sent = []
    hub = DraftHub(lambda: Engine(), publish=sent.append, clock=lambda: t[0], latency=lat)
    sess = hub.open("r", "s")
    lat_marks = lambda: dict(lat._marks.get("B1", {}))
    assert hub.feed(sess, b"\0" * 640) is None and "r:s:1" in lat_marks()
    hub.feed(sess, b"\0" * 640)
    ev = hub.feed(sess, b"\0" * 640)
    assert ev and ev["zh"] == "今天"
    assert lat.finish("B1", ev["id"]) is not None          # what the listener write hook does
    assert lat.snapshot()["B1"]["n"] == 1


@pytest.mark.anyio
async def test_a7_recorded_when_a_listener_receives_the_caption():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await client.post("/api/rooms/open", json={"room_id": "class"},
                              headers={**auth(token), "content-type": "application/json"})
            async with Socket(app, "/ws/listen?room_id=class") as ws:
                await push(client, token, "class", "s", 1, "第一句".encode())
                for _ in range(20):
                    m = await ws.recv(3)
                    if isinstance(m, dict) and m.get("zh") == "第一句":
                        break
            snap = (await client.get("/api/metrics", headers=auth(token))).json()
            stages = snap["latency"]
            assert stages["A7"]["n"] >= 1 and stages["A7"]["p50_ms"] is not None, stages
            assert "B1" in stages
    finally:
        await stop(app)
