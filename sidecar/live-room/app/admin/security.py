"""Admin backend protections for a loopback-only console (backend-review §2, P0).

Layers
1. run.py binds 127.0.0.1 (or ::1) only; 8645 (Hermes) is refused everywhere.
2. LoopbackOnly (pure ASGI, http + websocket):
   - TCP peer must be loopback (IPv4-mapped ::ffff:127.0.0.1 is unwrapped first);
   - Host must match exactly, *including the port* ({127.0.0.1,localhost,[::1]}:PORT).
     Starlette TrustedHostMiddleware is deliberately not used: it drops the port;
   - missing Host or an absolute-form request target -> 400;
   - unsafe methods: an Origin, when present, must match exactly (scheme+host+port) and
     Sec-Fetch-Site same-site/cross-site is refused (that is 8780 or 8645 calling);
   - websocket handshakes must carry an allowed Origin;
   - no CORS headers are ever sent; CSP / nosniff / no-referrer / no-store are added.
3. Auth (server.py): browser = one-time #code login -> HttpOnly SameSite=Strict session
   cookie + session-bound synchronizer CSRF token; CLI = Bearer token whose sha256 only is
   stored on disk. Cookie writes additionally require Origin and Sec-Fetch-Site: same-origin.
4. Outbound calls only to loopback, never to port 8645 (guard_outbound_url).
5. RedactingFilter keeps Authorization / Cookie / tokens / codes out of logs.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("zen.admin")

RESERVED_PORTS = frozenset({8645})   # Hermes gateway. Never bind, proxy or call it.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
COOKIE_NAME = "zen_admin_sid"
SESSION_IDLE_S = 8 * 3600
LOGIN_CODE_TTL_S = 300
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


_SECRET_QUERY = re.compile(r"(?:^|&)(?:token|access_token|auth|authorization|key|api_key|sid|code)=")


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset(f"http://{h}" for h in allowed_hosts(port))


def is_loopback_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip("[]"))
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


def _problem_body(status: int, code: str, detail: str) -> bytes:
    return json.dumps({"type": f"https://zen-bridge.local/problems/{code}", "title": code,
                       "status": status, "detail": detail, "code": code}, ensure_ascii=False).encode()


def security_headers(path: str) -> list[tuple[bytes, bytes]]:
    headers = [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"),
               (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
               (b"cross-origin-resource-policy", b"same-origin")]
    if path.startswith("/admin/api"):
        headers.append((b"cache-control", b"no-store"))
    return headers


class LoopbackOnly:
    """Outermost pure ASGI middleware (no BaseHTTPMiddleware; streaming-safe)."""

    def __init__(self, app, port: int):
        self.app = app
        self.port = port
        self.hosts = allowed_hosts(port)
        self.origins = allowed_origins(port)

    def check(self, scope) -> tuple[int, str, str] | None:
        client = scope.get("client") or ("", 0)
        headers: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
        if not is_loopback_ip(str(client[0] or "")):
            return 403, "forbidden", "後台只接受本機連線"
        raw_path = scope.get("raw_path") or scope.get("path", "").encode()
        if not raw_path.startswith(b"/"):
            return 400, "bad_request", "不接受 absolute-form 請求目標"
        query = (scope.get("query_string") or b"").decode("latin-1").lower()
        if _SECRET_QUERY.search(query):
            return 400, "bad_request", "權杖不可放在網址（請用 Cookie 或 Authorization 標頭）"
        host = headers.get("host")
        if not host:
            return 400, "bad_request", "缺少 Host"
        if host.lower() not in self.hosts:
            return 403, "forbidden", "Host 不在白名單（需含正確埠號）"
        origin = headers.get("origin")
        if scope["type"] == "websocket":
            if origin is None or origin.lower() not in self.origins:
                return 403, "forbidden", "WebSocket Origin 不在白名單"
            return None
        if scope.get("method", "GET").upper() not in SAFE_METHODS:
            if origin is not None and origin.lower() not in self.origins:
                return 403, "forbidden", "Origin 不在白名單（需含正確埠號）"
            site = headers.get("sec-fetch-site")
            if site is not None and site != "same-origin":
                return 403, "forbidden", f"拒絕 Sec-Fetch-Site: {site}"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        problem = self.check(scope)
        if problem is not None:
            status, code, detail = problem
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/problem+json")] + security_headers("/admin/api")})
            await send({"type": "http.response.body", "body": _problem_body(status, code, detail)})
            return
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers") or []}
                extra = [(k, v) for k, v in security_headers(path) if k not in existing]
                # Never emit CORS: strip any Access-Control-* a handler might add.
                kept = [(k, v) for k, v in (message.get("headers") or []) if not k.lower().startswith(b"access-control-")]
                message = {**message, "headers": kept + extra}
            await send(message)

        return await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- tokens
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_token_hash(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token_hash(token) + "\n", encoding="utf-8")
    restrict_file(path)


def read_token_hash(path: Path) -> str | None:
    """admin.token stores only sha256(token). ZEN_ADMIN_TOKEN (plain) overrides for tests/CI."""
    env_token = (os.getenv("ZEN_ADMIN_TOKEN") or "").strip()
    if env_token:
        return token_hash(env_token)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return text
    return None


def restrict_file(path: Path) -> None:
    """chmod 600 on POSIX; icacls (current user only) on Windows. Best effort, logged."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                               check=False, capture_output=True, timeout=10)
        else:
            os.chmod(path, 0o600)
    except Exception:
        log.warning("could not restrict permissions on %s", path, exc_info=True)


