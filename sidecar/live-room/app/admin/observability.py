"""總覽 / Observability: the admin landing page data.

System overview, hardware (psutil, guarded), pipeline telemetry (per-segment timeline, per-clip
ASR timings from the worker's BREEZE_TIMING log), data inventory, unified event feed, session
drill-down, persisted metrics (metrics table + daily rollups + retention) and CSV/JSON export.
Metrics rows carry numbers and room/session ids only: never caption text or user data.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import time
from pathlib import Path

from fastapi import Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.admin import db as zdb
from app.admin.info import METRIC_NAMES, hardware, parse_timing_log, redact, rtf_color

STALE_S = 10.0


def sample_row(live_metrics: dict, hw: dict) -> dict:
    out = {k: live_metrics.get(k) for k in METRIC_NAMES if isinstance(live_metrics.get(k), (int, float))
           and not isinstance(live_metrics.get(k), bool)}
    cores = hw.get("cpu_per_core") or []
    if cores:
        out["cpu_pct"] = round(sum(cores) / len(cores), 1)
    if hw.get("ram"):
        out["ram_pct"] = hw["ram"]["percent"]
    return out


def persist_sample(c: sqlite3.Connection, ts: float, values: dict) -> int:
    for name, v in values.items():
        c.execute("INSERT INTO metrics(ts, name, value) VALUES (?,?,?)", (ts, name, float(v)))
    return len(values)


def rollup_and_prune(c: sqlite3.Connection, now: float, keep_days: int | None = None) -> dict:
    """Roll raw metrics older than keep_days into metrics_rollup (per Taipei day) and delete them."""
    if keep_days is None:
        row = c.execute("SELECT keep_days FROM retention_policy WHERE item='metrics_raw'").fetchone()
        keep_days = row[0] if row and row[0] else 30
    cutoff = now - keep_days * 86400
    groups: dict = {}
    for ts, name, room, value in c.execute("SELECT ts, name, COALESCE(room_id,''), value FROM metrics WHERE ts < ?", (cutoff,)):
        day = time.strftime("%Y-%m-%d", time.gmtime(ts + 8 * 3600))
        groups.setdefault((day, name, room), []).append(value)
    for (day, name, room), vals in groups.items():
        s = sorted(vals)
        pct = lambda q: s[min(len(s) - 1, int(round(q * (len(s) - 1))))]  # noqa: E731
        c.execute("INSERT INTO metrics_rollup(day, name, room_id, n, p50, p95, max, avg) VALUES (?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(day, name, room_id) DO UPDATE SET n=n+excluded.n, p50=excluded.p50, p95=excluded.p95, "
                  "max=MAX(max, excluded.max), avg=excluded.avg",
                  (day, name, room, len(s), pct(0.5), pct(0.95), s[-1], sum(s) / len(s)))
    n = c.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,)).rowcount
    return {"rolled_groups": len(groups), "deleted": n, "keep_days": keep_days}


def register(app, ctx, *, session_row, export_rows, event_list):
    API, need, conn, Problem, clock, live = ctx.api, ctx.need, ctx.conn, ctx.Problem, ctx.clock, ctx.live
    state = {"last_sample": None, "last_live": None, "last_live_ts": None, "last_rollup": 0.0}

    def paths():
        return {"data": Path(ctx.db_path).parent, "backups": Path(ctx.backup_dir_fn())}

    def live_snapshot():
        try:
            m = live.metrics()
            state["last_live"], state["last_live_ts"] = m, clock()
            return m, None
        except Exception as exc:
            return state["last_live"], type(exc).__name__

    def take_sample() -> dict:
        m, err = live_snapshot()
        hw = hardware(paths())
        vals = sample_row(m or {} if err is None else {}, hw)
        now = clock()
        # QA dbtest P2: a busy zen.sqlite3 (ledger batch, backup) must not lose the sample.
        for attempt in range(5):
            try:
                with conn(write=True) as c:
                    persist_sample(c, now, vals)
                    if now - state["last_rollup"] > 3600:
                        rollup_and_prune(c, now)
                        state["last_rollup"] = now
                break
            except sqlite3.OperationalError as exc:
                if attempt == 4 or "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.05 * (2 ** attempt))
        state["last_sample"] = now
        return {"ts": now, "values": vals, "live_error": err}

    app.state.take_sample = take_sample

    async def sampler():
        while True:
            try:
                await asyncio.to_thread(take_sample)
            except Exception:
                pass
            await asyncio.sleep(ctx.metrics_sample_s)

    if ctx.metrics_sample_s and ctx.metrics_sample_s > 0:
        app.state.bg_tasks.append(sampler)

    def components() -> dict:
        out = {}
        for name in ("live", "ollama", "llama_server", "asr_worker", "vad"):
            probe = ctx.probes.get(name) or (ctx.llama_probe if name == "llama_server" else None)
            try:
                out[name] = {"status": probe() if probe else "unknown"}
            except Exception:
                out[name] = {"status": "down"}
        try:
            with conn() as c:
                v = c.execute("PRAGMA user_version").fetchone()[0]
            out["db"] = {"status": "ok", "schema_version": v, "sqlite": sqlite3.sqlite_version}
        except Exception as exc:
            out["db"] = {"status": f"error: {type(exc).__name__}"}
        out["admin"] = {"status": "up", "uptime_s": round(clock() - ctx.started_at, 1), "worker": ctx.worker.state}
        return out

    def timing_stats():
        clips = parse_timing_log(ctx.asr_log_path() if callable(ctx.asr_log_path) else ctx.asr_log_path)
        rtfs = [c["rtf"] for c in clips if isinstance(c.get("rtf"), (int, float))]
        return {"clips": clips[-50:], "count": len(clips),
                "rtf_avg": round(sum(rtfs) / len(rtfs), 3) if rtfs else None,
                "rtf_last": rtfs[-1] if rtfs else None,
                "repetition_guard_hits": sum(1 for c in clips if c.get("retried_full_ctx"))}

    def inventory():
        with conn() as c:
            q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
            counts = {
                "sessions": q("SELECT COUNT(*) FROM sessions"), "segments": q("SELECT COUNT(*) FROM segments"),
                "translations_en": q("SELECT COUNT(*) FROM translations WHERE tgt_lang='en' AND is_current=1"),
                "translations_ja": q("SELECT COUNT(*) FROM translations WHERE tgt_lang='ja' AND is_current=1"),
                "tm_live": q("SELECT COUNT(*) FROM tm_units WHERE quality>=3"),
                "tm_pending": q("SELECT COUNT(*) FROM tm_units WHERE quality=2"),
                "tm_disabled": q("SELECT COUNT(*) FROM tm_units WHERE quality<=1"),
                "glossary_en": q("SELECT COUNT(*) FROM glossary_terms t LEFT JOIN admin_glossary_lang l ON l.glossary_id=t.glossary_id "
                                 "WHERE COALESCE(l.tgt_lang,'en')='en' AND t.status='active'"),
                "glossary_ja": q("SELECT COUNT(*) FROM glossary_terms t JOIN admin_glossary_lang l ON l.glossary_id=t.glossary_id "
                                 "WHERE l.tgt_lang='ja' AND t.status='active'"),
                "glossary_proposed": q("SELECT COUNT(*) FROM glossary_terms WHERE status='proposed'"),
                "corrections": q("SELECT COUNT(*) FROM corrections"),
                "jobs": q("SELECT COUNT(*) FROM jobs"), "events": q("SELECT COUNT(*) FROM events"),
                "metrics_raw": q("SELECT COUNT(*) FROM metrics"), "metrics_rollup": q("SELECT COUNT(*) FROM metrics_rollup"),
            }
            retention = [dict(r) for r in c.execute("SELECT item, keep_days FROM retention_policy ORDER BY item")]
        sizes = {}
        for name, p in (("main_db", Path(ctx.db_path)), ("identity_db", Path(ctx.identity_path) if ctx.identity_path else None)):
            if p is not None:
                sizes[name] = sum(f.stat().st_size for f in p.parent.glob(p.name + "*") if f.is_file())
        bdir = Path(ctx.backup_dir_fn())
        backups = zdb.list_backups(bdir) if bdir.is_dir() else []
        last = backups[-1] if backups else None
        verified = False
        if last is not None:
            stamp = last.name.split("-", 1)[-1].rsplit(".", 1)[0]
            verified = any(m.is_file() for m in bdir.glob(f"*{stamp}*manifest*.json"))
        age = zdb.latest_backup_age_s(bdir, clock())
        return {"counts": counts, "sizes": sizes, "retention": retention,
                "backups": {"count": len(backups), "last": last.name if last else None,
                            "last_age_s": None if age is None else int(age), "manifest_present": verified,
                            "bytes": sum(b.stat().st_size for b in backups)}}

    def pipeline():
        with conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT g.id AS segment_id, g.session_id, g.seq, g.status, g.created_at AS received_at, "
                "t.created_at AS asr_done_at, t.asr_ms, t.audio_ms, tr.created_at AS translated_at, tr.latency_ms, "
                "tr.status AS mt_status, (SELECT MIN(e.ts) FROM events e WHERE e.segment_id=g.id AND e.kind LIKE '%publish%') AS published_at "
                "FROM segments g LEFT JOIN transcripts t ON t.segment_id=g.id AND t.version=1 "
                "LEFT JOIN translations tr ON tr.segment_id=g.id AND tr.version=1 ORDER BY g.created_at DESC LIMIT 50")]
            agg = {
                "stale": c.execute("SELECT COUNT(*) FROM translations WHERE status='stale'").fetchone()[0],
                "skipped": c.execute("SELECT COUNT(*) FROM translations WHERE status='skipped'").fetchone()[0],
                "merged": c.execute("SELECT COUNT(*) FROM translations WHERE status='merged'").fetchone()[0],
                "locked_term_violations": c.execute(
                    "SELECT COUNT(*) FROM translations WHERE is_current=1 AND term_flags <> '[]'").fetchone()[0],
                "failed_segments": c.execute(
                    "SELECT COUNT(*) FROM segments WHERE status IN ('error','timeout','missing')").fetchone()[0],
            }
            rtf_trend = [(r[0], r[1]) for r in c.execute(
                "SELECT ts, value FROM metrics WHERE name='rtf' ORDER BY ts DESC LIMIT 120")][::-1]
        for r in rows:          # timeline: audio end -> ASR start/end -> translate start/end -> published
            asr_end = r["asr_done_at"]
            r["timeline"] = {
                "audio_end": r["received_at"],
                "asr_start": (asr_end - r["asr_ms"] / 1000) if asr_end and r["asr_ms"] else None,
                "asr_end": asr_end,
                "translate_start": (r["translated_at"] - r["latency_ms"] / 1000) if r["translated_at"] and r["latency_ms"] else None,
                "translate_end": r["translated_at"], "published": r["published_at"]}
        return {"segments": rows, "counts": agg, "rtf_trend": rtf_trend}

    @app.get(f"{API}/overview")
    def overview(user=Depends(need("viewer"))):
        m, err = live_snapshot()
        age = None if state["last_live_ts"] is None else clock() - state["last_live_ts"]
        cfg = {}
        try:
            cfg = redact(ctx.config_fn()) if ctx.config_fn else {}
        except Exception:
            cfg = {}
        rtf = (m or {}).get("rtf")
        return {"components": components(), "live": m, "live_error": err, "live_age_s": age,
                "stale": age is None or age > STALE_S, "rtf": rtf, "rtf_color": rtf_color(rtf),
                "config": cfg, "ts": clock()}

    @app.get(f"{API}/overview/hardware")
    def hw(user=Depends(need("viewer"))):
        return hardware(paths())

    @app.get(f"{API}/overview/pipeline")
    def pipe(user=Depends(need("viewer"))):
        return {**pipeline(), "asr": timing_stats()}

    @app.get(f"{API}/overview/asr-timings")
    def asr_timings(user=Depends(need("viewer"))):
        return timing_stats()

    @app.get(f"{API}/overview/inventory")
    def inv(user=Depends(need("viewer"))):
        return inventory()

    @app.get(f"{API}/overview/feed")
    def feed(level: str | None = None, kind: str | None = None, limit: int = Query(100, ge=1, le=500),
             user=Depends(need("viewer"))):
        ev = event_list(kind, level, None, None, limit)["items"]
        jobs = ctx.store.list(state=None, kind=None, before=None, limit=50)
        if user.get("role") not in ("admin", "owner"):
            ev = [e for e in ev if not str(e["kind"]).startswith("audit.")]     # audit detail is admin-only
        return {"events": ev, "jobs": jobs}

    @app.get(f"{API}/sessions/{{sid}}/drilldown")
    def drilldown(sid: str, user=Depends(need("viewer"))):
        with conn() as c:
            s = session_row(c, sid)
            segs = [dict(r) for r in c.execute(
                "SELECT g.id, g.seq, g.t0_ms, g.t1_ms, g.status, t.text AS zh, t.origin AS zh_origin, t.version AS zh_version, "
                "t.asr_ms, t.audio_ms, tr.text AS tgt, tr.origin AS tgt_origin, tr.version AS tgt_version, tr.latency_ms, "
                "tr.term_flags FROM segments g LEFT JOIN transcripts t ON t.segment_id=g.id AND t.is_current=1 "
                "LEFT JOIN translations tr ON tr.segment_id=g.id AND tr.tgt_lang=? AND tr.is_current=1 "
                "WHERE g.session_id=? ORDER BY g.seq LIMIT 2000", (s["tgt_lang"], sid))]
            corr = [dict(r) for r in c.execute(
                "SELECT c.id, c.segment_id, c.target_type, c.before_text, c.after_text, c.status, c.created_at FROM corrections c "
                "JOIN segments g ON g.id=c.segment_id WHERE g.session_id=? ORDER BY c.id", (sid,))]
            errors = [dict(r) for r in c.execute(
                "SELECT id, ts, kind, level, segment_id FROM events WHERE session_id=? AND level IN ('warn','error') "
                "ORDER BY id DESC LIMIT 200", (sid,))]
            exports = [dict(r) for r in c.execute("SELECT id, format, variant, created_at FROM exports WHERE session_id=? "
                                                  "ORDER BY id DESC", (sid,))]
        asr = [g["asr_ms"] for g in segs if g["asr_ms"]]
        mt = [g["latency_ms"] for g in segs if g["latency_ms"]]
        rtfs = [g["asr_ms"] / g["audio_ms"] for g in segs if g["asr_ms"] and g["audio_ms"]]
        timings = {"asr_ms_avg": round(sum(asr) / len(asr)) if asr else None,
                   "mt_ms_avg": round(sum(mt) / len(mt)) if mt else None,
                   "rtf_avg": round(sum(rtfs) / len(rtfs), 3) if rtfs else None}
        return {"session": s, "segments": segs, "corrections": corr, "errors": errors, "exports": exports, "timings": timings}

    @app.post(f"{API}/metrics/sample")
    def sample_now(user=Depends(need("admin"))):
        return take_sample()

    @app.post(f"{API}/metrics/rollup")
    def rollup(user=Depends(need("admin"))):
        with conn(write=True) as c:
            return rollup_and_prune(c, clock())

    @app.get(f"{API}/metrics/export")
    def metrics_export(name: str | None = None, since: float | None = None, until: float | None = None,
                       format: str = "csv", rollup: int = 0, user=Depends(need("viewer"))):
        if format not in ("csv", "json"):
            raise Problem(422, "invalid", "format 只能是 csv 或 json")
        with conn() as c:
            if rollup:
                sql, args, cols = "SELECT day, name, room_id, n, p50, p95, max, avg FROM metrics_rollup WHERE 1=1", [], \
                    ["day", "name", "room_id", "n", "p50", "p95", "max", "avg"]
                if name:
                    sql += " AND name=?"; args.append(name)
                rows = [list(r) for r in c.execute(sql + " ORDER BY day LIMIT 100000", args)]
            else:
                sql, args, cols = "SELECT ts, name, value, room_id, session_id FROM metrics WHERE 1=1", [], \
                    ["ts", "name", "value", "room_id", "session_id"]
                if name:
                    sql += " AND name=?"; args.append(name)
                if since is not None:
                    sql += " AND ts>=?"; args.append(since)
                if until is not None:
                    sql += " AND ts<=?"; args.append(until)
                rows = [list(r) for r in c.execute(sql + " ORDER BY ts LIMIT 100000", args)]
        if format == "json":
            return {"columns": cols, "rows": rows}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="metrics.csv"'})

    @app.get(f"{API}/monitor/stream")
    async def monitor_stream(request: Request, user=Depends(need("viewer"))):
        """SSE dashboard: live metrics + rtf colour + stale flag + recent errors. Same auth and
        per-identity caps as /live/stream; logout ends it with event: logout."""
        from app.admin.security import COOKIE_NAME
        sse_open = ctx.sse_open
        ident = "bearer" if user.get("via") == "bearer" else f"u{user.get('user_id')}:{request.cookies.get(COOKIE_NAME, '')[:12]}"
        if sse_open.get(ident, 0) >= ctx.sse_per_user or sum(sse_open.values()) >= ctx.sse_total:
            raise Problem(429, "too_many_streams", "同時開啟的即時串流太多，請關掉其他分頁")
        sse_open[ident] = sse_open.get(ident, 0) + 1

        async def gen():
            try:
                yield "retry: 3000\n\n"
                sent, last_err = 0, None
                while ctx.sse_max_events is None or sent < ctx.sse_max_events:
                    if await request.is_disconnected():
                        break
                    if not await asyncio.to_thread(ctx.authenticate, request):
                        yield "event: logout\ndata: {}\n\n"
                        break
                    m, err = await asyncio.to_thread(live_snapshot)
                    age = None if state["last_live_ts"] is None else clock() - state["last_live_ts"]
                    rtf = (m or {}).get("rtf")
                    payload = {"metrics": m, "live_error": err, "stale": err is not None or age is None or age > STALE_S,
                               "age_s": age, "rtf_color": rtf_color(rtf), "ts": clock()}
                    sent += 1
                    yield f"id: {sent}\nevent: metrics\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    errs = await asyncio.to_thread(lambda: event_list(None, "error", None, None, 5)["items"])
                    if errs and errs[0]["id"] != last_err:
                        last_err = errs[0]["id"]
                        yield f"event: errors\ndata: {json.dumps(errs, ensure_ascii=False, default=str)}\n\n"
                    if ctx.sse_max_events is not None and sent >= ctx.sse_max_events:
                        break
                    await asyncio.sleep(ctx.sse_interval_s)
            finally:
                sse_open[ident] = max(0, sse_open.get(ident, 1) - 1)
                if not sse_open[ident]:
                    sse_open.pop(ident, None)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
