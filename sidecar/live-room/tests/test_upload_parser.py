"""Capped upload body, then Starlette's multipart parser. Request.form is not replaced."""

import asyncio
import gc
import struct
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.settings import Settings, parse_env_file
from tests.test_round2 import app_for, auth, stop, token_of


def _multipart(parts: list[tuple[str, bytes]], *, boundary: str = "breezebound") -> tuple[bytes, str]:
    """parts are (headers, body). headers is the raw header block without the blank line."""
    chunks: list[bytes] = []
    for headers, body in parts:
        chunks.append(f"--{boundary}\r\n".encode() + headers + b"\r\n\r\n" + body + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _text_part(name: str, value: str) -> tuple[bytes, bytes]:
    header = f'Content-Disposition: form-data; name="{name}"'.encode()
    return header, value.encode()


def _file_part(name: str, filename: str, payload: bytes, *, extra: str = "") -> tuple[bytes, bytes]:
    header = f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'
    if extra:
        header += "; " + extra
    header += "\r\nContent-Type: audio/webm"
    return header.encode(), payload


def test_default_audio_cap_fits_a_short_slice():
    settings = Settings()
    assert settings.max_audio_bytes == 2 * 1024 * 1024
    assert settings.max_audio_seconds == 30.0
    assert settings.upload_read_timeout_s == 20.0
    from_env = Settings.from_env({})
    assert from_env.max_audio_bytes == 2 * 1024 * 1024
    assert from_env.upload_read_timeout_s == 20.0


# New installs copy .env.example onto .env (scripts/install_runtime.py). These two
# lines are the documented install profile, not the in-process fallback: README
# ships resident ASR and a sqlite caption file. The fallback stays cli / empty so
# a process with no .env does not open a database or require a resident server.
_INSTALL_PROFILE = {
    "BREEZE_ASR": "resident",
    "BREEZE_DATA_PATH": "data/captions.sqlite3",
}
# Read straight from the environment, not via Settings.
_DIRECT_DEFAULTS = {
    "OPENAI_API_KEY": "",
    "OPENAI_TRANSLATION_MODEL": "gpt-4.1-mini",
}


def test_env_example_matches_code_defaults():
    """Every key install_runtime would copy has to match the code default.

    A value that is not the default becomes the default of a new install.
    BREEZE_ASR and BREEZE_DATA_PATH are the install profile above; every other
    key must leave Settings unchanged from Settings.from_env({}). from_env({})
    itself must match the Settings() field defaults, so a dataclass default
    that drifts away from the from_env fallback is caught too.
    """
    root = Path(__file__).resolve().parents[1]
    example_path = root / ".env.example"
    example = parse_env_file(example_path)
    assert example["BREEZE_MAX_AUDIO_BYTES"] == "2097152"
    assert example["BREEZE_UPLOAD_READ_TIMEOUT"] == "20"
    bare = Settings.from_env({})
    builtin = Settings()
    for name in Settings.__dataclass_fields__:
        assert getattr(bare, name) == getattr(builtin, name), name
    loaded = Settings.from_env({}, env_file=example_path)
    for key, value in example.items():
        if key in _DIRECT_DEFAULTS:
            assert value == _DIRECT_DEFAULTS[key], key
            continue
        if key in _INSTALL_PROFILE:
            assert value == _INSTALL_PROFILE[key], key
            continue
        only = Settings.from_env({key: value})
        assert only == bare, key
    for name in Settings.__dataclass_fields__:
        if name == "asr_mode":
            assert loaded.asr_mode == "resident"
            assert bare.asr_mode == "cli"
            continue
        if name == "data_path":
            assert loaded.data_path == "data/captions.sqlite3"
            assert bare.data_path == ""
            continue
        assert getattr(loaded, name) == getattr(bare, name), name
    unknown = set(example) - set(_DIRECT_DEFAULTS) - set(_INSTALL_PROFILE)
    # Any other key must be one Settings.from_env actually reads. A typo would
    # have passed the per-key check above, because an unread key changes nothing.
    known = {
        "BREEZE_PORT",
        "BREEZE_MAX_AUDIO_BYTES",
        "BREEZE_MAX_AUDIO_SECONDS",
        "BREEZE_MAX_ROOMS",
        "BREEZE_MAX_LISTENERS",
        "BREEZE_MAX_QUEUE",
        "BREEZE_MAX_INFLIGHT_BYTES",
        "BREEZE_ASR_WORKERS",
        "BREEZE_ASR_THREADS",
        "BREEZE_ASR_AUDIO_CONTEXT",
        "BREEZE_ASR_BEAM_SIZE",
        "BREEZE_ASR_BEST_OF",
        "BREEZE_HISTORY_LIMIT",
        "BREEZE_MAX_RESULTS",
        "BREEZE_MAX_HELD",
        "BREEZE_ALLOW_TESTCLIENT",
        "BREEZE_SHARE_HOST",
        "BREEZE_SHARE_SCHEME",
        "BREEZE_TRANSLATE",
        "BREEZE_MODEL",
        "BREEZE_WHISPER",
        "BREEZE_WHISPER_SERVER",
        "BREEZE_RESIDENT_URL",
        "BREEZE_RESIDENT_STARTUP_TIMEOUT",
        "BREEZE_GAP_WAIT",
        "BREEZE_DECODE_TIMEOUT",
        "BREEZE_ASR_TIMEOUT",
        "BREEZE_TRANSLATE_TIMEOUT",
        "BREEZE_TRANSLATE_QUEUE",
        "BREEZE_TRANSLATE_WORKERS",
        "BREEZE_HEARTBEAT",
        "BREEZE_IDLE_TIMEOUT",
        "BREEZE_ROOM_IDLE",
        "BREEZE_LISTENER_QUEUE",
        "BREEZE_SILENCE_RMS",
        "BREEZE_CAPTION_TTL",
        "BREEZE_ROOM_CAPTION_CAP",
        "BREEZE_REPLAY_PER_MINUTE",
        "BREEZE_STOP_FLUSH",
        "BREEZE_SHUTDOWN_FLUSH",
        "BREEZE_TOKEN_BUDGET",
        "BREEZE_ALLOWED_HOSTS",
        "BREEZE_ALLOWED_SCHEMES",
        "BREEZE_SEGMENT_MS",
        "BREEZE_UPLOAD_READ_TIMEOUT",
    }
    assert unknown <= known, unknown - known


@pytest.mark.anyio
async def test_valid_upload_returns_chinese():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await client.post(
                "/api/push",
                data={"room_id": "class", "session_id": "s", "seq": "1"},
                files={"audio": ("a.webm", "課堂".encode(), "audio/webm")},
                headers=auth(token),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "課堂"
            assert app.state.asr.calls == 1
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_non_multipart_is_415_after_auth():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            naked = await client.post(
                "/api/push",
                content=b"{}",
                headers={"content-type": "application/json"},
            )
            assert naked.status_code == 401
            token = await token_of(app, client)
            resp = await client.post(
                "/api/push",
                content=b"{}",
                headers={**auth(token), "content-type": "application/json"},
            )
            assert resp.status_code == 415
            assert resp.json()["detail"] == "上傳格式不正確"
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_malformed_multipart_is_400():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            missing = await client.post(
                "/api/push",
                content=b"--no-boundary\r\n\r\n",
                headers={**auth(token), "content-type": "multipart/form-data"},
            )
            assert missing.status_code == 400, missing.text
            truncated, ctype = _multipart([
                _file_part("audio", "a.webm", b"hello"),
            ])
            truncated = truncated.split(b"\r\n--" )[0]  # drop the closing boundary
            cut = await client.post(
                "/api/push",
                content=truncated,
                headers={**auth(token), "content-type": ctype},
            )
            assert cut.status_code == 400, cut.text
            odd_header = (
                'Content-Disposition: form-data; name="audio"; filename*=undefined\'\'weird.wav\r\n'
                "Content-Type: audio/webm"
            ).encode()
            odd, odd_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                (odd_header, b"not-a-file"),
            ])
            named = await client.post(
                "/api/push",
                content=odd,
                headers={**auth(token), "content-type": odd_type},
            )
            assert named.status_code == 400, named.text
            assert named.status_code != 500
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_too_many_fields_and_files_are_400():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            fields = [_text_part(f"f{i}", "v") for i in range(17)]
            body, ctype = _multipart(fields)
            resp = await client.post(
                "/api/push",
                content=body,
                headers={**auth(token), "content-type": ctype},
            )
            assert resp.status_code == 400, resp.text
            two, two_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                _file_part("audio", "a.webm", b"one"),
                _file_part("extra", "b.webm", b"two"),
            ])
            files = await client.post(
                "/api/push",
                content=two,
                headers={**auth(token), "content-type": two_type},
            )
            assert files.status_code == 400, files.text
            fat = b"y" * (64 * 1024 + 8)
            wide, wide_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                (b'Content-Disposition: form-data; name="pad"', fat),
                _file_part("audio", "a.webm", b"one"),
            ])
            oversized = await client.post(
                "/api/push",
                content=wide,
                headers={**auth(token), "content-type": wide_type},
            )
            assert oversized.status_code == 400, oversized.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_chunked_oversize_is_413():
    limit = 1024
    app = app_for(settings=Settings(allow_testclient=True, max_audio_bytes=limit))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def chunks():
                yield b"x" * (limit + 65536 + 1)

            resp = await client.post(
                "/api/push",
                content=chunks(),
                headers={**auth(token), "content-type": "multipart/form-data; boundary=breezebound"},
            )
            assert resp.status_code == 413, resp.text
            assert str(limit) in resp.json()["detail"]
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_slow_body_times_out():
    app = app_for(settings=Settings(allow_testclient=True, upload_read_timeout_s=0.3))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=5) as client:
            token = await token_of(app, client)

            async def slow():
                yield b"--breezebound\r\n"
                await asyncio.sleep(2)
                yield b"--breezebound--\r\n"

            started = time.monotonic()
            resp = await asyncio.wait_for(
                client.post(
                    "/api/push",
                    content=slow(),
                    headers={**auth(token), "content-type": "multipart/form-data; boundary=breezebound"},
                ),
                timeout=3,
            )
            elapsed = time.monotonic() - started
            assert resp.status_code == 408, resp.text
            assert resp.json()["detail"] == "上傳逾時"
            assert elapsed < 2
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_asr_slot_is_not_held_while_the_body_is_read():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            body, ctype = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                _file_part("audio", "a.webm", "還在讀".encode()),
            ])
            entered = asyncio.Event()
            release = asyncio.Event()
            first, rest = body[:24], body[24:]

            async def drip():
                yield first
                entered.set()
                await release.wait()
                yield rest

            task = asyncio.create_task(client.post(
                "/api/push",
                content=drip(),
                headers={**auth(token), "content-type": ctype},
            ))
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not task.done()
            assert app.state.pipeline.stats()["pending"] == 0
            assert not app.state.pipeline._reserved
            release.set()
            resp = await asyncio.wait_for(task, timeout=3)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "還在讀"
            assert app.state.pipeline.stats()["pending"] == 0
            assert app.state.asr.calls == 1
    finally:
        await stop(app)


