"""B-c. SRT timestamps stay monotonic, including a second session and a restart.

Storage-on 1000-segment SRT comes from the shared run. Storage-off export of a full
class is this branch's behavior (caption state, not the 200-event live window) and is
asserted in B-L4. Bad t0/t1 are normalized so the stored span satisfies 0 <= t0 < t1.
"""

import shutil
from contextlib import asynccontextmanager

import pytest

from app.server import create_app
from app.translate import Translator
from tests.sim import (
    SEGMENTS,
    Listener,
    ScriptedTranslator,
    TextAsr,
    VirtualHost,
    copy_decoder,
    export_json,
    export_srt,
    open_room,
    parse_srt,
    post_segment,
    run_100min,
    serving,
    sim_settings,
    token_of,
)
from tests.test_round2 import Socket, auth, stop
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="module")
def report():
    return run_100min()


def test_100min_srt_valid_and_monotonic(report):
    """B-c1. Storage was on for the shared run. Cues are contiguous 6s steps."""
    cues = parse_srt(report.srt)
    assert [cue[0] for cue in cues] == list(range(1, len(cues) + 1))
    for index, (_n, start, end, _text) in enumerate(cues):
        assert end > start
        if index:
            assert start >= cues[index - 1][2]
        if index < len(cues) - 1:
            assert 5500 <= (end - start) <= 6500
    assert len(cues) + report.silent + report.missing == SEGMENTS
    last_end = cues[-1][2] / 1000
    assert abs(last_end - SEGMENTS * 6) <= 2


@pytest.mark.anyio
@pytest.mark.parametrize("storage", [True, False])
async def test_second_session_same_room_srt_monotonic(storage, tmp_path):
    """B-c2. Session B's t0 starts at 0, like a browser that started over.
    Export places B after A's last cue. This replaces the old per-session zero.
    """
    path = str(tmp_path / "c.sqlite3") if storage else ""
    settings = sim_settings(data_path=path, translate=False)
    async with serving(settings=settings, asr=TextAsr(0)) as (app, client, token):
        await open_room(client, token, "class")
        host_a = VirtualHost(client, token, "class", "sessionA")
        await host_a.run(20, pace=False)
        ended = await host_a.stop()
        assert ended.status_code == 200, ended.text
        host_b = VirtualHost(client, token, "class", "sessionB")
        await host_b.run(20, pace=False)
        text = await export_srt(client, token, "class")
    cues = parse_srt(text)
    assert len(cues) == 40
    for index in range(1, len(cues)):
        assert cues[index][1] >= cues[index - 1][2]
    # 20 cues of 6s each: B starts at or after 120s.
    assert cues[20][1] >= cues[19][2]


@pytest.mark.anyio
async def test_push_rejects_or_normalizes_bad_times():
    """B-c3. Inverted or negative spans are stored with 0 <= t0 < t1.
    A span longer than max_audio_seconds is still a positive duration; the audio
    length limit is separate from the timestamp fields.
    """
    async with serving(asr=TextAsr(0), settings=sim_settings(translate=False)) as (app, client, token):
        await open_room(client, token, "class")
        cases = [
            (-5000, -1000),
            (1000, 1000),
            (8000, 1000),
            (0, 10_000_000),
        ]
        for seq, (t0, t1) in enumerate(cases, start=1):
            resp = await post_segment(client, token, "class", "s", seq, f"第{seq}句".encode(), t0, t1)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["t0_ms"] >= 0
            assert body["t1_ms"] > body["t0_ms"]


@pytest.mark.anyio
async def test_srt_leaves_holes_for_waiting_and_missing():
    """B-c4. Waiting pauses the recorder, so no cue covers a waiting interval.

    Seq 3 already has Chinese: mark_missing must not punch that cue out. Seq 9 was
    never uploaded; marking it missing leaves it out of the SRT entirely.
    """
    async with serving(asr=TextAsr(9.0), translator=ScriptedTranslator(lambda zh: ("ok", 0.0))) as (app, client, token):
        await open_room(client, token, "class")
        host = VirtualHost(client, token, "class", "holes")
        await host.run(8)
        assert host.waiting, "ASR slower than the slice should have paused the recorder"
        kept = await client.post(
            "/api/segment/missing",
            json={"room_id": "class", "session_id": "holes", "seq": 3, "reason": "主持端放棄這段"},
            headers={**auth(token), "content-type": "application/json"},
        )
        assert kept.status_code == 200, kept.text
        absent = await client.post(
            "/api/segment/missing",
            json={"room_id": "class", "session_id": "holes", "seq": 9, "reason": "沒有這段"},
            headers={**auth(token), "content-type": "application/json"},
        )
        assert absent.status_code == 200, absent.text
        text = await export_srt(client, token, "class")
        assert "沒有這段" not in text
        assert "主持端放棄這段" not in text
        cues = parse_srt(text)
        assert len(cues) == 8
        for start_v, end_v in host.waiting:
            for _n, start, end, _text in cues:
                assert not (start / 1000 < end_v and end / 1000 > start_v)


@pytest.mark.anyio
async def test_srt_survives_restart_identical(report):
    """B-c5. The shared run's sqlite is reopened. Lifespan replays it; the SRT bytes match.
    The shared process is already shut down, which is the restart.
    """
    app = create_app(
        sim_settings(data_path=report.db_path),
        asr=TextAsr(0),
        translator=Translator(enabled=False),
        decoder=copy_decoder,
    )
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(app, client)
                text = await export_srt(client, token, report.room)
        assert text == report.srt
    finally:
        await stop(app)
