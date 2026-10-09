"""P1-1 / N-1. Audience sockets need an allowed origin and this open's listen key.

A missing Origin is allowed on purpose: the pytest sockets and scripts/device_check.py
do not send one. Browsers always send Origin, and a present Origin must match the
allowlist (loopback, allowed_hosts, or the share host printed on the QR code).
The Host header is always checked, so a missing Origin is not a DNS-rebinding hole.
A refused listen is accepted, named, then closed, and never gets hello or a caption.
Closing before accept is an HTTP 403, which a browser shows as 1006.
"""

import json
import time
import urllib.parse
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.dispatch import for_listener
from app.server import _ReplayGate, _cap_replay_hello, _json_bytes, _replay_address_key, _trim_audience_rows
from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, app_for, auth, open_room, push, stop, token_of


ROOT = Path(__file__).resolve().parents[1]


def _headers(host="127.0.0.1:8780", origin=None, authorization=None):
    rows = [(b"host", host.encode())]
    if origin is not None:
        rows.append((b"origin", origin.encode()))
    if authorization is not None:
        rows.append((b"authorization", authorization.encode()))
    return rows


async def drive(app, path, *, client=("127.0.0.1", 5000), headers=None, with_key=True, timeout=2.0):
    """Speak ASGI websocket without requiring accept. Returns the raw messages."""
    import asyncio

    from tests.test_round2 import append_listen_key

    if with_key:
        path = append_listen_key(app, path)
    path, _, query = path.partition("?")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers if headers is not None else _headers(),
        "client": client,
        "server": ("127.0.0.1", 8780),
        "subprotocols": [],
    }
    out: asyncio.Queue = asyncio.Queue()
    inc: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await inc.get()

    async def send(message):
        await out.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    await inc.put({"type": "websocket.connect"})
    messages = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(out.get(), max(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            messages.append(msg)
            if msg.get("type") == "websocket.close":
                break
            if msg.get("type") != "websocket.send":
                continue
            try:
                body = json.loads(msg.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(body, dict) and body.get("type") == "hello":
                break
        return messages
    finally:
        await inc.put({"type": "websocket.disconnect", "code": 1000})
        if not task.done():
            try:
                await asyncio.wait_for(task, 1)
            except Exception:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _sent_text(messages) -> str:
    parts = []
    for msg in messages:
        if msg.get("type") == "websocket.send":
            parts.append(msg.get("text") or "")
    return "\n".join(parts)


def _close_code(messages):
    for msg in messages:
        if msg.get("type") == "websocket.close":
            return msg.get("code")
    return None


def _assert_rejected(messages, code):
    """Accept, then the refusal, then the close. No hello and no caption."""
    kinds = [msg.get("type") for msg in messages]
    assert "websocket.accept" in kinds, messages
    assert "websocket.send" in kinds, messages
    assert "websocket.close" in kinds, messages
    assert kinds.index("websocket.accept") < kinds.index("websocket.send") < kinds.index("websocket.close")
    assert _close_code(messages) == code
    sent = []
    for msg in messages:
        if msg.get("type") != "websocket.send":
            continue
        sent.append(json.loads(msg.get("text") or "{}"))
    assert len(sent) == 1, sent
    note = sent[0]
    assert note.get("type") == "room_unavailable"
    assert "backfill" not in note and "history" not in note and "zh" not in note and "events" not in note
    assert "hello" not in _sent_text(messages)
    if code == 4401:
        assert note.get("reason") == "link_invalid"
    elif code == 1008:
        assert note.get("reason") == "rejected"
    else:
        raise AssertionError(code)


def _hello(messages) -> dict:
    for msg in messages:
        if msg.get("type") != "websocket.send":
            continue
        body = json.loads(msg.get("text") or "{}")
        if isinstance(body, dict) and body.get("type") == "hello":
            return body
    raise AssertionError(messages)


def _zh(rows) -> list:
    return [item.get("zh") for item in rows or [] if isinstance(item, dict)]


async def _close_room(client, token, room):
    resp = await client.post(
        "/api/rooms/close",
        json={"room_id": room},
        headers={**auth(token), "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp


@pytest.mark.anyio
async def test_bad_origin_and_host_rejected_without_hello():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            await push(client, token, "class", "s", 1, "不該漏出".encode())
            share = "203.0.113.10"
            quoted = urllib.parse.quote(key, safe="")
            path = f"/ws/listen?room_id=class&k={quoted}&replay=1"

            evil = await drive(
                app, path, with_key=False,
                headers=_headers(origin="http://evil.example:8780"),
            )
            _assert_rejected(evil, 1008)
            assert "不該漏出" not in _sent_text(evil)

            # A cross-site page targeting the share host still sends the page's Origin.
            await client.post(
                "/api/share-host",
                json={"host": share, "room_id": "class"},
                headers={**auth(token), "content-type": "application/json"},
            )
            crossed = await drive(
                app, path, with_key=False,
                headers=_headers(host=f"{share}:8780", origin="http://evil.example:8780"),
            )
            _assert_rejected(crossed, 1008)

            rebound = await drive(
                app, path, with_key=False,
                headers=_headers(host="127.0.0.1.evil.example:8780"),
            )
            _assert_rejected(rebound, 1008)
            wrong_port = await drive(
                app, path, with_key=False,
                headers=_headers(host="127.0.0.1:9999"),
            )
            _assert_rejected(wrong_port, 1008)

            # Not a share host yet: a LAN origin is not a blanket allow.
            stranger = await drive(
                app, path, with_key=False,
                headers=_headers(host="203.0.113.9:8780", origin="http://203.0.113.9:8780"),
            )
            _assert_rejected(stranger, 1008)

            phone = await drive(
                app, path, with_key=False,
                headers=_headers(host=f"{share}:8780", origin=f"http://{share}:8780"),
            )
            assert any(msg.get("type") == "websocket.accept" for msg in phone)
            hello = _hello(phone)
            assert hello["room_id"] == "class"
            assert "不該漏出" in _zh(hello.get("backfill"))

            # Non-browser clients omit Origin. Host still has to match.
            blank = await drive(app, path, with_key=False, headers=_headers(origin=""))
            assert any(msg.get("type") == "websocket.accept" for msg in blank)
            assert _hello(blank)["type"] == "hello"
            missing = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=True, headers=_headers())
            assert _hello(missing)["type"] == "hello"

            illegal = await drive(app, "/ws/listen?room_id=bad%20room", with_key=False)
            _assert_rejected(illegal, 1008)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_missing_or_wrong_listen_key_rejected_without_hello():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            again = await open_room(client, token, "class")
            assert again.json()["listen_key"] == key
            await push(client, token, "class", "s", 1, "只有持鑰聽得到".encode())

            missing = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=False)
            _assert_rejected(missing, 4401)
            assert "只有持鑰聽得到" not in _sent_text(missing)

            wrong = await drive(app, "/ws/listen?room_id=class&replay=1&k=wrong-key", with_key=False)
            _assert_rejected(wrong, 4401)

            # The host token is not accepted in the query string, and the listen key is not a bearer token.
            as_query = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(token, safe=""),
                with_key=False,
            )
            _assert_rejected(as_query, 4401)
            as_bearer = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + key),
            )
            _assert_rejected(as_bearer, 4401)

            host = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + token),
            )
            assert any(msg.get("type") == "websocket.accept" for msg in host)
            assert "只有持鑰聽得到" in _zh(_hello(host).get("backfill"))

            # A rejected key must not take the only listener slot.
            tight = app_for(
                settings=Settings(allow_testclient=True, translate=False, max_listeners=1),
                translator=Translator(enabled=False),
            )
            try:
                async with AsyncClient(transport=ASGITransport(app=tight), base_url="http://127.0.0.1:8780") as other:
                    other_token = await token_of(tight, other)
                    opened_tight = await open_room(other, other_token, "class")
                    tight_key = opened_tight.json()["listen_key"]
                    denied = await drive(tight, "/ws/listen?room_id=class&k=nope", with_key=False)
                    _assert_rejected(denied, 4401)
                    allowed = await drive(
                        tight,
                        "/ws/listen?room_id=class&k=" + urllib.parse.quote(tight_key, safe=""),
                        with_key=False,
                    )
                    assert _hello(allowed)["type"] == "hello"
            finally:
                await stop(tight)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_reopen_rejects_old_key_and_limits_replay_to_this_open():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await open_room(client, token, "class")
            old_key = first.json()["listen_key"]
            pushed = await push(client, token, "class", "s", 1, "上一堂".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            await _close_room(client, token, "class")

            exported = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            assert exported.status_code == 200, exported.text
            assert "上一堂" in exported.text
            srt = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert srt.status_code == 200, srt.text
            assert "上一堂" in srt.text

            second = await open_room(client, token, "class")
            new_key = second.json()["listen_key"]
            assert new_key and new_key != old_key

            stale = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(old_key, safe=""),
                with_key=False,
            )
            _assert_rejected(stale, 4401)
            assert "上一堂" not in _sent_text(stale)

            fresh = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(new_key, safe=""),
                with_key=False,
            )
            hello = _hello(fresh)
            blob = json.dumps(hello, ensure_ascii=False)
            assert "上一堂" not in blob
            assert _zh(hello.get("history")) == []
            assert _zh(hello.get("backfill")) == []

            # A new class gets a new session id, the same way the host page does.
            newer = await push(client, token, "class", "t", 1, "這一堂".encode(), t0_ms=0, t1_ms=1000)
            assert newer.status_code == 200, newer.text
            live = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=True)
            hello = _hello(live)
            assert "這一堂" in _zh(hello.get("backfill"))
            assert "上一堂" not in json.dumps(hello, ensure_ascii=False)

            # A cursor past the live window is the same backfill, still limited to this open.
            gapped = await drive(app, "/ws/listen?room_id=class&cursor=1000000000", with_key=True)
            gap_hello = _hello(gapped)
            assert gap_hello["gap"] is True
            assert "上一堂" not in json.dumps(gap_hello, ensure_ascii=False)
            assert "這一堂" in _zh(gap_hello.get("backfill"))

            hosted = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + token),
            )
            hosted_hello = _hello(hosted)
            assert "這一堂" in _zh(hosted_hello.get("backfill"))
            assert "上一堂" not in json.dumps(hosted_hello, ensure_ascii=False)

            both = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            rows = json.loads(both.text)
            assert "上一堂" in _zh(rows) and "這一堂" in _zh(rows)
            srt_both = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert "上一堂" in srt_both.text and "這一堂" in srt_both.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_replay_backfill_is_rate_limited_per_ip():
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, replay_per_minute=1, history_limit=1),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)

            first = _hello(await drive(app, "/ws/listen?room_id=class&replay=1", client=("127.0.0.1", 5000)))
            assert "甲" in _zh(first.get("backfill"))
            assert "甲" not in _zh(first.get("history"))

            second = _hello(await drive(app, "/ws/listen?room_id=class&replay=1", client=("127.0.0.1", 5001)))
            assert "backfill" not in second
            assert "甲" not in json.dumps(second, ensure_ascii=False)
            assert "乙" in _zh(second.get("history"))

            other = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1", client=("198.51.100.8", 5000),
            ))
            assert "甲" in _zh(other.get("backfill"))

            # Live captions are not counted against the replay budget.
            async with Socket(app, "/ws/listen?room_id=class&cursor=0") as sock:
                hello = await sock.recv()
                assert hello["type"] == "hello"
                assert "backfill" not in hello
                made = await push(client, token, "class", "s", 3, "丙".encode(), t0_ms=2000, t1_ms=3000)
                assert made.status_code == 200, made.text
                seen = None
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and seen is None:
                    msg = await sock.recv(timeout=max(0.1, deadline - time.monotonic()))
                    if msg.get("zh") == "丙":
                        seen = msg
                assert seen is not None
    finally:
        await stop(app)


