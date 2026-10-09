import asyncio
import json
import re
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.pipeline import Pipeline, Segment
from app.server import create_app
from app.settings import Settings
from app.store import CaptionStore
from app.textutil import export_text
from app.translate import TranslateResult, Translator

ALLOWED_STATUS = {
    "queued",
    "decoding",
    "transcribing",
    "zh_ready",
    "ready",
    "silent",
    "translate_failed",
    "missing",
    "error",
    "timeout",
    "cancelled",
}


class EchoAsr:
    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del prompt
        return AsrResult(ok=True, text=wav.read_bytes().decode())


def copy_decoder(src: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    wav.write_bytes(src.read_bytes())
    return wav


def settings_with(**kwargs):
    """Drop fields the original Settings dataclass does not have, so these tests run on it."""
    fields = getattr(Settings, "__dataclass_fields__", {})
    return Settings(**{key: value for key, value in kwargs.items() if key in fields})


def app_for(**kwargs):
    settings = kwargs.pop("settings", None) or Settings(allow_testclient=True)
    return create_app(
        settings,
        asr=kwargs.pop("asr", None) or EchoAsr(),
        translator=kwargs.pop("translator", None) or Translator(enabled=True, key=""),
        decoder=kwargs.pop("decoder", None) or copy_decoder,
    )


def auth(token):
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1"}


async def token_of(app, client):
    resp = await client.get("/api/host-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def push(client, token, room, session, seq, payload, *, wait_translation=None, async_header=False, t0_ms=None, t1_ms=None, retry=False):
    headers = auth(token)
    if async_header:
        headers["x-breeze-async-translation"] = "1"
    if retry:
        headers["x-breeze-retry"] = "1"
    data = {"room_id": room, "session_id": session, "seq": str(seq)}
    if wait_translation is not None:
        data["wait_translation"] = wait_translation
    if retry:
        data["retry"] = "1"
    if t0_ms is not None:
        data["t0_ms"] = str(t0_ms)
    if t1_ms is not None:
        data["t1_ms"] = str(t1_ms)
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data=data,
        files={"audio": ("a.webm", payload, "audio/webm")},
        headers=headers,
    )


async def stop(app):
    await app.state.shutdown()


def versions_of(app, room):
    found = {}
    for item in app.state.bus._log.get(room, []):
        if not item.get("id"):
            continue
        found.setdefault(item["id"], []).append(int(item["version"]))
    return found


def assert_versions_only_increase(app, room):
    for seg_id, versions in versions_of(app, room).items():
        assert versions == sorted(versions), (seg_id, versions)
        assert len(versions) == len(set(versions)), (seg_id, versions)


class HoldEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="test-key")
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.order = []
        self.threads = []

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            if self.inflight >= 2:
                self.started.set()
        self.release.wait(5)
        with self.lock:
            self.inflight -= 1
            self.order.append(zh)
        return TranslateResult("EN " + zh, "ok")


class RacingEnglish(Translator):
    """Blocks until two translations are inside translate(), then finishes the fast line first."""

    def __init__(self):
        super().__init__(enabled=True, key="test-key")
        self.barrier = threading.Barrier(2, timeout=5)
        self.lock = threading.Lock()
        self.max_inflight = 0
        self.inflight = 0
        self.order = []
        self.threads = []

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            return TranslateResult("", "error", "英譯失敗，中文仍保留")
        time.sleep(0.2 if "慢" in zh else 0.04)
        with self.lock:
            self.order.append(zh)
            self.inflight -= 1
        return TranslateResult("EN " + zh, "ok")


class SleepEnglish(Translator):
    def __init__(self, delay: float):
        super().__init__(enabled=True, key="test-key")
        self.delay = delay

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        time.sleep(self.delay)
        return TranslateResult("EN " + zh, "ok")