class LoginCodes:
    """One-time login codes (memory only, 5 minutes, single use)."""

    def __init__(self, clock=time.time, ttl: float = LOGIN_CODE_TTL_S):
        self._clock = clock
        self._ttl = ttl
        self._codes: dict[str, float] = {}

    def issue(self) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[token_hash(code)] = self._clock() + self._ttl
        return code

    def redeem(self, code: str) -> bool:
        now = self._clock()
        for h, exp in list(self._codes.items()):
            if exp < now:
                self._codes.pop(h, None)
        h = token_hash(code or "")
        exp = self._codes.pop(h, None)
        return exp is not None and exp >= now


class RateLimiter:
    """Tiny in-memory sliding window: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str = "") -> bool:
        now = self._clock()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


def csrf_equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


ROLE_RANK = {"viewer": 0, "host": 1, "editor": 2, "admin": 3, "owner": 4}


def role_allows(role: str, need: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(need, 99)


# ---------------------------------------------------------------- outbound guard
class OutboundBlocked(ValueError):
    pass


def guard_outbound_url(url: str) -> str:
    """Only http://loopback:port, never 8645, no credentials. Raises OutboundBlocked."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError as exc:
        raise OutboundBlocked("網址無法解析") from exc
    if parts.scheme != "http":
        raise OutboundBlocked("只允許 http://（本機）")
    host = (parts.hostname or "").lower()
    if host != "localhost" and not is_loopback_ip(host):
        raise OutboundBlocked(f"只允許連本機，拒絕 {host or '空位址'}")
    if port is None:
        port = 80
    if port in RESERVED_PORTS:
        raise OutboundBlocked(f"埠 {port} 保留給 Hermes，禁止連線")
    if parts.username is not None or parts.password is not None:
        raise OutboundBlocked("網址不能帶帳號密碼")
    return url.rstrip("/")


# ---------------------------------------------------------------- log redaction
_REDACT_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;'\"]+"), r"\1\2***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"), r"\1***"),
    (re.compile(r"(?i)(zen_admin_sid=)[^;\s]+"), r"\1***"),
    (re.compile(r"(?i)(x-zen-(?:csrf|confirm)\s*[:=]\s*)[^\s,;'\"]+"), r"\1***"),
    (re.compile(r"(?i)(\"[^\"]*(?:token|code|ticket|password|secret|key|csrf)[^\"]*\"\s*:\s*)\"[^\"]*\""), r'\1"***"'),
    (re.compile(r"(?i)\b((?:[a-z_]*?)(?:token|ticket|password|secret|api_key|csrf|login_code))(\s*[=:]\s*)[^\s,;&'\"]+"), r"\1\2***"),
    (re.compile(r"#code=[^\s&'\"]+"), "#code=***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
]


def redact(text: str) -> str:
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    """Rewrites message, args and exception text through redact(). Attach to handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    flt = RedactingFilter()
    target = logger or logging.getLogger()
    target.addFilter(flt)
    for handler in target.handlers:
        handler.addFilter(flt)
    return flt


def load_or_create_token(path: Path) -> tuple[str, str | None]:
    """Return (sha256 hash, plain token or None). Plain is only returned when newly created.

    The plain token is shown once on the console; only its hash is ever written to disk.
    """
    existing = read_token_hash(path)
    if existing:
        return existing, None
    token = secrets.token_urlsafe(32)
    write_token_hash(path, token)
    return token_hash(token), token


# ---------------------------------------------------------------- browser sessions
class SessionStore:
    """HttpOnly cookie sessions. The DB keeps sha256(sid) and a per-session CSRF token."""

    def __init__(self, connect, clock=time.time, idle_s: float = SESSION_IDLE_S):
        self._connect = connect      # () -> sqlite3.Connection on the main DB
        self._clock = clock
        self.idle_s = idle_s

    def create(self, role: str = "owner", user_id: int | None = None) -> tuple[str, str]:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._clock()
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
            c.execute("INSERT INTO admin_sessions(sid_hash, user_id, role, csrf_token, created_at, last_seen_at, expires_at)"
                      " VALUES (?,?,?,?,?,?,?)", (token_hash(sid), user_id, role, csrf, now, now, now + self.idle_s))
            c.execute("COMMIT")
        finally:
            c.close()
        return sid, csrf

    def lookup(self, sid: str) -> dict | None:
        if not sid:
            return None
        now = self._clock()
        c = self._connect()
        try:
            row = c.execute("SELECT sid_hash, user_id, role, csrf_token, expires_at FROM admin_sessions WHERE sid_hash=?",
                            (token_hash(sid),)).fetchone()
            if not row or row[4] < now:
                return None
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE admin_sessions SET last_seen_at=?, expires_at=? WHERE sid_hash=?",
                      (now, now + self.idle_s, row[0]))
            c.execute("COMMIT")
            return {"role": row[2], "user_id": row[1], "csrf": row[3], "via": "session"}
        finally:
            c.close()

    def drop(self, sid: str) -> None:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM admin_sessions WHERE sid_hash=?", (token_hash(sid or ""),))
            c.execute("COMMIT")
        finally:
            c.close()