def test_replay_per_minute_setting_defaults_and_rejects_zero():
    # Classroom floor stays. 180 is the highest accepted address cap; the
    # behaviour tests below are what keep a rotating client id inside it.
    assert Settings().replay_per_minute >= 120
    assert Settings().replay_per_minute <= 180
    assert Settings().replay_client_per_minute == 8
    assert Settings().replay_client_per_minute <= Settings().replay_per_minute
    assert Settings.from_env({"BREEZE_REPLAY_PER_MINUTE": "3"}).replay_per_minute == 3
    assert Settings.from_env({"BREEZE_REPLAY_CLIENT_PER_MINUTE": "4"}).replay_client_per_minute == 4
    with pytest.raises(ValueError, match="BREEZE_REPLAY_PER_MINUTE"):
        Settings(replay_per_minute=0)
    with pytest.raises(ValueError, match="BREEZE_REPLAY_CLIENT_PER_MINUTE"):
        Settings(replay_client_per_minute=0)


def _long_caption(seq: int, zh: str, en: str) -> dict:
    return {
        "type": "caption",
        "id": f"class:s:{seq}",
        "room_id": "class",
        "session_id": "s",
        "session_ord": 1,
        "seq": seq,
        "version": 1,
        "zh": zh,
        "en": en,
        "status": "ready",
        "translate_status": "ok",
        "error": "",
        "t0_ms": (seq - 1) * 1000,
        "t1_ms": seq * 1000,
    }


def _reference_trim(rows: list[dict], budget: int) -> list[dict]:
    """The old quadratic trim, kept so a linear rewrite cannot change who is kept."""
    visible = [for_listener(item) for item in rows][-200:]

    def encoded(items: list[dict]) -> int:
        return _json_bytes(items)

    while len(visible) > 1 and encoded(visible) > budget:
        del visible[0]
    if len(visible) == 1 and encoded(visible) > budget:
        item = dict(visible[0])
        for key in ("zh", "en", "error"):
            text = item.get(key)
            if isinstance(text, str) and len(text) > 80:
                item[key] = text[:80]
        visible = [item] if encoded([item]) <= budget else []
    return visible


