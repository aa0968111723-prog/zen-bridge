"""示意圖 V2: independent broadcast channel + FastAPI router for visual drafts.

This module is deliberately separate from the caption path:

* ``VisualChannel`` has its own per-room subscriber sets and its own replay
  history. It only carries ``visual_*`` messages, so a caption event can never
  leak onto it (and visual drafts never reach ``/ws/listen``).
* Every subscriber has a bounded queue. When a slow viewer falls behind, the
  oldest pending message is dropped (drop-oldest); ``publish`` never blocks and
  never awaits a socket.
* ``VisualHub`` owns one ``app.visual.VisualGenerator`` per room (trigger,
  per-room cooldown, normalized de-dupe, LLM with timeout + fallback). ``feed``
  is cheap, synchronous and thread-safe, so the caption fan-out only pays for a
  dict check and a ``call_soon_threadsafe``.
* The LLM is injected. ``VisualHub.from_env`` uses ``app.visual.build_llm_from_env``
  which is local-only (loopback base URL) and disabled when nothing is set.
* ``build_visual_router`` serves ``/visual`` (viewer page), ``/visual/visual.js``,
  ``GET /api/visual/{room_id}``, ``POST /api/visual/{room_id}/trigger`` and the
  WebSocket ``/ws/visual?room_id=...``.

server.py is not touched here; see NOTES.md for the mounting lines.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import threading
from collections import deque
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app import visual as V
from app.rooms import RoomIdError, validate_room_id

log = logging.getLogger("breeze.visual.routes")

STATIC = Path(__file__).resolve().parent / "static"
CHANNEL_TYPES = frozenset({"visual_draft", "visual_hello"})
DEFAULT_SUBSCRIBER_QUEUE = 8
DEFAULT_HISTORY = 5
DEFAULT_MAX_SUBSCRIBERS = 64
_NO_STORE = {"Cache-Control": "no-store"}


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

class VisualSubscriber:
    """One viewer. Bounded queue, drop-oldest. Used from the event loop only."""

    def __init__(self, room_id: str, maxsize: int = DEFAULT_SUBSCRIBER_QUEUE):
        self.room_id = room_id
        self.maxsize = max(1, int(maxsize))
        self._queue: deque[dict] = deque()
        self._ready = asyncio.Event()
        self.dropped = 0
        self.delivered = 0
        self.closed = False

    def offer(self, message: dict) -> bool:
        if self.closed:
            return False
        if len(self._queue) >= self.maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(message)
        self._ready.set()
        return True

    @property
    def pending(self) -> int:
        return len(self._queue)

    def get_nowait(self) -> dict | None:
        if not self._queue:
            return None
        self.delivered += 1
        return self._queue.popleft()

    async def get(self) -> dict | None:
        """Next message, or None once closed and drained."""
        while not self._queue:
            if self.closed:
                return None
            self._ready.clear()
            await self._ready.wait()
        return self.get_nowait()

    def close(self) -> None:
        self.closed = True
        self._ready.set()


class VisualChannel:
    """Per-room fan-out for visual messages only. Separate from RoomBus."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_SUBSCRIBER_QUEUE,
        history: int = DEFAULT_HISTORY,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
    ):
        self.queue_size = queue_size
        self.history_size = max(0, int(history))
        self.max_subscribers = max_subscribers
        self._subs: dict[str, set[VisualSubscriber]] = {}
        self._history: dict[str, deque[dict]] = {}
        self.published = 0
        self.rejected = 0

    def subscribe(self, room_id: str) -> VisualSubscriber | None:
        subs = self._subs.setdefault(room_id, set())
        if len(subs) >= self.max_subscribers:
            return None
        sub = VisualSubscriber(room_id, self.queue_size)
        subs.add(sub)
        return sub

    def unsubscribe(self, sub: VisualSubscriber) -> None:
        sub.close()
        subs = self._subs.get(sub.room_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sub.room_id, None)

    def subscriber_count(self, room_id: str) -> int:
        return len(self._subs.get(room_id, ()))

    def history(self, room_id: str) -> list[dict]:
        return [dict(item) for item in self._history.get(room_id, ())]

    def publish(self, room_id: str, message: Mapping) -> int:
        """Fan out one visual message. Returns the number of subscribers offered.

        Raises ValueError for anything that is not a ``visual_*`` message of this
        room, so the caption stream cannot be mixed into this channel.
        """
        if not isinstance(message, Mapping) or message.get("type") not in CHANNEL_TYPES:
            self.rejected += 1
            raise ValueError("visual channel only carries visual_* messages")
        if message.get("room_id") not in (None, room_id):
            self.rejected += 1
            raise ValueError("message belongs to another room")
        payload = json.loads(json.dumps(dict(message), ensure_ascii=False))  # detached, JSON-safe copy
        if payload["type"] == "visual_draft" and self.history_size:
            bucket = self._history.setdefault(room_id, deque(maxlen=self.history_size))
            bucket.append(payload)
        self.published += 1
        count = 0
        for sub in list(self._subs.get(room_id, ())):
            if sub.offer(payload):
                count += 1
        return count

    def drop_room(self, room_id: str) -> None:
        for sub in list(self._subs.pop(room_id, ())):
            sub.close()
        self._history.pop(room_id, None)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

