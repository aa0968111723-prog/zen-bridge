"""Admin backend: create_admin_app(...). Separate process (python -m app.admin.run), 127.0.0.1:8791.

API conventions (backend-review §1.4)
- Prefix /admin/api/v1. Errors are application/problem+json {type,title,status,detail,code}.
- Optimistic locking: GET returns ETag; writes on that resource need If-Match
  (missing -> 428, stale -> 412).
- Long work -> 202 + Location: /admin/api/v1/jobs/{id} (jobs table + one asyncio worker).
- POST creates accept Idempotency-Key (same key + same body -> stored reply,
  same key + other body -> 422).
- Lists use opaque keyset cursors (?cursor=, response "next").
- /docs, /redoc, /openapi.json are disabled.
Auth: browser = one-time #code -> HttpOnly SameSite=Strict cookie + session-bound CSRF;
CLI = Bearer admin token (only its sha256 is on disk) or an identity-DB api token.
No credential is ever accepted from a query string (SSE included).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import socket
import sqlite3
import time
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import feedback
from app.admin import db as zdb
from app.admin import search as zsearch
from app.admin.jobs import KINDS, JobStore, JobWorker, backup_handler
from app.admin.live_client import LiveDown, LiveRoomClient
from app.admin.security import (COOKIE_NAME, RESERVED_PORTS, SAFE_METHODS, LoginCodes, LoopbackOnly, RateLimiter,
                                SessionStore, allowed_origins, csrf_equal, install_redaction, role_allows,
                                token_hash)

log = logging.getLogger("zen.admin")
DEFAULT_PORT = 8791
API = "/admin/api/v1"
STATIC = Path(__file__).with_name("static")
OLLAMA_URL = "http://127.0.0.1:11434"
MAX_BODY = 64 * 1024
_IDEM_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
PROBLEM = "application/problem+json"


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


def problem_response(status: int, code: str, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"type": f"https://zen-bridge.local/problems/{code}", "title": code, "status": status,
            "detail": detail, "code": code, **extra}
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


# ------------------------------------------------------------------ cursors / etags
def enc_cursor(values) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, separators=(",", ":")).encode()).decode().rstrip("=")


def dec_cursor(raw: str | None, n: int):
    if not raw:
        return None
    try:
        vals = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        raise Problem(400, "bad_cursor", "cursor 無法解析")
    if not isinstance(vals, list) or len(vals) != n:
        raise Problem(400, "bad_cursor", "cursor 格式不符")
    return vals


def term_etag(term_id: int, rev: int) -> str:
    return f'"t{term_id}-r{rev}"'


def check_if_match(request: Request, current: str) -> None:
    got = request.headers.get("if-match")
    if not got:
        raise Problem(428, "precondition_required", "需要 If-Match（請先 GET 取得 ETag）")
    tags = [t.strip() for t in got.split(",")]
    if current not in tags and "*" not in tags:
        raise Problem(412, "precondition_failed", "資料已被修改，請重新載入", etag=current)


# ------------------------------------------------------------------ probes
def _tcp_probe(host: str, port: int, timeout: float = 0.5) -> str:
    if port in RESERVED_PORTS:
        return "blocked"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "up"
    except OSError:
        return "down"


def _ollama_probe(timeout: float = 1.0) -> str:
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=timeout) as resp:
            return "up" if resp.status == 200 else "down"
    except Exception:
        return "down"


DEFAULT_PROBES = {"live": lambda: _tcp_probe("127.0.0.1", 8780), "ollama": _ollama_probe}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def create_admin_app(db_path: str | Path, *, token_hash_hex: str | None, port: int = DEFAULT_PORT,
                     identity_path: str | Path | None = None, probes: dict | None = None,
                     live_client: LiveRoomClient | None = None, embed_handler=None, backup_dir=None,
                     start_worker: bool = True, embed_scan_s: float = 0.0, sse_interval_s: float = 2.0,
                     sse_max_events: int | None = None, clock=time.time) -> FastAPI:
    if port in RESERVED_PORTS:
        raise ValueError("8645 保留給 Hermes")
    db_path = Path(db_path)
    zdb.migrate(db_path)
    identity_path = Path(identity_path) if identity_path else None
    if identity_path is not None:
        zdb.migrate_identity(identity_path)
    probes = DEFAULT_PROBES if probes is None else probes
    live = live_client or LiveRoomClient()
    store = JobStore(db_path, clock=clock)
    sessions = SessionStore(lambda: zdb.connect(db_path), clock=clock)
    codes = LoginCodes(clock=clock)
    login_limit = RateLimiter(10, 60.0)
    write_limit = RateLimiter(120, 60.0)
    handlers = {"backup": backup_handler(db_path, backup_dir or zdb.backup_dir)}
    if embed_handler is not None:
        handlers["embed_backfill"] = embed_handler
    worker = JobWorker(store, handlers)
    install_redaction(logging.getLogger("uvicorn"))
    install_redaction(logging.getLogger("zen"))

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if start_worker:
            worker.start()
        if embed_scan_s > 0 and "embed_backfill" in handlers:
            tasks.append(asyncio.create_task(_embed_scan(), name="zen-embed-scan"))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await worker.stop()

    async def _embed_scan():
        # After-session / idle embeddings: enqueue (deduped) when rows are waiting. The job
        # itself re-checks the idle gate before every batch.
        from app.embed import pending_transcripts
        model = getattr(embed_handler, "model", None) or "qwen3-embedding:0.6b"
        while True:
            await asyncio.sleep(embed_scan_s)
            try:
                def waiting():
                    with conn() as c:
                        return bool(pending_transcripts(c, model, 1))
                if await asyncio.to_thread(waiting):
                    await asyncio.to_thread(store.create, "embed_backfill", {}, {"model": model})
                    worker.wake()
            except Exception:
                log.exception("embed scan failed")

    app = FastAPI(title="zen-admin", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.worker, app.state.codes, app.state.live = store, worker, codes, live
    app.state.db_path = db_path

    @contextmanager
    def conn(write: bool = False):
        c = zdb.connect(db_path)
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
                try:
                    yield c
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
            else:
                yield c
        finally:
            c.close()

    # -------------------------------------------------------------- auth
    def _identity_lookup(digest: str) -> dict | None:
        if identity_path is None or not identity_path.exists():
            return None
        c = zdb.connect(identity_path, readonly=True)
        try:
            row = c.execute("SELECT user_id, scopes FROM api_tokens WHERE token_hash=? AND revoked_at IS NULL "
                            "AND (expires_at IS NULL OR expires_at > ?)", (digest, clock())).fetchone()
        finally:
            c.close()
        if not row:
            return None
        with conn() as c:
            u = c.execute("SELECT role, disabled FROM users WHERE id=?", (row[0],)).fetchone()
        if not u or u[1]:
            return None
        role = u[0] if "write" in row[1] else "viewer"   # a read-only token never writes
        return {"role": role, "user_id": row[0], "via": "api_token"}

    def authenticate(request: Request) -> dict | None:
        authz = request.headers.get("authorization", "")
        if authz:
            scheme, _, cred = authz.partition(" ")
            if scheme.lower() != "bearer" or not cred.strip():
                return None
            digest = token_hash(cred.strip())
            if token_hash_hex and csrf_equal(digest, token_hash_hex):
                return {"role": "owner", "user_id": None, "via": "bearer"}
            return _identity_lookup(digest)
        sid = request.cookies.get(COOKIE_NAME)
        return sessions.lookup(sid) if sid else None

    origins = allowed_origins(port)

    def need(role: str):
        def dep(request: Request) -> dict:
            user = authenticate(request)
            if not user:
                raise Problem(401, "unauthorized", "需要登入或後台權杖")
            if not role_allows(user.get("role", ""), role):
                raise Problem(403, "forbidden", "權限不足")
            if request.method.upper() not in SAFE_METHODS:
                if not write_limit.allow(user.get("via", "")):
                    raise Problem(429, "rate_limited", "寫入太頻繁，請稍後再試")
                if user["via"] == "session":
                    # Cookie writes: Origin must be present and exact, the browser must say
                    # same-origin, and the CSRF token must be this session's token.
                    if (request.headers.get("origin") or "").lower() not in origins:
                        raise Problem(403, "forbidden", "寫入需要同源 Origin")
                    if request.headers.get("sec-fetch-site") != "same-origin":
                        raise Problem(403, "forbidden", "寫入需要 Sec-Fetch-Site: same-origin")
                    if not csrf_equal(request.headers.get("x-zen-csrf", ""), user.get("csrf", "")):
                        raise Problem(403, "csrf", "缺少或錯誤的 X-Zen-CSRF")
            return user
        return dep

    async def read_json(request: Request) -> tuple[dict, bytes]:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise Problem(413, "too_large", "內容太大")
        if not raw:
            return {}, raw
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Problem(400, "bad_json", "需要 JSON")
        if not isinstance(body, dict):
            raise Problem(400, "bad_json", "需要 JSON 物件")
        return body, raw

    def idem_key(request: Request) -> str | None:
        key = request.headers.get("idempotency-key")
        if key is None:
            return None
        if not _IDEM_RE.match(key):
            raise Problem(400, "bad_idempotency_key", "Idempotency-Key 需 8–128 個英數字元")
        return key

    def idem_replay(c, key: str | None, route: str, raw: bytes) -> JSONResponse | None:
        if key is None:
            return None
        c.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (clock() - 86400,))
        row = c.execute("SELECT body_sha256, status, response_json, headers_json FROM idempotency_keys "
                        "WHERE key=? AND route=?", (key, route)).fetchone()
        if not row:
            return None
        if row[0] != hashlib.sha256(raw).hexdigest():
            raise Problem(422, "idempotency_mismatch", "同一個 Idempotency-Key 用在不同內容")
        headers = {**json.loads(row[3]), "Idempotent-Replayed": "true"}
        return JSONResponse(json.loads(row[2]), status_code=row[1], headers=headers)

    def idem_store(c, key: str | None, route: str, raw: bytes, status: int, body: dict, headers: dict) -> None:
        if key is None:
            return
        c.execute("INSERT INTO idempotency_keys(key, route, body_sha256, status, response_json, headers_json, created_at) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key, route) DO NOTHING",
                  (key, route, hashlib.sha256(raw).hexdigest(), status, json.dumps(body, ensure_ascii=False),
                   json.dumps(headers), clock()))

    # -------------------------------------------------------------- errors
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem):
        return problem_response(exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(feedback.FeedbackError)
    async def _fb(request: Request, exc: feedback.FeedbackError):
        return problem_response(exc.status, exc.code, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return problem_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        errs = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        return problem_response(422, "invalid", "參數不正確", errors=errs)

    @app.exception_handler(LiveDown)
    async def _live_down(request: Request, exc: LiveDown):
        return problem_response(503, "live_unavailable", "直播服務（8780）目前連不上", headers={"Retry-After": "5"})

    # -------------------------------------------------------------- pages (static, CSP script-src 'self')
    def page(name: str):
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})

    @app.get("/admin")
    def index():
        return page("index.html")

    @app.get("/admin/login")
    def login_page():
        return page("login.html")

    @app.get("/admin/static/{name}")
    def static(name: str):
        if name == 'mermaid.min.js':
            return page('vendor/mermaid.min.js')
        if name == 'mermaid-safe.js':
            return page('mermaid-safe.js')
        if name not in {"app.js", "login.js", "app.css"}:
            raise Problem(404, "not_found", "找不到")
        return page(name)

    # -------------------------------------------------------------- auth endpoints
    @app.post(f"{API}/auth/login")
    async def login(request: Request):
        if not login_limit.allow("login"):
            raise Problem(429, "rate_limited", "登入嘗試太多次")
        if (request.headers.get("origin") or "").lower() not in origins:
            raise Problem(403, "forbidden", "登入需要同源 Origin")
        body, _ = await read_json(request)
        if not codes.redeem(str(body.get("code") or "")):
            raise Problem(401, "bad_code", "登入碼無效或已過期（5 分鐘、只能用一次）")
        sid, csrf = await asyncio.to_thread(sessions.create, "owner")
        resp = JSONResponse({"role": "owner", "csrf": csrf})
        resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="strict", path="/admin", max_age=sessions.idle_s)
        return resp

    @app.post(f"{API}/auth/login-code")
    def login_code(user=Depends(need("owner"))):
        if user["via"] == "session":
            raise Problem(403, "forbidden", "登入碼只能用 CLI 權杖產生")
        return {"url": f"http://127.0.0.1:{port}/admin/login#code={codes.issue()}", "expires_in": 300}

    @app.get(f"{API}/auth/session")
    def whoami(user=Depends(need("viewer"))):
        return {"role": user["role"], "via": user["via"], "csrf": user.get("csrf")}

    @app.post(f"{API}/auth/logout")
    def logout(request: Request, user=Depends(need("viewer"))):
        sessions.drop(request.cookies.get(COOKIE_NAME, ""))
        resp = Response(status_code=204)
        resp.delete_cookie(COOKIE_NAME, path="/admin")
        return resp

    # -------------------------------------------------------------- health / metrics
    @app.get(f"{API}/health")
    def health(deep: int = 0, user=Depends(need("viewer"))):
        out = {"ok": True, "db": "ok", "schema_version": None, "sqlite": sqlite3.sqlite_version,
               "worker": worker.state}
        try:
            with conn() as c:
                out["schema_version"] = c.execute("PRAGMA user_version").fetchone()[0]
                if deep:
                    out["fts"] = zdb.fts_integrity(c)
        except Exception as exc:
            out.update(ok=False, db=f"error: {type(exc).__name__}")
        for name, probe in probes.items():
            try:
                out[name] = probe()
            except Exception:
                out[name] = "down"
        return out

    @app.get(f"{API}/metrics")
    def metrics(name: str | None = None, since: float | None = None, room_id: str | None = None,
                user=Depends(need("viewer"))):
        with conn() as c:
            counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("rooms", "sessions", "segments", "transcripts", "translations", "tm_units",
                                "embeddings", "jobs")}
            by_origin = dict(c.execute("SELECT origin, COUNT(*) FROM translations WHERE is_current=1 GROUP BY origin").fetchall())
            out = {
                "counts": counts, "translations_by_origin": by_origin,
                "terms_proposed": c.execute("SELECT COUNT(*) FROM glossary_terms WHERE status='proposed'").fetchone()[0],
                "translations_flagged": c.execute(
                    "SELECT COUNT(*) FROM translations WHERE is_current=1 AND term_flags IS NOT NULL "
                    "AND term_flags NOT IN ('', '[]')").fetchone()[0],
                "segments_silent": c.execute("SELECT COUNT(*) FROM segments WHERE status='silent'").fetchone()[0],
                "ts": clock(),
            }
            if name:
                sql, args = "SELECT ts, value FROM metrics WHERE name=?", [name]
                if since is not None:
                    sql += " AND ts>=?"
                    args.append(since)
                if room_id:
                    sql += " AND room_id=?"
                    args.append(room_id)
                pts = [(r[0], r[1]) for r in c.execute(sql + " ORDER BY ts LIMIT 5000", args)]
                vals = [p[1] for p in pts]
                out["series"] = {"name": name, "points": pts, "p50": _pct(vals, 0.5), "p95": _pct(vals, 0.95)}
        return out

    # -------------------------------------------------------------- sessions / segments
    @app.get(f"{API}/sessions")
    def list_sessions(room_id: str | None = None, status: str | None = None, cursor: str | None = None,
                      limit: int = Query(50, ge=1, le=200), user=Depends(need("viewer"))):
        after = dec_cursor(cursor, 2)
        sql = ("SELECT s.id, s.room_id, s.title, s.started_at, s.ended_at, s.status, "
               "(SELECT COUNT(*) FROM segments g WHERE g.session_id=s.id) AS segments FROM sessions s WHERE 1=1")
        args: list = []
        if room_id:
            sql += " AND s.room_id=?"
            args.append(room_id)
        if status:
            sql += " AND s.status=?"
            args.append(status)
        if after:
            sql += " AND (s.started_at < ? OR (s.started_at = ? AND s.id < ?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY s.started_at DESC, s.id DESC LIMIT ?"
        args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        nxt = enc_cursor([rows[limit - 1]["started_at"], rows[limit - 1]["id"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt}

    @app.get(f"{API}/sessions/{{sid}}")
    def session_detail(sid: str, user=Depends(need("viewer"))):
        with conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise Problem(404, "not_found", "找不到場次")
            st = c.execute("SELECT COUNT(*), SUM(status='silent'), SUM(status IN ('error','timeout','missing')) "
                           "FROM segments WHERE session_id=?", (sid,)).fetchone()
            origins_ = dict(c.execute("SELECT t.origin, COUNT(*) FROM translations t JOIN segments g ON g.id=t.segment_id "
                                      "WHERE g.session_id=? AND t.is_current=1 GROUP BY t.origin", (sid,)).fetchall())
        return {"session": dict(row), "stats": {"segments": st[0], "silent": st[1] or 0, "failed": st[2] or 0,
                                                 "by_origin": origins_}}

    @app.get(f"{API}/sessions/{{sid}}/segments")
    def session_segments(sid: str, cursor: str | None = None, flagged: int = 0,
                         limit: int = Query(200, ge=1, le=1000), user=Depends(need("viewer"))):
        after = dec_cursor(cursor, 1)
        sql, args = "SELECT * FROM v_segment_current WHERE session_id=?", [sid]
        if after:
            sql += " AND seq > ?"
            args.append(after[0])
        if flagged:
            sql += " AND term_flags IS NOT NULL AND term_flags NOT IN ('', '[]')"
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        nxt = enc_cursor([rows[limit - 1]["seq"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt}

    @app.get(f"{API}/segments/search")
    def search(q: str = Query(..., min_length=1, max_length=200), scope: str = "transcript",
               session_id: str | None = None, cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
               user=Depends(need("viewer"))):
        if scope not in ("transcript", "translation"):
            raise Problem(422, "invalid", "scope 只能是 transcript 或 translation")
        q = q.strip()
        if not q:
            raise Problem(422, "invalid", "q 不能空白")
        after = dec_cursor(cursor, 2)
        fn = zsearch.search_transcripts if scope == "transcript" else zsearch.search_translations
        with conn() as c:
            rows, mode = fn(c, q, limit=limit + 1, after=tuple(after) if after else None, session_id=session_id)
        nxt = enc_cursor([rows[limit - 1]["score"], rows[limit - 1]["owner_id"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt, "mode": mode}

    @app.post(f"{API}/segments/{{segment_id}}/retranslate")
    async def retranslate(segment_id: str, user=Depends(need("editor"))):
        # Live-room retranslate is synchronous (runs the LLM, returns the new English), so
        # this is 200 with the result, not 202. Never retried automatically.
        with conn() as c:
            row = c.execute("SELECT room_id, session_id, seq FROM segments WHERE id=?", (segment_id,)).fetchone()
        if not row:
            raise Problem(404, "not_found", "找不到這個段落")
        status, data = await asyncio.to_thread(live.retranslate, row[0], row[1], row[2])
        if status != 200:
            raise Problem(502 if status >= 500 else status, "live_error", "直播服務拒絕重新翻譯", upstream=data)
        return data

    # -------------------------------------------------------------- corrections -> TM / proposals
    @app.get(f"{API}/corrections")
    def list_corrections(status: str = "applied", cursor: str | None = None,
                         limit: int = Query(100, ge=1, le=500), user=Depends(need("editor"))):
        after = dec_cursor(cursor, 2)
        sql, args = "SELECT * FROM corrections WHERE status=?", [status]
        if after:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        nxt = enc_cursor([rows[limit - 1]["created_at"], rows[limit - 1]["id"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt}

    @app.post(f"{API}/corrections", status_code=201)
    async def create_correction(request: Request, user=Depends(need("editor"))):
        key = idem_key(request)
        body, raw = await read_json(request)
        route = "POST /corrections"
        seg = str(body.get("segment_id") or "")
        ttype = str(body.get("target_type") or "translation")
        propose = body.get("propose_term")
        if propose is not None and not isinstance(propose, dict):
            raise Problem(422, "invalid", "propose_term 必須是 {zh, en}")

        def work():
            with conn(write=True) as c:
                replay = idem_replay(c, key, route, raw)
                if replay is not None:
                    return replay
                rec = feedback.record_correction(c, segment_id=seg, target_type=ttype, text=str(body.get("text") or ""),
                                                 reason=body.get("reason"), author_id=user.get("user_id"))
                applied = feedback.promote_correction(
                    c, rec["correction_id"], to_tm=bool(body.get("promote_tm", True)) and ttype == "translation",
                    propose=propose)
                c.execute("INSERT INTO events(segment_id, room_id, kind, payload) VALUES (?,?,?,?)",
                          (seg, rec["room_id"], "admin.correction",
                           json.dumps({"correction_id": rec["correction_id"], "target_type": ttype})))
                out = {"correction_id": rec["correction_id"], "version": rec["version"], **applied}
                headers = {"Location": f"{API}/corrections/{rec['correction_id']}"}
                idem_store(c, key, route, raw, 201, out, headers)
                return JSONResponse(out, status_code=201, headers=headers)
        return await asyncio.to_thread(work)

    # -------------------------------------------------------------- glossary proposals (ETag / If-Match)
    @app.get(f"{API}/glossary/proposals")
    def proposals(cursor: str | None = None, limit: int = Query(100, ge=1, le=500), user=Depends(need("viewer"))):
        after = dec_cursor(cursor, 1)
        sql = ("SELECT t.id, t.zh, t.en, t.note, t.hit_count, t.source, t.rev, t.created_at, g.name AS glossary "
               "FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id WHERE t.status='proposed'")
        args: list = []
        if after:
            sql += " AND t.id > ?"
            args.append(after[0])
        sql += " ORDER BY t.id LIMIT ?"
        args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["etag"] = term_etag(r["id"], r["rev"])
        nxt = enc_cursor([rows[limit - 1]["id"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt}

    @app.get(f"{API}/glossary/terms/{{term_id}}")
    def get_term(term_id: int, user=Depends(need("viewer"))):
        with conn() as c:
            row = c.execute("SELECT * FROM glossary_terms WHERE id=?", (term_id,)).fetchone()
        if not row:
            raise Problem(404, "not_found", "找不到這個詞")
        return JSONResponse(dict(row), headers={"ETag": term_etag(row["id"], row["rev"])})

    def decide(request: Request, term_id: int, approve: bool):
        with conn(write=True) as c:
            row = c.execute("SELECT rev FROM glossary_terms WHERE id=?", (term_id,)).fetchone()
            if not row:
                raise Problem(404, "not_found", "找不到這個詞")
            check_if_match(request, term_etag(term_id, row[0]))
            out = feedback.decide_term(c, term_id, approve)
            rev = c.execute("SELECT rev FROM glossary_terms WHERE id=?", (term_id,)).fetchone()[0]
        return JSONResponse(out, headers={"ETag": term_etag(term_id, rev)})

    @app.post(f"{API}/glossary/proposals/{{term_id}}/approve")
    def approve(term_id: int, request: Request, user=Depends(need("editor"))):
        return decide(request, term_id, True)

    @app.post(f"{API}/glossary/proposals/{{term_id}}/reject")
    def reject(term_id: int, request: Request, user=Depends(need("editor"))):
        return decide(request, term_id, False)

    @app.post(f"{API}/rooms/{{room_id}}/glossary/push")
    async def push_glossary(room_id: str, request: Request, user=Depends(need("editor"))):
        """Live PUT overwrites the whole room glossary: diff first, keep room-only terms,
        and send if_version so a concurrent host edit wins (409) instead of being lost."""
        body, _ = await read_json(request)
        with conn() as c:
            active = [dict(r) for r in c.execute(
                "SELECT t.zh, t.en, t.aliases, t.locked FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id "
                "WHERE t.status='active' AND (g.scope='global' OR (g.scope='room' AND g.room_id=?)) ORDER BY t.id",
                (room_id,))]
        room = await asyncio.to_thread(live.room_glossary, room_id)
        current = [dict(t) for t in room.get("terms") or [] if isinstance(t, dict)]
        by_zh = {t.get("zh"): t for t in current}
        added, updated = [], []
        for a in active:
            want = {"zh": a["zh"], "en": a["en"], "aliases": json.loads(a["aliases"] or "[]"), "locked": bool(a["locked"])}
            have = by_zh.get(a["zh"])
            if have is None:
                added.append(want)
            elif have.get("en") != want["en"]:
                updated.append({"zh": a["zh"], "from": have.get("en"), "to": want["en"]})
                have["en"] = want["en"]
        diff = {"room_id": room_id, "version": room.get("version"), "added": added, "updated": updated,
                "changed": bool(added or updated)}
        if body.get("dry_run") or not diff["changed"]:
            return diff
        status, data = await asyncio.to_thread(live.put_room_glossary, room_id, current + added, int(room.get("version") or 0))
        if status == 409:
            raise Problem(409, "room_glossary_changed", "主持人剛改過這個房間的詞表，請重新比對", upstream=data)
        if status != 200:
            raise Problem(502, "live_error", "直播服務拒絕詞表", upstream=data)
        return {**diff, "version": (data or {}).get("version")}

    # -------------------------------------------------------------- jobs (202 + Location)
    @app.post(f"{API}/jobs", status_code=202)
    async def create_job(request: Request, user=Depends(need("admin"))):
        key = idem_key(request)
        body, raw = await read_json(request)
        kind = str(body.get("kind") or "")
        if kind not in KINDS:
            raise Problem(422, "invalid", f"kind 必須是 {', '.join(KINDS)}")
        if kind not in handlers:
            raise Problem(422, "not_available", f"{kind} 這一版還沒有實作")
        target = body.get("target") or {}
        params = body.get("params") or {}
        if not isinstance(target, dict) or not isinstance(params, dict):
            raise Problem(422, "invalid", "target / params 必須是物件")
        route = "POST /jobs"
        if key is not None:
            with conn(write=True) as c:
                replay = idem_replay(c, key, route, raw)
            if replay is not None:
                return replay
        job, _created = await asyncio.to_thread(store.create, kind, target, params, created_by=user.get("user_id"),
                                                 idempotency_key=key)
        out = {"job": job, "status_url": f"{API}/jobs/{job['id']}"}
        headers = {"Location": f"{API}/jobs/{job['id']}"}
        if key is not None:
            with conn(write=True) as c:
                idem_store(c, key, route, raw, 202, out, headers)
        worker.wake()
        return JSONResponse(out, status_code=202, headers=headers)

    @app.get(f"{API}/jobs")
    def list_jobs(state: str | None = None, kind: str | None = None, cursor: str | None = None,
                  limit: int = Query(50, ge=1, le=200), user=Depends(need("viewer"))):
        after = dec_cursor(cursor, 2)
        rows = store.list(state=state, kind=kind, before=tuple(after) if after else None, limit=limit + 1)
        nxt = enc_cursor([rows[limit - 1]["created_at"], rows[limit - 1]["id"]]) if len(rows) > limit else None
        return {"items": rows[:limit], "next": nxt}

    @app.get(f"{API}/jobs/{{jid}}")
    def get_job(jid: str, user=Depends(need("viewer"))):
        job = store.get(jid)
        if not job:
            raise Problem(404, "not_found", "找不到這個工作")
        headers = {} if job["state"] in ("succeeded", "failed", "cancelled") else {"Retry-After": "2"}
        return JSONResponse(job, headers=headers)

    @app.post(f"{API}/jobs/{{jid}}/cancel", status_code=202)
    def cancel_job(jid: str, user=Depends(need("admin"))):
        job = store.cancel(jid)
        if not job:
            raise Problem(404, "not_found", "找不到這個工作")
        return JSONResponse(job, status_code=202)

    # -------------------------------------------------------------- live monitoring
    @app.get(f"{API}/live/metrics")
    async def live_metrics(user=Depends(need("viewer"))):
        return await asyncio.to_thread(live.metrics)

    @app.get(f"{API}/live/stream")
    async def live_stream(request: Request, user=Depends(need("viewer"))):
        """SSE. Auth is the cookie/Authorization header only; a token in the URL is rejected
        by LoopbackOnly before it reaches here."""
        async def gen():
            sent = 0
            yield "retry: 5000\n\n"
            while sse_max_events is None or sent < sse_max_events:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.to_thread(live.metrics)
                    keep = {k: data.get(k) for k in ("pending", "inflight", "translate_queued", "translate_busy",
                                                     "translate_skipped", "translate_merged", "backlog_s", "rtf",
                                                     "listeners", "rooms", "rss_bytes", "ledger_queued") if k in data}
                    yield f"event: metrics\ndata: {json.dumps(keep, ensure_ascii=False)}\n\n"
                except Exception:
                    yield "event: down\ndata: {}\n\n"
                sent += 1
                if sse_max_events is not None and sent >= sse_max_events:
                    break
                await asyncio.sleep(sse_interval_s)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # LoopbackOnly is the outermost layer (added last).
    app.add_middleware(LoopbackOnly, port=port)
    return app