def test_backfill_trim_matches_the_old_rows_and_stays_linear(monkeypatch):
    """Dropping oldest rows must encode each caption once, not the whole tail again.

    The quadratic loop re-encoded about n²/2 caption bytes. A byte budget a few
    times the input catches that; a wall-clock cap catches a slow rewrite that
    still walks the tail one row at a time.
    """
    small = [_long_caption(seq, f"第{seq}句", "ok") for seq in range(1, 31)]
    assert _trim_audience_rows(small, 100 * 1024) == _reference_trim(small, 100 * 1024)
    huge = _long_caption(2, "字" * 100_000, "e" * 100_000)
    huge["zh_raw"] = "密" * 1000
    trimmed = _trim_audience_rows([_long_caption(1, "舊", "old"), huge], 100 * 1024)
    assert [item["seq"] for item in trimmed] == [2]
    assert len(trimmed[0]["zh"]) == 80
    assert "zh_raw" not in trimmed[0]
    assert trimmed == _reference_trim([_long_caption(1, "舊", "old"), huge], 100 * 1024)
    assert _trim_audience_rows([_long_caption(1, "字" * 500, "e" * 500)], 10) == []

    zh = "字" * 1500
    en = "e" * 1500
    rows = [_long_caption(seq, zh, en) for seq in range(1, 201)]
    input_bytes = sum(_json_bytes(for_listener(row)) for row in rows)
    real = json.dumps
    dumped = {"bytes": 0}

    def counting(obj, *args, **kwargs):
        text = real(obj, *args, **kwargs)
        dumped["bytes"] += len(text.encode("utf-8"))
        return text

    monkeypatch.setattr("app.server.json.dumps", counting)
    started = time.perf_counter()
    kept = _trim_audience_rows(rows, 100 * 1024)
    elapsed = time.perf_counter() - started
    assert kept[-1]["seq"] == 200
    assert kept[0]["seq"] != 1
    assert _json_bytes(kept) <= 100 * 1024
    # One encode per row, plus the single-row check that does not run here.
    # The quadratic tail rewrite moves tens of times this many bytes.
    assert dumped["bytes"] < input_bytes * 4
    assert elapsed < 0.2, elapsed


def test_replay_hello_keeps_newest_captions_inside_100kib():
    """history used to repeat the backfill. The whole hello is the budget."""
    zh = "測" * 92
    en = "e" * 296
    rows = [_long_caption(seq, zh, en) for seq in range(1, 221)]
    backfill = _trim_audience_rows(rows, 100 * 1024)
    history = [for_listener(row) for row in rows[-200:]]
    hello = {
        "type": "hello",
        "history": history,
        "events": list(history[-50:]),
        "gap": False,
        "latest_cursor": 220,
        "oldest_cursor": 21,
        "room_id": "class",
        "epoch": 7,
        "host_live": False,
        "backfill": backfill,
    }
    before = _json_bytes(hello)
    assert before > 100 * 1024
    backfill_before = len(hello["backfill"])
    _cap_replay_hello(hello)
    wire = _json_bytes(hello)
    assert wire <= 100 * 1024
    assert len(hello["backfill"]) >= backfill_before - 1
    backfill_ids = {item.get("id") for item in hello["backfill"] if isinstance(item, dict) and item.get("id")}
    history_ids = {item.get("id") for item in hello["history"] if isinstance(item, dict) and item.get("id")}
    assert backfill_ids.isdisjoint(history_ids)
    assert hello["backfill"][-1]["id"] == "class:s:220"
    assert hello["backfill"][-1]["zh"] == zh
    assert all(item.get("id") != "class:s:1" for item in hello["backfill"])
    assert all(item.get("id") != "class:s:1" for item in hello["history"])
    assert all(item.get("id") != "class:s:1" for item in hello["events"])
    short = {
        "type": "hello",
        "history": [_long_caption(1, "甲", "a")],
        "events": [_long_caption(1, "甲", "a")],
        "gap": True,
        "latest_cursor": 1,
        "oldest_cursor": 1,
        "room_id": "class",
        "epoch": 1,
        "host_live": True,
        "backfill": [_long_caption(1, "甲", "a"), _long_caption(2, "乙", "b")],
    }
    snapshot = json.loads(json.dumps(short))
    _cap_replay_hello(short)
    assert short == snapshot


def _hello_wire(messages) -> int:
    for msg in messages:
        if msg.get("type") != "websocket.send":
            continue
        text = msg.get("text") or ""
        if text.startswith('{"type":"hello"'):
            return len(text.encode("utf-8"))
    raise AssertionError(messages)


@pytest.mark.anyio
async def test_default_cap_rejects_181st_cid_and_bounds_the_replay_hello():
    """Default settings, one TCP address, 181 well-formed client ids.

    The 181st replay is deferred. X-Forwarded-For does not open a new bucket
    or move the cost onto another address. Every replay hello, including the
    deferred one, is at most 100 KiB, and the newest caption is the one kept.
    """
    cap = Settings().replay_per_minute
    client_cap = Settings().replay_client_per_minute
    assert cap == 180
    assert client_cap == 8
    zh = "測" * 92
    en = "e" * 296
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=40),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "other")
            bus = app.state.bus
            for seq in range(1, 221):
                bus.publish(_long_caption(seq, zh, en))
            bus.publish({
                "type": "caption",
                "id": "other:s:1",
                "room_id": "other",
                "session_id": "s",
                "session_ord": 1,
                "seq": 1,
                "version": 1,
                "zh": "別班",
                "en": "other",
                "status": "ready",
            })
            ip = "203.0.113.181"
            full = 0
            wires = []
            # Same rows the socket trims into backfill, before the hello cut.
            pre_cut = _trim_audience_rows(bus.caption_state("class"))
            for index in range(cap + 1):
                headers = _spoofed(index) if index in {0, cap} else None
                messages = await drive(
                    app,
                    f"/ws/listen?room_id=class&cursor=0&replay=1&cid=phone-{index}",
                    client=(ip, 9000 + index),
                    headers=headers,
                )
                hello = _hello(messages)
                wire = _hello_wire(messages)
                wires.append(wire)
                assert wire <= 100 * 1024
                blob = json.dumps(hello, ensure_ascii=False)
                assert "別班" not in blob
                if index < cap:
                    assert _full_replay(hello)
                    assert hello["backfill"][-1]["id"] == "class:s:220"
                    assert hello["backfill"][-1]["zh"] == zh
                    assert all(item.get("id") != "class:s:1" for item in hello["backfill"])
                    assert len(hello["backfill"]) >= len(pre_cut) - 1
                    backfill_ids = {item.get("id") for item in hello["backfill"] if isinstance(item, dict) and item.get("id")}
                    history_ids = {item.get("id") for item in hello.get("history") or [] if isinstance(item, dict) and item.get("id")}
                    assert backfill_ids.isdisjoint(history_ids)
                    full += 1
                else:
                    assert hello.get("backfill_deferred") is True
                    assert "backfill" not in hello
            assert full == cap
            assert max(wires) <= 100 * 1024
            # 180 * 100 KiB = 18.4 MB. The measured hellos have to land under that.
            assert cap * max(wires) <= cap * 100 * 1024
            still = _hello(await drive(
                app,
                "/ws/listen?room_id=class&cursor=0&replay=1&cid=phone-extra",
                client=(ip, 9999),
                headers=_spoofed(7),
            ))
            assert still.get("backfill_deferred") is True
            other = _hello(await drive(
                app,
                "/ws/listen?room_id=class&cursor=0&replay=1&cid=phone-0",
                client=("198.51.100.181", 9100),
                headers=_headers() + [
                    (b"x-forwarded-for", ip.encode()),
                    (b"x-real-ip", ip.encode()),
                    (b"forwarded", f"for={ip}".encode()),
                ],
            ))
            assert _full_replay(other)
            assert other["backfill"][-1]["zh"] == zh

            anon_ip = "198.51.100.182"
            anon_full = 0
            for index in range(client_cap + 1):
                hello = _hello(await drive(
                    app,
                    "/ws/listen?room_id=class&cursor=0&replay=1",
                    client=(anon_ip, 9200 + index),
                ))
                if index < client_cap:
                    assert _full_replay(hello)
                    anon_full += 1
                else:
                    assert hello.get("backfill_deferred") is True
            assert anon_full == client_cap
            for query in ("&cid=", "&cid=bad.cid", "&cid=" + ("a" * 80), "&cid=anon"):
                hello = _hello(await drive(
                    app,
                    "/ws/listen?room_id=class&cursor=0&replay=1" + query,
                    client=(anon_ip, 9300),
                ))
                assert hello.get("backfill_deferred") is True
                assert "backfill" not in hello
            fresh = _hello(await drive(
                app,
                "/ws/listen?room_id=class&cursor=0&replay=1&cid=real-phone",
                client=(anon_ip, 9400),
            ))
            assert _full_replay(fresh)
            assert fresh["backfill"][-1]["zh"] == zh
    finally:
        await stop(app)


