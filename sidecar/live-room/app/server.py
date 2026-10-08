from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote_to_bytes

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from app.aio import cancellation_pending, wait_bounded
from app.asr import CliAsr, ResidentAsr
from app.native_asr import NativeResidentAsr
from app.audio import AudioError, convert_to_wav, ffmpeg_bin, wav_duration_seconds
from app.auth import audience_origin_allowed, new_host_token, require_host, require_local_host, same_secret
from app.dispatch import ListenerSlot, RoomBus, for_listener
from app.pipeline import Pipeline, PipelineError, Segment
from app.rooms import RoomBook, RoomIdError, validate_room_id, validate_session_id
from app.settings import Settings, fill_process_environ
from app.share import list_share_hosts, listen_url
from app.store import CaptionStore
from app.textutil import export_text, parse_glossary
from app.translate import Translator

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
DEFAULT_WHISPER = ROOT / "tools" / "whisper-cli.exe"
DEFAULT_SERVER = ROOT / "tools" / "whisper-server.exe"
TMP = ROOT / "tmp"
PROMPT = "以下是台灣國語的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"

_TRACKED: list[FastAPI] = []


class RevalidatingStaticFiles(StaticFiles):
    """Send Cache-Control: no-cache and keep ETag so browsers revalidate.

    A cached room_client.js paired with a newer host.html or room.html throws
    on import and the page script never starts.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: dict,
        status_code: int = 200,
    ) -> Response:
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            headers={"Cache-Control": "no-cache"},
        )
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident = int(handle.read().split()[1])
        return resident * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


class Conn:
    def __init__(self, ws: WebSocket, maxsize: int):
        self.ws = ws
        self.slot = ListenerSlot(ws.send_json, maxsize=maxsize)
        self.slot.last_pong = time.monotonic()


class _ReplayGate:
    """Caps full replay/backfill dumps per client IP. Live captions are not counted."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = max(1, int(limit))
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.get(ip)
        if bucket is None:
            bucket = []
            self._hits[ip] = bucket
        cutoff = now - self.window_s
        if bucket and bucket[0] <= cutoff:
            bucket[:] = [item for item in bucket if item > cutoff]
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True


def _content_too_large(request: Request, settings: Settings) -> bool:
    raw = request.headers.get("content-length")
    if not raw:
        return False
    try:
        size = int(raw)
    except ValueError:
        return False
    return size > settings.max_audio_bytes + 65536


def _early_key(request: Request) -> tuple[str, str, int] | None:
    room = request.query_params.get("room_id")
    session = request.query_params.get("session_id")
    seq = request.query_params.get("seq")
    if not room or not session or not seq:
        return None
    try:
        return (validate_room_id(room), validate_session_id(session), int(seq))
    except (RoomIdError, ValueError):
        return None


async def _wait_until_join_ready(pipeline: Pipeline, key: tuple[str, str, int], timeout: float) -> None:
    """Owner has reserved this segment but not registered a flight. Do not take another slot."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while key in pipeline._reserved or key in pipeline._active:
        flight = pipeline._flight.get(key)
        if flight is not None and not flight.done():
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.01)


async def _read_upload(upload, limit: int) -> bytes:
    if upload is None or isinstance(upload, str):
        raise AudioError(400, "沒有收到音訊")
    chunks = []
    total = 0
    while True:
        block = await upload.read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise AudioError(413, f"音訊超過 {limit} bytes，已拒絕")
        chunks.append(block)
    if total == 0:
        raise AudioError(400, "沒有收到音訊")
    return b"".join(chunks)


class _PushFormError(Exception):
    """Multipart body is not a form this route can read."""


class _MemoryUpload:
    """File part kept in memory. read() does not hop to the threadpool."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        end = self._pos + size
        if end > len(self._data):
            end = len(self._data)
        block = self._data[self._pos:end]
        self._pos = end
        return block

    async def close(self) -> None:
        self._data = b""
        self._pos = 0


class _PushForm:
    """Last field wins, same as Starlette's form mapping."""

    def __init__(self) -> None:
        self._values: dict[str, str | _MemoryUpload] = {}

    def add(self, name: str, value: str | _MemoryUpload) -> None:
        self._values[name] = value

    def get(self, name: str, default=None):
        return self._values.get(name, default)

    async def close(self) -> None:
        for value in self._values.values():
            close = getattr(value, "close", None)
            if close is not None:
                await close()
        self._values.clear()


def _multipart_boundary(content_type: str) -> bytes | None:
    media, _, rest = content_type.partition(";")
    if media.strip().lower() != "multipart/form-data":
        return None
    for section in rest.split(";"):
        piece = section.strip()
        if not piece.lower().startswith("boundary="):
            continue
        raw = piece.split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        if not raw or len(raw) > 200:
            return None
        try:
            token = raw.encode("latin-1")
        except UnicodeEncodeError:
            return None
        if b"\r" in token or b"\n" in token:
            return None
        return token
    return None


