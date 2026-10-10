"""round4 #3: A2-A6 per-stage latency on segments and p50/p95 in /api/metrics."""
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.latency import StageLatency, upload_ms
from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import app_for, auth, push, stop, token_of


class SlowAsr:
    def transcribe(self, wav, prompt=""):
        time.sleep(1.0)
        return AsrResult(ok=True, text=wav.read_bytes().decode())


class SlowEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="test-key")

    def translate(self, zh, glossary=None, context=None):
        time.sleep(0.5)
        return TranslateResult("EN " + zh, "ok")


@pytest.mark.anyio
async def test_fake_asr_1s_and_mt_half_second_show_in_metrics():
    app = app_for(asr=SlowAsr(), translator=SlowEnglish())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        for seq in (1, 2):
            sent = int(time.time() * 1000) - 150          # slice ended 150 ms ago on the host
            headers = auth(token)
            r = await client.post("/api/push", params={"room_id": "lat", "session_id": "s1", "seq": str(seq)},
                                  data={"room_id": "lat", "session_id": "s1", "seq": str(seq),
                                        "wait_translation": "1", "t1_wall_ms": str(sent)},
                                  files={"audio": ("a.webm", f"第{seq}句".encode(), "audio/webm")}, headers=headers)
            assert r.status_code == 200, r.text
        m = (await client.get("/api/metrics", headers=auth(token))).json()
        lat = m["latency"]
        assert set(lat) == {"A2", "A3", "A4", "A5", "A6"}
        assert lat["A5"]["n"] == 2 and 950 <= lat["A5"]["p50_ms"] <= 1300
        assert lat["A6"]["n"] == 2 and 450 <= lat["A6"]["p50_ms"] <= 800
        assert lat["A2"]["n"] == 2 and 100 <= lat["A2"]["p50_ms"] <= 1500
        assert lat["A3"]["p95_ms"] is not None and lat["A4"]["p95_ms"] is not None
        seg = app.state.pipeline.get("lat", "s1", 1)
        assert set(seg.lat) >= {"A2", "A3", "A4", "A5", "A6"}
        assert "lat" not in seg.public()                       # listeners never see timing internals
        await stop(app)


@pytest.mark.anyio
async def test_missing_or_bogus_host_clock_gives_no_a2_sample():
    app = app_for(asr=SlowAsr(), translator=SlowEnglish())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        r = await push(client, token, "lat2", "s1", 1, "一".encode(), wait_translation="0")
        assert r.status_code == 200
        m = (await client.get("/api/metrics", headers=auth(token))).json()
        assert m["latency"]["A2"]["n"] == 0 and m["latency"]["A5"]["n"] == 1
        await stop(app)


def test_window_is_bounded_and_percentiles():
    lat = StageLatency(window=16)

    class S:
        def __init__(self):
            self.lat = {}
    for v in range(100):
        lat.note(S(), "A5", v)
    snap = lat.snapshot()["A5"]
    assert snap["n"] == 16 and snap["p50_ms"] == 92 and snap["p95_ms"] == 98
    lat.note(S(), "A2", 120_000)                       # host clock off by minutes -> ignored
    lat.note(S(), "A6", -5)
    assert lat.snapshot()["A2"]["n"] == 0 and lat.snapshot()["A6"]["n"] == 0
    assert upload_ms(None, 1.0) is None and upload_ms(1000, 2.0) == 1000.0