def test_run_does_not_trust_proxy_headers(monkeypatch):
    """Loopback X-Forwarded-For must not rewrite ws.client. The cap is the TCP peer."""
    import app.run as run_mod

    monkeypatch.setattr(run_mod.os, "chdir", lambda path: None)
    monkeypatch.setattr(run_mod, "inspect", lambda settings: {"ok": True, "checks": []})
    monkeypatch.setattr(run_mod.Settings, "from_env", classmethod(lambda cls: Settings()))
    monkeypatch.setenv("BREEZE_OPEN_BROWSER", "0")
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(run_mod.uvicorn, "run", fake_run)
    run_mod.main()
    assert captured["args"] == ("app.server:app",)
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["proxy_headers"] is False


def _wire_bytes(rows) -> int:
    return len(json.dumps(list(rows or []), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _full_replay(hello: dict) -> bool:
    """A full replay is a hello whose backfill list is present and not deferred."""
    if hello.get("backfill_deferred") is True or "backfill" not in hello:
        return False
    rows = hello.get("backfill")
    assert isinstance(rows, list) and rows, hello
    assert len(rows) <= 200
    assert _wire_bytes(rows) <= 100 * 1024
    return True


def _spoofed(index: int):
    """Forwarded headers a proxy might add. The listen path must ignore them."""
    return _headers() + [
        (b"x-forwarded-for", f"198.51.100.{index % 200}, 203.0.113.9".encode()),
        (b"x-real-ip", f"203.0.113.{index % 200}".encode()),
        (b"forwarded", f'for=192.0.2.{index % 200};proto=http'.encode()),
    ]


@pytest.mark.anyio
async def test_same_ip_rotating_cids_stay_inside_the_replay_cap():
    """One TCP address, three bursts of 40 replay hellos.

    Distinct well-formed client ids all succeed (40 is under the address cap,
    and each id is used once). Omitting the id, or repeating one id, stops at
    the per-client cap. The address total of the three bursts stays within the
    address cap: a client id can only tighten it.
    """
    ip_cap = Settings().replay_per_minute
    client_cap = Settings().replay_client_per_minute
    assert 40 <= ip_cap <= 180
    assert client_cap == 8
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=100, history_limit=8),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text

            async def burst(query_for, count, port_base):
                full = 0
                for index in range(count):
                    hello = _hello(await drive(
                        app,
                        "/ws/listen?room_id=class&replay=1" + query_for(index),
                        client=("203.0.113.40", port_base + index),
                    ))
                    if _full_replay(hello):
                        full += 1
                        assert "甲" in _zh(hello.get("backfill"))
                    else:
                        assert hello.get("backfill_deferred") is True
                        assert "backfill" not in hello
                return full

            rotating = await burst(lambda index: f"&cid=phone-{index:02d}", 40, 5000)
            missing = await burst(lambda index: "", 40, 6000)
            fixed = await burst(lambda index: "&cid=same-phone", 40, 7000)
            assert rotating == 40
            assert missing == client_cap
            assert fixed == client_cap
            assert rotating + missing + fixed <= ip_cap
            assert missing < rotating and fixed < rotating
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_forwarded_headers_cannot_reset_the_tcp_replay_cap():
    """X-Forwarded-For, X-Real-IP, and Forwarded are not the replay address."""
    cap = 5
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=cap,
            replay_client_per_minute=8,
            max_listeners=80,
            history_limit=4,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            exhausted = "203.0.113.9"
            full = 0
            for index in range(40):
                hello = _hello(await drive(
                    app,
                    f"/ws/listen?room_id=class&replay=1&cid=rot-{index:02d}",
                    client=(exhausted, 8000 + index),
                    headers=_spoofed(index),
                ))
                if _full_replay(hello):
                    full += 1
            assert full == cap
            other = _hello(await drive(
                app,
                "/ws/listen?room_id=class&replay=1&cid=other-net",
                client=("198.51.100.8", 8100),
                headers=_headers() + [
                    (b"x-forwarded-for", exhausted.encode()),
                    (b"x-real-ip", exhausted.encode()),
                    (b"forwarded", f"for={exhausted}".encode()),
                ],
            ))
            assert _full_replay(other)
            still = _hello(await drive(
                app,
                "/ws/listen?room_id=class&replay=1&cid=rot-extra",
                client=(exhausted, 8101),
                headers=_spoofed(99),
            ))
            assert still.get("backfill_deferred") is True
            assert "backfill" not in still
    finally:
        await stop(app)


def test_replay_gate_address_cap_bounds_rotating_cids_and_keys():
    """Production limits: rotating ids stop at the address cap, and the tables stay bounded."""
    cap = Settings().replay_per_minute
    client_cap = Settings().replay_client_per_minute
    now = {"t": 1000.0}
    gate = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert gate.ip_limit == cap
    assert gate.limit == cap
    assert gate.client_limit == client_cap
    assert _ReplayGate(cap, cap + 50, clock=lambda: now["t"]).client_limit == cap
    allowed = 0
    for index in range(cap + 40):
        ok, retry = gate.allow("203.0.113.9", f"cid-{index}")
        if ok:
            allowed += 1
        else:
            assert retry >= 250
    assert allowed == cap
    assert gate.allow("203.0.113.9", "cid-extra")[0] is False
    assert len(gate._client_hits) == cap
    assert ("203.0.113.9", "cid-extra") not in gate._client_hits
    assert gate.allow("198.51.100.8", "cid-0")[0] is True

    fixed = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert sum(fixed.allow("203.0.113.9", "same")[0] for _ in range(40)) == client_cap
    assert list(fixed._client_hits) == [("203.0.113.9", "same")]
    missing = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert sum(missing.allow("203.0.113.9", "")[0] for _ in range(40)) == client_cap
    assert list(missing._client_hits) == [("203.0.113.9", "")]

    now["t"] = 0.0
    denied = _ReplayGate(1, 1, clock=lambda: now["t"], max_keys=4)
    assert denied.allow("10.0.0.1", "kept")[0] is True
    for index in range(30):
        assert denied.allow("10.0.0.1", f"nope-{index}")[0] is False
    assert list(denied._client_hits) == [("10.0.0.1", "kept")]
    assert len(denied._ip_hits) == 1

    now["t"] = 10.0
    bounded = _ReplayGate(2, 2, window_s=60.0, clock=lambda: now["t"], max_keys=2)
    assert bounded.allow("10.1.0.1", "a")[0] is True
    now["t"] = 20.0
    assert bounded.allow("10.1.0.2", "b")[0] is True
    now["t"] = 30.0
    kept = list(bounded._ip_hits["10.1.0.1"])
    overflowed, overflow_retry = bounded.allow("10.1.0.3", "c")
    assert overflowed is True
    assert overflow_retry == 0
    assert bounded._ip_hits["10.1.0.1"] == kept
    assert ("10.1.0.3", "c") not in bounded._client_hits
    assert set(bounded._ip_hits) == {"10.1.0.1", "10.1.0.2"}
    assert len(bounded._client_hits) == 2
    assert bounded.allow("10.1.0.1", "a")[0] is True
    assert bounded._ip_hits["10.1.0.1"] == [10.0, 30.0]
    now["t"] = 91.0
    assert bounded.allow("10.1.0.9", "fresh")[0] is True
    assert "10.1.0.1" not in bounded._ip_hits
    assert ("10.1.0.1", "a") not in bounded._client_hits
    assert "10.1.0.2" not in bounded._ip_hits
    assert ("10.1.0.2", "b") not in bounded._client_hits
    assert "10.1.0.9" in bounded._ip_hits
    assert len(bounded._ip_hits) <= 2
    assert len(bounded._client_hits) <= 2

    now["t"] = 0.0
    throttled = _ReplayGate(4, 4, window_s=0.4, clock=lambda: now["t"], max_keys=10)
    assert throttled.allow("10.9.0.1", "a")[0] is True
    now["t"] = 0.5
    assert throttled.allow("10.9.0.2", "b")[0] is True
    assert "10.9.0.1" in throttled._ip_hits
    now["t"] = 1.0
    assert throttled.allow("10.9.0.2", "b")[0] is True
    assert "10.9.0.1" not in throttled._ip_hits

    now["t"] = 5.0
    v6 = _ReplayGate(2, 2, clock=lambda: now["t"])
    assert v6.allow("2001:db8:1:2::1", "a")[0] is True
    assert v6.allow("2001:db8:1:2::ffff", "b")[0] is True
    assert v6.allow("2001:db8:1:2::abc", "c")[0] is False
    assert v6.allow("2001:db8:9:9::1", "c")[0] is True
    assert set(v6._ip_hits) == {"2001:db8:1:2::/64", "2001:db8:9:9::/64"}
    mapped = _ReplayGate(1, 1, clock=lambda: now["t"])
    assert mapped.allow("203.0.113.50", "a")[0] is True
    assert mapped.allow("::ffff:203.0.113.50", "b")[0] is False
    assert list(mapped._ip_hits) == ["203.0.113.50"]
    plain = _ReplayGate(1, 1, clock=lambda: now["t"])
    assert plain.allow("not-an-ip", "a")[0] is True
    assert plain.allow("not-an-ip", "b")[0] is False
    assert "not-an-ip" in plain._ip_hits
    assert plain.allow("other-name", "a")[0] is True

    now["t"] = 0.0
    again = _ReplayGate(1, 1, window_s=60.0, clock=lambda: now["t"])
    assert again.allow("10.2.0.1", "a")[0] is True
    assert again.allow("10.2.0.1", "a")[0] is False
    now["t"] = 61.0
    assert again.allow("10.2.0.1", "a")[0] is True
    assert len(again._ip_hits["10.2.0.1"]) == 1
    assert len(again._client_hits[("10.2.0.1", "a")]) == 1

    # Client ids must not consume address slots. A full address table shares
    # one overflow bucket; retry_after is the real wait, not a fixed 1000 or 60000.
    now["t"] = 0.0
    wide = _ReplayGate(180, 8, window_s=60.0, clock=lambda: now["t"], max_keys=4)
    for ip_index in range(3):
        for cid_index in range(8):
            assert wide.allow(f"10.4.0.{ip_index}", f"c{cid_index}")[0] is True
    assert len(wide._ip_hits) == 3
    assert len(wide._client_hits) == 24
    assert len(wide._client_hits) > wide.max_keys
    assert wide.allow("10.4.0.9", "c")[0] is True
    assert "10.4.0.9" in wide._ip_hits
    shared, shared_retry = wide.allow("10.4.0.10", "c")
    assert shared is True and shared_retry == 0
    assert "10.4.0.10" not in wide._ip_hits
    assert len(wide._ip_hits) == wide.max_keys

    now["t"] = 0.0
    timed = _ReplayGate(1, 1, window_s=60.0, clock=lambda: now["t"], max_keys=1, overflow_limit=1)
    assert timed.allow("10.5.0.1", "a")[0] is True
    assert timed.allow("10.5.0.2", "b")[0] is True
    now["t"] = 20.0
    refused, retry_ms = timed.allow("10.5.0.3", "c")
    assert refused is False
    assert 39000 <= retry_ms <= 41000
    assert retry_ms != 1000
    assert retry_ms != 60000
    assert list(timed._ip_hits) == ["10.5.0.1"]
    assert timed._ip_hits["10.5.0.1"] == [0.0]

    now["t"] = 0.0
    expired = _ReplayGate(1, 1, window_s=0.2, clock=lambda: now["t"], max_keys=1)
    assert expired.allow("10.6.0.1", "a")[0] is True
    now["t"] = 0.3
    assert expired.allow("10.6.0.2", "b")[0] is True
    assert "10.6.0.1" not in expired._ip_hits
    assert "10.6.0.2" in expired._ip_hits
    assert ("10.6.0.1", "a") not in expired._client_hits

    assert _replay_address_key("fe80::1%eth0") == "fe80::/64"
    assert _replay_address_key("[fe80::1%eth0]") == "fe80::/64"
    assert _replay_address_key("fe80::2%25wlan0") == "fe80::/64"
    assert _replay_address_key("2001:db8::1%a%b") == "2001:db8::/64"


@pytest.mark.anyio
async def test_full_replay_table_still_delivers_live_captions():
    """Filling every client slot, then the address table, must not drop live captions.

    23 addresses × the per-address cap used to fill the 4096 client-key table and
    the page then closed the socket. Replay for a new address may wait, at most
    one window. A cursor=0 listener still receives the next caption.
    """
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=4),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            gate = app.state.replay_gate
            for index in range(23):
                ip = f"198.51.100.{index + 1}"
                for cid in range(gate.ip_limit):
                    ok, retry = gate.allow(ip, f"cid-{cid}")
                    assert ok is True and retry == 0
            assert len(gate._ip_hits) == 23
            assert len(gate._client_hits) == 23 * gate.ip_limit
            assert len(gate._client_hits) > gate.max_keys

            async with Socket(app, "/ws/listen?room_id=class&cursor=0", client=("203.0.113.77", 9100)) as live:
                hello = await live.recv()
                assert hello["type"] == "hello"
                assert hello.get("backfill_deferred") is not True
                made = await push(client, token, "class", "s", 1, "即時".encode(), t0_ms=0, t1_ms=1000)
                assert made.status_code == 200, made.text
                seen = None
                for _ in range(8):
                    msg = await live.recv()
                    if isinstance(msg, dict) and msg.get("zh") == "即時":
                        seen = msg
                        break
                assert seen is not None, "live caption missing while the replay table is full"

            filled = len(gate._ip_hits)
            for index in range(gate.max_keys - filled):
                ok, retry = gate.allow(f"10.{index // 65025}.{(index // 255) % 255}.{index % 255}", "a")
                assert ok is True and retry == 0
            assert len(gate._ip_hits) == gate.max_keys
            for index in range(gate.overflow_limit):
                ok, retry = gate.allow("203.0.113.250", f"ov-{index}")
                assert ok is True and retry == 0
            assert len(gate._ip_hits) == gate.max_keys
            assert "203.0.113.250" not in gate._ip_hits
            async with Socket(
                app,
                "/ws/listen?room_id=class&replay=1&cid=late",
                client=("203.0.113.251", 9101),
            ) as late:
                deferred = await late.recv()
                assert deferred["type"] == "hello"
                assert deferred.get("backfill_deferred") is True
                retry_ms = int(deferred["retry_after_ms"])
                assert 1000 < retry_ms <= 61000
                assert "backfill" not in deferred
                assert any(item.get("zh") == "即時" for item in deferred.get("history") or [])
                made_late = await push(client, token, "class", "s", 2, "延後後".encode(), t0_ms=1000, t1_ms=2000)
                assert made_late.status_code == 200, made_late.text
                late_seen = None
                closed = False
                for _ in range(8):
                    msg = await late.recv()
                    if isinstance(msg, dict) and msg.get("type") == "websocket.close":
                        closed = True
                        break
                    if isinstance(msg, dict) and msg.get("zh") == "延後後":
                        late_seen = msg
                        break
                assert late_seen is not None, "deferred listener dropped the live caption"
                assert closed is False, "backfill_deferred must not close the live socket"
                try:
                    extra = await late.recv(timeout=0.2)
                except Exception:
                    extra = None
                assert not (isinstance(extra, dict) and extra.get("type") == "websocket.close")
    finally:
        await stop(app)


