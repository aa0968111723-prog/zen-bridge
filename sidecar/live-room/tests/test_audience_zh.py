"""Audience sockets must not receive zh_raw.

zh_raw is the recognition text from before a host edit, and the partial text kept
when recognition fails or is silent. Storage, host export, and host HTTP bodies
still keep it. Listeners see the edited zh only.
"""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.dispatch import _LISTENER_PASS, _PASS, RoomBus, for_listener
from app.settings import Settings
from app.translate import TranslateResult, Translator
from tests.test_round2 import Socket, app_for, auth, open_room, push, stop, token_of


_SECRET_KEYS = {
    "zh_raw", "term_flags", "host_token", "token", "listen_key", "listen_url", "authorization",
}
ORIGINAL = "誤辨的張三"
EDITED = "已遮蔽"
FAILED_PARTIAL = "失敗的半句"
SILENT_PARTIAL = "靜音半句"


class NamedEnglish(Translator):
    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        return TranslateResult("EN " + (zh or ""), "ok")


class PartialAsr:
    """Failure and silence both carry a partial that must stay off the audience socket."""

    def transcribe(self, wav, prompt):
        del prompt
        text = wav.read_bytes().decode()
        if text == "fail":
            return AsrResult(ok=False, text=FAILED_PARTIAL, error="辨識程序失敗")
        if text == "quiet":
            return AsrResult(ok=False, text=SILENT_PARTIAL, error="")
        return AsrResult(ok=True, text=text)


def test_audience_whitelist_omits_raw_text_and_secrets():
    assert "zh_raw" in _PASS
    assert "term_flags" in _PASS
    assert _SECRET_KEYS.isdisjoint(_LISTENER_PASS)
    event = {
        "type": "caption",
        "id": "class:s:1",
        "room_id": "class",
        "session_id": "s",
        "seq": 1,
        "version": 2,
        "zh": EDITED,
        "zh_raw": ORIGINAL,
        "en": "EN " + EDITED,
        "status": "ready",
        "cursor": 3,
        "epoch": 1,
        "t0_ms": 1000,
        "t1_ms": 4000,
        "host_token": "host-secret",
        "token": "host-secret",
        "listen_key": "listen-secret",
        "listen_url": "http://evil.example/listen",
        "authorization": "Bearer host-secret",
        "term_flags": [{"zh": "般若", "en": "LOCKED-PRAJNA", "reason": "missing"}],
    }
    out = for_listener(event)
    assert out["zh"] == EDITED
    assert out["en"] == "EN " + EDITED
    assert out["cursor"] == 3
    assert out["t0_ms"] == 1000
    blob = json.dumps(out, ensure_ascii=False)
    assert "zh_raw" not in out
    assert "term_flags" not in out
    assert ORIGINAL not in blob
    assert "LOCKED-PRAJNA" not in blob
    assert "host-secret" not in blob
    assert "listen-secret" not in blob
    assert _SECRET_KEYS.isdisjoint(out)


def test_bus_keeps_zh_raw_for_storage_and_export_state():
    bus = RoomBus()
    snap = bus.publish({
        "id": "class:s:1",
        "room_id": "class",
        "session_id": "s",
        "seq": 1,
        "version": 1,
        "zh": EDITED,
        "zh_raw": ORIGINAL,
        "term_flags": [{"zh": "般若", "en": "LOCKED-PRAJNA", "reason": "missing"}],
    })
    assert snap is not None
    assert snap["zh_raw"] == ORIGINAL
    assert snap["term_flags"][0]["reason"] == "missing"
    assert bus.history("class")[0]["zh_raw"] == ORIGINAL
    assert bus.caption_state("class")[0]["zh_raw"] == ORIGINAL
    assert bus.caption_state("class")[0]["term_flags"][0]["en"] == "LOCKED-PRAJNA"
    resumed = bus.since("class", 99)
    assert resumed["gap"] is True
    assert resumed["backfill"][0]["zh_raw"] == ORIGINAL
    visible = for_listener(resumed["backfill"][0])
    assert visible["zh"] == EDITED
    assert "zh_raw" not in visible
    assert "term_flags" not in visible
    assert "LOCKED-PRAJNA" not in json.dumps(visible, ensure_ascii=False)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _assert_no_secrets(payload, *banned_text):
    for item in _walk(payload):
        assert _SECRET_KEYS.isdisjoint(item)
    blob = json.dumps(payload, ensure_ascii=False)
    for text in banned_text:
        assert text not in blob, text
    return blob