async def wait_for_english(app, room, count, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = [item for item in app.state.bus.history(room) if item.get("en")]
        if len(ready) >= count:
            return app.state.bus.history(room)
        await asyncio.sleep(0.02)
    return app.state.bus.history(room)


@pytest.mark.anyio
async def test_opt_in_push_returns_chinese_before_slow_english():
    translator = HoldEnglish()
    app = app_for(translator=translator)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def form_opt(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            async def header_opt(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    async_header=True,
                )

            try:
                form_resp, header_resp = await asyncio.wait_for(
                    asyncio.gather(form_opt(1, "般若"), header_opt(2, "空性")),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                raise AssertionError("opt-in push blocked until English finished")
            for resp, text in ((form_resp, "般若"), (header_resp, "空性")):
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["zh"] == text
                assert body["en"] == ""
                assert body["ok"] is True
                assert body["status"] == "zh_ready"
                assert body["status"] in ALLOWED_STATUS
            assert translator.release.is_set() is False
            translator.release.set()
            history = await wait_for_english(app, "class", 2)
        by_seq = {item["seq"]: item for item in history}
        assert by_seq[1]["en"] == "EN 般若"
        assert by_seq[1]["zh"] == "般若"
        assert by_seq[2]["en"] == "EN 空性"
        assert by_seq[2]["zh"] == "空性"
        assert all(item["status"] in ALLOWED_STATUS for item in history)
        assert_versions_only_increase(app, "class")
        assert by_seq[1]["version"] >= 2
        assert by_seq[2]["version"] >= 2
    finally:
        translator.release.set()
        await stop(app)


@pytest.mark.anyio
async def test_parallel_translations_finish_out_of_order_on_their_own_segments():
    translator = RacingEnglish()
    app = app_for(translator=translator, settings=settings_with(allow_testclient=True, translate_workers=2))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(one(1, "慢句"), one(2, "快句")),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                raise AssertionError("push waited on the single translation worker")
            assert [item.status_code for item in results] == [200, 200]
            assert all(item.json()["en"] == "" and item.json()["status"] == "zh_ready" for item in results)
            history = await wait_for_english(app, "class", 2, timeout=2.0)
        assert translator.max_inflight >= 2
        assert translator.order and "快" in translator.order[0]
        assert all(name.startswith("breeze-translate") for name in translator.threads)
        by_seq = {item["seq"]: item for item in history}
        assert by_seq[1]["zh"] == "慢句" and by_seq[1]["en"] == "EN 慢句"
        assert by_seq[2]["zh"] == "快句" and by_seq[2]["en"] == "EN 快句"
        assert_versions_only_increase(app, "class")
    finally:
        translator.barrier.abort()
        await stop(app)


@pytest.mark.anyio
async def test_async_pushes_return_while_translation_is_still_queued():
    translator = SleepEnglish(0.4)
    app = app_for(translator=translator)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq):
                return await push(
                    client, token, "class", "s", seq, f"第{seq}句".encode(),
                    wait_translation="0",
                )

            started = time.monotonic()
            try:
                results = await asyncio.wait_for(asyncio.gather(*(one(seq) for seq in range(1, 7))), timeout=1.2)
            except asyncio.TimeoutError:
                raise AssertionError("six pushes waited out serial translation")
            elapsed = time.monotonic() - started
            assert elapsed < 1.2
            assert all(item.status_code == 200 for item in results)
            for seq, item in enumerate(results, start=1):
                body = item.json()
                assert body["zh"] == f"第{seq}句"
                assert body["en"] == ""
                assert body["status"] == "zh_ready"
            history = await wait_for_english(app, "class", 6, timeout=3.0)
        by_seq = {item["seq"]: item for item in history}
        assert len(by_seq) == 6
        for seq in range(1, 7):
            assert by_seq[seq]["zh"] == f"第{seq}句"
            assert by_seq[seq]["en"] == f"EN 第{seq}句"
            assert by_seq[seq]["status"] == "ready"
        assert translator.calls == 6
        assert_versions_only_increase(app, "class")
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_queued_translation_past_the_timeout_is_skipped_without_dropping_chinese():
    translator = HoldEnglish()
    app = app_for(
        translator=translator,
        settings=settings_with(allow_testclient=True, translate_workers=1, translate_timeout_s=2.0, translate_queue=4),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            try:
                first, second, third = await asyncio.wait_for(
                    asyncio.gather(one(1, "先走"), one(2, "太舊"), one(3, "也太舊")),
                    timeout=2,
                )
            except asyncio.TimeoutError:
                raise AssertionError("push blocked on the single translation worker before a stale line could be skipped")
            assert first.json()["zh"] == "先走" and first.json()["en"] == ""
            for item in (first, second, third):
                assert item.status_code == 200
                assert item.json()["zh"]
                assert item.json()["status"] in ALLOWED_STATUS
            deadline = time.monotonic() + 2
            while translator.calls < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert translator.calls == 1
            queued = app.state.pipeline._translate_q._queue
            assert list(queued), "expected later segments to still be waiting on the single worker"
            aged = []
            for item in list(queued):
                epoch, _enqueued_at, segment = item
                aged.append((epoch, time.monotonic() - 5, segment))
            queued.clear()
            queued.extend(aged)
            translator.release.set()
            history = await wait_for_english(app, "class", 1, timeout=2)
            skipped = [item for item in history if item.get("translate_status") == "skipped"]
            assert len(skipped) >= 2
            assert {item["zh"] for item in skipped} >= {"太舊", "也太舊"}
            assert app.state.pipeline.translate_stale >= 2
            assert app.state.pipeline.translate_skipped == 0
            assert app.state.pipeline.translate_timeouts == 0
            for item in skipped:
                assert item["en"] == ""
                assert item["status"] == "translate_failed"
                assert item["status"] in ALLOWED_STATUS
            done = {item["seq"]: item for item in history}
            assert done[1]["zh"] == "先走" and done[1]["en"] == "EN 先走"
            assert translator.calls == 1
            assert_versions_only_increase(app, "class")
    finally:
        translator.release.set()
        await stop(app)


def test_translate_deadline_bounds_retry_sleep():
    slept = []

    def opener(req, timeout=40):
        del req
        raise TimeoutError()

    def sleeper(delay: float) -> None:
        slept.append(delay)
        time.sleep(delay)

    translator = Translator(
        enabled=True,
        key="k",
        max_attempts=3,
        max_backoff=30,
        sleeper=sleeper,
        opener=opener,
    )
    past = translator.translate("般若", deadline=time.monotonic() - 1)
    assert past.status == "timeout"
    assert past.detail == "英譯逾時，不假設沒有計費。中文仍保留"
    assert translator.calls == 0
    assert slept == []
    started = time.monotonic()
    bounded = translator.translate("般若", deadline=started + 0.06)
    assert bounded.status == "timeout"
    assert "不假設沒有計費" in bounded.detail
    # Uncapped retries would sleep 0.2s then 0.4s. The deadline must cut that off.
    assert time.monotonic() - started < 0.4
    assert sum(slept) < 0.15


def test_translate_workers_defaults_to_two_and_rejects_zero():
    assert Settings().translate_workers == 2
    assert Settings.from_env({}).translate_workers == 2
    assert Settings.from_env({"BREEZE_TRANSLATE_WORKERS": "4"}).translate_workers == 4
    with pytest.raises(ValueError, match="BREEZE_TRANSLATE_WORKERS"):
        Settings(translate_workers=0)


def test_host_page_opts_into_async_translation_and_listens_for_english():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    upload = text.split("upload: async", 1)[1].split("onPhase", 1)[0]
    assert 'wait_translation", "0"' in upload
    assert "x-breeze-async-translation" in upload
    assert "x-breeze-retry" in upload
    assert "connectRoom" in text
    assert "/ws/listen?room_id=" in text
    assert "mergeCaptionUpdate" in text
    assert "throw new Error" in upload


def test_host_retry_button_uses_host_fetch_for_skipped_english():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    assert "重試英譯" in text
    handler = text.split('querySelector("#retranslate").onclick', 1)[1].split('querySelector("#export")', 1)[0]
    assert "/api/segment/retranslate" in handler
    assert "hostFetch(" in handler
    assert "await fetch(" not in handler
    script = Path("app/static/host_caption.js").read_text(encoding="utf-8")
    assert "英譯積壓已略過，可重試" in script
    assert "英譯失敗，可重試" in script


class _BlockOneEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context, deadline, cancel
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        return TranslateResult("EN " + zh, "ok")


@pytest.mark.anyio
async def test_skipped_backlog_segment_can_be_retranslated():
    translator = _BlockOneEnglish()
    app = app_for(
        translator=translator,
        settings=settings_with(
            allow_testclient=True,
            translate_workers=1,
            translate_queue=1,
            translate_timeout_s=3,
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            headers = {**auth(token), "content-type": "application/json"}
            first = asyncio.create_task(push(
                client, token, "class", "s", 1, "新".encode(),
                wait_translation="0", async_header=True,
            ))
            assert await asyncio.to_thread(translator.started.wait, 2)
            pipe = app.state.pipeline
            extra = Segment(room_id="class", session_id="s", seq=9, zh="重", status="zh_ready", version=1)
            pipe._stamp_gen(extra)
            pipe.results[extra.key] = extra
            pipe._emitted_segs.add(extra.key)
            pipe._put_translation(extra)
            pipe._put_translation(extra)
            pipe._drop_queued(extra.key)
            second = await push(
                client, token, "class", "s", 2, "舊".encode(),
                wait_translation="0", async_header=True,
            )
            third = await push(
                client, token, "class", "s", 3, "最新".encode(),
                wait_translation="0", async_header=True,
            )
            assert second.status_code == 200 and third.status_code == 200, (second.text, third.text)
            await asyncio.sleep(0.05)
            skipped = [
                item for item in app.state.bus.caption_state("class")
                if item.get("translate_status") == "skipped_backlog"
            ]
            assert skipped and skipped[0]["zh"] == "舊"
            assert skipped[0]["status"] == "translate_failed"
            assert skipped[0]["en"] == ""
            translator.release.set()
            await asyncio.wait_for(first, 2)
            revived = await client.post(
                "/api/segment/retranslate",
                json={"room_id": "class", "session_id": "s", "seq": 2},
                headers=headers,
            )
            assert revived.status_code == 200, revived.text
            deadline = asyncio.get_running_loop().time() + 2
            latest = {}
            while asyncio.get_running_loop().time() < deadline:
                latest = {item["seq"]: item for item in app.state.bus.caption_state("class")}
                row = latest.get(2) or {}
                if row.get("status") == "ready" and row.get("en"):
                    break
                await asyncio.sleep(0.02)
            row = latest.get(2) or {}
            assert row.get("status") == "ready", row
            assert row.get("en") == "EN 舊"
            assert row.get("translate_status") != "skipped_backlog"
    finally:
        translator.release.set()
        await stop(app)


def test_host_upload_failure_throws_and_delete_control_is_present():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    upload = text.split("upload: async", 1)[1].split("onPhase", 1)[0]
    assert "throw new Error" in upload
    assert "刪除此段" in text
    assert "onDelete" in text
    delete_handler = text.split('querySelector("#delete-seg")', 1)[1].split('querySelector("#export")', 1)[0]
    assert "hostFetch(" in delete_handler
    assert "await fetch(" not in delete_handler
    assert "captions_cleared" in text
    room = Path("app/static/room.html").read_text(encoding="utf-8")
    assert "onDelete" in room
    assert "createCaptionView" in room
    client = Path("app/static/room_client.js").read_text(encoding="utf-8")
    assert "caption_deleted" in client
    assert "latest < cursor" in client


_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def srt_cues(payload: str) -> list[dict]:
    text = payload.replace("\r\n", "\n").replace("\r", "\n").strip()
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        time_line = lines[1] if lines[0].strip().isdigit() else lines[0]
        index = int(lines[0]) if lines[0].strip().isdigit() else None
        match = _SRT_TIME.search(time_line)
        if not match:
            cues.append({"index": index, "ok": False, "text": "\n".join(lines)})
            continue
        parts = [int(item) for item in match.groups()]
        start = ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * 1000 + parts[3]
        end = ((parts[4] * 60 + parts[5]) * 60 + parts[6]) * 1000 + parts[7]
        body_from = 2 if lines[0].strip().isdigit() else 1
        cues.append({
            "index": index,
            "ok": True,
            "start": start,
            "end": end,
            "stamp": time_line.strip(),
            "text": "\n".join(lines[body_from:]).strip(),
        })
    return cues


def assert_monotonic_cues(cues: list[dict]) -> None:
    assert cues, "no cues"
    assert all(cue["ok"] for cue in cues)
    assert [cue["index"] for cue in cues] == list(range(1, len(cues) + 1))
    for cue in cues:
        assert cue["end"] > cue["start"], cue
    for prev, nxt in zip(cues, cues[1:]):
        assert prev["start"] <= nxt["start"], (prev, nxt)
        assert prev["end"] <= nxt["start"], (prev["end"], nxt["start"])


class CountingAsr(EchoAsr):
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        self.calls += 1
        return super().transcribe(wav, prompt)


class GateEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="test-key")
        self.started = threading.Event()
        self.release = threading.Event()

    def translate(self, zh: str, glossary=None, context=None, deadline: float | None = None) -> TranslateResult:
        del glossary, context, deadline
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return TranslateResult("EN " + zh, "ok")


class Socket:
    def __init__(self, app, path: str, client=("127.0.0.1", 5000), headers=None, with_key: bool = True):
        self.app = app
        self.path = path
        self.client = client
        self.headers = headers
        self.with_key = with_key
        self.out: asyncio.Queue = asyncio.Queue()
        self.inc: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        from tests.test_round2 import append_listen_key

        path = append_listen_key(self.app, self.path) if self.with_key else self.path
        path, _, query = path.partition("?")
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": self.headers if self.headers is not None else [(b"host", b"127.0.0.1:8780")],
            "client": self.client,
            "server": ("127.0.0.1", 8780),
            "subprotocols": [],
        }

        async def receive():
            return await self.inc.get()

        async def send(message):
            await self.out.put(message)

        self.task = asyncio.create_task(self.app(scope, receive, send))
        await self.inc.put({"type": "websocket.connect"})
        first = await asyncio.wait_for(self.out.get(), 2)
        assert first["type"] == "websocket.accept", first
        return self

    async def recv(self, timeout: float = 2):
        msg = await asyncio.wait_for(self.out.get(), timeout)
        if msg["type"] == "websocket.send":
            return json.loads(msg["text"])
        return {"type": msg["type"], "code": msg.get("code")}

    async def close(self):
        await self.inc.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 1)
            except Exception:
                self.task.cancel()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False


async def export_of(client, token, room, kind):
    resp = await client.get("/api/export", params={"room_id": room, "kind": kind}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp


async def end_session(client, token, room, session):
    resp = await client.post(
        "/api/session/end",
        json={"room_id": room, "session_id": session},
        headers={**auth(token), "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text


def test_srt_cues_monotonic_and_non_overlapping():
    srt = export_text(
        [
            {"zh": "一", "status": "ready", "session_id": "a", "session_ord": 1, "seq": 1, "t0_ms": 0, "t1_ms": 5000},
            {"zh": "二", "status": "ready", "session_id": "a", "session_ord": 1, "seq": 2, "t0_ms": 3000, "t1_ms": 8000},
            {"zh": "三", "status": "ready", "session_id": "b", "session_ord": 2, "seq": 1, "t0_ms": 0, "t1_ms": 2000},
            {"zh": "負", "status": "ready", "session_id": "b", "session_ord": 2, "seq": 2, "t0_ms": -400, "t1_ms": -50},
            {"zh": "倒", "status": "ready", "session_id": "b", "session_ord": 2, "seq": 3, "t0_ms": 1000, "t1_ms": 100},
        ],
        "srt",
    )
    cues = srt_cues(srt)
    assert_monotonic_cues(cues)
    assert cues[0]["text"].startswith("一")
    assert cues[2]["start"] >= cues[1]["end"]
    assert cues[2]["start"] != 0
    vtt = export_text([{"zh": "一", "status": "ready", "session_id": "a", "seq": 1, "t0_ms": 0, "t1_ms": 1500}], "vtt")
    assert "00:00:00.000 --> 00:00:01.500" in vtt


@pytest.mark.anyio
async def test_srt_second_session_does_not_restart_at_zero():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await push(client, token, "class", "s-old", 1, "上一場".encode(), t0_ms=0, t1_ms=6000, async_header=True, wait_translation="0")
            assert first.status_code == 200, first.text
            second = await push(client, token, "class", "s-new", 1, "下一場".encode(), t0_ms=0, t1_ms=4000, async_header=True, wait_translation="0")
            assert second.status_code == 200, second.text
            await end_session(client, token, "class", "s-old")
            srt = await export_of(client, token, "class", "srt")
            cues = srt_cues(srt.text)
            assert_monotonic_cues(cues)
            assert len(cues) == 2
            assert cues[0]["start"] == 0
            assert cues[1]["start"] >= cues[0]["end"]
            assert cues[1]["start"] >= 6000
            assert "下一場" in cues[1]["text"]
            exported = await export_of(client, token, "class", "json")
            rows = exported.json()
            assert [row["session_id"] for row in rows] == ["s-old", "s-new"]
            assert rows[1]["t0_ms"] >= rows[0]["t1_ms"]
            assert {"zh", "en", "status", "seq", "session_id", "t0_ms", "t1_ms"} <= set(rows[0])
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_export_without_storage_keeps_full_session():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, history_limit=2))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            for seq in range(1, 31):
                resp = await push(
                    client, token, "class", "s", seq, f"段{seq}".encode(),
                    t0_ms=(seq - 1) * 6000, t1_ms=seq * 6000, async_header=True, wait_translation="0",
                )
                assert resp.status_code == 200, resp.text
            assert len(app.state.bus.history("class")) <= 2
            exported = await export_of(client, token, "class", "json")
            rows = exported.json()
            assert [row["seq"] for row in rows] == list(range(1, 31))
            assert rows[0]["zh"] == "段1"
            srt = await export_of(client, token, "class", "srt")
            cues = srt_cues(srt.text)
            assert len(cues) == 30
            assert "段1" in srt.text and "段30" in srt.text
    finally:
        await stop(app)


async def _push_half(app, client, token, room, session, count, text):
    marked = None
    for seq in range(1, count + 1):
        resp = await push(
            client, token, room, session, seq, text(session, seq).encode(),
            t0_ms=(seq - 1) * 6000, t1_ms=seq * 6000, async_header=True, wait_translation="0",
        )
        assert resp.status_code == 200, resp.text[:300]
        if seq == 50:
            marked = app.state.bus.latest_cursor(room)
    return marked


@pytest.mark.anyio
async def test_srt_100_minute_session_formats_hours(tmp_path):
    """~1000 segments, two takes, storage off and on. Also backfill, retry, and memory."""
    half = 500

    async def run(store_path: str | None, track_memory: bool) -> None:
        asr = CountingAsr()
        settings = settings_with(
            allow_testclient=True,
            translate=False,
            history_limit=200,
            max_results=500,
            data_path=store_path or "",
            room_caption_cap=5000,
        )
        app = app_for(settings=settings, asr=asr)
        started_trace = False
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
                token = await token_of(app, client)
                before = 0
                if track_memory and not tracemalloc.is_tracing():
                    tracemalloc.start()
                    started_trace = True
                if track_memory:
                    before = tracemalloc.get_traced_memory()[0]
                cursor_at_minute_5 = await _push_half(app, client, token, "class", "s1", half, lambda session, seq: f"{session}-{seq}")
                await _push_half(app, client, token, "class", "s2", half, lambda session, seq: f"{session}-{seq}")
                if track_memory:
                    after, peak = tracemalloc.get_traced_memory()
                    delta = max(0, after - before)
                    report = (
                        f"delta={delta}\npeak={peak}\nindex={len(app.state.pipeline._index)}\n"
                        f"results={len(app.state.pipeline.results)}\n"
                        f"state={len(app.state.bus.caption_state('class'))}\n"
                    )
                    # tmp_path, not /tmp: Windows has no /tmp and Path("/tmp/...") becomes \tmp\...
                    destination = tmp_path / "breeze_mem_1000.txt"
                    destination.write_text(report, encoding="utf-8")
                    print(report, end="")
                    assert delta < 80_000_000, delta
                assert len(app.state.bus.history("class")) <= 200
                state = app.state.bus.caption_state("class")
                assert len(state) == half * 2
                srt = await export_of(client, token, "class", "srt")
                cues = srt_cues(srt.text)
                assert len(cues) == half * 2
                assert_monotonic_cues(cues)
                assert any(cue["start"] >= 3_600_000 for cue in cues), cues[-1]
                assert re.search(r"01:\d{2}:\d{2},\d{3}", srt.text)
                assert "s1-1" in srt.text and "s2-1" in srt.text
                assert sum(1 for cue in cues if cue["start"] == 0) == 1
                exported = await export_of(client, token, "class", "json")
                rows = exported.json()
                assert len(rows) == half * 2
                assert rows[0]["zh"] == "s1-1" and rows[0]["seq"] == 1
                assert {"zh", "en", "status", "seq", "session_id", "t0_ms", "t1_ms"} <= set(rows[0])
                assert rows[half]["session_id"] == "s2"
                assert rows[half]["t0_ms"] >= 3_600_000 or rows[half]["t0_ms"] >= rows[half - 1]["t1_ms"]
                assert rows[half]["t0_ms"] >= rows[half - 1]["t1_ms"]
                if track_memory:
                    async with Socket(app, f"/ws/listen?room_id=class&cursor={cursor_at_minute_5}") as sock:
                        hello = await sock.recv()
                    assert hello["gap"] is True
                    assert "backfill" in hello
                    backfill = hello["backfill"]
                    # The class is still 1000 rows (export and caption_state above).
                    # An audience replay is only the last 200, under the byte budget.
                    full = (
                        [("s1", seq) for seq in range(1, half + 1)]
                        + [("s2", seq) for seq in range(1, half + 1)]
                    )
                    assert len(full) == half * 2
                    assert len(backfill) == 200
                    assert len({item["id"] for item in backfill}) == 200
                    assert [(item["session_id"], item["seq"]) for item in backfill] == full[-200:]
                    assert (backfill[0]["session_id"], backfill[0]["seq"]) != ("s1", 1)
                    encoded = json.dumps(backfill, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    assert len(encoded) <= 100 * 1024
                    calls = asr.calls
                    again = await push(
                        client, token, "class", "s1", 3, "s1-3".encode(),
                        t0_ms=12000, t1_ms=18000, retry=True, async_header=True, wait_translation="0",
                    )
                    assert again.status_code == 200, again.text
                    assert again.json()["zh"] == "s1-3"
                    assert asr.calls == calls
        finally:
            if started_trace:
                tracemalloc.stop()
            await stop(app)

    await run(None, True)
    await run(str(tmp_path / "captions.sqlite3"), False)


@pytest.mark.anyio
async def test_room_idle_31_min_keeps_captions_exportable():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, room_idle_s=1800))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            for seq in range(1, 5):
                resp = await push(
                    client, token, "class", "s", seq, f"休息{seq}".encode(),
                    t0_ms=(seq - 1) * 6000, t1_ms=seq * 6000, async_header=True, wait_translation="0",
                )
                assert resp.status_code == 200, resp.text
            room = app.state.room_book.rooms["class"]
            room["session_active"] = False
            room["listeners"].clear()
            room["last_active"] = time.monotonic() - (31 * 60)
            await app.state.sweep_once()
            assert "class" not in app.state.room_book.rooms
            srt = await export_of(client, token, "class", "srt")
            cues = srt_cues(srt.text)
            assert len(cues) == 4
            assert "休息1" in srt.text and "休息4" in srt.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_room_delete_not_resurrected_by_late_translation():
    gate = GateEnglish()
    app = app_for(translator=gate, settings=settings_with(allow_testclient=True, data_path=""))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            pushed = await push(client, token, "class", "s", 1, "般若".encode(), async_header=True, wait_translation="0")
            assert pushed.status_code == 200, pushed.text
            deadline = time.monotonic() + 2
            while not gate.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert gate.started.is_set()
            deleted = await client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
            assert deleted.status_code == 200, deleted.text
            gate.release.set()
            await asyncio.sleep(0.3)
            state = app.state.bus.caption_state("class")
            assert all("般若" not in (item.get("zh") or "") and "EN" not in (item.get("en") or "") for item in state)
            exported = await export_of(client, token, "class", "txt")
            assert "般若" not in exported.text
            nxt = await push(client, token, "class", "s", 2, "下一句".encode(), async_header=True, wait_translation="0", t0_ms=6000, t1_ms=12000)
            assert nxt.status_code == 200, nxt.text
            assert nxt.json()["zh"] == "下一句"
            again = await push(client, token, "class", "s", 1, "般若".encode(), retry=True)
            assert again.status_code == 409
            exported = await export_of(client, token, "class", "txt")
            assert "般若" not in exported.text
            assert "下一句" in exported.text
    finally:
        gate.release.set()
        await stop(app)


@pytest.mark.anyio
async def test_room_delete_not_resurrected_by_retranslate():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            pushed = await push(client, token, "class", "s", 1, "般若".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            deleted = await client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
            assert deleted.status_code == 200, deleted.text
            again = await client.post(
                "/api/segment/retranslate",
                json={"room_id": "class", "session_id": "s", "seq": 1, "zh": "般若"},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert again.status_code == 404
            exported = await export_of(client, token, "class", "txt")
            assert "般若" not in exported.text
            assert app.state.bus.caption_state("class") == []
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_single_caption_delete_removes_from_store_bus_export_and_notifies_listener(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await client.post("/api/rooms/open", json={"room_id": "class"}, headers={**auth(token), "content-type": "application/json"})
            first = await push(client, token, "class", "s", 1, "留下".encode(), t0_ms=0, t1_ms=1000)
            second = await push(client, token, "class", "s", 2, "刪掉".encode(), t0_ms=1000, t1_ms=2000)
            assert first.status_code == 200 and second.status_code == 200
            async with Socket(app, "/ws/listen?room_id=class&cursor=0") as sock:
                hello = await sock.recv()
                assert hello["type"] == "hello"
                deleted = await client.delete(
                    "/api/captions",
                    params={"room_id": "class", "id": "class:s:2"},
                    headers=auth(token),
                )
                assert deleted.status_code == 200, deleted.text
                assert deleted.json()["id"] == "class:s:2"
                notice = await sock.recv()
            assert notice["type"] == "caption_deleted"
            assert notice["id"] == "class:s:2"
            assert notice["room_id"] == "class"
            assert all(item.get("id") != "class:s:2" for item in app.state.bus.history("class"))
            assert all(item.get("id") != "class:s:2" for item in app.state.bus.caption_state("class"))
            app.state.store.flush()
            assert all(row["id"] != "class:s:2" for row in app.state.store.room_rows("class"))
            exported = await export_of(client, token, "class", "txt")
            assert "刪掉" not in exported.text
            assert "留下" in exported.text
            retry = await push(client, token, "class", "s", 2, "刪掉".encode(), retry=True, t0_ms=1000, t1_ms=2000)
            assert retry.status_code == 409
            exported = await export_of(client, token, "class", "txt")
            assert "刪掉" not in exported.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_delete_is_room_isolated(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await push(client, token, "room-a", "s", 1, "甲一".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "room-a", "s", 2, "甲二".encode(), t0_ms=1000, t1_ms=2000)
            await push(client, token, "room-b", "s", 1, "乙一".encode(), t0_ms=0, t1_ms=1000)
            deleted = await client.delete(
                "/api/captions",
                params={"room_id": "room-a", "session_id": "s", "seq": 1},
                headers=auth(token),
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["id"] == "room-a:s:1"
            kept_a = await export_of(client, token, "room-a", "txt")
            kept_b = await export_of(client, token, "room-b", "txt")
            assert "甲一" not in kept_a.text and "甲二" in kept_a.text
            assert "乙一" in kept_b.text
            wiped = await client.delete("/api/captions", params={"room_id": "room-a"}, headers=auth(token))
            assert wiped.status_code == 200
            assert "甲" not in (await export_of(client, token, "room-a", "txt")).text
            assert "乙一" in (await export_of(client, token, "room-b", "txt")).text
            assert any(item.get("zh") == "乙一" for item in app.state.bus.caption_state("room-b"))
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_delete_requires_host_token():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await push(client, token, "class", "s", 1, "留下".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "class", "s", 2, "也留".encode(), t0_ms=1000, t1_ms=2000)
            anonymous = await client.delete("/api/captions", params={"room_id": "class", "id": "class:s:1"})
            assert anonymous.status_code == 401
            bad = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:1"},
                headers={"authorization": "Bearer nope", "origin": "http://127.0.0.1"},
            )
            assert bad.status_code == 401
            authed = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:2"},
                headers=auth(token),
            )
            assert authed.status_code == 200, authed.text
            assert authed.json()["id"] == "class:s:2"
            exported = await export_of(client, token, "class", "txt")
            assert "留下" in exported.text
            assert "也留" not in exported.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_reconnect_after_delete_old_cursor_not_blank():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await push(client, token, "class", "s", 1, "舊字幕".encode(), t0_ms=0, t1_ms=1000)
            held = app.state.bus.latest_cursor("class")
            assert held >= 1
            deleted = await client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
            assert deleted.status_code == 200, deleted.text
            async with Socket(app, f"/ws/listen?room_id=class&cursor={held}") as sock:
                hello = await sock.recv()
            assert hello["type"] == "hello"
            assert hello["latest_cursor"] >= held
            assert any(item.get("type") == "captions_cleared" for item in hello["events"])
            assert hello.get("epoch", 1) >= 1
            assert "舊字幕" not in json.dumps(hello.get("history") or [])
            nxt = await push(client, token, "class", "s", 2, "新字幕".encode(), t0_ms=6000, t1_ms=9000, async_header=True, wait_translation="0")
            assert nxt.status_code == 200, nxt.text
            assert "新字幕" in (await export_of(client, token, "class", "txt")).text
            assert "舊字幕" not in (await export_of(client, token, "class", "txt")).text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_listener_error_does_not_stall_later_segments():
    app = app_for(settings=settings_with(allow_testclient=True, translate=False))
    try:
        original = app.state.pipeline.on_event

        def boom(event):
            if int(event.get("seq") or 0) == 1 and int(event.get("version") or 1) == 1:
                raise RuntimeError("listener failed")
            return original(event)

        app.state.pipeline.on_event = boom
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await push(client, token, "class", "s", 1, "第一".encode(), async_header=True, wait_translation="0")
            second = await push(client, token, "class", "s", 2, "第二".encode(), async_header=True, wait_translation="0")
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            rows = app.state.bus.caption_state("class")
            assert any(item.get("zh") == "第二" for item in rows)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_emitted_segment_index_respects_caption_cap():
    """_emitted_segs used to grow for the life of the process. It now drops with the caption index."""
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, room_caption_cap=3))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            for seq in range(1, 6):
                resp = await push(
                    client, token, "class", "s", seq, f"第{seq}句".encode(),
                    async_header=True, wait_translation="0", t0_ms=(seq - 1) * 1000, t1_ms=seq * 1000,
                )
                assert resp.status_code == 200, resp.text
            pipe = app.state.pipeline
            assert len(pipe._index) <= 3
            assert len(pipe._emitted_segs) <= 3
            kept = {key[2] for key in pipe._emitted_segs}
            assert kept <= {3, 4, 5}
            assert 1 not in kept
    finally:
        await stop(app)


def test_tr_epoch_is_bounded_by_room_caption_cap(tmp_path):
    """Translation epochs follow the caption cap and are not reused after a drop.

    A long class used to keep one epoch per segment for the life of the process.
    Trimming the caption index drops epochs that are no longer live. Another
    room is left alone. A dropped epoch number cannot match an in-flight worker.
    """
    pipe = Pipeline(
        asr=None,
        translator=Translator(enabled=False),
        prompt="",
        tmp=tmp_path,
        settings=Settings(room_caption_cap=3),
    )
    for seq in range(1, 8):
        key = ("class", "s", seq)
        pipe._index[key] = {"session_ord": seq, "version": 1}
        pipe._next_epoch(key)
        pipe._trim_index("class")
    class_epochs = [key for key in pipe._tr_epoch if key[0] == "class"]
    assert len(class_epochs) <= 3
    assert {key[2] for key in class_epochs} <= {5, 6, 7}
    other = ("room-b", "s", 1)
    pipe._index[other] = {"session_ord": 1, "version": 1}
    held_other = pipe._next_epoch(other)
    newer = ("class", "s", 8)
    pipe._index[newer] = {"session_ord": 8, "version": 1}
    pipe._next_epoch(newer)
    pipe._trim_index("class")
    assert pipe._tr_epoch.get(other) == held_other
    class_epochs = [key for key in pipe._tr_epoch if key[0] == "class"]
    assert len(class_epochs) <= 3
    assert newer in pipe._tr_epoch
    segment = Segment(room_id="class", session_id="s", seq=1)
    held = pipe._next_epoch(segment.key)
    pipe._drop_epoch(segment.key)
    assert pipe._epoch_current(segment, held) is False
    recycled = pipe._next_epoch(segment.key)
    assert recycled != held
    assert pipe._epoch_current(segment, held) is False
    assert pipe._epoch_current(segment, recycled) is True


@pytest.mark.anyio
async def test_delete_unknown_caption_does_not_seal_the_seq(tmp_path):
    """Deleting an id that was never uploaded must 404 and leave that seq usable."""
    app = app_for(settings=settings_with(
        allow_testclient=True,
        translate=False,
        data_path=str(tmp_path / "captions.sqlite3"),
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            missing = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:4"},
                headers=auth(token),
            )
            assert missing.status_code == 404, missing.text
            assert "class:s:4" not in app.state.pipeline._sealed.get("class", ())
            pushed = await push(client, token, "class", "s", 4, "後來才到".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            exported = await export_of(client, token, "class", "txt")
            assert "後來才到" in exported.text
            deleted = await client.delete(
                "/api/captions",
                params={"room_id": "class", "session_id": "s", "seq": 4},
                headers=auth(token),
            )
            assert deleted.status_code == 200, deleted.text
            again = await push(client, token, "class", "s", 4, "後來才到".encode(), t0_ms=0, t1_ms=1000, retry=True)
            assert again.status_code == 409
            exported = await export_of(client, token, "class", "txt")
            assert "後來才到" not in exported.text
            second = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:4"},
                headers=auth(token),
            )
            assert second.status_code == 404
            assert "class:s:4" in app.state.pipeline._sealed.get("class", ())
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_reopen_after_close_or_idle_keeps_session_order(tmp_path):
    """Closing or idle-reclaiming a room must not hand the next session ordinal 1."""
    app = app_for(settings=settings_with(
        allow_testclient=True,
        translate=False,
        room_idle_s=30,
        data_path=str(tmp_path / "order.sqlite3"),
    ))

    async def sessions(client, token, room, first, second):
        headers = {**auth(token), "content-type": "application/json"}
        opened = await client.post("/api/rooms/open", json={"room_id": room}, headers=headers)
        assert opened.status_code == 200, opened.text
        for session, text, t0 in ((first, "甲", 0), (second, "乙", 0)):
            resp = await push(
                client, token, room, session, 1, text.encode(),
                t0_ms=t0, t1_ms=t0 + 1000, async_header=True, wait_translation="0",
            )
            assert resp.status_code == 200, resp.text
        return headers

    def assert_order(rows, room_label):
        ordered = [(row["session_id"], row["zh"], int(row["session_ord"])) for row in rows]
        assert [item[0] for item in ordered] == ["a", "b", "c"], (room_label, ordered)
        assert ordered[2][2] > ordered[1][2] > ordered[0][2], (room_label, ordered)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            headers = await sessions(client, token, "closed", "a", "b")
            closed = await client.post("/api/rooms/close", json={"room_id": "closed"}, headers=headers)
            assert closed.status_code == 200, closed.text
            again = await push(
                client, token, "closed", "c", 1, "丙".encode(),
                t0_ms=0, t1_ms=1000, async_header=True, wait_translation="0",
            )
            assert again.status_code == 200, again.text
            exported = await export_of(client, token, "closed", "json")
            assert_order(exported.json(), "close-json")
            srt = await export_of(client, token, "closed", "srt")
            cues = srt_cues(srt.text)
            assert_monotonic_cues(cues)
            assert [cue["text"].split("\n", 1)[0] for cue in cues] == ["甲", "乙", "丙"]

            await sessions(client, token, "idle", "a", "b")
            room = app.state.room_book.rooms["idle"]
            room["session_active"] = False
            room["last_active"] = time.monotonic() - 60
            await app.state.sweep_once()
            assert app.state.room_book.get("idle") is None
            nxt = await push(
                client, token, "idle", "c", 1, "丙".encode(),
                t0_ms=0, t1_ms=1000, async_header=True, wait_translation="0",
            )
            assert nxt.status_code == 200, nxt.text
            idle_json = await export_of(client, token, "idle", "json")
            assert_order(idle_json.json(), "idle-json")
            idle_srt = await export_of(client, token, "idle", "srt")
            idle_cues = srt_cues(idle_srt.text)
            assert_monotonic_cues(idle_cues)
            assert [cue["text"].split("\n", 1)[0] for cue in idle_cues] == ["甲", "乙", "丙"]
    finally:
        await stop(app)


class _DelayAsr(EchoAsr):
    def __init__(self, delay: float):
        self.delay = delay

    def transcribe(self, wav, prompt: str):
        time.sleep(self.delay)
        return super().transcribe(wav, prompt)


@pytest.mark.anyio
async def test_stop_flush_admits_chunk_inside_window_and_last_seq_returns_early():
    """A chunk still in transit after /api/session/end is exported, not 409 or missing.

    Without last_seq an idle session that has not seen a seq stays open for the
    flush window. With last_seq the call stays open until that seq settles and
    then returns, instead of sitting out the rest of the window.
    """
    app = app_for(
        asr=_DelayAsr(0.05),
        settings=settings_with(
            allow_testclient=True,
            translate=False,
            gap_wait_s=30,
            stop_flush_s=1.2,
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            headers = {**auth(token), "content-type": "application/json"}
            ending = asyncio.create_task(client.post(
                "/api/session/end",
                json={"room_id": "class", "session_id": "live"},
                headers=headers,
            ))
            await asyncio.sleep(0.35)
            assert not ending.done(), "end returned before the flush window; an in-transit chunk would be 409"
            late = await push(client, token, "class", "live", 1, "路上".encode(), t0_ms=0, t1_ms=400)
            assert late.status_code == 200, late.text
            assert late.json()["zh"] == "路上"
            assert late.json()["status"] != "missing"
            done = await asyncio.wait_for(ending, 2)
            assert done.status_code == 200, done.text
            exported = await export_of(client, token, "class", "json")
            rows = {row["seq"]: row for row in exported.json()}
            assert rows[1]["zh"] == "路上"
            assert rows[1]["status"] != "missing"
            refused = await push(client, token, "class", "live", 2, "太晚".encode())
            assert refused.status_code == 409
            assert refused.json()["detail"] == "這個會話已結束"

            prior = await push(client, token, "class", "held", 1, "已到".encode(), t0_ms=0, t1_ms=400)
            assert prior.status_code == 200, prior.text
            held_end = asyncio.create_task(client.post(
                "/api/session/end",
                json={"room_id": "class", "session_id": "held"},
                headers=headers,
            ))
            await asyncio.sleep(0.35)
            assert not held_end.done(), "a landed session must stay open for the flush window"
            held_late = await push(client, token, "class", "held", 2, "後到".encode(), t0_ms=400, t1_ms=800)
            assert held_late.status_code == 200, held_late.text
            assert held_late.json()["zh"] == "後到"
            assert held_late.json()["status"] != "missing"
            held_done = await asyncio.wait_for(held_end, 2)
            assert held_done.status_code == 200, held_done.text
            held_export = await export_of(client, token, "class", "json")
            held_rows = {(row["session_id"], row["seq"]): row for row in held_export.json()}
            assert held_rows[("held", 2)]["zh"] == "後到"
            assert held_rows[("held", 2)]["status"] != "missing"

            started = time.monotonic()
            named = asyncio.create_task(client.post(
                "/api/session/end",
                json={"room_id": "class", "session_id": "named", "last_seq": 1},
                headers=headers,
            ))
            await asyncio.sleep(0.25)
            assert not named.done()
            arrived = await push(client, token, "class", "named", 1, "尾段".encode(), t0_ms=0, t1_ms=500)
            assert arrived.status_code == 200, arrived.text
            assert arrived.json()["zh"] == "尾段"
            finished = await asyncio.wait_for(named, 2)
            elapsed = time.monotonic() - started
            assert finished.status_code == 200, finished.text
            assert elapsed < 0.9, elapsed
            named_rows = await export_of(client, token, "class", "json")
            body = named_rows.json()
            assert any(row["session_id"] == "named" and row["seq"] == 1 and row["zh"] == "尾段" for row in body)
            after = await push(client, token, "class", "named", 2, "不收".encode())
            assert after.status_code == 409
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_store_delete_failure_keeps_captions_and_reports_failure(tmp_path):
    """A raised store delete must not claim success or drop captions that will replay."""
    import sqlite3

    path = tmp_path / "captions.sqlite3"
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(path)))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            headers = auth(token)
            first = await push(client, token, "class", "s", 1, "留下".encode(), async_header=True, wait_translation="0")
            second = await push(client, token, "class", "s", 2, "可刪".encode(), async_header=True, wait_translation="0")
            assert first.status_code == 200 and second.status_code == 200
            app.state.store.flush()
            original_id = app.state.store._delete_id_now
            original_room = app.state.store._delete_room_now

            def boom(*_args, **_kwargs):
                raise sqlite3.OperationalError("disk full")

            app.state.store._delete_id_now = boom
            app.state.store._delete_room_now = boom
            denied = await client.delete(
                "/api/captions",
                params={"room_id": "class", "id": "class:s:1"},
                headers=headers,
            )
            assert denied.status_code == 503, denied.text
            body = denied.json()
            assert body["ok"] is False
            assert "畫面上的字幕還留著" in body["detail"]
            assert any(row.get("zh") == "留下" for row in app.state.bus.caption_state("class"))
            assert any(row.get("zh") == "留下" for row in app.state.store.room_rows("class"))
            room_denied = await client.delete("/api/captions", params={"room_id": "class"}, headers=headers)
            assert room_denied.status_code == 503, room_denied.text
            assert room_denied.json()["ok"] is False
            assert any(row.get("zh") == "留下" for row in app.state.bus.caption_state("class"))
            assert any(row.get("zh") == "可刪" for row in app.state.bus.caption_state("class"))
            app.state.store._delete_id_now = original_id
            app.state.store._delete_room_now = original_room
            removed = await client.delete(
                "/api/captions",
                params={"room_id": "class", "session_id": "s", "seq": 2},
                headers=headers,
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["ok"] is True
            again = await push(client, token, "class", "s", 2, "不該復活".encode(), async_header=True, wait_translation="0")
            assert again.status_code == 409
            kept = await export_of(client, token, "class", "json")
            assert any(row.get("zh") == "留下" for row in kept.json())
            assert all(row.get("zh") != "可刪" for row in kept.json())
    finally:
        await stop(app)
    restarted = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(path)))
    try:
        async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(restarted, client)
            exported = await export_of(client, token, "class", "json")
            rows = exported.json()
            assert any(row.get("zh") == "留下" for row in rows)
            assert all(row.get("zh") != "可刪" for row in rows)
    finally:
        await stop(restarted)


@pytest.mark.anyio
async def test_active_room_expires_each_caption_not_the_whole_room():
    """An open room must drop captions older than the ttl without sealing the seq."""
    app = app_for(settings=settings_with(
        allow_testclient=True, translate=False, caption_ttl_s=30, room_idle_s=3600,
    ))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            headers = {**auth(token), "content-type": "application/json"}
            first = await push(client, token, "class", "s", 1, "舊".encode(), async_header=True, wait_translation="0")
            second = await push(client, token, "class", "s", 2, "新".encode(), async_header=True, wait_translation="0")
            assert first.status_code == 200 and second.status_code == 200
            assert app.state.room_book.get("class") is not None
            app.state.bus._caption_at["class"]["class:s:1"] = time.time() - 90
            await app.state.sweep_once()
            state = {row["id"]: row for row in app.state.bus.caption_state("class")}
            assert "class:s:1" not in state
            assert state["class:s:2"]["zh"] == "新"
            assert app.state.room_book.get("class") is not None
            exported = await export_of(client, token, "class", "json")
            texts = [row["zh"] for row in exported.json()]
            assert texts == ["新"]
            revived = await client.post(
                "/api/segment/retranslate",
                json={"room_id": "class", "session_id": "s", "seq": 1},
                headers=headers,
            )
            assert revived.status_code == 404
            again = await push(client, token, "class", "s", 1, "再來".encode(), async_header=True, wait_translation="0")
            assert again.status_code == 200, again.text
            assert again.json()["zh"] == "再來"

            idle_a = await push(client, token, "quiet", "s", 1, "甲".encode(), async_header=True, wait_translation="0")
            idle_b = await push(client, token, "quiet", "s", 2, "乙".encode(), async_header=True, wait_translation="0")
            assert idle_a.status_code == 200 and idle_b.status_code == 200
            closed = await client.post("/api/rooms/close", json={"room_id": "quiet"}, headers=headers)
            assert closed.status_code == 200, closed.text
            app.state.bus._caption_at["quiet"]["quiet:s:1"] = time.time() - 90
            await app.state.sweep_once()
            quiet = {row["id"]: row for row in app.state.bus.caption_state("quiet")}
            assert "quiet:s:1" not in quiet
            assert quiet["quiet:s:2"]["zh"] == "乙"
            quiet_export = await export_of(client, token, "quiet", "json")
            assert [row["zh"] for row in quiet_export.json()] == ["乙"]
    finally:
        await stop(app)


def test_device_acceptance_storage_off_export_covers_a_class():
    """A 100-minute export fits in the room caption cap. Storage is for restart, not for that export."""
    text = Path("docs/DEVICE-ACCEPTANCE.md").read_text(encoding="utf-8")
    assert "再送一次術語，然後" not in text
    assert "只在按儲存時送出" in text
    assert "401 不會自動再送一次術語" in text
    assert "沒開儲存時 100 分鐘匯出會缺掉大部分" not in text
    assert "BREEZE_ROOM_CAPTION_CAP" in text
    assert "預設 5000" in text
    assert "不能因為沒開儲存就把 C-1 的匯出判成缺段" in text
    assert "沒開儲存時 C-6、E-4 直接 FAIL" in text
    assert "沒開儲存時只會剩近期字幕，直接 FAIL" in text
    statuses = [line.strip() for line in text.splitlines() if line.strip().startswith("- 狀態：")]
    assert statuses
    assert statuses == ["- 狀態：尚未驗證"] * len(statuses)


@pytest.mark.anyio
async def test_cancel_finishes_listener_and_sender():
    """Python 3.11 wait_for can swallow a cancel that lands as the inner await finishes."""
    from app.dispatch import ListenerSlot

    async def finishes(task: asyncio.Task, seconds: float = 0.5) -> bool:
        if task.done():
            return True
        try:
            async with asyncio.timeout(seconds):
                await asyncio.shield(task)
        except (TimeoutError, asyncio.CancelledError):
            return task.done()
        return task.done()

    async def one_sender() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def sender(_msg, release=release, entered=entered) -> None:
            entered.set()
            await release.wait()

        slot = ListenerSlot(sender, send_timeout=30)
        slot.start()
        assert slot.task is not None
        assert slot.offer({"n": 1})
        await entered.wait()
        release.set()
        slot.task.cancel()
        assert await finishes(slot.task), "sender kept running after cancel"

    async def one_close() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def sender(_msg, release=release, entered=entered) -> None:
            entered.set()
            await release.wait()

        slot = ListenerSlot(sender, send_timeout=30)
        slot.start()
        assert slot.task is not None
        assert slot.offer({"n": 1})
        await entered.wait()
        release.set()
        try:
            async with asyncio.timeout(1):
                await slot.close()
        except TimeoutError as exc:
            raise AssertionError("sender close hung") from exc
        assert slot.task.done()

    try:
        async with asyncio.timeout(20):
            for _ in range(30):
                await one_sender()
            for _ in range(15):
                await one_close()
            app = app_for(settings=settings_with(allow_testclient=True, translate=False, idle_timeout_s=30))
            try:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
                    token = await token_of(app, client)
                    opened = await client.post(
                        "/api/rooms/open",
                        json={"room_id": "class"},
                        headers={**auth(token), "content-type": "application/json"},
                    )
                    assert opened.status_code == 200, opened.text
                    for _ in range(15):
                        sock = Socket(app, "/ws/listen?room_id=class")
                        await sock.__aenter__()
                        hello = await sock.recv()
                        assert hello["type"] == "hello"
                        await sock.inc.put({"type": "websocket.receive", "text": '{"type":"pong"}'})
                        assert sock.task is not None
                        sock.task.cancel()
                        assert await finishes(sock.task), "listener kept running after cancel"
                        assert not app.state.room_book.rooms["class"]["listeners"]
            finally:
                try:
                    async with asyncio.timeout(2):
                        await stop(app)
                except TimeoutError:
                    pass
    except TimeoutError as exc:
        raise AssertionError("cancel test exceeded its real timeout") from exc