def test_expired_addresses_scan_the_client_table_once():
    """A thousand addresses expiring together walk the client table one time.

    The visit counter is the number of client keys compared, not a wall clock.
    One pass equals the key count. A pass per address would be addresses times keys.
    """
    now = {"t": 0.0}
    addresses = 1000
    per_address = 3
    gate = _ReplayGate(
        8,
        per_address,
        window_s=60.0,
        clock=lambda: now["t"],
        max_keys=addresses + 10,
        max_client_keys=addresses * per_address + 10,
    )
    for index in range(addresses):
        ip = f"10.{index // 65536}.{(index // 256) % 256}.{index % 256}"
        for cid in range(per_address):
            ok, retry = gate.allow(ip, f"c{cid}")
            assert ok is True and retry == 0
    client_keys = len(gate._client_hits)
    assert client_keys == addresses * per_address
    assert gate._expire_passes == 0
    assert gate._expire_visits == 0
    now["t"] = 61.0
    ok, retry = gate.allow("192.0.2.10", "fresh")
    assert ok is True and retry == 0
    assert gate._expire_passes == 1
    assert gate._expire_visits == client_keys
    assert gate._expire_visits < addresses * client_keys
    assert len(gate._ip_hits) == 1
    assert len(gate._client_hits) == 1
    assert ("192.0.2.10", "fresh") in gate._client_hits


