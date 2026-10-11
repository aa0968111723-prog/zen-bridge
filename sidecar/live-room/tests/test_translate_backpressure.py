"""hardware.md §5.3 backpressure: queue max 4, oldest line >8 s stale -> skip or merge; env-tunable."""
import asyncio
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from app import runtime_tuning as rt
from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import app_for, push, settings_with, stop, token_of


class Gate(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen: list[str] = []

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        self.seen.append(zh)
        self.started.set()
        self.release.wait(3)
        return TranslateResult("EN " + zh, "ok")


def test_defaults():
    assert rt.stale_s({}) == 8.0
    assert rt.stale_policy({}) == "skip"
    assert rt.translate_priority({}) == "below_normal"


def test_env_overrides_and_bad_values():
    assert rt.stale_s({"BREEZE_TRANSLATE_STALE_S": "3.5"}) == 3.5
    assert rt.stale_s({"BREEZE_TRANSLATE_STALE_S": "abc"}) == 8.0
    assert rt.stale_s({"BREEZE_TRANSLATE_STALE_S": "-1"}) == 0.0
    assert rt.stale_policy({"BREEZE_TRANSLATE_STALE_POLICY": "MERGE"}) == "merge"
    assert rt.stale_policy({"BREEZE_TRANSLATE_STALE_POLICY": "drop"}) == "skip"
    assert rt.translate_priority({"BREEZE_TRANSLATE_PRIORITY": "idle"}) == "idle"


def test_lower_thread_priority_never_raises():
    out = []
    t = threading.Thread(target=lambda: out.append(rt.lower_current_thread("below_normal")))
    t.start()
    t.join()
    assert out and isinstance(out[0], bool)
    assert rt.lower_current_thread("normal") is False


def test_queue_default_is_four():
    from app.settings import Settings
    assert Settings().translate_queue == 4


async def _run(monkeypatch, policy: str):
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_S", "0.2")
    monkeypatch.setenv("BREEZE_TRANSLATE_STALE_POLICY", policy)
    translator = Gate()
    app = app_for(translator=translator, settings=settings_with(allow_testclient=True, translate_workers=1,
                                                                 translate_queue=4, translate_timeout_s=5))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, "class", "s", 1, "一".encode(),
                                             wait_translation="0", async_header=True))
            assert await asyncio.to_thread(translator.started.wait, 2)
            for seq, zh in ((2, "舊句"), (3, "新句")):
                r = await push(client, token, "class", "s", seq, zh.encode(), wait_translation="0", async_header=True)
                assert r.status_code == 200
            await asyncio.sleep(0.35)          # seq 2 is now older than the 0.2 s stale budget
            translator.release.set()
            await asyncio.wait_for(first, 3)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3
            while loop.time() < deadline:
                rows = {i["seq"]: i for i in app.state.bus.caption_state("class")}
                if rows.get(3, {}).get("en"):
                    break
                await asyncio.sleep(0.02)
            return app, translator, {i["seq"]: i for i in app.state.bus.caption_state("class")}
    finally:
        translator.release.set()
        await stop(app)


@pytest.mark.anyio
async def test_stale_oldest_is_skipped_when_newer_lines_wait(monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE_LATE_POLICY", "publish")   # tests the dequeue rule only
    app, translator, rows = await _run(monkeypatch, "skip")
    assert rows[2]["translate_status"] == "skipped_backlog" and rows[2]["en"] == ""
    assert rows[2]["zh"] == "舊句"                      # Chinese caption is kept
    assert rows[3]["en"] == "EN 新句"                   # last line still translated although old: nothing newer waits
    assert "舊句" not in translator.seen
    assert app.state.pipeline.translate_skipped >= 1


@pytest.mark.anyio
async def test_stale_oldest_is_merged_into_next_request(monkeypatch):
    monkeypatch.setenv("BREEZE_TRANSLATE_LATE_POLICY", "publish")   # tests the dequeue rule only
    app, translator, rows = await _run(monkeypatch, "merge")
    assert rows[2]["translate_status"] == "skipped_backlog"
    assert rows[3]["zh"] == "新句"                      # caption text itself is untouched
    assert "舊句，新句" in translator.seen               # one request carries both lines
    assert rows[3]["en"] == "EN 舊句，新句"
    assert app.state.pipeline.stats()["translate_merged"] == 1


@pytest.mark.anyio
async def test_stale_off_translates_everything(monkeypatch):
    app, translator, rows = await _run(monkeypatch, "off")
    assert rows[2]["en"] == "EN 舊句" and rows[3]["en"] == "EN 新句"
