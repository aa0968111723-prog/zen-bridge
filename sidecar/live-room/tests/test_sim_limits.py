"""B-L. 100-minute limits on this branch.

Export with storage off covers the whole class, not the last 200 events.
Retranslate of an early segment still works after the 500-result trim because the
compact index keeps it. Idle reclaim drops the live room but keeps caption state.
"""

import os
import time

import pytest

from app.rooms import RoomBook
from app.translate import Translator
from tests.sim import (
    SCALE,
    Listener,
    caption_rows,
    TextAsr,
    VirtualHost,
    export_json,
    open_room,
    post_segment,
    run_100min,
    serving,
    sim_settings,
)
from tests.test_round2 import auth


@pytest.fixture(scope="module")
def report():
    return run_100min()


@pytest.mark.anyio
async def test_backfill_window_is_events_not_segments():
    """B-L1. since() is still a 200-event window (about 100 segments when English doubles them).
    caption_state, which backfill uses, has all 150 segments on this branch.
    """
    from tests.sim import ScriptedTranslator

    translator = ScriptedTranslator(lambda zh: ("ok", 0.0))
    async with serving(asr=TextAsr(0), translator=translator) as (app, client, token):
        await open_room(client, token, "class")
        for seq in range(1, 151):
            resp = await post_segment(client, token, "class", "s", seq, f"第{seq}句".encode(), 0, 1000)
            assert resp.status_code == 200, resp.text
        for _ in range(400):
            if len(translator.finished) >= 150 and app.state.pipeline.stats()["translate_queued"] == 0:
                break
            await __import__("asyncio").sleep(0.005)
        resumed = app.state.bus.since("class", 1)
        assert len(resumed["events"]) == 200
        covered = {item.get("seq") for item in resumed["events"] if item.get("seq") is not None}
        assert len(covered) <= 101
        state = caption_rows(app, "class")
        assert len({item["id"] for item in state}) == 150


@pytest.mark.anyio
async def test_results_cap_and_old_retranslate():
    """B-L2. results stays <= 500 after 600 segments. Retranslate of seq 1 still finds it.
    On main this was a 404 once results were trimmed. The caption index keeps the early line.
    """
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        host = VirtualHost(client, token, "class", "s")
        await host.run(600, pace=False)
        assert len(app.state.pipeline.results) <= 500
        resp = await client.post(
            "/api/segment/retranslate",
            json={"room_id": "class", "session_id": "s", "seq": 1},
            headers={**auth(token), "content-type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["zh"] == "第1句"


def test_room_reclaim_after_30min_only_when_idle():
    """B-L3. RoomBook reclaims at idle_s only when there is no listener and no active session."""
    book = RoomBook(8, 1800)
    room = book.open("class")
    # A whole-number base keeps opened + 1800 - opened exactly 1800.0. With a raw
    # monotonic value (large on a long-running Windows runner) the float subtraction
    # can land at 1799.9999999 and the boundary check flips.
    room["last_active"] = opened = 1000.0
    assert book.sweep(now=opened + 1799) == []
    assert "class" in book.rooms
    room["listeners"].add(object())
    assert book.sweep(now=opened + 1800) == []
    room["listeners"].clear()
    room["session_active"] = True
    assert book.sweep(now=opened + 1800) == []
    room["session_active"] = False
    assert book.sweep(now=opened + 1800) == ["class"]


@pytest.mark.anyio
async def test_room_reclaim_keeps_captions_at_app_layer():
    """B-L3 app path. Idle reclaim retires the live window and drops pipeline runtime,
    but caption state (and therefore export with storage off) stays.
    """
    settings = sim_settings(translate=False, room_idle_s=1 * SCALE * 60)
    async with serving(settings=settings, asr=TextAsr(0)) as (app, client, token):
        await open_room(client, token, "class")
        resp = await post_segment(client, token, "class", "s", 1, "留下".encode(), 0, 6000)
        assert resp.status_code == 200, resp.text
        app.state.room_book.rooms["class"]["listeners"].add(object())
        app.state.room_book.rooms["class"]["last_active"] = time.monotonic() - 1000
        await app.state.sweep_once()
        assert "class" in app.state.rooms
        app.state.room_book.rooms["class"]["listeners"].clear()
        await app.state.sweep_once()
        assert "class" not in app.state.rooms
        rows = await export_json(client, token, "class")
        assert [row["zh"] for row in rows] == ["留下"]


@pytest.mark.anyio
async def test_storage_off_export_is_full_session():
    """B-L4. Storage off used to export the last 200 bus rows. This branch exports all 1000.
    /api/setup reports storage false, which is what makes the host page show the warn state.
    """
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False, data_path="")) as (app, client, token):
        await open_room(client, token, "class")
        setup = (await client.get("/api/setup", params={"room_id": "class"})).json()
        assert setup["storage"] is False
        host = VirtualHost(client, token, "class", "s")
        await host.run(1000, pace=False)
        rows = await export_json(client, token, "class")
        assert [row["seq"] for row in rows] == list(range(1, 1001))


def test_storage_on_export_is_full(report):
    """B-L4 storage on. The shared run's JSON export has every segment."""
    seqs = [row["seq"] for row in report.export_json]
    assert seqs == list(range(1, report.segments + 1))


def test_100min_metrics_snapshot_written(report):
    """B-L5. Write the metrics series when CI provides GITHUB_STEP_SUMMARY."""
    lines = ["", "### 100-minute simulation", ""]
    lines.append("| seq | pending | results | held | translate_queued | traced |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for snap in report.metrics:
        if int(snap["seq"]) % 100 != 0:
            continue
        lines.append(
            f"| {snap['seq']} | {snap['pending']} | {snap['results']} | {snap['held']} | {snap['translate_queued']} | {snap['traced']} |"
        )
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    assert report.metrics