def test_full_client_table_uses_overflow_without_resetting_peers():
    """The client-key cap shares the overflow bucket and leaves in-window hits alone."""
    now = {"t": 0.0}
    gate = _ReplayGate(4, 4, window_s=60.0, clock=lambda: now["t"], max_keys=10, max_client_keys=3)
    assert gate.allow("10.0.0.1", "a") == (True, 0)
    assert gate.allow("10.0.0.2", "b") == (True, 0)
    assert gate.allow("10.0.0.3", "c") == (True, 0)
    kept = list(gate._ip_hits["10.0.0.1"])
    client_kept = list(gate._client_hits[("10.0.0.1", "a")])
    ok, retry = gate.allow("10.0.0.4", "d")
    assert ok is True and retry == 0
    assert retry != 1000
    assert "10.0.0.4" not in gate._ip_hits
    assert ("10.0.0.4", "d") not in gate._client_hits
    assert gate._ip_hits["10.0.0.1"] == kept
    assert gate._client_hits[("10.0.0.1", "a")] == client_kept
    assert gate.allow("10.0.0.1", "a") == (True, 0)
    assert gate._ip_hits["10.0.0.1"] == [0.0, 0.0]


def test_overflow_bucket_clears_on_the_next_window_without_dropping_the_seated_peer():
    """Overflow hits expire with the window. A peer refreshed inside it keeps the slot."""
    now = {"t": 0.0}
    gate = _ReplayGate(2, 2, window_s=60.0, clock=lambda: now["t"], max_keys=1, overflow_limit=2)
    assert gate.allow("10.8.0.1", "a") == (True, 0)
    assert gate.allow("10.8.0.2", "b") == (True, 0)
    assert gate.allow("10.8.0.3", "c") == (True, 0)
    assert gate.allow("10.8.0.4", "d")[0] is False
    assert list(gate._ip_hits) == ["10.8.0.1"]
    now["t"] = 50.0
    assert gate.allow("10.8.0.1", "a") == (True, 0)
    assert gate._ip_hits["10.8.0.1"] == [0.0, 50.0]
    assert gate.allow("10.8.0.5", "e")[0] is False
    now["t"] = 61.0
    opened, retry = gate.allow("10.8.0.9", "z")
    assert opened is True and retry == 0
    assert "10.8.0.1" in gate._ip_hits
    assert 50.0 in gate._ip_hits["10.8.0.1"]
    assert "10.8.0.9" not in gate._ip_hits


def test_overflow_default_admits_48_then_waits():
    """The shared bucket defaults to 48. The 49th new address waits for the window."""
    now = {"t": 0.0}
    gate = _ReplayGate(1, 1, window_s=60.0, clock=lambda: now["t"], max_keys=1)
    assert gate.allow("10.7.0.1", "seat") == (True, 0)
    allowed = 0
    for index in range(1, 61):
        ok, _retry = gate.allow(f"10.7.1.{index}", "x")
        if not ok:
            break
        allowed += 1
    assert allowed == 48
    denied, retry = gate.allow("10.7.2.1", "x")
    assert denied is False
    assert retry != 1000
    assert gate._ip_hits["10.7.0.1"] == [0.0]


@pytest.mark.anyio
async def test_backfill_supplement_does_not_take_the_last_listener_seat():
    """One seat left stays available for the next person while this page catches up.

    The extra replay socket shares the seated client id and does not count.
    A second extra, or a supplement for someone who is not seated, still counts
    and is refused with room_full once the seated listeners fill the room.
    """
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=2),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")

            async with Socket(
                app,
                "/ws/listen?room_id=class&cursor=0&cid=phone-a",
                client=("203.0.113.10", 1),
            ) as seated:
                hello = await seated.recv()
                assert hello["type"] == "hello"
                assert (await client.get("/api/metrics", headers=auth(token))).json()["listeners"] == 1

                async with Socket(
                    app,
                    "/ws/listen?room_id=class&replay=1&supplement=1&cid=phone-a",
                    client=("203.0.113.10", 2),
                ) as supplement:
                    extra = await supplement.recv()
                    assert extra["type"] == "hello"
                    assert extra.get("reason") != "full"
                    assert (await client.get("/api/metrics", headers=auth(token))).json()["listeners"] == 1

                    async with Socket(
                        app,
                        "/ws/listen?room_id=class&cursor=0&cid=phone-b",
                        client=("203.0.113.11", 1),
                    ) as nxt:
                        nxt_hello = await nxt.recv()
                        assert nxt_hello["type"] == "hello"
                        assert nxt_hello.get("reason") != "full"
                        assert (await client.get("/api/metrics", headers=auth(token))).json()["listeners"] == 2

                        async with Socket(
                            app,
                            "/ws/listen?room_id=class&cursor=0&cid=phone-c",
                            client=("203.0.113.12", 1),
                        ) as full:
                            note = await full.recv()
                            assert note["type"] == "room_unavailable"
                            assert note["reason"] == "full"
                            closed = await full.recv()
                            assert closed["type"] == "websocket.close"
                            assert closed["code"] == 1013

                        async with Socket(
                            app,
                            "/ws/listen?room_id=class&replay=1&supplement=1&cid=phone-d",
                            client=("203.0.113.13", 1),
                        ) as bypass:
                            refused = await bypass.recv()
                            assert refused["type"] == "room_unavailable"
                            assert refused["reason"] == "full"
                            closed = await bypass.recv()
                            assert closed["code"] == 1013

                        async with Socket(
                            app,
                            "/ws/listen?room_id=class&replay=1&supplement=1",
                            client=("203.0.113.14", 1),
                        ) as anon:
                            refused = await anon.recv()
                            assert refused["type"] == "room_unavailable"
                            assert refused["reason"] == "full"

                        async with Socket(
                            app,
                            "/ws/listen?room_id=class&replay=1&supplement=1&cid=phone-a",
                            client=("203.0.113.10", 3),
                        ) as second:
                            refused = await second.recv()
                            assert refused["type"] == "room_unavailable"
                            assert refused["reason"] == "full"
                            closed = await second.recv()
                            assert closed["code"] == 1013
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_unseated_supplement_still_takes_a_listener_seat():
    """supplement=1 is not a free pass. Without a seated peer it occupies the seat."""
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=1),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            async with Socket(
                app,
                "/ws/listen?room_id=class&replay=1&supplement=1&cid=lonely",
                client=("203.0.113.20", 1),
            ) as lonely:
                hello = await lonely.recv()
                assert hello["type"] == "hello"
                assert (await client.get("/api/metrics", headers=auth(token))).json()["listeners"] == 1
                async with Socket(
                    app,
                    "/ws/listen?room_id=class&cursor=0&cid=other",
                    client=("203.0.113.21", 1),
                ) as other:
                    note = await other.recv()
                    assert note["type"] == "room_unavailable"
                    assert note["reason"] == "full"
                    closed = await other.recv()
                    assert closed["code"] == 1013
    finally:
        await stop(app)