def _assert_hello(hello, room="class"):
    assert hello["type"] == "hello"
    assert hello["room_id"] == room
    assert isinstance(hello["history"], list)
    assert isinstance(hello["events"], list)
    assert "gap" in hello
    assert "latest_cursor" in hello
    assert "oldest_cursor" in hello
    assert isinstance(hello["epoch"], int)


async def _collect(sock, pred, timeout=3.0):
    found = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found.append(await sock.recv(0.05))
        except asyncio.TimeoutError:
            if pred(found):
                return found
            continue
        if pred(found):
            quiet = time.monotonic() + 0.1
            while time.monotonic() < quiet:
                try:
                    found.append(await sock.recv(0.05))
                except asyncio.TimeoutError:
                    break
            return found
    return found


async def _hello_from(app, path):
    async with Socket(app, path) as sock:
        hello = await sock.recv()
    _assert_hello(hello)
    return hello


def _captions(messages):
    return [item for item in messages if item.get("type") == "caption"]


@pytest.mark.anyio
async def test_host_edit_hides_zh_raw_from_live_history_and_replay(tmp_path):
    settings = Settings(
        allow_testclient=True,
        translate=True,
        data_path=str(tmp_path / "captions.sqlite3"),
    )
    app = app_for(settings=settings, translator=NamedEnglish(enabled=True, key="test-key"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            listen_key = opened.json()["listen_key"]
            assert listen_key
            async with Socket(app, "/ws/listen?room_id=class") as sock:
                hello = await sock.recv()
                _assert_hello(hello)
                assert hello["history"] == []
                _assert_no_secrets(hello, token, listen_key)
                pushed = await push(
                    client, token, "class", "s", 1, ORIGINAL.encode(), t0_ms=1000, t1_ms=4000,
                )
                assert pushed.status_code == 200, pushed.text
                # The host HTTP body is allowed to keep the recognition text.
                assert pushed.json()["zh_raw"] == ORIGINAL
                live = await _collect(
                    sock,
                    lambda rows: any(item.get("zh") == ORIGINAL and item.get("status") == "ready" for item in rows),
                )
                captions = _captions(live)
                assert captions, live
                assert any(item.get("zh") == ORIGINAL for item in captions)
                _assert_no_secrets(live, token, listen_key)
                pre_cursor = max(int(item["cursor"]) for item in captions)
                edited = await client.post(
                    "/api/segment/retranslate",
                    json={"room_id": "class", "session_id": "s", "seq": 1, "zh": EDITED},
                    headers={**auth(token), "content-type": "application/json"},
                )
                assert edited.status_code == 200, edited.text
                body = edited.json()
                assert body["zh"] == EDITED
                assert body["zh_raw"] == ORIGINAL
                assert body["en"] == "EN " + EDITED
                update = await _collect(sock, lambda rows: any(item.get("zh") == EDITED for item in rows))
                edited_live = [item for item in _captions(update) if item.get("zh") == EDITED]
                assert edited_live, update
                assert all(item.get("en") == "EN " + EDITED for item in edited_live)
                _assert_no_secrets(update, ORIGINAL, token, listen_key)
            history = await _hello_from(app, "/ws/listen?room_id=class&cursor=0")
            assert any(item.get("zh") == EDITED for item in history["history"])
            assert all(item.get("zh") != ORIGINAL for item in history["history"])
            _assert_no_secrets(history, ORIGINAL, token, listen_key)
            resumed = await _hello_from(app, f"/ws/listen?room_id=class&cursor={pre_cursor}")
            assert resumed["gap"] is False
            assert any(item.get("zh") == EDITED for item in resumed["events"])
            _assert_no_secrets(resumed, ORIGINAL, token, listen_key)
            replay = await _hello_from(app, "/ws/listen?room_id=class&replay=1")
            assert any(item.get("zh") == EDITED for item in replay.get("backfill") or [])
            _assert_no_secrets(replay, ORIGINAL, token, listen_key)
            gap = await _hello_from(app, "/ws/listen?room_id=class&cursor=999999")
            assert gap["gap"] is True
            assert gap["events"] == []
            assert any(item.get("zh") == EDITED for item in gap.get("backfill") or [])
            _assert_no_secrets(gap, ORIGINAL, token, listen_key)
            stored = app.state.bus.caption_state("class")
            assert stored[0]["zh"] == EDITED
            assert stored[0]["zh_raw"] == ORIGINAL
            exported = await client.get(
                "/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token),
            )
            assert exported.status_code == 200, exported.text
            rows = exported.json()
            assert len(rows) == 1
            row = rows[0]
            assert row["zh"] == EDITED
            assert row["en"] == "EN " + EDITED
            assert row["zh_raw"] == ORIGINAL
            assert row["t0_ms"] == 1000
            assert row["t1_ms"] == 4000
            assert row["timeline_offset_ms"] == 0
            srt = await client.get(
                "/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token),
            )
            assert srt.status_code == 200, srt.text
            assert "00:00:01,000 --> 00:00:04,000" in srt.text
            assert EDITED in srt.text
            assert "EN " + EDITED in srt.text
            assert ORIGINAL not in srt.text
            assert token not in exported.text
            assert listen_key not in exported.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_failed_and_silent_partials_stay_off_the_audience_socket(tmp_path):
    settings = Settings(
        allow_testclient=True,
        translate=True,
        data_path=str(tmp_path / "captions.sqlite3"),
    )
    app = app_for(
        settings=settings,
        asr=PartialAsr(),
        translator=NamedEnglish(enabled=True, key="test-key"),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            listen_key = opened.json()["listen_key"]
            async with Socket(app, "/ws/listen?room_id=class") as sock:
                hello = await sock.recv()
                _assert_hello(hello)
                failed = await push(client, token, "class", "s", 1, b"fail", t0_ms=0, t1_ms=1000)
                assert failed.status_code == 422, failed.text
                assert failed.json()["zh"] == ""
                assert failed.json()["zh_raw"] == FAILED_PARTIAL
                assert failed.json()["status"] == "error"
                quiet = await push(client, token, "class", "s", 2, b"quiet", t0_ms=1000, t1_ms=2000)
                assert quiet.status_code == 200, quiet.text
                assert quiet.json()["zh"] == ""
                assert quiet.json()["zh_raw"] == SILENT_PARTIAL
                assert quiet.json()["status"] == "silent"
                live = await _collect(
                    sock,
                    lambda rows: {item.get("seq") for item in _captions(rows)} >= {1, 2},
                )
                captions = _captions(live)
                assert {item.get("seq") for item in captions} >= {1, 2}
                assert all(item.get("zh") == "" for item in captions)
                _assert_no_secrets(live, FAILED_PARTIAL, SILENT_PARTIAL, token, listen_key)
            history = await _hello_from(app, "/ws/listen?room_id=class")
            assert {item.get("seq") for item in history["history"]} >= {1, 2}
            _assert_no_secrets(history, FAILED_PARTIAL, SILENT_PARTIAL, token, listen_key)
            replay = await _hello_from(app, "/ws/listen?room_id=class&replay=1")
            assert {item.get("seq") for item in replay.get("backfill") or []} >= {1, 2}
            _assert_no_secrets(replay, FAILED_PARTIAL, SILENT_PARTIAL, token, listen_key)
            gap = await _hello_from(app, "/ws/listen?room_id=class&cursor=999999")
            assert gap["gap"] is True
            assert {item.get("seq") for item in gap.get("backfill") or []} >= {1, 2}
            _assert_no_secrets(gap, FAILED_PARTIAL, SILENT_PARTIAL, token, listen_key)
            saved = {row["seq"]: row for row in app.state.store.room_rows("class")}
            assert saved[1]["zh_raw"] == FAILED_PARTIAL
            assert saved[1]["zh"] == ""
            assert saved[2]["zh_raw"] == SILENT_PARTIAL
            assert saved[2]["zh"] == ""
            exported = await client.get(
                "/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token),
            )
            assert exported.status_code == 200, exported.text
            rows = {row["seq"]: row for row in exported.json()}
            # Error rows with no zh/en stay out of the export. Silent rows stay, raw text included.
            assert 1 not in rows
            assert rows[2]["zh"] == ""
            assert rows[2]["zh_raw"] == SILENT_PARTIAL
            assert rows[2]["status"] == "silent"
            assert "t0_ms" in rows[2] and "timeline_offset_ms" in rows[2]
    finally:
        await stop(app)
