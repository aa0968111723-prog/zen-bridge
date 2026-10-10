"""Pending user decisions, implemented as switches with the safest default (no product choice made):
AI-P1-3 locked-term violation: BREEZE_LOCKED_TERM_POLICY=flag (default: publish + flag) | withhold.
CTO-12 late English: BREEZE_TRANSLATE_LATE_POLICY=drop (default, after BREEZE_TRANSLATE_LATE_S =
stale limit) | publish."""
import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app import runtime_tuning
from app.pipeline import Segment
from app.translate import TranslateResult
from tests.test_pipeline_repair import app_for, push, settings_with, stop, token_of

LOCKED = [{"zh": "禪修", "en": "Chan practice", "locked": True}]


def finish(monkeypatch, policy, text):
    if policy:
        monkeypatch.setenv("BREEZE_LOCKED_TERM_POLICY", policy)
    app = app_for()
    p = app.state.pipeline
    seg = Segment("class", "s", 1, zh="我們今天禪修")
    seg.glossary_snapshot = LOCKED
    assert p._finish_translation(seg, TranslateResult(text, "ok"), None, None)
    return p, seg


def test_defaults_are_the_safest_choices():
    assert runtime_tuning.locked_term_policy({}) == "flag"
    assert runtime_tuning.late_policy({}) == "drop"
    assert runtime_tuning.late_s({}) == runtime_tuning.stale_s({}) == 8.0
    assert runtime_tuning.late_policy({"BREEZE_TRANSLATE_LATE_POLICY": "nonsense"}) == "drop"


def test_locked_violation_flag_publishes_with_flag(monkeypatch):
    p, seg = finish(monkeypatch, None, "We meditate today.")
    assert seg.en == "We meditate today." and seg.translate_status == "ok"
    assert seg.term_flags[0]["reason"] == "missing"


def test_locked_violation_withhold_hides_english(monkeypatch):
    p, seg = finish(monkeypatch, "withhold", "We meditate today.")
    assert seg.en == "" and seg.translate_status == "term_violation" and seg.zh == "我們今天禪修"
    assert p.locked_withheld == 1


def test_locked_term_used_is_never_withheld(monkeypatch):
    p, seg = finish(monkeypatch, "withhold", "Today we do Chan practice.")
    assert seg.en == "Today we do Chan practice." and seg.translate_status == "ok"


class Slow:
    def __init__(self, delay):
        self.delay = delay

    def translate(self, zh, **kw):
        time.sleep(self.delay)
        return TranslateResult(f"EN {zh}", "ok")


async def _two(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        await push(client, token, "class", "s", 1, "慢句".encode(), wait_translation="0", async_header=True)
        await push(client, token, "class", "s", 2, "下一句".encode(), wait_translation="0", async_header=True)
        loop = asyncio.get_running_loop(); end = loop.time() + 4
        rows = {}
        while loop.time() < end:
            rows = {i["seq"]: i for i in app.state.bus.caption_state("class")}
            done = [r for r in rows.values() if r.get("translate_status") not in (None, "", "pending", "queued")]
            if len(done) == 2:
                break
            await asyncio.sleep(0.02)
        return rows


@pytest.mark.anyio
@pytest.mark.parametrize("policy,shown", [(None, False), ("publish", True)])
async def test_late_english(monkeypatch, policy, shown):
    monkeypatch.setenv("BREEZE_TRANSLATE_LATE_S", "0.1")
    if policy:
        monkeypatch.setenv("BREEZE_TRANSLATE_LATE_POLICY", policy)
    app = app_for(translator=Slow(0.3), settings=settings_with(allow_testclient=True, translate_timeout_s=5,
                                                               translate_workers=1))
    try:
        rows = await _two(app)
    finally:
        await stop(app)
    assert rows[2].get("en") == "EN 下一句", rows        # the newest line is never dropped as late
    if shown:
        assert rows[1].get("en") == "EN 慢句", rows
    else:
        assert not rows[1].get("en") and rows[1].get("translate_status") == "skipped_backlog", rows
        assert app.state.pipeline.translate_late_dropped == 1