LLMFactory = Callable[[], Optional[V.VisualLLM]]


class VisualHub:
    """Rooms -> VisualGenerator, wired to a VisualChannel."""

    def __init__(
        self,
        llm: V.VisualLLM | None,
        *,
        config: V.VisualConfig | None = None,
        glossary_provider: V.GlossaryProvider | None = None,
        clock: Callable[[], float] | None = None,
        channel: VisualChannel | None = None,
    ):
        self.llm = llm
        self.config = config or V.VisualConfig()
        self.channel = channel or VisualChannel()
        self._glossary_provider = glossary_provider
        self._clock = clock
        self._generators: dict[str, V.VisualGenerator] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.suppressed = 0
        self.broadcast = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs) -> "VisualHub":
        config = kwargs.pop("config", None) or V.VisualConfig.from_env(env)
        return cls(V.build_llm_from_env(env, config), config=config, **kwargs)

    @property
    def enabled(self) -> bool:
        return self.llm is not None

    # -- loop handling -----------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = loop or asyncio.get_running_loop()

    async def start(self) -> None:
        self.bind_loop(asyncio.get_running_loop())

    def _on_loop(self) -> bool:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._loop is None:
            self.bind_loop(running)
        return running is self._loop

    def _dispatch(self, fn: Callable[[], Any]) -> bool:
        """Run fn on the hub loop: inline when already there, else thread-safe."""
        if self._on_loop():
            fn()
            return True
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:
            return False
        return True

    # -- generators ----------------------------------------------------------
    def _generator(self, room_id: str) -> V.VisualGenerator:
        gen = self._generators.get(room_id)
        if gen is None:
            gen = V.VisualGenerator(
                room_id,
                self.llm,
                config=self.config,
                glossary_provider=self._glossary_provider,
                on_draft=partial(self._on_draft, room_id),
                clock=self._clock,
            )
            gen.start()
            self._generators[room_id] = gen
        return gen

    def generator(self, room_id: str) -> V.VisualGenerator | None:
        return self._generators.get(room_id)

    def _on_draft(self, room_id: str, draft: dict) -> None:
        if not isinstance(draft, dict) or draft.get("room_id") != room_id or not V.is_presentable(draft):
            self.suppressed += 1
            log.info("visual: draft for %s suppressed (not presentable)", room_id)
            return
        try:
            self.channel.publish(room_id, draft)
            self.broadcast += 1
        except ValueError:
            self.suppressed += 1

    # -- public API ------------------------------------------------------------
    def feed(self, event: Mapping) -> bool:
        """Offer a caption event (bus snapshot / Segment.public()). Thread-safe, non-blocking.

        Only ``final`` events with a valid room id are considered.
        """
        if not self.enabled or not isinstance(event, Mapping) or event.get("type") != "final":
            return False
        try:
            room_id = validate_room_id(str(event.get("room_id") or ""))
        except RoomIdError:
            return False
        if V.caption_from_event(event) is None:
            return False
        snapshot = dict(event)
        return self._dispatch(lambda: self._generator(room_id).feed(snapshot))

    def trigger(self, room_id: str, force: bool = False) -> bool:
        """Manual host trigger. Must be called on the hub loop (router does)."""
        if not self.enabled:
            return False
        if not self._on_loop():
            return self._dispatch(lambda: self._generator(room_id).trigger(force=force))
        return self._generator(room_id).trigger(force=force)

    async def drain(self, room_id: str | None = None) -> None:
        gens = [self._generators[room_id]] if room_id in self._generators else (
            [] if room_id is not None else list(self._generators.values())
        )
        for gen in gens:
            await gen.drain()

    async def close_room(self, room_id: str) -> None:
        gen = self._generators.pop(room_id, None)
        if gen is not None:
            await gen.stop()
        self.channel.drop_room(room_id)

    async def stop(self) -> None:
        for room_id in list(self._generators):
            gen = self._generators.pop(room_id)
            await gen.stop()
        close = getattr(self.llm, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.warning("visual: llm close failed", exc_info=True)

    def stats(self, room_id: str) -> dict:
        gen = self._generators.get(room_id)
        return {
            "enabled": self.enabled,
            "subscribers": self.channel.subscriber_count(room_id),
            "skipped": dict(gen.trigger_state.skipped) if gen else {"cooldown": 0, "duplicate": 0, "empty": 0},
            "dropped_jobs": gen.dropped if gen else 0,
        }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

Guard = Callable[..., Union[None, bool, Awaitable[Union[None, bool]]]]


def _is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_only_host_guard(request: Request) -> None:
    """Default guard for the manual trigger: only this machine may trigger."""
    client = request.client.host if request.client else None
    if not _is_loopback_client(client):
        raise HTTPException(status_code=403, detail="visual trigger is local-only")


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _room(raw: str) -> str:
    try:
        return validate_room_id(raw)
    except RoomIdError:
        raise HTTPException(status_code=400, detail="invalid room id")


def build_visual_router(
    hub: VisualHub,
    *,
    host_guard: Guard | None = local_only_host_guard,
    ws_guard: Guard | None = None,
    static_dir: Path = STATIC,
) -> APIRouter:
    """Router for the visual channel.

    ``host_guard(request)`` protects the manual trigger (raise HTTPException or
    return False to refuse). ``ws_guard(websocket, room_id)`` may refuse a viewer
    (return False), e.g. by reusing the room's listen-key check.
    """
    router = APIRouter()
    static_dir = Path(static_dir)

    @router.get("/visual", include_in_schema=False)
    async def visual_page():
        return FileResponse(static_dir / "visual.html", media_type="text/html", headers=_NO_STORE)

    @router.get("/visual/visual.js", include_in_schema=False)
    async def visual_script():
        return FileResponse(static_dir / "visual.js", media_type="application/javascript", headers=_NO_STORE)

    @router.get("/api/visual/{room_id}")
    async def visual_state(room_id: str):
        room_id = _room(room_id)
        hub.bind_loop()
        return {"room_id": room_id, "history": hub.channel.history(room_id), **hub.stats(room_id)}

    @router.post("/api/visual/{room_id}/trigger")
    async def visual_trigger(room_id: str, request: Request, force: int = 0):
        room_id = _room(room_id)
        if host_guard is not None and await _maybe_await(host_guard(request)) is False:
            raise HTTPException(status_code=403, detail="forbidden")
        hub.bind_loop()
        if not hub.enabled:
            return {"room_id": room_id, "accepted": False, "reason": "disabled"}
        accepted = hub.trigger(room_id, force=bool(force))
        return {"room_id": room_id, "accepted": bool(accepted), "reason": None if accepted else "cooldown_duplicate_or_empty"}

    @router.websocket("/ws/visual")
    async def visual_ws(ws: WebSocket, room_id: str = ""):
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError:
            await ws.close(code=1008)
            return
        if ws_guard is not None and await _maybe_await(ws_guard(ws, room_id)) is False:
            await ws.close(code=1008)
            return
        hub.bind_loop()
        sub = hub.channel.subscribe(room_id)
        await ws.accept()
        if sub is None:
            await ws.send_json({"type": "visual_unavailable", "room_id": room_id, "reason": "full"})
            await ws.close(code=1013)
            return
        try:
            await ws.send_json({
                "type": "visual_hello",
                "room_id": room_id,
                "enabled": hub.enabled,
                "history": hub.channel.history(room_id),
            })

            async def pump() -> None:
                while True:
                    message = await sub.get()
                    if message is None:
                        return
                    await ws.send_json(message)

            async def drain_client() -> None:
                while True:
                    incoming = await ws.receive()
                    if incoming.get("type") == "websocket.disconnect":
                        return

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.channel.unsubscribe(sub)
            try:
                await ws.close()
            except Exception:
                pass

    return router
