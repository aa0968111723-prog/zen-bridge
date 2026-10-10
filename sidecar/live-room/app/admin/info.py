"""Admin information management (後台資訊管理系統): sessions, review workbench, glossary (en/ja),
TM browser, observability, search, exports, config, audit, users.

register(app, ctx) is called from create_admin_app() before LoopbackOnly is added, so every
route below sits behind the same LoopbackOnly / need(role) / CSRF / Origin / Sec-Fetch-Site
checks as the original endpoints. Nothing here relaxes them.

Admin-side extension tables (admin_glossary_lang, admin_term_meta, admin_settings) are created
with CREATE TABLE IF NOT EXISTS at start-up; schema.sql (checksummed) is not modified.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import feedback
from app.admin import db as zdb
from app.admin import search as zsearch

TGT_LANGS = ("en", "ja")
JA_GLOSSARY = "日文詞表"
EXPORT_FORMATS = ("srt", "vtt", "md")       # python-docx is not installed in the venv -> no docx
ROLES = ("admin", "editor", "host", "viewer")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|key|auth|cookie|credential)", re.I)
EXT_SQL = """
CREATE TABLE IF NOT EXISTS admin_glossary_lang (
  glossary_id INTEGER PRIMARY KEY REFERENCES glossaries(id) ON DELETE CASCADE,
  tgt_lang    TEXT NOT NULL CHECK (tgt_lang IN ('en','ja')));
CREATE TABLE IF NOT EXISTS admin_term_meta (
  term_id     INTEGER PRIMARY KEY REFERENCES glossary_terms(id) ON DELETE CASCADE,
  reading     TEXT CHECK (reading IS NULL OR length(reading) <= 80));
CREATE TABLE IF NOT EXISTS admin_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL DEFAULT (unixepoch('subsec')));
"""
# whitelisted config keys the admin may switch (everything else is read-only / Codex-owned)
SETTABLE = {"translate_profile", "default_tgt_lang"}
METRIC_NAMES = ("rtf", "backlog_s", "pending", "inflight", "translate_queued", "translate_skipped",
                "translate_merged", "listeners", "rss_bytes", "cpu_pct", "ram_pct")


def ensure_ext(c) -> None:
    for stmt in EXT_SQL.split(";"):
        if stmt.strip():
            c.execute(stmt)


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEY_RE.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def csv_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s     # CSV formula injection


def fmt_ts(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render_export(rows: list[dict], fmt: str, variant: str, title: str, lang: str) -> str:
    def text(r):
        zh, tg = (r.get("zh") or "").strip(), (r.get("tgt") or "").strip()
        if variant == "zh":
            return zh
        if variant == "en":
            return tg
        return "\n".join(x for x in (zh, tg) if x)
    rows = [r for r in rows if text(r)]
    if fmt == "srt":
        return "\n".join(f"{i}\n{fmt_ts(r['t0_ms'], ',')} --> {fmt_ts(r['t1_ms'], ',')}\n{text(r)}\n"
                         for i, r in enumerate(rows, 1))
    if fmt == "vtt":
        return "WEBVTT\n\n" + "\n".join(f"{fmt_ts(r['t0_ms'], '.')} --> {fmt_ts(r['t1_ms'], '.')}\n{text(r)}\n"
                                         for r in rows)
    out = [f"# {title}", "", f"目標語言：{lang}", ""]
    for r in rows:
        out.append(f"**[{fmt_ts(r['t0_ms'], '.')}]** " + text(r).replace("\n", "  \n"))
        out.append("")
    return "\n".join(out)


def parse_timing_log(path, limit: int = 200) -> list[dict]:
    """Per-clip ASR timings: 'BREEZE_TIMING {json}' lines of the worker's rotating log (tail only)."""
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    out = []
    for line in data.splitlines():
        i = line.find("BREEZE_TIMING ")
        if i < 0:
            continue
        try:
            d = json.loads(line[i + 14:])
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append({k: d.get(k) for k in ("audio_s", "asr_s", "rtf", "audio_ctx", "max_tokens", "retried_full_ctx")})
    return out[-limit:]


def rtf_color(rtf) -> str:
    if rtf is None:
        return "unknown"
    return "green" if rtf < 0.5 else ("amber" if rtf < 0.9 else "red")


def hardware(paths: dict) -> dict:
    """psutil, guarded: anything missing degrades to null instead of failing."""
    out: dict = {"available": False}
    try:
        import psutil
    except Exception:
        return out
    out["available"] = True
    try:
        out["cpu_per_core"] = psutil.cpu_percent(interval=None, percpu=True)
        out["cpu_count"] = psutil.cpu_count()
    except Exception:
        out["cpu_per_core"] = None
    try:
        vm = psutil.virtual_memory()
        out["ram"] = {"total": vm.total, "available": vm.available, "percent": vm.percent}
    except Exception:
        out["ram"] = None
    disks = {}
    for name, p in paths.items():
        try:
            p = Path(p)
            while not p.exists() and p != p.parent:
                p = p.parent
            du = psutil.disk_usage(str(p))
            disks[name] = {"free": du.free, "total": du.total, "percent": du.percent}
        except Exception:
            disks[name] = None
    out["disks"] = disks
    try:
        bat = psutil.sensors_battery()
        out["power"] = None if bat is None else {"ac": bool(bat.power_plugged), "battery_pct": bat.percent}
    except Exception:
        out["power"] = None
    out["power_mode"] = None       # Windows power mode: only via powercfg on Windows; not probed here
    procs = []
    try:
        for pr in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "create_time"]):
            cmd = " ".join(pr.info.get("cmdline") or [])
            if any(k in cmd for k in ("app.admin.run", "app.server", "native_worker", "llama-server", "ollama")):
                mi = pr.info.get("memory_info")
                procs.append({"pid": pr.info["pid"], "name": pr.info.get("name"),
                              "role": next(k for k in ("app.admin.run", "app.server", "native_worker", "llama-server",
                                                      "ollama") if k in cmd),
                              "rss": mi.rss if mi else None, "started": pr.info.get("create_time")})
    except Exception:
        pass
    out["processes"] = procs
    return out