def _boundary_line(body: bytes, at: int, token: bytes) -> tuple[bool, int] | None:
    """Return (closing, index after the line) when a boundary line starts at `at`."""
    if not body.startswith(token, at):
        return None
    index = at + len(token)
    while index < len(body) and body[index] in (0x20, 0x09):
        index += 1
    closing = False
    if body.startswith(b"--", index):
        closing = True
        index += 2
        while index < len(body) and body[index] in (0x20, 0x09):
            index += 1
    if index == len(body):
        return (True, index) if closing else None
    if body.startswith(b"\r\n", index):
        return closing, index + 2
    return None


def _find_boundary(body: bytes, start: int, token: bytes) -> tuple[int, bool, int] | None:
    """Next boundary at or after `start`: (line start, closing, resume)."""
    if start == 0:
        opened = _boundary_line(body, 0, token)
        if opened is not None:
            closing, resume = opened
            return 0, closing, resume
    needle = b"\r\n" + token
    scan = start
    while True:
        at = body.find(needle, scan)
        if at < 0:
            return None
        opened = _boundary_line(body, at + 2, token)
        if opened is not None:
            closing, resume = opened
            return at, closing, resume
        scan = at + 2


def _split_semicolon(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted:
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char == ";" and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _unquote_param(raw: str) -> str:
    text = raw.strip()
    if text.startswith('"'):
        chars: list[str] = []
        escaped = False
        for char in text[1:]:
            if escaped:
                chars.append(char)
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                break
            chars.append(char)
        text = "".join(chars)
    else:
        text = text.split()[0] if text else ""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _decode_ext_param(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    charset, sep, rest = text.partition("'")
    if not sep:
        return _unquote_param(raw)
    _lang, sep2, encoded = rest.partition("'")
    if not sep2:
        return _unquote_param(raw)
    data = unquote_to_bytes(encoded)
    for encoding in (charset or "utf-8", "utf-8"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")


def _disposition_params(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for index, piece in enumerate(_split_semicolon(value)):
        piece = piece.strip()
        if index == 0 or "=" not in piece:
            continue
        key, _, raw = piece.partition("=")
        key = key.strip().lower()
        if not key:
            continue
        params[key] = _decode_ext_param(raw.strip()) if key.endswith("*") else _unquote_param(raw.strip())
    return params


def _decode_field(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _header_map(blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not blob:
        return headers
    for line in blob.decode("latin-1").split("\r\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def _add_part(form: _PushForm, raw: bytes, files: list[int], fields: list[int]) -> None:
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise _PushFormError("part has no header")
    params = _disposition_params(_header_map(raw[:sep]).get("content-disposition", ""))
    name = params.get("name", "")
    if not name:
        raise _PushFormError("part has no name")
    is_file = "filename" in params or "filename*" in params
    if is_file:
        files[0] += 1
        if files[0] > 1000:
            raise _PushFormError("too many files")
        form.add(name, _MemoryUpload(raw[sep + 4:]))
        return
    fields[0] += 1
    if fields[0] > 1000:
        raise _PushFormError("too many fields")
    form.add(name, _decode_field(raw[sep + 4:]))


def _parse_multipart(body: bytes, boundary: bytes) -> _PushForm:
    token = b"--" + boundary
    found = _find_boundary(body, 0, token)
    if found is None:
        raise _PushFormError("missing boundary")
    _line, closing, resume = found
    form = _PushForm()
    if closing:
        return form
    counts = [0]
    fields = [0]
    pos = resume
    while True:
        nxt = _find_boundary(body, pos, token)
        if nxt is None:
            raise _PushFormError("truncated multipart")
        line_start, closing, resume = nxt
        _add_part(form, body[pos:line_start], counts, fields)
        if closing:
            return form
        pos = resume


async def _read_body_capped(request: Request, limit: int, settings: Settings) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        chunks.append(chunk)
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


async def _push_form(request: Request, settings: Settings):
    """Read the upload form.

    python-multipart walks the body one byte at a time on the event loop.
    A traced few-hundred-kilobyte slice then holds the loop longer than the
    compressed 6s period, so the host books a recorder wait. Bulk search
    stays on the loop but does not scale with every byte.
    """
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        return await request.form()
    boundary = _multipart_boundary(content_type)
    if boundary is None:
        raise HTTPException(status_code=400, detail="上傳格式不正確")
    body = await _read_body_capped(request, settings.max_audio_bytes + 65536, settings)
    try:
        return _parse_multipart(body, boundary)
    except _PushFormError as exc:
        raise HTTPException(status_code=400, detail="上傳格式不正確") from exc


def _field(form, request: Request, name: str) -> str:
    value = form.get(name)
    if value is None or isinstance(value, str) and value == "":
        value = request.query_params.get(name, "")
    return str(value or "")


_MAX_SEGMENT_MS = 48 * 60 * 60 * 1000


def _optional_ms(form, request: Request, name: str) -> int | None:
    raw = _field(form, request, name)
    if raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 0:
        return 0
    if value > _MAX_SEGMENT_MS:
        return _MAX_SEGMENT_MS
    return value


def _public_result(done: Segment) -> JSONResponse:
    if done.status == "timeout":
        code = 408
    elif done.status == "cancelled":
        code = 409
    elif done.status == "error":
        code = 422
    else:
        code = 200
    body = done.public()
    body["ok"] = code == 200
    body["detail"] = done.error
    return JSONResponse(status_code=code, content=body)


def create_app(settings: Settings | None = None, asr=None, translator: Translator | None = None, decoder=None) -> FastAPI:
    if settings is None:
        fill_process_environ()
        settings = Settings.from_env()
    token = new_host_token()
    translator = translator or Translator(
        enabled=settings.translate,
        key=os.getenv("OPENAI_API_KEY", ""),
        token_budget=settings.token_budget,
    )
    model = Path(settings.model_path) if settings.model_path else DEFAULT_MODEL
    whisper = Path(settings.whisper_path) if settings.whisper_path else DEFAULT_WHISPER
    resident_error = ""
    book = RoomBook(settings.max_rooms, settings.room_idle_s)
    bus = RoomBus(settings.history_limit, caption_cap=getattr(settings, "room_caption_cap", 5000))
    store = CaptionStore(settings.data_path or None)
    if asr is None:
        if settings.asr_mode == "native":
            asr = NativeResidentAsr(model, threads=settings.asr_threads,
                startup_timeout_s=settings.resident_startup_s, inference_timeout_s=settings.asr_timeout_s,
                audio_context=settings.asr_audio_context, beam_size=settings.asr_beam_size, best_of=settings.asr_best_of)
            started = asr.start()
            if not started.ok:
                resident_error = started.error
        elif settings.asr_mode == "resident":
            resident = ResidentAsr(
                settings.resident_url,
                server_bin=Path(settings.server_path) if settings.server_path else DEFAULT_SERVER,
                model=model,
                threads=settings.asr_threads,
                startup_timeout_s=settings.resident_startup_s,
                inference_timeout_s=settings.asr_timeout_s,
                audio_context=settings.asr_audio_context,
                beam_size=settings.asr_beam_size,
                best_of=settings.asr_best_of,
            )
            started = resident.start()
            if started.ok:
                asr = resident
            else:
                resident_error = started.error
                asr = resident
        else:
            asr = CliAsr(whisper, model, threads=settings.asr_threads, timeout_s=settings.asr_timeout_s, audio_context=settings.asr_audio_context, beam_size=settings.asr_beam_size, best_of=settings.asr_best_of)
    share_override = {"host": settings.share_host}
    tasks: list[asyncio.Task] = []
    replay_floors: dict[str, float] = {}
    replay_gate = _ReplayGate(settings.replay_per_minute)
    share_cache: dict[str, object] = {"at": 0.0, "hosts": []}

    def current_host() -> str | None:
        host = (share_override["host"] or "").strip()
        return host or None

    def _audience_extra_hosts() -> tuple[str, ...]:
        now = time.monotonic()
        cached_at = float(share_cache.get("at") or 0.0)
        cached = share_cache.get("hosts")
        if not isinstance(cached, list) or now - cached_at >= 5:
            try:
                cached = list_share_hosts()
            except Exception:
                logging.getLogger("breeze.server").exception("share host list failed")
                cached = []
            share_cache["hosts"] = cached
            share_cache["at"] = now
        hosts = [str(item) for item in cached if item]
        chosen = current_host()
        if chosen:
            hosts.append(chosen)
        if settings.share_host:
            hosts.append(settings.share_host)
        return tuple(hosts)

    def _load_replay_floor(room_id: str) -> float | None:
        if room_id in replay_floors:
            return replay_floors[room_id]
        if not store.enabled:
            return None
        try:
            value = store.get_replay_floor(room_id)
        except Exception:
            logging.getLogger("breeze.server").exception("replay floor lookup failed")
            return None
        if value is None:
            return None
        replay_floors[room_id] = value
        return value

    def _seal_replay(room_id: str) -> None:
        # Flush first so saved updated_at values are already behind this floor.
        if store.enabled:
            try:
                store.flush()
            except Exception:
                logging.getLogger("breeze.server").exception("caption store flush before replay seal failed")
        floor = time.time()
        replay_floors[room_id] = floor
        if store.enabled:
            try:
                store.set_replay_floor(room_id, floor)
            except Exception:
                logging.getLogger("breeze.server").exception("replay floor save failed")

    def ensure_room(room_id: str) -> dict:
        fresh = book.get(room_id) is None
        room = book.open(room_id)
        if fresh:
            room["replay_not_before"] = _load_replay_floor(room_id)
        return room

    def _host_authorized(request: Request) -> bool:
        try:
            require_host(request, token, settings)
        except HTTPException:
            return False
        return True

    def _listen_key_of(room_id: str) -> str:
        room = book.get(room_id)
        if not room:
            return ""
        return str(room.get("listen_key") or "")

    def _listener_authorized(ws: WebSocket, room: dict, listen_key: str) -> bool:
        if same_secret(listen_key, str(room.get("listen_key") or "")):
            return True
        header = ws.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return same_secret(supplied, token)

    def _captions_since_open(room_id: str, rows: list[dict]) -> list[dict]:
        room = book.get(room_id)
        floor = None if room is None else room.get("replay_not_before")
        if floor is None:
            return list(rows)
        stamps = bus._caption_at.get(room_id, {})
        kept: list[dict] = []
        for row in rows:
            kind = str(row.get("type") or "")
            if kind in {"captions_cleared", "caption_deleted", "ping", "pong"}:
                kept.append(row)
                continue
            stamp = stamps.get(str(row.get("id") or ""))
            if stamp is not None and float(stamp) > float(floor):
                kept.append(row)
        return kept

    def _audience_captions(rows: list[dict]) -> list[dict]:
        return [for_listener(item) for item in rows]

    def _fanout(snap: dict) -> None:
        room = book.get(str(snap.get("room_id") or ""))
        if room is None:
            return
        room["history"] = bus.history(str(snap.get("room_id") or ""))
        room["last_active"] = time.monotonic()
        outgoing = for_listener(snap)
        dead = []
        for conn in list(room["listeners"]):
            if not conn.slot.offer(outgoing):
                dead.append(conn)
        for conn in dead:
            room["listeners"].discard(conn)

    def on_event(event: dict):
        try:
            snap = bus.publish(event)
            if snap is None:
                return None
            _fanout(snap)
            if store.enabled and snap.get("id") and snap.get("type") not in {"captions_cleared", "caption_deleted"}:
                store.submit_save(snap)
            return snap
        except Exception:
            logging.getLogger("breeze.server").exception("caption publish failed")
            return None

    def _release_room(room_id: str) -> None:
        """Idle or close drops runtime, not captions that are still inside the ttl."""
        keep = bus.has_captions(room_id)
        if not keep and store.enabled:
            try:
                keep = store.has_room(room_id)
            except Exception:
                logging.getLogger("breeze.server").exception("caption store lookup failed")
                keep = True
        retained: list[dict] = []
        if keep:
            bus.retire(room_id)
            retained = bus.caption_state(room_id)
            if not retained and store.enabled:
                try:
                    retained = store.room_rows(room_id)
                except Exception:
                    logging.getLogger("breeze.server").exception("caption order lookup failed")
                    retained = []
        else:
            bus.drop(room_id)
        pipeline.drop_room(room_id)
        if retained:
            pipeline.note_retained_order(room_id, retained)
        _seal_replay(room_id)

    def _split_kept_id(room_id: str, seg_id: str) -> tuple[str, int]:
        prefix = room_id + ":"
        if not str(seg_id).startswith(prefix):
            return "", 0
        session, sep, seq_text = str(seg_id)[len(prefix):].rpartition(":")
        if not sep:
            return "", 0
        try:
            seq = int(seq_text)
        except ValueError:
            return "", 0
        if seq < 1 or not session:
            return "", 0
        return session, seq

    def _expire_captions() -> None:
        # Each caption expires on its own updated time, including in an open room.
        # SQLite purge uses the same rule. An empty idle room then drops its runtime.
        for room_id, seg_id in bus.prune_expired(settings.caption_ttl_s):
            session_id, seq = _split_kept_id(room_id, seg_id)
            if session_id and seq:
                pipeline.forget_expired(room_id, session_id, seq)
        for room_id in list(bus._state):
            if bus.has_captions(room_id) or book.get(room_id) is not None:
                continue
            bus.drop(room_id)
            pipeline.drop_room(room_id)

    pipeline = Pipeline(asr, translator, PROMPT, TMP, settings, on_event=on_event)
    hydrated: set[str] = set()
    hydrate_jobs: dict[str, asyncio.Task] = {}

    async def _hydrate_room(room_id: str) -> None:
        if room_id in hydrated:
            return
        if not store.enabled:
            hydrated.add(room_id)
            return
        try:
            rows = await asyncio.to_thread(store.room_rows, room_id)
        except Exception:
            logging.getLogger("breeze.server").exception("caption hydrate failed")
            hydrate_jobs.pop(room_id, None)
            return
        cutoff = time.time() - float(settings.caption_ttl_s)
        fresh = []
        for row in rows or []:
            try:
                updated = float(row.get("updated_at") or 0)
            except (TypeError, ValueError):
                updated = 0.0
            if updated and updated < cutoff:
                continue
            fresh.append(row)
        hydrated.add(room_id)
        bus.hydrate(room_id, fresh)
        pipeline.seed_from_store(room_id, fresh)

    async def ensure_hydrated(room_id: str) -> None:
        """Load this room from SQLite the first time it is opened, joined, or pushed."""
        if room_id in hydrated:
            return
        job = hydrate_jobs.get(room_id)
        if job is None:
            job = asyncio.get_running_loop().create_task(_hydrate_room(room_id))
            hydrate_jobs[room_id] = job
        await job

    def decode(src: Path, work: Path) -> Path:
        if decoder:
            return decoder(src, work)
        ffmpeg = ffmpeg_bin(ROOT)
        if not ffmpeg:
            raise AudioError(422, "找不到 ffmpeg。請安裝 ffmpeg，或把 ffmpeg.exe 放進 tools\\")
        wav = convert_to_wav(src, work, ffmpeg, timeout=int(settings.decode_timeout_s))
        seconds = wav_duration_seconds(wav)
        if seconds is not None and seconds > settings.max_audio_seconds:
            raise AudioError(413, f"音訊長於 {settings.max_audio_seconds} 秒，已拒絕")
        return wav

    async def sweep_once() -> None:
        try:
            for room_id in book.sweep():
                _release_room(room_id)
            _expire_captions()
            if store.enabled:
                await asyncio.to_thread(store.purge_expired, settings.caption_ttl_s)
        except Exception:
            logging.getLogger("breeze.server").exception("sweep failed")

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(1)
            await sweep_once()

    async def shutdown() -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        await pipeline.aclose()
        if hasattr(asr, "close"):
            asr.close()
        store.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.ensure_workers()
        tasks.append(asyncio.create_task(sweep_loop()))
        # After create_app returns the bus is still empty (tests depend on that).
        # Replaying starts here, before the first request is served.
        if store.enabled:
            try:
                room_ids = await asyncio.to_thread(store.room_ids)
            except Exception:
                logging.getLogger("breeze.server").exception("caption room list failed")
                room_ids = []
            for room_id in room_ids:
                await ensure_hydrated(room_id)
        try:
            yield
        finally:
            await shutdown()

    app = FastAPI(title="breeze-live-room", lifespan=lifespan)
    static_files = RevalidatingStaticFiles(directory=STATIC)
    app.mount("/static", static_files, name="static")
    app.state.settings = settings
    app.state.token = token
    app.state.pipeline = pipeline
    app.state.rooms = book.rooms
    app.state.room_book = book
    app.state.bus = bus
    app.state.translator = translator
    app.state.asr = asr
    app.state.store = store
    app.state.share_override = share_override
    app.state.shutdown = shutdown
    app.state.sweep_once = sweep_once
    app.state.resident_error = resident_error

    def share_for(room_id: str, *, include_key: bool = False) -> str | None:
        key = _listen_key_of(room_id) if include_key else ""
        return listen_url(room_id, settings.port, settings.share_scheme, current_host(), key or None)

    async def _page(path: Path, request: Request) -> Response:
        stat_result = await asyncio.to_thread(path.stat)
        return static_files.file_response(
            path,
            stat_result,
            {"type": "http", "headers": request.scope["headers"]},
        )

    @app.get("/")
    async def host_page(request: Request) -> Response:
        return await _page(STATIC / "host.html", request)

    @app.get("/r/{room_id}")
    async def room_page(request: Request, room_id: str) -> Response:
        try:
            validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _page(STATIC / "room.html", request)

    @app.get("/api/host-token")
    async def host_token(request: Request) -> Response:
        require_local_host(request, settings)
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/api/setup")
    async def setup(request: Request, room_id: str = "class") -> dict:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        host_view = _host_authorized(request)
        url = share_for(room_id, include_key=host_view)
        resident_ready = isinstance(asr, ResidentAsr) and await asyncio.to_thread(asr.health)
        asr_ready = resident_ready if isinstance(asr, ResidentAsr) else (whisper.is_file() and model.is_file() if isinstance(asr, CliAsr) else True)
        payload = {
            "room": room_id,
            "listen_url": url,
            "share_ready": url is not None,
            "share_message": None if url else "尚無可供其他裝置使用的連結",
            "share_hosts": list_share_hosts(),
            "share_host": current_host() or "",
            "port": settings.port,
            "whisper": whisper.exists(),
            "model": model.exists(),
            "ffmpeg": ffmpeg_bin(ROOT) is not None,
            "translate_configured": bool(translator.key) and settings.translate,
            "translate_verified": False,
            "translate_label": translator.status_label(),
            "asr_mode": "native" if isinstance(asr, NativeResidentAsr) else ("resident" if isinstance(asr, ResidentAsr) else "cli"),
            "asr_ready": asr_ready,
            "model_reloads_each_segment": isinstance(asr, CliAsr),
            "resident_error": getattr(asr, "last_error", "") or resident_error,
            "host_token": None,
            "queue": pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "storage": store.enabled,
            "storage_recovered": bool(getattr(store, "recovered", False)),
        }
        if host_view:
            key = _listen_key_of(room_id)
            if key:
                payload["listen_key"] = key
        return payload

    @app.get("/api/health")
    async def health() -> Response:
        asr_ready = await asyncio.to_thread(asr.health) if isinstance(asr, ResidentAsr) else (whisper.is_file() and model.is_file() if isinstance(asr, CliAsr) else True)
        ready = asr_ready and (decoder is not None or ffmpeg_bin(ROOT) is not None)
        error = getattr(asr, "last_error", "") or resident_error
        return JSONResponse({"service": "breeze-live-room", "ready": ready, "asr_ready": asr_ready, "error": error, "instance_id": os.getenv("BREEZE_DESKTOP_INSTANCE", "")}, status_code=200 if ready else 503, headers={"Cache-Control": "no-store"})

    @app.get("/api/qr")
    async def qr(request: Request, room_id: str = "class") -> Response:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = share_for(room_id, include_key=_host_authorized(request))
        if not url:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "尚無可供其他裝置使用的連結"})
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png")

    @app.post("/api/rooms/open")
    async def open_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or "class"))
        room = ensure_room(room_id)
        await ensure_hydrated(room_id)
        return {
            "ok": True,
            "room": room_id,
            "listen_url": share_for(room_id, include_key=True),
            "listen_key": room.get("listen_key") or "",
        }

    @app.post("/api/rooms/touch")
    async def touch_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            raise HTTPException(status_code=404, detail="房間不存在或已結束")
        book.touch(room_id)
        return {"ok": True}

    @app.post("/api/rooms/close")
    async def close_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        room = book.close(room_id)
        closing = []
        if room:
            closing = list(room["listeners"])
            room["listeners"].clear()
        removed = book.sweep()
        for gone in removed:
            _release_room(gone)
        for conn in closing:
            try:
                await conn.ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "ended"})
            except Exception:
                pass
            conn.slot.alive = False
        for conn in closing:
            try:
                await conn.ws.close(code=4404)
            except Exception:
                pass
        return {"ok": True}

    @app.post("/api/session/active")
    async def session_active(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            ensure_room(room_id)
        book.set_session_active(room_id, bool(body.get("active")))
        return {"ok": True}

    @app.post("/api/session/end")
    async def session_end(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        await ensure_hydrated(room_id)
        flush_s = None
        if "flush_s" in body:
            try:
                flush_s = max(0.0, float(body.get("flush_s")))
            except (TypeError, ValueError):
                flush_s = None
        if str(body.get("flush", "1")).strip().lower() in {"0", "false", "no"}:
            flush_s = 0.0
        last_seq = None
        if "last_seq" in body and body.get("last_seq") is not None:
            try:
                last_seq = int(body.get("last_seq"))
            except (TypeError, ValueError):
                last_seq = None
        await pipeline.end_session(room_id, session_id, flush_s=flush_s, last_seq=last_seq)
        await asyncio.to_thread(store.flush)
        book.set_session_active(room_id, False)
        return {"ok": True}

    @app.post("/api/segment/missing")
    async def segment_missing(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        if book.get(room_id) is None:
            ensure_room(room_id)
        segment = pipeline.mark_missing(room_id, session_id, seq, str(body.get("reason") or "主持端放棄這段"))
        return {"ok": True, **segment.public()}

    @app.post("/api/segment/cancel")
    async def segment_cancel(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        try:
            segment = pipeline.request_cancel(room_id, session_id, seq)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status == "cancelled", **segment.public()}

    @app.post("/api/segment/retranslate")
    async def retranslate(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        zh = body.get("zh")
        try:
            segment = await pipeline.retranslate(room_id, session_id, seq, zh if isinstance(zh, str) else None)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status != "error", **segment.public()}

    @app.post("/api/glossary")
    async def set_glossary(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or "default"))
        rows = parse_glossary(str(body.get("text") or ""))
        pipeline.glossary[(room_id, session_id)] = rows
        return {"ok": True, "count": len(rows)}

    @app.post("/api/share-host")
    async def set_share_host(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        share_override["host"] = str(body.get("host") or "").strip()
        room_id = str(body.get("room_id") or "class")
        return {
            "ok": True,
            "listen_url": share_for(room_id, include_key=True),
            "listen_key": _listen_key_of(room_id),
        }

    @app.get("/api/metrics")
    async def metrics(request: Request) -> dict:
        require_host(request, token, settings)
        return {
            **pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "rooms": book._active_count(),
            "rss_bytes": rss_bytes(),
            "tokens_used": translator.tokens_used,
            "price": translator.price_note(),
            "store_errors": store.errors,
            "storage_recovered": bool(getattr(store, "recovered", False)),
        }

    @app.get("/api/export")
    async def export(request: Request, room_id: str, kind: str = "txt") -> Response:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        if kind not in {"txt", "json", "srt", "vtt"}:
            raise HTTPException(status_code=400, detail="不支援的匯出格式")
        try:
            if store.enabled:
                events = await asyncio.to_thread(store.room_rows, room_id)
            else:
                events = bus.caption_state(room_id)
            payload = export_text(events, kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media = "application/json" if kind == "json" else "text/plain; charset=utf-8"
        return Response(payload, media_type=media, headers={"Cache-Control": "no-store"})

    @app.delete("/api/captions")
    async def delete_captions(request: Request, room_id: str, id: str = "", session_id: str = "", seq: int = 0) -> dict:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        if id or session_id or seq:
            segment_id, parsed_session, parsed_seq = _caption_target(room_id, id, session_id, seq)
            await ensure_hydrated(room_id)
            known = pipeline.caption_known(room_id, parsed_session, parsed_seq) or bus.has_caption(room_id, segment_id)
            if not known and store.enabled:
                try:
                    known = await asyncio.to_thread(store.has_id, room_id, segment_id)
                except Exception as exc:
                    logging.getLogger("breeze.server").exception("caption lookup failed")
                    raise HTTPException(status_code=503, detail="字幕儲存暫時無法讀取，沒有刪除") from exc
            if not known:
                raise HTTPException(status_code=404, detail="找不到這段字幕")
            # Seal before the store delete so a late emit cannot save the row again.
            # Memory and the bus stay until the store accepts the delete. A raised
            # store error must not report success or hide a row that will replay.
            pipeline.brace_delete(room_id, parsed_session, parsed_seq)
            pending = store.enqueue_delete_id(room_id, segment_id)
            try:
                removed = await asyncio.wrap_future(pending)
            except Exception:
                pipeline.abort_delete(room_id, parsed_session, parsed_seq)
                logging.getLogger("breeze.server").exception("caption store delete failed")
                return JSONResponse(
                    status_code=503,
                    content={"ok": False, "detail": "字幕儲存暫時無法刪除，畫面上的字幕還留著"},
                )
            pipeline.delete_segment(room_id, parsed_session, parsed_seq)
            event = bus.delete_caption(room_id, segment_id, parsed_session, parsed_seq)
            _fanout(event)
            return {"ok": True, "deleted": int(removed or 0), "id": segment_id}
        pipeline.mute_room(room_id)
        pending = store.enqueue_delete_room(room_id)
        try:
            removed = await asyncio.wrap_future(pending)
        except Exception:
            pipeline.unmute_room(room_id, abort=True)
            logging.getLogger("breeze.server").exception("caption store delete failed")
            return JSONResponse(
                status_code=503,
                content={"ok": False, "detail": "字幕儲存暫時無法刪除，畫面上的字幕還留著"},
            )
        pipeline.unmute_room(room_id)
        pipeline.invalidate_room(room_id)
        event = bus.clear_room(room_id)
        _fanout(event)
        room = book.get(room_id)
        if room is not None:
            room["history"] = []
        return {"ok": True, "deleted": int(removed or 0)}

    @app.post("/api/push")
    async def push(request: Request) -> dict:
        require_host(request, token, settings)
        if _content_too_large(request, settings):
            raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        query_key = _early_key(request)
        reserved = False
        reserved_key = None
        if not (query_key and pipeline.joinable_without_slot(query_key)):
            if not pipeline.try_admit_count():
                raise HTTPException(status_code=429, detail="辨識佇列已滿，請稍後再送")
            reserved = True
            if query_key:
                pipeline.note_reserved(query_key)
                reserved_key = query_key
        form = None
        try:
            form = await _push_form(request, settings)
            try:
                room_id = validate_room_id(_field(form, request, "room_id"))
                session_id = validate_session_id(_field(form, request, "session_id"))
            except RoomIdError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                seq = int(_field(form, request, "seq"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="段落序號不正確") from exc
            if seq < 1 or seq > 100000:
                raise HTTPException(status_code=400, detail="段落序號不正確")
            key = (room_id, session_id, seq)
            # A speculative slot must not stack on a segment someone else already owns.
            if reserved and reserved_key != key and pipeline.joinable_without_slot(key):
                pipeline.release_slot()
                if reserved_key:
                    pipeline.clear_reserved(reserved_key)
                reserved = False
                reserved_key = None
            elif reserved_key and reserved_key != key:
                pipeline.clear_reserved(reserved_key)
                pipeline.note_reserved(key)
                reserved_key = key
            elif reserved and reserved_key is None:
                pipeline.note_reserved(key)
                reserved_key = key
            ensure_room(room_id)
            await ensure_hydrated(room_id)
            retry = _field(form, request, "retry") == "1" or request.headers.get("x-breeze-retry") == "1"
            t0_ms = _optional_ms(form, request, "t0_ms")
            t1_ms = _optional_ms(form, request, "t1_ms")
            # Both ends are present: keep a positive duration. Equal ends (and a
            # negative span that clamped to the same instant) would export a cue
            # with end == start, which is not a valid subtitle interval.
            if t0_ms is not None and t1_ms is not None and t1_ms <= t0_ms:
                if t0_ms >= _MAX_SEGMENT_MS:
                    t0_ms = _MAX_SEGMENT_MS - 1
                t1_ms = t0_ms + 1
            segment = Segment(
                room_id=room_id,
                session_id=session_id,
                seq=seq,
                t0_ms=t0_ms,
                t1_ms=t1_ms,
            )
            held = reserved
            reserved = False
            try:
                raw = await _read_upload(form.get("audio"), settings.max_audio_bytes)
            except AudioError as exc:
                pipeline.fail_received(segment, exc.detail)
                if held:
                    pipeline.release_slot()
                    held = False
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            if not held and (key in pipeline._reserved or key in pipeline._active):
                await _wait_until_join_ready(pipeline, key, pipeline._result_wait_s())
            # Default still waits for English. Opt in to return at Chinese: form
            # wait_translation=0 and/or header x-breeze-async-translation: 1.
            opt_out = _field(form, request, "wait_translation").strip().lower() in {"0", "false"}
            if request.headers.get("x-breeze-async-translation") == "1":
                opt_out = True
            try:
                done = await pipeline.submit(
                    segment, raw, decode, slot_held=held, retry=retry, owner=held, wait_translation=not opt_out,
                )
            except AudioError as exc:
                recorded = pipeline.get(segment.room_id, segment.session_id, segment.seq)
                if recorded is not None and recorded.status in {"error", "timeout", "cancelled"}:
                    body = recorded.public()
                    body["ok"] = False
                    body["detail"] = exc.detail or recorded.error
                    # AudioError keeps its own status. _public_result would turn every error into 422.
                    return JSONResponse(status_code=exc.status, content=body)
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            except PipelineError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            return _public_result(done)
        finally:
            if reserved:
                pipeline.release_slot()
            if reserved_key:
                pipeline.clear_reserved(reserved_key)
            if form is not None:
                await form.close()

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket, room_id: str = "class", cursor: int = 0, replay: int = 0, k: str = "") -> None:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError:
            await ws.close(code=1008)
            return
        # Origin and the listen key are checked before accept, so a rejected
        # socket never receives hello or a caption.
        if not audience_origin_allowed(
            ws.headers.get("origin"),
            ws.headers.get("host", ""),
            settings,
            _audience_extra_hosts(),
        ):
            await ws.close(code=1008)
            return
        room = book.get(room_id)
        if room is not None and not _listener_authorized(ws, room, k):
            await ws.close(code=4401)
            return
        await ws.accept()
        if room is None:
            await ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "unknown_or_ended"})
            await ws.close(code=4404)
            return
        if len(room["listeners"]) >= settings.max_listeners:
            await ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "full"})
            await ws.close(code=1013)
            return
        conn = Conn(ws, settings.listener_queue)
        room["listeners"].add(conn)
        await ensure_hydrated(room_id)
        resumed = bus.since(room_id, cursor)
        history = bus.history(room_id) if cursor <= 0 else []
        events = list(resumed["events"]) if cursor > 0 else []
        hello = {
            "type": "hello",
            "history": _audience_captions(_captions_since_open(room_id, history)),
            "events": _audience_captions(_captions_since_open(room_id, events)),
            "gap": bool(resumed["gap"]) if cursor > 0 else False,
            "latest_cursor": bus.latest_cursor(room_id),
            "oldest_cursor": resumed["oldest_cursor"],
            "room_id": room_id,
            "epoch": bus.epoch(room_id),
        }
        wants_backfill = int(replay or 0) == 1 or (cursor > 0 and bool(resumed.get("gap")))
        if wants_backfill:
            if int(replay or 0) == 1:
                source = bus.caption_state(room_id)
            else:
                source = list(resumed.get("backfill") or [])
            visible = _captions_since_open(room_id, source)
            ip = ws.client.host if ws.client is not None else ""
            if replay_gate.allow(ip):
                hello["backfill"] = _audience_captions(visible)
        try:
            await ws.send_json(hello)
        except Exception:
            room["listeners"].discard(conn)
            return
        conn.slot.start()
        ping_task = asyncio.create_task(_ping(conn, settings))
        try:
            while True:
                if cancellation_pending():
                    raise asyncio.CancelledError()
                try:
                    raw = await wait_bounded(ws.receive_text(), settings.idle_timeout_s)
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    raise
                if cancellation_pending():
                    raise asyncio.CancelledError()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "pong":
                    conn.slot.last_pong = time.monotonic()
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            room["listeners"].discard(conn)
            await conn.slot.close()

    _TRACKED.append(app)
    return app


def _caption_target(room_id: str, caption_id: str, session_id: str, seq: int) -> tuple[str, str, int]:
    if caption_id:
        prefix = room_id + ":"
        if not str(caption_id).startswith(prefix):
            raise HTTPException(status_code=400, detail="段落不屬於這個房間")
        session, sep, seq_text = str(caption_id)[len(prefix):].rpartition(":")
        if not sep:
            raise HTTPException(status_code=400, detail="段落代號不正確")
        try:
            parsed = int(seq_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="段落序號不正確") from exc
        if parsed < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        return str(caption_id), validate_session_id(session), parsed
    if session_id and seq:
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        session = validate_session_id(session_id)
        return f"{room_id}:{session}:{seq}", session, seq
    raise HTTPException(status_code=400, detail="刪除單段需要 id 或 session_id 與 seq")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="需要 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="需要 JSON 物件")
    return body


async def _ping(conn: Conn, settings: Settings) -> None:
    while True:
        await asyncio.sleep(max(settings.heartbeat_s, 0.05))
        if time.monotonic() - conn.slot.last_pong > settings.idle_timeout_s:
            try:
                await conn.ws.close(code=1001)
            except Exception:
                pass
            return
        if not conn.slot.offer({"type": "ping", "t": time.time()}):
            return


async def shutdown_tracked() -> None:
    while _TRACKED:
        app = _TRACKED.pop()
        shutdown = getattr(app.state, "shutdown", None)
        if shutdown:
            await shutdown()


app = create_app()
