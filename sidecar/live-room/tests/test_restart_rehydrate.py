import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.translate import TranslateResult, Translator
from tests.test_pipeline_repair import Socket, app_for, auth, push, settings_with, stop, token_of


class English(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline, cancel
        return TranslateResult("EN " + zh, "ok")


def _settings(path: Path, **extra):
    base = dict(allow_testclient=True, data_path=str(path), gap_wait_s=0.2, history_limit=2, translate_timeout_s=2)
    base.update(extra)
    return settings_with(**base)


@asynccontextmanager
async def _serving(app):
    """httpx ASGITransport does not run lifespan. Uvicorn does, and that is the replay path."""
    async with app.router.lifespan_context(app):
        yield


async def _push_lines(client, token, lines, session="s"):
    for seq, text in lines:
        resp = await push(client, token, "class", session, seq, text.encode(), t0_ms=(seq - 1) * 1000, t1_ms=seq * 1000)
        assert resp.status_code == 200, resp.text
        assert resp.json()["zh"] == text


@pytest.mark.anyio
async def test_restart_replays_store_into_history_with_versions(tmp_path):
    path = tmp_path / "captions.sqlite3"
    first = app_for(settings=_settings(path), translator=English())
    first_epoch = None
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await _push_lines(client, token, [(1, "甲"), (2, "乙"), (3, "丙")])
            await asyncio.to_thread(first.state.store.flush)
            first_epoch = first.state.bus.epoch("class")
            stored = {row["seq"]: row for row in first.state.store.room_rows("class")}
            assert stored[1]["version"] >= 2
    finally:
        await stop(first)

    resumed = app_for(settings=_settings(path), translator=English())
    assert resumed.state.bus.history("class") == []
    try:
        async with _serving(resumed):
            async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(resumed, client)
                state = resumed.state.bus.caption_state("class")
                assert [item["seq"] for item in state] == [1, 2, 3]
                assert len({item["id"] for item in state}) == 3
                assert all(item["version"] >= 2 for item in state)
                assert [item["zh"] for item in state] == ["甲", "乙", "丙"]
                assert resumed.state.bus.epoch("class") != first_epoch
                assert all(item.get("epoch") == resumed.state.bus.epoch("class") for item in state)
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_restart_does_not_resurrect_expired_seq_as_missing(tmp_path):
    """Early captions expire on their own TTL. Restart must not fill those holes as missing."""
    path = tmp_path / "captions.sqlite3"
    first = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await _push_lines(client, token, [(1, "甲"), (2, "乙"), (3, "丙")])
            await asyncio.to_thread(first.state.store.flush)
    finally:
        await stop(first)

    with sqlite3.connect(path) as conn:
        conn.execute("update captions set updated_at = ? where seq = 1", (time.time() - 90_000,))
        conn.commit()

    resumed = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with _serving(resumed):
            async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(resumed, client)
                del token
                await resumed.state.sweep_once()
                await asyncio.to_thread(resumed.state.store.flush)
                bus = {item["seq"]: item for item in resumed.state.bus.caption_state("class")}
                stored = {row["seq"]: row for row in resumed.state.store.room_rows("class")}
                history_seqs = {
                    item["seq"]
                    for item in resumed.state.bus.history("class")
                    if item.get("seq") is not None
                }
                assert 1 not in bus
                assert 1 not in stored
                assert 1 not in history_seqs
                assert [bus[seq]["zh"] for seq in (2, 3)] == ["乙", "丙"]
                assert bus[2]["status"] != "missing" and bus[3]["status"] != "missing"
                assert stored[2]["status"] != "missing" and stored[3]["status"] != "missing"
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_restart_continuing_session_has_no_fake_missing_gaps(tmp_path):
    path = tmp_path / "captions.sqlite3"
    first = app_for(settings=_settings(path, gap_wait_s=30), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await _push_lines(client, token, [(1, "甲"), (2, "乙"), (3, "丙")])
            await asyncio.to_thread(first.state.store.flush)
    finally:
        await stop(first)
    with sqlite3.connect(path) as conn:
        conn.execute("delete from captions where seq = 2")
        conn.execute("update captions set version = 1, status = 'zh_ready', en = ''")
        conn.commit()

    resumed = app_for(settings=_settings(path, gap_wait_s=0.3), translator=Translator(enabled=False))
    assert resumed.state.bus.history("class") == []
    try:
        async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(resumed, client)
            started = time.monotonic()
            resp = await push(client, token, "class", "s", 4, "丁".encode(), t0_ms=3000, t1_ms=4000)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "丁"
            assert time.monotonic() - started < 0.7
            await asyncio.sleep(0.8)
            rows = resumed.state.store.room_rows("class")
            by_seq = {}
            for row in rows:
                by_seq.setdefault(row["seq"], []).append(row)
            assert list(by_seq) == [1, 2, 3, 4] or set(by_seq) == {1, 2, 3, 4}
            for seq in (1, 2, 3, 4):
                assert len(by_seq[seq]) == 1
            assert by_seq[1][0]["zh"] == "甲" and by_seq[1][0]["status"] != "missing"
            assert by_seq[2][0]["status"] == "missing"
            assert by_seq[3][0]["zh"] == "丙" and by_seq[3][0]["status"] != "missing"
            assert by_seq[4][0]["zh"] == "丁" and by_seq[4][0]["status"] != "missing"
            live = {item["seq"]: item for item in resumed.state.bus.caption_state("class")}
            assert live[1]["zh"] == "甲" and live[1]["status"] != "missing"
            assert live[3]["zh"] == "丙" and live[3]["status"] != "missing"
            ordered = [item["seq"] for item in resumed.state.bus.caption_state("class")]
            assert ordered == [1, 2, 3, 4]
            assert len(ordered) == len(set(ordered))
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_restart_reconnect_old_cursor_backfills_no_dup_no_gap(tmp_path):
    path = tmp_path / "captions.sqlite3"
    first = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await _push_lines(client, token, [(1, "甲"), (2, "乙"), (3, "丙"), (4, "丁")])
            await asyncio.to_thread(first.state.store.flush)
    finally:
        await stop(first)

    resumed = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(resumed, client)
            opened = await client.post(
                "/api/rooms/open",
                json={"room_id": "class"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert opened.status_code == 200
            async with Socket(resumed, "/ws/listen?room_id=class&cursor=80") as ws:
                hello = await ws.recv()
            assert hello["type"] == "hello"
            assert hello["gap"] is True
            assert hello["events"] == []
            assert isinstance(hello["epoch"], int) and hello["epoch"] > 0
            backfill = hello["backfill"]
            assert [item["seq"] for item in backfill] == [1, 2, 3, 4]
            assert len({item["id"] for item in backfill}) == 4
            assert [item["zh"] for item in backfill] == ["甲", "乙", "丙", "丁"]
            assert len(resumed.state.bus.history("class")) <= 2
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_restart_update_after_restart_has_higher_version(tmp_path):
    path = tmp_path / "captions.sqlite3"
    first = app_for(settings=_settings(path), translator=English())
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            resp = await push(client, token, "class", "s", 1, "甲".encode())
            assert resp.status_code == 200
            stored_version = int(resp.json()["version"])
            assert stored_version >= 2
            await asyncio.to_thread(first.state.store.flush)
    finally:
        await stop(first)

    resumed = app_for(settings=_settings(path), translator=English())
    try:
        async with _serving(resumed):
            async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(resumed, client)
                before = resumed.state.bus.caption_state("class")[0]["version"]
                assert before == stored_version
                updated = await client.post(
                    "/api/segment/retranslate",
                    json={"room_id": "class", "session_id": "s", "seq": 1},
                    headers={**auth(token), "content-type": "application/json"},
                )
                assert updated.status_code == 200, updated.text
                body = updated.json()
                assert body["version"] > stored_version
                assert body["en"] == "EN 甲"
                versions = [
                    int(item["version"])
                    for item in resumed.state.bus._log.get("class", [])
                    if item.get("id") == "class:s:1"
                ]
                assert versions
                assert versions == sorted(versions)
                assert versions[-1] > stored_version
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_corrupt_store_is_quarantined_not_deleted(tmp_path):
    path = tmp_path / "captions.sqlite3"
    payload = b"this is not sqlite"
    path.write_bytes(payload)
    wal = Path(str(path) + "-wal")
    wal.write_bytes(b"wal-bytes")
    app = app_for(settings=_settings(path, gap_wait_s=3), translator=Translator(enabled=False))
    try:
        store = app.state.store
        assert store.recovered is True
        assert store.quarantine_path is not None
        assert store.quarantine_path.exists()
        assert store.quarantine_path.read_bytes() == payload
        quarantined_wal = Path(str(store.quarantine_path) + "-wal")
        assert quarantined_wal.read_bytes() == b"wal-bytes"
        # A fresh database may open its own WAL. That file must not still be the corrupt sidecar.
        if wal.exists():
            assert wal.read_bytes() != b"wal-bytes"
        assert path.exists()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            setup = (await client.get("/api/setup")).json()
            assert setup["storage_recovered"] is True
            assert setup["host_token"] is None
            metrics = await client.get("/api/metrics", headers=auth(token))
            assert metrics.status_code == 200
            assert metrics.json()["storage_recovered"] is True
            assert token not in metrics.text
    finally:
        await stop(app)
    assert path.exists()
    assert app.state.store.quarantine_path.exists()


def test_corrupt_store_connection_is_closed_before_quarantine(tmp_path, monkeypatch):
    """Windows refuses to rename a file with an open handle (WinError 32).

    The first PRAGMA on a non-SQLite file raises inside CaptionStore._connect.
    That connection must be closed before the file is moved aside, otherwise the
    quarantine rename fails on Windows. Linux allows the rename, so this checks
    the handle directly.
    """
    import sqlite3 as _sqlite3

    from app import store as store_mod

    opened = []
    real_connect = _sqlite3.connect

    class Tracked:
        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def close(self):
            self.closed = True
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def tracking_connect(*args, **kwargs):
        conn = Tracked(real_connect(*args, **kwargs))
        opened.append(conn)
        return conn

    path = tmp_path / "captions.sqlite3"
    path.write_bytes(b"this is not sqlite")
    monkeypatch.setattr(store_mod.sqlite3, "connect", tracking_connect)
    renamed_while_open = []
    real_quarantine = store_mod.CaptionStore._quarantine

    def checking_quarantine(self, p):
        renamed_while_open.extend(c for c in opened if not c.closed)
        return real_quarantine(self, p)

    monkeypatch.setattr(store_mod.CaptionStore, "_quarantine", checking_quarantine)
    store = store_mod.CaptionStore(path)
    try:
        assert store.recovered is True
        assert store.quarantine_path is not None and store.quarantine_path.exists()
        assert renamed_while_open == []
    finally:
        store.close()