def _room_listeners(app, room_id="class"):
    room = app.state.room_book.get(room_id)
    assert room is not None
    conns = list(room["listeners"])
    seated = [conn for conn in conns if not conn.supplement]
    supplements = [conn for conn in conns if conn.supplement]
    return seated, supplements


async def _metrics_listeners(client, token) -> int:
    resp = await client.get("/api/metrics", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return int(resp.json()["listeners"])


@pytest.mark.anyio
async def test_supplement_exemption_needs_replay_and_a_named_seat():
    """supplement=1 alone is not an exemption.

    It also needs replay=1 and a named seat. Two anonymous sockets must not
    count as that seat for each other, and a supplement without replay still
    takes the last listener slot.
    """
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=1),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            async with Socket(
                app,
                "/ws/listen?room_id=class&cursor=0",
                client=("203.0.113.80", 1),
            ) as anon:
                hello = await anon.recv()
                assert hello["type"] == "hello"
                async with Socket(
                    app,
                    "/ws/listen?room_id=class&replay=1&supplement=1",
                    client=("203.0.113.80", 2),
                ) as other_anon:
                    note = await other_anon.recv()
                    assert note["type"] == "room_unavailable"
                    assert note["reason"] == "full"
                    closed = await other_anon.recv()
                    assert closed["type"] == "websocket.close"
                    assert closed["code"] == 1013
            async with Socket(
                app,
                "/ws/listen?room_id=class&cursor=0&cid=phone-a",
                client=("203.0.113.81", 1),
            ) as seat:
                sat = await seat.recv()
                assert sat["type"] == "hello"
                async with Socket(
                    app,
                    "/ws/listen?room_id=class&supplement=1&cid=phone-a",
                    client=("203.0.113.81", 2),
                ) as noreplay:
                    note = await noreplay.recv()
                    assert note["type"] == "room_unavailable"
                    assert note["reason"] == "full"
                    closed = await noreplay.recv()
                    assert closed["code"] == 1013
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_supplement_closes_when_its_seat_leaves():
    """A backfill socket rides the seat. The seat leaving closes it.

    It must not stay in the fan-out as an uncounted live listener.
    """
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=2),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            async with Socket(
                app,
                "/ws/listen?room_id=class&cursor=0&cid=phone-a",
                client=("203.0.113.40", 1),
            ) as seat:
                hello = await seat.recv()
                assert hello["type"] == "hello"
                supplement = Socket(
                    app,
                    "/ws/listen?room_id=class&replay=1&supplement=1&cid=phone-a",
                    client=("203.0.113.40", 2),
                )
                await supplement.__aenter__()
                try:
                    extra = await supplement.recv()
                    assert extra["type"] == "hello"
                    assert extra.get("reason") != "full"
                    assert await _metrics_listeners(client, token) == 1
                    seated, supplements = _room_listeners(app)
                    assert len(seated) == 1 and len(supplements) == 1
                    await seat.close()
                    closed = await supplement.recv()
                    assert closed["type"] == "websocket.close"
                    assert closed["code"] == 1000
                    assert await _metrics_listeners(client, token) == 0
                    seated, supplements = _room_listeners(app)
                    assert seated == [] and supplements == []
                    async with Socket(
                        app,
                        "/ws/listen?room_id=class&cursor=0&cid=phone-b",
                        client=("203.0.113.41", 1),
                    ) as nxt:
                        nxt_hello = await nxt.recv()
                        assert nxt_hello["type"] == "hello"
                        assert nxt_hello.get("reason") != "full"
                        delivered = app.state.pipeline.on_event({
                            "type": "caption",
                            "id": "class:s:1",
                            "room_id": "class",
                            "session_id": "s",
                            "session_ord": 1,
                            "seq": 1,
                            "version": 1,
                            "zh": "座位走了",
                            "en": "seat left",
                            "status": "ready",
                        })
                        assert delivered is not None
                        live = await nxt.recv()
                        assert live["zh"] == "座位走了"
                        with pytest.raises(TimeoutError):
                            await supplement.recv(timeout=0.4)
                finally:
                    await supplement.close()
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_supplement_rotation_does_not_accumulate_uncounted_listeners():
    """Sitting, opening supplement=1, then leaving must not stack live sockets.

    A deferred replay still accepts the socket. That socket has to die with the
    seat, or eight cids can sit past max_listeners and keep receiving captions.
    """
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            max_listeners=3,
            replay_per_minute=2,
            replay_client_per_minute=1,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            async with Socket(
                app,
                "/ws/listen?room_id=class&cursor=0&cid=anchor-1",
                client=("203.0.113.51", 1),
            ) as anchor_a, Socket(
                app,
                "/ws/listen?room_id=class&cursor=0&cid=anchor-2",
                client=("203.0.113.52", 1),
            ) as anchor_b:
                assert (await anchor_a.recv())["type"] == "hello"
                assert (await anchor_b.recv())["type"] == "hello"
                assert await _metrics_listeners(client, token) == 2
                deferred = 0
                nat = "203.0.113.50"
                for index in range(8):
                    seat = Socket(
                        app,
                        f"/ws/listen?room_id=class&cursor=0&cid=rot-{index}",
                        client=(nat, 1000 + index),
                    )
                    supplement = Socket(
                        app,
                        f"/ws/listen?room_id=class&replay=1&supplement=1&cid=rot-{index}",
                        client=(nat, 2000 + index),
                    )
                    await seat.__aenter__()
                    try:
                        sat = await seat.recv()
                        assert sat["type"] == "hello" and sat.get("reason") != "full"
                        await supplement.__aenter__()
                        try:
                            extra = await supplement.recv()
                            assert extra["type"] == "hello"
                            assert extra.get("reason") != "full"
                            if extra.get("backfill_deferred") is True:
                                deferred += 1
                            assert await _metrics_listeners(client, token) == 3
                            seated, supplements = _room_listeners(app)
                            assert len(seated) == 3 and len(supplements) == 1
                            await seat.close()
                            closed = await supplement.recv()
                            assert closed["type"] == "websocket.close"
                            assert closed["code"] == 1000
                        finally:
                            await supplement.close()
                    finally:
                        await seat.close()
                    assert await _metrics_listeners(client, token) == 2
                    seated, supplements = _room_listeners(app)
                    assert len(seated) == 2 and supplements == []
                assert deferred >= 6
                async with Socket(
                    app,
                    "/ws/listen?room_id=class&cursor=0&cid=late",
                    client=("203.0.113.53", 1),
                ) as late:
                    late_hello = await late.recv()
                    assert late_hello["type"] == "hello"
                    assert late_hello.get("reason") != "full"
                    assert await _metrics_listeners(client, token) == 3
                    delivered = app.state.pipeline.on_event({
                        "type": "caption",
                        "id": "class:s:9",
                        "room_id": "class",
                        "session_id": "s",
                        "session_ord": 1,
                        "seq": 9,
                        "version": 1,
                        "zh": "只有座位",
                        "en": "seats only",
                        "status": "ready",
                    })
                    assert delivered is not None
                    seen = await late.recv()
                    assert seen["zh"] == "只有座位"
                    for anchor in (anchor_a, anchor_b):
                        row = await anchor.recv()
                        assert row["zh"] == "只有座位"
                seated, supplements = _room_listeners(app)
                assert len(seated) == 2 and supplements == []
                assert await _metrics_listeners(client, token) == 2
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_host_export_keeps_previous_class_across_restart(tmp_path):
    path = tmp_path / "captions.sqlite3"
    settings = Settings(allow_testclient=True, translate=False, data_path=str(path))
    first = app_for(settings=settings, translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await open_room(client, token, "class")
            pushed = await push(client, token, "class", "s", 1, "上一堂".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            await _close_room(client, token, "class")
            await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
    finally:
        await stop(first)

    resumed = app_for(
        settings=Settings(allow_testclient=True, translate=False, data_path=str(path)),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(resumed, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            hello = _hello(await drive(
                resumed,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(key, safe=""),
                with_key=False,
            ))
            assert "上一堂" not in json.dumps(hello, ensure_ascii=False)
            exported = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            assert exported.status_code == 200, exported.text
            rows = json.loads(exported.text)
            assert "上一堂" in _zh(rows)
            srt = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert "上一堂" in srt.text
            assert token not in exported.text
            assert token not in srt.text
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_listen_key_does_not_cross_rooms_and_unknown_rooms_stay_unknown():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            room_a = (await open_room(client, token, "rooma")).json()["listen_key"]
            room_b = (await open_room(client, token, "roomb")).json()["listen_key"]
            assert room_a != room_b
            await push(client, token, "rooma", "s", 1, "甲房".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "roomb", "s", 1, "乙房".encode(), t0_ms=0, t1_ms=1000)

            crossed = await drive(
                app,
                "/ws/listen?room_id=roomb&replay=1&k=" + urllib.parse.quote(room_a, safe=""),
                with_key=False,
            )
            _assert_rejected(crossed, 4401)
            assert "甲房" not in _sent_text(crossed) and "乙房" not in _sent_text(crossed)

            own = _hello(await drive(
                app,
                "/ws/listen?room_id=roomb&replay=1&k=" + urllib.parse.quote(room_b, safe=""),
                with_key=False,
            ))
            assert "乙房" in _zh(own.get("backfill"))
            assert "甲房" not in json.dumps(own, ensure_ascii=False)

            before = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
            unknown = await drive(app, "/ws/listen?room_id=nosuchroom", with_key=False)
            assert any(msg.get("type") == "websocket.accept" for msg in unknown)
            note = json.loads(next(msg["text"] for msg in unknown if msg.get("type") == "websocket.send"))
            assert note["type"] == "room_unavailable"
            assert note["reason"] == "unknown_or_ended"
            assert _close_code(unknown) == 4404
            after = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
            assert after == before
            assert "nosuchroom" not in app.state.rooms

            async with Socket(app, "/ws/listen?room_id=roomb") as sock:
                hello = await sock.recv()
                assert hello["type"] == "hello"
                await push(client, token, "rooma", "s", 2, "甲房第二句".encode(), t0_ms=1000, t1_ms=2000)
                leaked = False
                try:
                    msg = await sock.recv(timeout=0.4)
                except Exception:
                    msg = None
                if isinstance(msg, dict) and (msg.get("zh") or "") == "甲房第二句":
                    leaked = True
                assert leaked is False
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_public_setup_and_qr_omit_listen_key_until_the_host_asks(monkeypatch):
    captured = {}

    def fake_make(url):
        captured["url"] = url

        class Img:
            def save(self, buf, kind):
                buf.write(b"\x89PNG\r\n")

        return Img()

    import qrcode
    monkeypatch.setattr(qrcode, "make", fake_make)

    app = app_for(settings=Settings(allow_testclient=True, translate=False, share_host="203.0.113.10"), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            assert "?k=" in (opened.json()["listen_url"] or "")
            assert key in opened.json()["listen_url"]

            public = (await client.get("/api/setup", params={"room_id": "class"})).json()
            assert "listen_key" not in public
            assert public["host_token"] is None
            assert public["listen_url"] == "http://203.0.113.10:8780/r/class"
            assert key not in json.dumps(public)
            assert token not in json.dumps(public)

            qr = await client.get("/api/qr", params={"room_id": "class"})
            assert qr.status_code == 200
            assert captured["url"] == "http://203.0.113.10:8780/r/class"
            assert key not in qr.content.decode("latin1")
            assert token.encode() not in qr.content

            wrong = (await client.get(
                "/api/setup",
                params={"room_id": "class"},
                headers={"authorization": "Bearer nope", "origin": "http://127.0.0.1:8780"},
            )).json()
            assert "listen_key" not in wrong
            assert "k=" not in (wrong.get("listen_url") or "")

            evil = (await client.get(
                "/api/setup",
                params={"room_id": "class"},
                headers={"authorization": f"Bearer {token}", "origin": "http://evil.example"},
            )).json()
            assert "listen_key" not in evil
            assert key not in json.dumps(evil)

            host = (await client.get("/api/setup", params={"room_id": "class"}, headers=auth(token))).json()
            assert host["listen_key"] == key
            assert host["listen_url"] == "http://203.0.113.10:8780/r/class?k=" + urllib.parse.quote(key, safe="")
            assert token not in json.dumps(host)

            hosted_qr = await client.get("/api/qr", params={"room_id": "class"}, headers=auth(token))
            assert hosted_qr.status_code == 200
            assert captured["url"] == host["listen_url"]
            assert key in captured["url"]
            assert token not in captured["url"]
    finally:
        await stop(app)


def test_audience_page_and_host_page_pass_the_listen_key():
    room = (ROOT / "app/static/room.html").read_text(encoding="utf-8")
    host = (ROOT / "app/static/host.html").read_text(encoding="utf-8")
    client = (ROOT / "app/static/room_client.js").read_text(encoding="utf-8")
    checker = (ROOT / "scripts/device_check.py").read_text(encoding="utf-8")
    assert 'URLSearchParams(location.search).get("k")' in room
    assert '&k=" + encodeURIComponent(listenKey)' in room
    assert 'authorization: "Bearer " + token' in host
    assert "data.listen_key" in host
    assert '&k=" + encodeURIComponent(listenKey)' in host
    # room_client.js calls url() on every attempt, so a reconnect keeps the key.
    address = client[client.index("function address()"): client.index("function isCaption")]
    assert "url()" in address
    assert "不會改走匿名連線" in checker
    assert "&cursor=0&replay=1&k=" in checker