def _two_megabyte_slice() -> bytes:
    """One second of silence, padded out to the 2 MB audio cap.

    The RIFF data chunk stays 1 second, so the duration check does not reject
    the padding. The multipart parser still reads every byte.
    """
    pcm = b"\x00\x00" * 16000
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    data = b"data" + struct.pack("<I", len(pcm))
    wav = header + fmt + data + pcm
    total = 2 * 1024 * 1024
    assert len(wav) < total
    return wav + b"\x00" * (total - len(wav))


class _ShortAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav, prompt: str = ""):
        del wav, prompt
        self.calls += 1
        return AsrResult(ok=True, text="中文")


@pytest.mark.anyio
async def test_two_megabyte_parse_does_not_block_the_loop():
    """A 2 MB multipart upload blocks the event loop less than 10 ms per slice.

    Tracing stays off. Automatic GC is off for the measured slice so a
    collection is not booked as parse time. The longest heartbeat gap is the
    longest stretch the loop did not run during that one upload.
    """
    import tracemalloc

    assert not tracemalloc.is_tracing()
    payload = _two_megabyte_slice()
    app = app_for(
        asr=_ShortAsr(),
        settings=Settings(allow_testclient=True, translate=False),
    )
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            # First 2 MB slice opens the spool file. The measured slice is the next one.
            for seq, label in ((1, "暖機"), (2, None)):
                piece = "暖機".encode() if label else payload
                name = "a.webm" if label else "a.wav"
                primed, primed_type = _multipart([
                    _text_part("room_id", "class"),
                    _text_part("session_id", "s"),
                    _text_part("seq", str(seq)),
                    _file_part("audio", name, piece),
                ])
                primed_resp = await client.post(
                    "/api/push",
                    content=primed,
                    headers={**auth(token), "content-type": primed_type},
                )
                assert primed_resp.status_code == 200, primed_resp.text
            body, ctype = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "3"),
                _file_part("audio", "a.wav", payload),
            ])
            gaps: list[float] = []
            stop_beat = asyncio.Event()

            async def beat():
                last = time.perf_counter()
                while not stop_beat.is_set():
                    await asyncio.sleep(0)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            beater = asyncio.create_task(beat())
            await asyncio.sleep(0)
            resp = await client.post(
                "/api/push",
                content=body,
                headers={**auth(token), "content-type": ctype},
            )
            stop_beat.set()
            await beater
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "中文"
            assert gaps
            assert max(gaps) < 0.010, max(gaps)
    finally:
        if was_enabled:
            gc.enable()
        await stop(app)
    assert not tracemalloc.is_tracing()
