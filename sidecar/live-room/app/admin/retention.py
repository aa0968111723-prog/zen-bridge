"""Retention enforcement and whole-session delete across zen.sqlite3 + zen-identity.sqlite3.

DBA D4 / 備份 D2: ``retention_policy`` existed but nothing enforced it.

* ``purge_session``: deletes one session everywhere. Main DB (one IMMEDIATE transaction):
  segments, transcripts, translations, corrections, speakers, summaries, exports (FK cascade);
  FTS rows and embeddings (schema triggers); ``events`` and ``metrics`` rows of the session
  (no FK by design). TM units survive (policy: glossary_tm is a curated asset) but lose their
  segment link. A ``session_purged`` tombstone (no content) stops the live ledger from
  re-creating the session from late events. Identity DB: ``speaker_identities`` (and
  voiceprints by cascade) for the session. The two files cannot share one atomic commit in
  WAL mode, so the identity step runs after the main commit and ``sweep_identity_orphans``
  repairs a crash in between (it runs on every enforcement pass). Refuses legal_hold sessions,
  and live sessions unless ``force``.
* ``enforce_retention``: applies every policy row with keep_days (NULL = keep forever).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from app.admin import db as zdb

log = logging.getLogger("zen.admin.retention")
DAY = 86400.0


class PurgeRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _policy(c: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in c.execute("SELECT item, keep_days FROM retention_policy")}


def _identity_conn(identity_path: Path | None):
    if identity_path is None or not Path(identity_path).exists():
        return None
    return zdb.connect(identity_path)


def purge_session(main_path: Path, identity_path: Path | None, session_id: str, *, force: bool = False,
                  actor_id: int | None = None, reason: str = "manual", clock=time.time) -> dict:
    c = zdb.connect(main_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT room_id, status, ended_at, legal_hold FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            c.execute("ROLLBACK")
            raise PurgeRefused("not_found", "找不到這個場次")
        room_id, status, ended_at, hold = row
        if hold:
            c.execute("ROLLBACK")
            raise PurgeRefused("legal_hold", "這個場次設了保全（legal_hold），不能刪除")
        if status == "live" and ended_at is None and not force:
            c.execute("ROLLBACK")
            raise PurgeRefused("session_live", "場次還在進行中；請先結束場次，或明確要求強制刪除")
        counts = {
            "segments": c.execute("SELECT COUNT(*) FROM segments WHERE session_id=?", (session_id,)).fetchone()[0],
            "events": c.execute("DELETE FROM events WHERE session_id=?", (session_id,)).rowcount,
            "metrics": c.execute("DELETE FROM metrics WHERE session_id=?", (session_id,)).rowcount,
        }
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))      # FK cascade + FTS/embedding triggers
        c.execute("INSERT INTO events(room_id, session_id, kind, level, actor_id, payload) VALUES (?,?,?,?,?,?)",
                  (room_id, session_id, "session_purged", "warn", actor_id,
                   json.dumps({"reason": reason, "segments": counts["segments"], "at": clock()})))
        c.execute("COMMIT")
    except PurgeRefused:
        raise
    except Exception:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    counts["identity"] = _purge_identity(identity_path, [session_id])
    log.info("purged session %s (%s): %s", session_id, reason, counts)
    return {"session_id": session_id, "deleted": counts}


def _purge_identity(identity_path: Path | None, session_ids: list[str]) -> int:
    ic = _identity_conn(identity_path)
    if ic is None or not session_ids:
        return 0
    try:
        ic.execute("BEGIN IMMEDIATE")
        n = 0
        for sid in session_ids:
            n += ic.execute("DELETE FROM speaker_identities WHERE session_id=?", (sid,)).rowcount
        ic.execute("COMMIT")
        return n
    except Exception:
        if ic.in_transaction:
            ic.execute("ROLLBACK")
        raise
    finally:
        ic.close()


def sweep_identity_orphans(main_path: Path, identity_path: Path | None) -> int:
    """Identity rows whose session no longer exists in the main DB (crash between the two commits)."""
    ic = _identity_conn(identity_path)
    if ic is None:
        return 0
    try:
        sids = [r[0] for r in ic.execute("SELECT DISTINCT session_id FROM speaker_identities")]
    finally:
        ic.close()
    if not sids:
        return 0
    c = zdb.connect(main_path)
    try:
        alive = {r[0] for r in c.execute(
            f"SELECT id FROM sessions WHERE id IN ({','.join('?' * len(sids))})", sids)}
    finally:
        c.close()
    return _purge_identity(identity_path, [s for s in sids if s not in alive])


def due_sessions(c: sqlite3.Connection, now: float, keep_days) -> list[str]:
    if keep_days is None:
        keep_cut = None
    else:
        keep_cut = now - float(keep_days) * DAY
    rows = c.execute(
        "SELECT id, purge_after, COALESCE(ended_at, started_at) FROM sessions "
        "WHERE legal_hold=0 AND NOT (status='live' AND ended_at IS NULL)").fetchall()
    out = []
    for sid, purge_after, ref in rows:
        if purge_after is not None:
            if purge_after <= now:
                out.append(sid)
        elif keep_cut is not None and ref is not None and ref < keep_cut:
            out.append(sid)
    return out


def enforce_retention(main_path: Path, identity_path: Path | None = None, *, now: float | None = None,
                      purge_sessions: bool = True) -> dict:
    """purge_sessions=False: everything except whole-session deletes (the server passes False
    while there is no backup from the last 24 h, so expired sessions are never deleted unbacked)."""
    now = time.time() if now is None else now
    c = zdb.connect(main_path)
    try:
        pol = _policy(c)
        sessions = due_sessions(c, now, pol.get("session_content")) if purge_sessions else []
    finally:
        c.close()
    done: dict = {"sessions": [], "refused": [], "sessions_deferred": not purge_sessions}
    for sid in sessions:
        try:
            purge_session(main_path, identity_path, sid, reason="retention")
            done["sessions"].append(sid)
        except PurgeRefused as exc:
            done["refused"].append({"session_id": sid, "code": exc.code})
    c = zdb.connect(main_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        kd = pol.get("transcripts_noncurrent")
        if kd is not None:
            done["transcripts_noncurrent"] = c.execute(
                "DELETE FROM transcripts WHERE is_current=0 AND created_at < ?", (now - kd * DAY,)).rowcount
        kd = pol.get("text_raw")
        if kd is not None:
            done["text_raw"] = c.execute(
                "UPDATE transcripts SET text_raw=NULL WHERE text_raw IS NOT NULL AND segment_id IN ("
                " SELECT g.id FROM segments g JOIN sessions s ON s.id=g.session_id"
                " WHERE s.ended_at IS NOT NULL AND s.ended_at < ? AND s.legal_hold=0)", (now - kd * DAY,)).rowcount
        kd = pol.get("events_debug_info")
        if kd is not None:
            done["events_debug_info"] = c.execute(
                "DELETE FROM events WHERE level IN ('debug','info') AND ts < ?", (now - kd * DAY,)).rowcount
        kd = pol.get("events_warn_error")
        if kd is not None:
            # tombstones stay as long as purged sessions could still send late events (warn level)
            done["events_warn_error"] = c.execute(
                "DELETE FROM events WHERE level IN ('warn','error') AND ts < ?", (now - kd * DAY,)).rowcount
        kd = pol.get("metrics_raw")
        if kd is not None:
            cut = now - kd * DAY
            c.execute(
                "INSERT OR IGNORE INTO metrics_rollup(day, name, room_id, n, avg, max) "
                "SELECT date(ts, 'unixepoch', '+8 hours'), name, COALESCE(room_id, ''), COUNT(*), AVG(value), MAX(value) "
                "FROM metrics WHERE ts < ? GROUP BY 1, 2, 3", (cut,))
            done["metrics_raw"] = c.execute("DELETE FROM metrics WHERE ts < ?", (cut,)).rowcount
        # DBA D11: idempotent responses (may quote captions) live 24 h; expired admin sessions go too.
        done["idempotency_keys"] = c.execute("DELETE FROM idempotency_keys WHERE created_at < ?",
                                             (now - DAY,)).rowcount
        done["admin_sessions"] = c.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,)).rowcount
        c.execute("COMMIT")
    except Exception:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    done["identity_orphans"] = sweep_identity_orphans(main_path, identity_path)
    return done