def register(app, ctx) -> int:
    """ctx: need, conn, read_json, Problem, check_if_match, enc_cursor, dec_cursor, live, store, worker,
    handlers, db_path, identity_path, probes, clock, backup_dir_fn, authenticate, sse_open, started_at,
    asr_log_path, config_fn, export_dir_fn, api."""
    API = ctx.api
    need, conn, read_json, Problem = ctx.need, ctx.conn, ctx.read_json, ctx.Problem
    check_if_match, enc_cursor, dec_cursor = ctx.check_if_match, ctx.enc_cursor, ctx.dec_cursor
    clock, live = ctx.clock, ctx.live
    with conn(write=True) as c:
        ensure_ext(c)
    n0 = len(app.routes)

    def audit(c, user, action: str, *, session_id=None, segment_id=None, level="info", **payload):
        c.execute("INSERT INTO events(kind, level, actor_id, session_id, segment_id, payload) VALUES (?,?,?,?,?,?)",
                  (f"audit.{action}", level, user.get("user_id"), session_id, segment_id,
                   json.dumps({"via": user.get("via"), "role": user.get("role"), **payload}, ensure_ascii=False)))

    def lang_of(v, default="en") -> str:
        v = str(v or default)
        if v not in TGT_LANGS:
            raise Problem(422, "invalid", "目標語言只能是 en 或 ja（一次一種）")
        return v

    def clean(v, name: str, lo: int, hi: int) -> str:
        s = str(v or "").strip()
        if not (lo <= len(s) <= hi) or _CTRL.search(s):
            raise Problem(422, "invalid", f"{name} 需 {lo}–{hi} 字且不含控制字元")
        return s

    # ============================================================ sessions
    def session_row(c, sid):
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise Problem(404, "not_found", "找不到場次")
        return dict(row)

    def session_etag(row: dict) -> str:
        return '"s-' + hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'

    @app.post(f"{API}/sessions", status_code=201)
    async def create_session(request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        room = ctx.check_room_id(str(body.get("room_id") or ""))
        tgt = lang_of(body.get("tgt_lang"))
        title = clean(body.get("title") or room, "title", 1, 120)
        sid = str(body.get("id") or f"adm-{int(clock())}-{secrets.token_hex(3)}")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", sid):
            raise Problem(422, "invalid", "場次 id 格式不正確")
        with conn(write=True) as c:
            if c.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise Problem(409, "conflict", "場次 id 已存在")
            c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
            c.execute("INSERT INTO sessions(id, room_id, title, started_at, tgt_lang, status) VALUES (?,?,?,?,?, 'live')",
                      (sid, room, title, clock(), tgt))
            audit(c, user, "session.create", session_id=sid, tgt_lang=tgt)
            row = session_row(c, sid)
        return JSONResponse(row, status_code=201, headers={"ETag": session_etag(row), "Location": f"{API}/sessions/{sid}"})

    @app.get(f"{API}/sessions/{{sid}}/etag")
    def session_get_etag(sid: str, user=Depends(need("viewer"))):
        with conn() as c:
            row = session_row(c, sid)
        return JSONResponse(row, headers={"ETag": session_etag(row)})

    @app.patch(f"{API}/sessions/{{sid}}")
    async def patch_session(sid: str, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        with conn(write=True) as c:
            row = session_row(c, sid)
            check_if_match(request, session_etag(row))
            sets, args = [], []
            if "title" in body:
                sets.append("title=?"); args.append(clean(body["title"], "title", 1, 120))
            if "notes" in body:
                sets.append("notes=?"); args.append(clean(body["notes"], "notes", 0, 2000))
            if "tgt_lang" in body:
                sets.append("tgt_lang=?"); args.append(lang_of(body["tgt_lang"]))
            for k in ("purge_after", "legal_hold"):
                if k in body:
                    if user["role"] not in ("admin", "owner"):
                        raise Problem(403, "forbidden", "保留期限與法律保全只有管理員能改")
                    v = body[k]
                    if k == "legal_hold":
                        v = 1 if v is True else 0 if v is False else None
                        if v is None:
                            raise Problem(422, "invalid", "legal_hold 必須是 true/false")
                    elif v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                        raise Problem(422, "invalid", "purge_after 必須是 epoch 秒或 null")
                    sets.append(f"{k}=?"); args.append(v)
            if not sets:
                raise Problem(422, "invalid", "沒有可更新的欄位")
            c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", (*args, sid))
            audit(c, user, "session.update", session_id=sid, fields=sorted(k for k in body if k != "notes"))
            row = session_row(c, sid)
        return JSONResponse(row, headers={"ETag": session_etag(row)})

    @app.post(f"{API}/sessions/{{sid}}/close")
    def close_session(sid: str, user=Depends(need("editor"))):
        with conn(write=True) as c:
            row = session_row(c, sid)
            if row["status"] != "live":
                raise Problem(409, "conflict", f"場次目前是 {row['status']}，不能結束")
            c.execute("UPDATE sessions SET status='ended', ended_at=MAX(?, started_at) WHERE id=?", (clock(), sid))
            audit(c, user, "session.close", session_id=sid)
            row = session_row(c, sid)
        return JSONResponse(row, headers={"ETag": session_etag(row)})

    @app.post(f"{API}/sessions/{{sid}}/delete-preview")
    def delete_preview(sid: str, user=Depends(need("admin"))):
        with conn() as c:
            row = session_row(c, sid)
            q = lambda sql: c.execute(sql, (sid,)).fetchone()[0]  # noqa: E731
            counts = {
                "segments": q("SELECT COUNT(*) FROM segments WHERE session_id=?"),
                "transcripts": q("SELECT COUNT(*) FROM transcripts t JOIN segments g ON g.id=t.segment_id WHERE g.session_id=?"),
                "translations": q("SELECT COUNT(*) FROM translations t JOIN segments g ON g.id=t.segment_id WHERE g.session_id=?"),
                "corrections": q("SELECT COUNT(*) FROM corrections c JOIN segments g ON g.id=c.segment_id WHERE g.session_id=?"),
                "exports": q("SELECT COUNT(*) FROM exports WHERE session_id=?"),
                "events": q("SELECT COUNT(*) FROM events WHERE session_id=?"),
            }
        return {"session_id": sid, "status": row["status"], "legal_hold": bool(row["legal_hold"]), "counts": counts,
                "confirm": {"confirm": sid}, "identity_db": ctx.identity_path is not None}

    @app.get(f"{API}/retention")
    def get_retention(user=Depends(need("viewer"))):
        with conn() as c:
            items = [dict(r) for r in c.execute("SELECT item, keep_days, note FROM retention_policy ORDER BY item")]
            due = c.execute("SELECT COUNT(*) FROM sessions WHERE legal_hold=0 AND purge_after IS NOT NULL AND purge_after < ?",
                            (clock(),)).fetchone()[0]
            held = c.execute("SELECT COUNT(*) FROM sessions WHERE legal_hold=1").fetchone()[0]
        age = zdb.latest_backup_age_s(ctx.backup_dir_fn(), clock())
        return {"items": items, "sessions_due": due, "legal_hold": held,
                "purge_enabled": age is not None and age < 86400.0}

    @app.put(f"{API}/retention/{{item}}")
    async def put_retention(item: str, request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)
        days = body.get("keep_days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 36500):
            raise Problem(422, "invalid", "keep_days 需 1–36500 或 null（永久）")
        with conn(write=True) as c:
            if not c.execute("SELECT 1 FROM retention_policy WHERE item=?", (item,)).fetchone():
                raise Problem(404, "not_found", "沒有這個保留項目")
            c.execute("UPDATE retention_policy SET keep_days=? WHERE item=?", (days, item))
            audit(c, user, "retention.update", item=item, keep_days=days)
        return {"item": item, "keep_days": days}

    # ============================================================ review workbench
    def seg_info(c, seg):
        row = c.execute("SELECT g.id, g.room_id, g.session_id, g.seq, s.tgt_lang FROM segments g "
                        "JOIN sessions s ON s.id=g.session_id WHERE g.id=?", (seg,)).fetchone()
        if not row:
            raise Problem(404, "not_found", "找不到這個段落")
        return dict(row)

    def current_version(c, seg, target):
        if target == "zh":
            r = c.execute("SELECT version, text, origin FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
        else:
            r = c.execute("SELECT version, text, origin FROM translations WHERE segment_id=? AND tgt_lang=? AND is_current=1",
                          (seg, target)).fetchone()
        return r

    def target_of(v):
        v = str(v or "")
        if v not in ("zh",) + TGT_LANGS:
            raise Problem(422, "invalid", "target 只能是 zh、en 或 ja")
        return v

    @app.get(f"{API}/segments/{{seg}}/history")
    def history(seg: str, user=Depends(need("viewer"))):
        with conn() as c:
            info = seg_info(c, seg)
            zh = [dict(r) for r in c.execute("SELECT version, is_current, text, origin, created_at FROM transcripts "
                                             "WHERE segment_id=? ORDER BY version", (seg,))]
            tr = [dict(r) for r in c.execute("SELECT tgt_lang, version, is_current, text, origin, model, latency_ms, "
                                             "created_at FROM translations WHERE segment_id=? ORDER BY tgt_lang, version", (seg,))]
            corr = [dict(r) for r in c.execute("SELECT id, target_type, before_text, after_text, reason, status, "
                                               "promoted_tm_id, created_at FROM corrections WHERE segment_id=? ORDER BY id", (seg,))]
        etags = {}
        for t in ("zh",) + TGT_LANGS:
            with conn() as c:
                cv = current_version(c, seg, t)
            if cv:
                etags[t] = f'"v{cv[0]}"'
        return {"segment": info, "transcripts": zh, "translations": tr, "corrections": corr, "etags": etags}

    @app.patch(f"{API}/segments/{{seg}}/text")
    async def edit_text(seg: str, request: Request, user=Depends(need("editor"))):
        """Keyboard workbench save: new human version (If-Match "v{n}"). Translation fixes become
        TM units that wait for admin approval unless an admin made them."""
        body, _ = await read_json(request)
        target = target_of(body.get("target"))
        with conn(write=True) as c:
            info = seg_info(c, seg)
            cv = current_version(c, seg, target)
            check_if_match(request, f'"v{cv[0] if cv else 0}"')
            rec = feedback.record_correction(c, segment_id=seg, target_type="transcript" if target == "zh" else "translation",
                                             text=str(body.get("text") or ""), reason=body.get("reason"),
                                             tgt_lang="en" if target == "zh" else target, author_id=user.get("user_id"))
            # round3 §5-1: the fix goes to staging (app/admin/staging.py); an admin approves it into TM.
            from app.admin import staging as zstaging
            staged = zstaging.stage_from_correction(c, rec["correction_id"], actor=user,
                                                    to_tm=target != "zh" and body.get("to_tm", True) is not False,
                                                    self_approve=getattr(ctx, "staging_self_approve", False))
            applied = {"status": "applied", "tm_id": None, "term_id": None, **staged}
            audit(c, user, "segment.edit", session_id=info["session_id"], segment_id=seg, target=target,
                  version=rec["version"], correction_id=rec["correction_id"])
        return JSONResponse({"version": rec["version"], "correction_id": rec["correction_id"], **applied,
                             "tm_pending": staged["tm_staging_id"] is not None
                                           and not staged.get("auto_approved", {}).get("tm", {}).get("ok")},
                            headers={"ETag": f'"v{rec["version"]}"'})

    @app.post(f"{API}/segments/{{seg}}/undo")
    async def undo(seg: str, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        target = target_of(body.get("target"))
        table, extra, args = ("transcripts", "", [seg]) if target == "zh" else ("translations", " AND tgt_lang=?", [seg, target])
        with conn(write=True) as c:
            info = seg_info(c, seg)
            cur = c.execute(f"SELECT id, version FROM {table} WHERE segment_id=?{extra} AND is_current=1", args).fetchone()
            if not cur:
                raise Problem(404, "not_found", "沒有可復原的版本")
            check_if_match(request, f'"v{cur[1]}"')
            prev = c.execute(f"SELECT id, version FROM {table} WHERE segment_id=?{extra} AND version<? "
                             "ORDER BY version DESC LIMIT 1", (*args, cur[1])).fetchone()
            if not prev:
                raise Problem(409, "conflict", "已經是最早的版本")
            c.execute(f"UPDATE {table} SET is_current=0 WHERE id=?", (cur[0],))
            c.execute(f"UPDATE {table} SET is_current=1 WHERE id=?", (prev[0],))
            audit(c, user, "segment.undo", session_id=info["session_id"], segment_id=seg, target=target,
                  from_version=cur[1], to_version=prev[1])
        return JSONResponse({"version": prev[1], "undone": cur[1]}, headers={"ETag": f'"v{prev[1]}"'})

    @app.post(f"{API}/segments/{{seg}}/push")
    def push_to_room(seg: str, user=Depends(need("editor"))):
        """Publish the corrected (human) line to the room as-is. Never re-runs MT over human text."""
        with conn() as c:
            info = seg_info(c, seg)
            tr = current_version(c, seg, "en")
            zh = current_version(c, seg, "zh")
        if info["tgt_lang"] != "en":
            raise Problem(422, "unsupported", "直播端目前只接受英文修正推送；日文場次請在直播端處理")
        if not tr or tr[2] not in ("human", "post_edit"):
            raise Problem(409, "not_human", "只有人工修正過的譯文才能推送（不會重新跑機器翻譯）")
        status, data = live.push_correction(info["room_id"], info["session_id"], info["seq"], tr[1],
                                            zh[1] if zh and zh[2] == "human" else None)
        if status != 200:
            raise Problem(502 if status >= 500 else status, "live_error", "直播服務拒絕修正", upstream=data)
        with conn(write=True) as c:
            audit(c, user, "segment.push", session_id=info["session_id"], segment_id=seg)
        return {"pushed": True, "live": data}

    @app.get(f"{API}/review/queue")
    def review_queue(user=Depends(need("editor"))):
        with conn() as c:
            tm = [dict(r) for r in c.execute("SELECT id, src_text, tgt_text, tgt_lang, segment_id, created_at FROM tm_units "
                                             "WHERE quality=2 ORDER BY id LIMIT 200")]
            # round3 §5-1: new corrections wait in staging_items (GET /staging); legacy quality-2 units stay above.
            staged = [dict(r) for r in c.execute("SELECT id, kind, tgt_lang, src_text, tgt_text, segment_id, created_at, rev "
                                                 "FROM staging_items WHERE state='pending' ORDER BY created_at, id LIMIT 200")]
        for r in staged:
            r["etag"] = f'"s{r["id"]}-r{r.pop("rev")}"'
        # tm_pending ids are tm_units ids (POST /tm/{id}/approve); staging ids are a different space.
        return {"tm_pending": tm, "staging_pending": staged}

    # ============================================================ glossary (en / ja)
    def glossary_for(c, lang: str) -> int:
        if lang == "en":
            return feedback._global_glossary(c)
        row = c.execute("SELECT glossary_id FROM admin_glossary_lang WHERE tgt_lang='ja' LIMIT 1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO glossaries(name, scope) VALUES (?, 'global')", (JA_GLOSSARY,))
        gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO admin_glossary_lang(glossary_id, tgt_lang) VALUES (?, 'ja')", (gid,))
        return gid

    TERM_SQL = ("SELECT t.*, COALESCE(l.tgt_lang, 'en') AS tgt_lang, m.reading FROM glossary_terms t "
                "LEFT JOIN admin_glossary_lang l ON l.glossary_id=t.glossary_id "
                "LEFT JOIN admin_term_meta m ON m.term_id=t.id")

    def term_out(r) -> dict:
        d = dict(r)
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        d["etag"] = f'"t{d["id"]}-r{d["rev"]}"'
        return d

    def get_term_row(c, tid):
        r = c.execute(TERM_SQL + " WHERE t.id=?", (tid,)).fetchone()
        if not r:
            raise Problem(404, "not_found", "找不到這個詞")
        return r

    def check_target_text(lang, s):
        s = clean(s, "譯詞", 1, 80)
        if lang == "en" and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", s):
            raise Problem(422, "invalid", "英文譯詞不能含中日文字")
        return s

    def check_aliases(v):
        if v is None:
            return []
        if not isinstance(v, list) or len(v) > 8:
            raise Problem(422, "invalid", "aliases 最多 8 個")
        return [clean(a, "別名", 1, 20) for a in v]

    @app.get(f"{API}/glossary/terms")
    def list_terms(lang: str = "en", q: str | None = None, status: str = "active", cursor: str | None = None,
                   limit: int = Query(100, ge=1, le=500), user=Depends(need("viewer"))):
        lang = lang_of(lang)
        after = dec_cursor(cursor, 1)
        sql, args = TERM_SQL + " WHERE COALESCE(l.tgt_lang,'en')=? AND t.status=?", [lang, status]
        if q:
            sql += " AND (t.zh LIKE ? OR t.en LIKE ? OR t.aliases LIKE ?)"
            args += [f"%{q}%"] * 3
        if after:
            sql += " AND t.id > ?"; args.append(after[0])
        sql += " ORDER BY t.id LIMIT ?"; args.append(limit + 1)
        with conn() as c:
            rows = [term_out(r) for r in c.execute(sql, args)]
        return {"items": rows[:limit], "next": enc_cursor([rows[limit - 1]["id"]]) if len(rows) > limit else None}

    @app.post(f"{API}/glossary/terms", status_code=201)
    async def create_term(request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        lang = lang_of(body.get("tgt_lang"))
        zh = clean(body.get("zh"), "zh", 1, 20)
        tgt = check_target_text(lang, body.get("target") or body.get("en"))
        aliases = check_aliases(body.get("aliases"))
        reading = body.get("reading")
        if reading is not None:
            reading = clean(reading, "reading", 1, 80) if lang == "ja" else None
        status = "proposed" if body.get("suggest") is True else "active"
        with conn(write=True) as c:
            gid = glossary_for(c, lang)
            if c.execute("SELECT 1 FROM glossary_terms WHERE glossary_id=? AND zh=?", (gid, zh)).fetchone():
                raise Problem(409, "conflict", "這個中文詞已經在詞表裡")
            c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, aliases, locked, source, status, created_by) "
                      "VALUES (?,?,?,?,?,?,?,?)", (gid, zh, tgt, json.dumps(aliases, ensure_ascii=False),
                                                  1 if body.get("locked") is True else 0,
                                                  "suggested" if status == "proposed" else "manual", status, user.get("user_id")))
            tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            if reading:
                c.execute("INSERT INTO admin_term_meta(term_id, reading) VALUES (?,?)", (tid, reading))
            audit(c, user, "glossary.create", term_id=tid, tgt_lang=lang, status=status)
            out = term_out(get_term_row(c, tid))
        return JSONResponse(out, status_code=201, headers={"ETag": out["etag"]})

    @app.patch(f"{API}/glossary/terms/{{tid}}")
    async def patch_term(tid: int, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        with conn(write=True) as c:
            row = get_term_row(c, tid)
            check_if_match(request, f'"t{tid}-r{row["rev"]}"')
            if row["locked"] and body.get("locked") is not False and any(k in body for k in ("target", "en", "aliases")):
                raise Problem(409, "locked", "這個詞已鎖定，請先解鎖再修改")
            sets, args = [], []
            if "target" in body or "en" in body:
                sets.append("en=?"); args.append(check_target_text(row["tgt_lang"], body.get("target", body.get("en"))))
            if "aliases" in body:
                sets.append("aliases=?"); args.append(json.dumps(check_aliases(body["aliases"]), ensure_ascii=False))
            if "locked" in body:
                sets.append("locked=?"); args.append(1 if body["locked"] is True else 0)
            if "note" in body:
                sets.append("note=?"); args.append(clean(body["note"], "note", 0, 200))
            if "reading" in body and row["tgt_lang"] == "ja":
                r = body["reading"]
                c.execute("INSERT INTO admin_term_meta(term_id, reading) VALUES (?,?) ON CONFLICT(term_id) DO UPDATE SET reading=excluded.reading",
                          (tid, clean(r, "reading", 1, 80) if r else None))
            sets.append("rev=rev+1"); sets.append("updated_at=unixepoch('subsec')")
            c.execute(f"UPDATE glossary_terms SET {', '.join(sets)} WHERE id=?", (*args, tid))
            audit(c, user, "glossary.update", term_id=tid, fields=sorted(body))
            out = term_out(get_term_row(c, tid))
        return JSONResponse(out, headers={"ETag": out["etag"]})

    @app.delete(f"{API}/glossary/terms/{{tid}}")
    def retire_term(tid: int, request: Request, user=Depends(need("editor"))):
        with conn(write=True) as c:
            row = get_term_row(c, tid)
            check_if_match(request, f'"t{tid}-r{row["rev"]}"')
            if row["locked"]:
                raise Problem(409, "locked", "已鎖定的詞不能刪除")
            c.execute("UPDATE glossary_terms SET status='retired', rev=rev+1 WHERE id=?", (tid,))
            audit(c, user, "glossary.retire", term_id=tid)
        return Response(status_code=204)

    @app.post(f"{API}/glossary/proposals/{{tid}}/merge")
    async def merge_alias(tid: int, request: Request, user=Depends(need("editor"))):
        """Suggestion -> merge as alias of an existing active term (same language)."""
        body, _ = await read_json(request)
        into = body.get("into_term_id")
        if isinstance(into, bool) or not isinstance(into, int):
            raise Problem(422, "invalid", "需要 into_term_id")
        with conn(write=True) as c:
            p = get_term_row(c, tid)
            check_if_match(request, f'"t{tid}-r{p["rev"]}"')
            if p["status"] != "proposed":
                raise Problem(409, "conflict", "只能合併待審的建議詞")
            t = get_term_row(c, into)
            if t["status"] != "active" or t["tgt_lang"] != p["tgt_lang"]:
                raise Problem(422, "invalid", "目標詞必須是同語言、已啟用的詞")
            if t["locked"]:
                raise Problem(409, "locked", "目標詞已鎖定")
            aliases = json.loads(t["aliases"] or "[]")
            if p["zh"] not in aliases and p["zh"] != t["zh"]:
                if len(aliases) >= 8:
                    raise Problem(422, "invalid", "目標詞的別名已滿（8 個）")
                aliases.append(p["zh"])
            c.execute("UPDATE glossary_terms SET aliases=?, rev=rev+1, updated_at=unixepoch('subsec') WHERE id=?",
                      (json.dumps(aliases, ensure_ascii=False), into))
            c.execute("UPDATE glossary_terms SET status='retired', note=?, rev=rev+1 WHERE id=?", (f"merged into {into}", tid))
            audit(c, user, "glossary.merge", term_id=tid, into=into)
            out = term_out(get_term_row(c, into))
        return out

    @app.get(f"{API}/glossary/export.csv")
    def export_csv(lang: str = "en", user=Depends(need("viewer"))):
        lang = lang_of(lang)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["zh", "target", "reading", "aliases", "locked", "status", "note"])
        with conn() as c:
            for r in c.execute(TERM_SQL + " WHERE COALESCE(l.tgt_lang,'en')=? AND t.status IN ('active','proposed') ORDER BY t.id", (lang,)):
                w.writerow([csv_cell(x) for x in (r["zh"], r["en"], r["reading"], "|".join(json.loads(r["aliases"] or "[]")),
                                                  r["locked"], r["status"], r["note"])])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="glossary-{lang}.csv"'})

    @app.post(f"{API}/glossary/import")
    async def import_csv(request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)          # {"csv": "...", "tgt_lang": "en", "dry_run": true}
        lang = lang_of(body.get("tgt_lang"))
        text = body.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise Problem(422, "invalid", "需要 csv 文字")
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) > 5000:
            raise Problem(422, "too_many", "一次最多 5000 列")
        add, update, errors = [], [], []
        with conn(write=not body.get("dry_run")) as c:
            gid = glossary_for(c, lang) if not body.get("dry_run") else None
            for i, r in enumerate(rows, 2):
                try:
                    zh = clean(r.get("zh"), "zh", 1, 20)
                    tg = check_target_text(lang, (r.get("target") or r.get("en") or "").lstrip("'"))
                    al = check_aliases([a for a in (r.get("aliases") or "").split("|") if a.strip()])
                except Exception as exc:
                    errors.append({"line": i, "detail": getattr(exc, "detail", str(exc))})
                    continue
                g = gid if gid is not None else c.execute(
                    "SELECT glossary_id FROM admin_glossary_lang WHERE tgt_lang=?", (lang,)).fetchone()
                g = g[0] if isinstance(g, tuple) or hasattr(g, "keys") else g
                if lang == "en" and g is None:
                    g = c.execute("SELECT id FROM glossaries WHERE scope='global' AND name=?",
                                  (feedback.GLOBAL_GLOSSARY,)).fetchone()
                    g = g[0] if g else None
                ex = c.execute("SELECT id, locked FROM glossary_terms WHERE glossary_id=? AND zh=?", (g, zh)).fetchone() if g else None
                if ex and ex[1]:
                    errors.append({"line": i, "detail": f"{zh} 已鎖定，略過"})
                    continue
                (update if ex else add).append({"zh": zh, "target": tg, "aliases": al})
                if not body.get("dry_run"):
                    if ex:
                        c.execute("UPDATE glossary_terms SET en=?, aliases=?, rev=rev+1 WHERE id=?",
                                  (tg, json.dumps(al, ensure_ascii=False), ex[0]))
                    else:
                        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, aliases, source, status) VALUES (?,?,?,?, 'csv', 'active')",
                                  (gid, zh, tg, json.dumps(al, ensure_ascii=False)))
                    rd = (r.get("reading") or "").strip()
                    if lang == "ja" and rd:
                        tid = ex[0] if ex else c.execute("SELECT last_insert_rowid()").fetchone()[0]
                        c.execute("INSERT INTO admin_term_meta(term_id, reading) VALUES (?,?) ON CONFLICT(term_id) DO UPDATE SET reading=excluded.reading",
                                  (tid, rd[:80]))
            if not body.get("dry_run"):
                audit(c, user, "glossary.import", tgt_lang=lang, added=len(add), updated=len(update))
        return {"dry_run": bool(body.get("dry_run")), "add": add[:200], "update": update[:200], "errors": errors[:200],
                "counts": {"add": len(add), "update": len(update), "errors": len(errors)}}

    # ============================================================ translation memory
    def tm_row(c, tid):
        r = c.execute("SELECT * FROM tm_units WHERE id=?", (tid,)).fetchone()
        if not r:
            raise Problem(404, "not_found", "找不到這筆翻譯記憶")
        return dict(r)

    def tm_etag(r):
        return f'"tm{r["id"]}-{int(r["updated_at"] * 1000)}-{hashlib.sha1(r["tgt_text"].encode()).hexdigest()[:8]}"'

    def tm_state(q):
        return "disabled" if q <= 1 else ("pending" if q == 2 else "live")

    @app.get(f"{API}/tm")
    def list_tm(q: str | None = None, status: str | None = None, lang: str | None = None, cursor: str | None = None,
                limit: int = Query(50, ge=1, le=200), user=Depends(need("viewer"))):
        after = dec_cursor(cursor, 1)
        sql, args = "SELECT * FROM tm_units WHERE 1=1", []
        if q:
            sql += " AND (src_text LIKE ? OR tgt_text LIKE ?)"; args += [f"%{q}%"] * 2
        if status:
            sql += {"live": " AND quality>=3", "pending": " AND quality=2", "disabled": " AND quality<=1"}.get(status, " AND 0")
        if lang:
            sql += " AND tgt_lang=?"; args.append(lang_of(lang))
        if after:
            sql += " AND id < ?"; args.append(after[0])
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["state"], r["etag"] = tm_state(r["quality"]), tm_etag(r)
        return {"items": rows[:limit], "next": enc_cursor([rows[limit - 1]["id"]]) if len(rows) > limit else None}

    @app.get(f"{API}/tm/{{tid}}")
    def get_tm(tid: int, user=Depends(need("viewer"))):
        with conn() as c:
            r = tm_row(c, tid)
            prov = {"origin": r["origin"], "room_id": r["room_id"], "segment_id": r["segment_id"], "session_id": None,
                    "corrections": [dict(x) for x in c.execute(
                        "SELECT id, author_id, status, created_at FROM corrections WHERE promoted_tm_id=?", (tid,))]}
            if r["segment_id"]:
                s = c.execute("SELECT session_id, seq, t0_ms FROM segments WHERE id=?", (r["segment_id"],)).fetchone()
                if s:
                    prov.update(session_id=s[0], seq=s[1], t0_ms=s[2])
        r["state"], r["provenance"] = tm_state(r["quality"]), prov
        return JSONResponse(r, headers={"ETag": tm_etag(r)})

    @app.patch(f"{API}/tm/{{tid}}")
    async def patch_tm(tid: int, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        with conn(write=True) as c:
            r = tm_row(c, tid)
            check_if_match(request, tm_etag(r))
            tgt = clean(body.get("tgt_text"), "tgt_text", 1, 2000)
            if r["tgt_lang"] == "en":
                from app.translate import validate_caption_en
                if validate_caption_en(tgt, zh=r["src_text"]) is None:
                    raise Problem(422, "invalid", "英文譯文必須是單行純英文")
            # an editor's edit goes back to review; an admin's edit stays live
            q = max(3, r["quality"]) if user.get("role") in ("admin", "owner") else 2
            c.execute("UPDATE tm_units SET tgt_text=?, quality=?, updated_at=? WHERE id=?", (tgt, q, clock(), tid))
            audit(c, user, "tm.update", tm_id=tid)
            r = tm_row(c, tid)
        r["state"] = tm_state(r["quality"])
        return JSONResponse(r, headers={"ETag": tm_etag(r)})

    @app.post(f"{API}/tm/{{tid}}/disable")
    def disable_tm(tid: int, user=Depends(need("editor"))):
        with conn(write=True) as c:
            tm_row(c, tid)
            c.execute("UPDATE tm_units SET quality=1, updated_at=? WHERE id=?", (clock(), tid))
            audit(c, user, "tm.disable", tm_id=tid)
        return {"tm_id": tid, "state": "disabled"}

    # ============================================================ search (FTS / trigram / unigram)
    @app.get(f"{API}/search")
    def search(q: str = Query(..., min_length=1, max_length=200), lang: str = "zh", in_session: str | None = None,
               date_from: float | None = None, date_to: float | None = None, limit: int = Query(20, ge=1, le=100),
               user=Depends(need("viewer"))):
        q = q.strip()
        if not q or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in q):
            raise Problem(422, "invalid", "q 不能空白或含控制字元")
        if lang not in ("zh",) + TGT_LANGS:
            raise Problem(422, "invalid", "lang 只能是 zh、en、ja")
        fn = zsearch.search_transcripts if lang == "zh" else zsearch.search_translations
        try:
            with conn() as c:
                rows, mode = fn(c, q, limit=500, after=None, session_id=in_session)
                if lang in TGT_LANGS:
                    rows = [r for r in rows if c.execute("SELECT tgt_lang FROM translations WHERE id=?",
                                                         (r["owner_id"],)).fetchone()[0] == lang]
                if date_from is not None or date_to is not None:
                    ok = {r[0] for r in c.execute("SELECT id FROM sessions WHERE started_at >= ? AND started_at <= ?",
                                                  (date_from if date_from is not None else 0, date_to if date_to is not None else 1e12))}
                    rows = [r for r in rows if r.get("session_id") in ok]
        except Exception as exc:
            if isinstance(exc, ctx.Problem):
                raise
            raise Problem(400, "bad_query", "搜尋字串無法解析")
        return {"items": rows[:limit], "mode": mode, "route": zsearch.route(q), "total_scanned": len(rows)}

    # ============================================================ exports (jobs)
    def export_rows(c, sid):
        s = session_row(c, sid)
        rows = [dict(r) for r in c.execute(
            "SELECT g.seq, g.t0_ms, g.t1_ms, t.text AS zh, tr.text AS tgt FROM segments g "
            "LEFT JOIN transcripts t ON t.segment_id=g.id AND t.is_current=1 "
            "LEFT JOIN translations tr ON tr.segment_id=g.id AND tr.tgt_lang=? AND tr.is_current=1 "
            "WHERE g.session_id=? AND g.status NOT IN ('silent','deleted') ORDER BY g.seq", (s["tgt_lang"], sid))]
        return s, rows

    def export_handler(jctx):
        job = jctx.job if hasattr(jctx, "job") else {}
        target = job.get("target") if isinstance(job, dict) else None
        params = job.get("params") if isinstance(job, dict) else None
        target = target or getattr(jctx, "target", {}) or {}
        params = params or getattr(jctx, "params", {}) or {}
        return run_export(target.get("session_id"), params.get("format"), params.get("variant", "bilingual"))

    def run_export(sid, fmt, variant):
        with conn() as c:
            s, rows = export_rows(c, sid)
        text = render_export(rows, fmt, variant, s.get("title") or sid, s["tgt_lang"])
        d = Path(ctx.export_dir_fn())
        d.mkdir(parents=True, exist_ok=True)
        name = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}-{variant}-{int(clock())}.{fmt}"
        (d / name).write_text(text, encoding="utf-8")
        with conn(write=True) as c:
            c.execute("INSERT INTO exports(session_id, format, variant) VALUES (?,?,?)", (sid, fmt, variant))
            eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO events(kind, session_id, payload) VALUES ('export.done', ?, ?)",
                      (sid, json.dumps({"export_id": eid, "file": name})))
        return {"export_id": eid, "file": name, "bytes": len(text.encode()), "segments": len(rows)}

    ctx.handlers["export"] = export_handler
    app.state.run_export = run_export

    @app.post(f"{API}/sessions/{{sid}}/exports", status_code=202)
    async def create_export(sid: str, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)
        fmt = str(body.get("format") or "")
        variant = str(body.get("variant") or "bilingual")
        if fmt not in EXPORT_FORMATS:
            raise Problem(422, "invalid", "format 只能是 srt、vtt、md（本機未安裝 python-docx，暫不提供 docx）")
        if variant not in ("zh", "en", "bilingual"):
            raise Problem(422, "invalid", "variant 只能是 zh、en（目標語）、bilingual")
        with conn() as c:
            session_row(c, sid)
        job, _ = await asyncio.to_thread(ctx.store.create, "export", {"session_id": sid}, {"format": fmt, "variant": variant},
                                         created_by=user.get("user_id"))
        with conn(write=True) as c:
            audit(c, user, "export.create", session_id=sid, format=fmt, variant=variant)
        ctx.worker.wake()
        return JSONResponse({"job": job, "status_url": f"{API}/jobs/{job['id']}"}, status_code=202,
                            headers={"Location": f"{API}/jobs/{job['id']}"})

    @app.get(f"{API}/exports")
    def list_exports(in_session: str | None = None, user=Depends(need("viewer"))):
        files = []
        d = Path(ctx.export_dir_fn())
        if d.is_dir():
            for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
                if p.suffix.lstrip(".") in EXPORT_FORMATS and (not in_session or p.name.startswith(re.sub(r'[^A-Za-z0-9_.-]', '_', in_session) + "-")):
                    files.append({"file": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime})
        return {"items": files}

    @app.get(f"{API}/exports/file/{{name}}")
    def export_file(name: str, user=Depends(need("viewer"))):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}\.(srt|vtt|md)", name) or ".." in name:
            raise Problem(404, "not_found", "找不到")
        p = Path(ctx.export_dir_fn()) / name
        if not p.is_file():
            raise Problem(404, "not_found", "找不到")
        mt = {"srt": "application/x-subrip", "vtt": "text/vtt", "md": "text/markdown"}[p.suffix[1:]]
        return Response(p.read_bytes(), media_type=mt + "; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-Type-Options": "nosniff"})

    # ============================================================ config / models
    def settings(c) -> dict:
        return {r[0]: json.loads(r[1]) for r in c.execute("SELECT key, value FROM admin_settings")}

    @app.get(f"{API}/config")
    def get_config(user=Depends(need("viewer"))):
        env = {k: v for k, v in os.environ.items() if k.startswith(("ZEN_", "BREEZE_", "TRANSLATE_", "OLLAMA_", "LLAMA_"))}
        with conn() as c:
            profiles = [dict(r) for r in c.execute("SELECT id, name, tier, config, is_active, updated_at FROM model_profiles ORDER BY id")]
            st = settings(c)
        for p in profiles:
            try:
                p["config"] = redact(json.loads(p["config"]))
            except ValueError:
                p["config"] = None
        return {"env": redact(env), "profiles": profiles, "settings": st, "settable": sorted(SETTABLE),
                "read_only_note": "安裝／更新／詞表驗證相關設定由 Codex 管理，這裡只顯示不修改",
                "extra": redact(ctx.config_fn() if ctx.config_fn else {})}

    @app.put(f"{API}/config/{{key}}")
    async def put_config(key: str, request: Request, user=Depends(need("admin"))):
        if key not in SETTABLE:
            raise Problem(403, "read_only", "這個設定是唯讀的（或由 Codex 管理）")
        body, _ = await read_json(request)
        v = body.get("value")
        with conn(write=True) as c:
            if key == "default_tgt_lang":
                v = lang_of(v)
            else:
                v = clean(v, "profile", 1, 64)
                if not c.execute("SELECT 1 FROM model_profiles WHERE name=?", (v,)).fetchone():
                    raise Problem(422, "invalid", "沒有這個翻譯引擎設定檔")
                c.execute("UPDATE model_profiles SET is_active=0 WHERE is_active=1")
                c.execute("UPDATE model_profiles SET is_active=1, updated_at=? WHERE name=?", (clock(), v))
            c.execute("INSERT INTO admin_settings(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                      "value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(v), clock()))
            audit(c, user, "config.set", key=key, value=v)
        return {"key": key, "value": v, "restart_required": key == "translate_profile"}

    @app.get(f"{API}/models/health")
    def models_health(user=Depends(need("viewer"))):
        out = {}
        for name in ("ollama", "llama_server", "live", "vad", "asr_worker"):
            probe = ctx.probes.get(name)
            if probe is None and name == "llama_server":
                probe = ctx.llama_probe
            try:
                out[name] = probe() if probe else "unknown"
            except Exception:
                out[name] = "down"
        return out

    # ============================================================ audit / events / jobs
    def event_list(kind_prefix, level, session_id, cursor, limit, since=None):
        after = dec_cursor(cursor, 1)
        sql, args = "SELECT id, ts, kind, level, actor_id, room_id, session_id, segment_id, payload FROM events WHERE 1=1", []
        if kind_prefix:
            sql += " AND kind LIKE ?"; args.append(kind_prefix.replace("%", "") + "%")
        if level:
            sql += " AND level=?"; args.append(level)
        if session_id:
            sql += " AND session_id=?"; args.append(session_id)
        if since is not None:
            sql += " AND ts>=?"; args.append(since)
        if after:
            sql += " AND id < ?"; args.append(after[0])
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit + 1)
        with conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            r["payload"] = json.loads(r["payload"]) if r["payload"] else None
        return {"items": rows[:limit], "next": enc_cursor([rows[limit - 1]["id"]]) if len(rows) > limit else None}

    @app.get(f"{API}/audit")
    def audit_log(cursor: str | None = None, in_session: str | None = None, limit: int = Query(100, ge=1, le=500),
                  user=Depends(need("admin"))):
        return event_list("audit.", None, in_session, cursor, limit)

    @app.get(f"{API}/events")
    def events(kind: str | None = None, level: str | None = None, in_session: str | None = None, since: float | None = None,
               cursor: str | None = None, limit: int = Query(100, ge=1, le=500), user=Depends(need("viewer"))):
        if level and level not in ("debug", "info", "warn", "error"):
            raise Problem(422, "invalid", "level 不正確")
        return event_list(kind, level, in_session, cursor, limit, since)

    # ============================================================ users / roles
    def ident():
        if ctx.identity_path is None:
            raise Problem(503, "no_identity", "未設定個資庫")
        return zdb.connect(ctx.identity_path)

    @app.get(f"{API}/users")
    def list_users(user=Depends(need("admin"))):
        with conn() as c:
            rows = [dict(r) for r in c.execute("SELECT id, role, disabled, created_at FROM users WHERE deleted_at IS NULL ORDER BY id")]
        ic = ident()
        try:
            names = {r[0]: (r[1], r[2]) for r in ic.execute("SELECT user_id, username, display_name FROM user_accounts")}
            toks = {}
            for r in ic.execute("SELECT user_id, COUNT(*) FROM api_tokens WHERE revoked_at IS NULL GROUP BY user_id"):
                toks[r[0]] = r[1]
        finally:
            ic.close()
        for r in rows:
            r["username"], r["display_name"] = names.get(r["id"], (None, None))
            r["tokens"] = toks.get(r["id"], 0)
        return {"items": rows}

    @app.post(f"{API}/users", status_code=201)
    async def create_user(request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)
        username = clean(body.get("username"), "username", 2, 40)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            raise Problem(422, "invalid", "username 只能用英數字")
        role = str(body.get("role") or "viewer")
        if role not in ROLES:
            raise Problem(422, "invalid", "role 只能是 admin、editor、host、viewer")
        if role == "admin" and user.get("role") != "owner":
            raise Problem(403, "forbidden", "只有擁有者能建立管理員")
        ic = ident()
        try:
            if ic.execute("SELECT 1 FROM user_accounts WHERE username=?", (username,)).fetchone():
                raise Problem(409, "conflict", "username 已存在")
            with conn(write=True) as c:
                c.execute("INSERT INTO users(role) VALUES (?)", (role,))
                uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                audit(c, user, "user.create", target_user=uid, role=role)
            ic.execute("INSERT INTO user_accounts(user_id, username, display_name) VALUES (?,?,?)",
                       (uid, username, (body.get("display_name") or None)))
            ic.commit()
        finally:
            ic.close()
        return JSONResponse({"id": uid, "username": username, "role": role}, status_code=201)

    @app.patch(f"{API}/users/{{uid}}")
    async def patch_user(uid: int, request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)
        if uid == user.get("user_id"):
            raise Problem(409, "self_change", "不能修改自己的角色或停用自己")
        with conn(write=True) as c:
            row = c.execute("SELECT role FROM users WHERE id=? AND deleted_at IS NULL", (uid,)).fetchone()
            if not row:
                raise Problem(404, "not_found", "找不到使用者")
            if row[0] == "owner" or (row[0] == "admin" and user.get("role") != "owner"):
                raise Problem(403, "forbidden", "權限不足以修改這位使用者")
            if "role" in body:
                if body["role"] not in ROLES or (body["role"] == "admin" and user.get("role") != "owner"):
                    raise Problem(422, "invalid", "role 不正確或需要擁有者權限")
                c.execute("UPDATE users SET role=? WHERE id=?", (body["role"], uid))
            if "disabled" in body:
                c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if body["disabled"] is True else 0, uid))
            audit(c, user, "user.update", target_user=uid, fields=sorted(body))
            out = dict(c.execute("SELECT id, role, disabled FROM users WHERE id=?", (uid,)).fetchone())
        return out

    @app.post(f"{API}/users/{{uid}}/tokens", status_code=201)
    async def create_token(uid: int, request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)
        scopes = str(body.get("scopes") or "read")
        if scopes not in ("read", "read,write"):
            raise Problem(422, "invalid", "scopes 只能是 read 或 read,write")
        with conn() as c:
            if not c.execute("SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (uid,)).fetchone():
                raise Problem(404, "not_found", "找不到使用者")
        from app.admin.security import token_hash
        plain = "zat_" + secrets.token_urlsafe(32)
        ic = ident()
        try:
            if not ic.execute("SELECT 1 FROM user_accounts WHERE user_id=?", (uid,)).fetchone():
                raise Problem(409, "conflict", "這位使用者沒有帳號資料")
            ic.execute("INSERT INTO api_tokens(user_id, token_hash, label, scopes, expires_at) VALUES (?,?,?,?,?)",
                       (uid, token_hash(plain), str(body.get("label") or "")[:40], scopes, clock() + 90 * 86400))
            tid = ic.execute("SELECT last_insert_rowid()").fetchone()[0]
            ic.commit()
        finally:
            ic.close()
        with conn(write=True) as c:
            audit(c, user, "token.create", target_user=uid, token_id=tid, scopes=scopes)
        return JSONResponse({"id": tid, "token": plain, "prefix": plain[:8], "shown_once": True}, status_code=201)

    @app.delete(f"{API}/tokens/{{tid}}")
    def revoke_token(tid: int, user=Depends(need("admin"))):
        ic = ident()
        try:
            n = ic.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (clock(), tid)).rowcount
            ic.commit()
        finally:
            ic.close()
        if not n:
            raise Problem(404, "not_found", "找不到權杖")
        with conn(write=True) as c:
            audit(c, user, "token.revoke", token_id=tid)
        return Response(status_code=204)

    # ============================================================ observability
    from app.admin import observability
    observability.register(app, ctx, session_row=session_row, export_rows=export_rows, event_list=event_list)
    return len(app.routes) - n0
