"""LiveRoomClient: the admin process talking to the live room (127.0.0.1:8780).

Rules (backend-review §2.3)
- The live host token lives in this object's memory only: never written to disk/DB, never
  returned to the browser, never logged, masked in repr. Refetched once on a 401 (the live
  room makes a new token on every restart).
- No proxies (ProxyHandler({})), no redirects, no Origin header, Host fixed to the target.
- Only loopback URLs, never port 8645 (guard_outbound_url).
- Per-call timeouts; only idempotent GETs are retried once; a circuit breaker opens after 3
  consecutive failures for 10 s.
- retranslate is synchronous on the live side (returns the segment payload, not "queued").
- A glossary PUT replaces the whole room glossary and needs if_version.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.admin.security import guard_outbound_url

DEFAULT_LIVE_URL = "http://127.0.0.1:8780"


class LiveDown(RuntimeError):
    """Live room unreachable or breaker open."""


class LiveError(RuntimeError):
    def __init__(self, status: int, body):
        super().__init__(f"live returned {status}")
        self.status, self.body = status, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


class Breaker:
    def __init__(self, fail_max: int = 3, reset_s: float = 10.0, clock=time.monotonic):
        self.fail_max, self.reset_s, self._clock = fail_max, reset_s, clock
        self.fails = 0
        self.opened_at: float | None = None

    @property
    def open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._clock() - self.opened_at >= self.reset_s:
            self.opened_at = None
            self.fails = 0
            return False
        return True

    def ok(self) -> None:
        self.fails = 0
        self.opened_at = None

    def fail(self) -> None:
        self.fails += 1
        if self.fails >= self.fail_max:
            self.opened_at = self._clock()


class LiveRoomClient:
    def __init__(self, base: str = DEFAULT_LIVE_URL, *, opener=None, clock=time.monotonic, sleeper=time.sleep):
        self.base = guard_outbound_url(base)
        parts = urlsplit(self.base)
        self._host_header = parts.netloc
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self.breaker = Breaker(clock=clock)
        self._sleep = sleeper
        self.token_fetches = 0

    def __repr__(self) -> str:
        return f"LiveRoomClient(base={self.base!r}, token={'***' if self._token else None})"

    # -------------------------------------------------------------- raw HTTP
    def _send(self, method: str, path: str, body=None, headers=None, timeout: float = 3.0):
        if not path.startswith("/"):
            raise ValueError("path must be absolute")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Host", self._host_header)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            status = exc.code
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise LiveDown(str(getattr(exc, "reason", exc))[:120]) from exc
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return status, parsed

    def _fetch_token(self) -> str:
        with self._token_lock:
            if self._token is None:
                status, data = self._send("GET", "/api/host-token", timeout=2.0)
                self.token_fetches += 1
                if status != 200 or not isinstance(data, dict) or not data.get("token"):
                    raise LiveDown(f"host-token unavailable ({status})")
                self._token = str(data["token"])
            return self._token

    def forget_token(self) -> None:
        with self._token_lock:
            self._token = None

    # -------------------------------------------------------------- authenticated call
    def call(self, method: str, path: str, *, body=None, timeout: float = 3.0, idempotent: bool | None = None):
        """Return (status, json). Raises LiveDown when unreachable or the breaker is open."""
        if self.breaker.open:
            raise LiveDown("breaker open")
        idempotent = (method.upper() == "GET") if idempotent is None else idempotent
        attempts = 2 if idempotent else 1
        refreshed = False
        last_exc: Exception | None = None
        attempt = 0
        while attempt < attempts:
            try:
                token = self._fetch_token()
                status, data = self._send(method, path, body=body, timeout=timeout,
                                          headers={"Authorization": f"Bearer {token}"})
            except LiveDown as exc:
                last_exc = exc
                attempt += 1
                if attempt < attempts:
                    self._sleep(0.2 + random.random() * 0.1)
                continue
            if status == 401 and not refreshed:
                # Live room restarted: new token. Refetch exactly once, then resend.
                refreshed = True
                self.forget_token()
                continue
            self.breaker.ok()
            return status, data
        self.breaker.fail()
        raise LiveDown(str(last_exc) if last_exc else "live unavailable")

    # -------------------------------------------------------------- typed helpers
    def metrics(self) -> dict:
        status, data = self.call("GET", "/api/metrics", timeout=3.0)
        if status != 200 or not isinstance(data, dict):
            raise LiveError(status, data)
        return data

    def room_glossary(self, room_id: str) -> dict:
        status, data = self.call("GET", f"/api/rooms/{room_id}/glossary", timeout=3.0)
        if status != 200 or not isinstance(data, dict):
            raise LiveError(status, data)
        return data

    def put_room_glossary(self, room_id: str, terms: list[dict], if_version: int):
        # Not retried automatically: if_version makes a blind retry unsafe to interpret.
        return self.call("PUT", f"/api/rooms/{room_id}/glossary", body={"if_version": int(if_version), "terms": terms},
                         timeout=5.0, idempotent=False)

    def retranslate(self, room_id: str, session_id: str, seq: int, zh: str | None = None):
        body = {"room_id": room_id, "session_id": session_id, "seq": int(seq)}
        if zh is not None:
            body["zh"] = zh
        # Synchronous on the live side (runs the LLM): long read timeout, never retried.
        return self.call("POST", "/api/segment/retranslate", body=body, timeout=30.0, idempotent=False)
