import asyncio
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import app_for, push, settings_with, stop, token_of


def test_cancel_event_stops_backoff_without_sleeping():
    slept = []
    cancel = threading.Event()

    def opener(req, timeout=40):
        del req, timeout
        cancel.set()
        raise TimeoutError()

    def sleeper(delay: float) -> None:
        slept.append(delay)

    translator = Translator(enabled=True, key="k", max_attempts=3, max_backoff=30, sleeper=sleeper, opener=opener)
    started = time.monotonic()
    result = translator.translate("般若", cancel=cancel)
    assert result.status == "timeout"
    assert "不假設沒有計費" in result.detail
    assert time.monotonic() - started < 0.4
    assert slept == []


class BoomThenOk(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.calls = 0

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline, cancel
        self.calls += 1
        if zh == "炸":
            raise RuntimeError("boom")
        return TranslateResult("EN " + zh, "ok")


class WatchCancel(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.cancel = None
        self.saw = threading.Event()

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del zh, glossary, context, deadline
        self.cancel = cancel
        self.saw.set()
        if cancel is not None and cancel.wait(5):
            return TranslateResult("", "timeout", "英譯逾時，不假設沒有計費。中文仍保留")
        return TranslateResult("late", "ok")


class SlowEnglish(Translator):
    def __init__(self, delay: float):
        super().__init__(enabled=True, key="k")
        self.delay = delay

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline
        if cancel is not None and cancel.wait(self.delay):
            return TranslateResult("", "timeout", "英譯逾時，不假設沒有計費。中文仍保留")
        if cancel is None:
            time.sleep(self.delay)
        return TranslateResult("EN " + zh, "ok")


@pytest.mark.anyio
async def test_translate_loop_survives_translator_exception():
    translator = BoomThenOk()
    app = app_for(translator=translator, settings=settings_with(allow_testclient=True, translate_timeout_s=2))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await asyncio.wait_for(push(client, token, "class", "s", 1, "炸".encode()), 2)
            second = await asyncio.wait_for(push(client, token, "class", "s", 2, "好".encode()), 2)
            assert first.status_code == 200
            assert first.json()["zh"] == "炸"
            assert first.json()["status"] == "translate_failed"
            assert second.status_code == 200, second.text
            assert second.json()["zh"] == "好"
            assert second.json()["en"] == "EN 好"
            assert translator.calls == 2
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_translation_timeout_sets_cancel_and_keeps_chinese():
    translator = WatchCancel()
    app = app_for(
        translator=translator,
        settings=settings_with(allow_testclient=True, translate_timeout_s=0.2, translate_workers=1),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            started = time.monotonic()
            resp = await asyncio.wait_for(push(client, token, "class", "s", 1, "般若".encode()), 2)
            assert time.monotonic() - started < 1.5
            assert translator.saw.is_set()
            assert translator.cancel is not None and translator.cancel.is_set()
            body = resp.json()
            assert body["zh"] == "般若"
            assert body["en"] == ""
            assert body["status"] == "translate_failed"
            assert body["translate_status"] == "timeout"
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_shutdown_drains_translation_queue_bounded():
    quick = SlowEnglish(0.15)
    app = app_for(
        translator=quick,
        settings=settings_with(allow_testclient=True, shutdown_flush_s=1.0, translate_timeout_s=2),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(
                client, token, "class", "s", 1, "留".encode(),
                wait_translation="0", async_header=True,
            )
            assert resp.status_code == 200
            assert resp.json()["zh"] == "留"
            assert resp.json()["en"] == ""
    finally:
        await stop(app)
    saved = [item for item in app.state.bus.caption_state("class") if item.get("seq") == 1]
    assert saved and saved[0]["zh"] == "留"
    assert saved[0]["en"] == "EN 留"

    blocked = SlowEnglish(30)
    stalled = app_for(
        translator=blocked,
        settings=settings_with(
            allow_testclient=True,
            shutdown_flush_s=0.25,
            translate_timeout_s=5,
            translate_workers=1,
        ),
    )
    started = time.monotonic()
    try:
        async with AsyncClient(transport=ASGITransport(app=stalled), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(stalled, client)
            resp = await push(
                client, token, "class", "s", 1, "卡".encode(),
                wait_translation="0", async_header=True,
            )
            assert resp.json()["zh"] == "卡"
            started = time.monotonic()
    finally:
        await stop(stalled)
    assert time.monotonic() - started < 1.2
