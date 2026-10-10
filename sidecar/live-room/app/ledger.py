"""Ledger: copy live caption events into zen.sqlite3 for the admin backend, search and TM.

Rules
- ``submit`` is called from ``server.on_event`` and never blocks and never raises. A full
  queue drops the event and counts it in ``dropped``.
- One background thread is the only writer. Any failure (no disk, locked file, bad schema,
  missing app.admin package) is logged and counted in ``errors``; captions are unaffected.
- Writes are batched (up to BATCH_MAX events or BATCH_WINDOW_S) in one BEGIN IMMEDIATE
  transaction with a SAVEPOINT per event, so one bad event does not lose the batch and a
  busy database never upgrades a read lock mid-transaction (database-review §5.2).
- Only UPSERT / plain INSERT is used (never INSERT OR REPLACE), so FTS triggers stay exact.
- The identity (PII) database is never opened here.
- Replaying the same event is idempotent: a transcript/translation version is only added
  when the text actually changed.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

try:
    from app.admin.db import to_uni
except Exception:  # pragma: no cover - app.admin not packaged: ledger then only counts errors
    def to_uni(text: str) -> str:
        return text

log = logging.getLogger("breeze.ledger")

_STOP = object()
_SKIP_TYPES = {"hello", "ping", "pong", "state", "listener_count"}
_FAIL_STATUSES = {"missing", "error", "timeout", "cancelled"}
_TRANSLATION_ORIGINS = {"mt", "post_edit", "human", "tm_exact", "import"}
_CAPTION_TYPES = {"caption", "final", "update"}
# Caption states worth an events row. In-progress versions (decoding, zh_ready) are not.
_EVENT_STATES = {"ready", "silent", "translate_failed", "missing", "error", "timeout", "cancelled"}
_REOPEN_AFTER_S = 30.0
BATCH_MAX = 200
BATCH_WINDOW_S = 0.05


def ledger_from_env(env: dict | None = None) -> "Ledger":
    """Opt-in: ZEN_LEDGER=1 turns it on (default off). Path: ZEN_DB_PATH, else %LOCALAPPDATA%\\ZenBridge\\data\\zen.sqlite3."""
    env = os.environ if env is None else env
    if (env.get("ZEN_LEDGER") or "0").strip() != "1":
        return Ledger(None)
    try:
        from app.admin.db import default_db_path
        path = default_db_path(env)
    except Exception:
        log.exception("ledger path could not be resolved; ledger disabled")
        return Ledger(None)
    return Ledger(path)


class Ledger:
    def __init__(self, path: str | Path | None, maxsize: int = 2000, connect=None, clock=time.time):
        self.enabled = bool(path)
        self.path = Path(path) if path else None
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self.engine = ""
        self.model = ""
        self._clock = clock
        self._connect = connect
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ producer side
    def submit(self, event) -> None:
        if not self.enabled or self._closed or not isinstance(event, dict):
            return
        try:
            if event.get("type") in _SKIP_TYPES:
                return
            self._ensure_thread()
            self._q.put_nowait(dict(event))
        except queue.Full:
            self.dropped += 1
        except Exception:  # pragma: no cover - defensive: never reach the caption path
            self.errors += 1

    def stats(self) -> dict:
        return {"ledger_dropped": self.dropped, "ledger_errors": self.errors, "ledger_written": self.written}

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Test helper: block until every submitted event was processed."""
        if not self.enabled or self._thread is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is None:
            return
        try:
            self._q.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        thread.join(timeout)

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="zen-ledger", daemon=True)
                self._thread.start()

    # ------------------------------------------------------------------ writer thread
    def _open(self) -> sqlite3.Connection:
        if self._connect is not None:
            return self._connect(self.path)
        from app.admin.db import connect, ensure_migrated
        ensure_migrated(self.path)
        return connect(self.path)

    def _collect(self, first) -> tuple[list, bool]:
        """First item plus whatever arrives within BATCH_WINDOW_S (max BATCH_MAX)."""
        batch, stop = [], False
        item = first
        deadline = time.monotonic() + BATCH_WINDOW_S
        while True:
            if item is _STOP:
                stop = True
                break
            batch.append(item)
            if len(batch) >= BATCH_MAX:
                break
            try:
                item = self._q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
        return batch, stop

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        failed_at = 0.0
        while True:
            first = self._q.get()
            batch, stop = self._collect(first)
            try:
                if not batch:
                    continue
                if conn is None:
                    if failed_at and self._clock() - failed_at < _REOPEN_AFTER_S:
                        self.errors += len(batch)
                        continue
                    try:
                        conn = self._open()
                        failed_at = 0.0
                    except Exception:
                        failed_at = self._clock()
                        self.errors += len(batch)
                        log.exception("ledger database unavailable: %s", self.path)
                        continue
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    ok = 0
                    for ev in batch:
                        conn.execute("SAVEPOINT ev")
                        try:
                            self._apply(conn, ev)
                            conn.execute("RELEASE ev")
                            ok += 1
                        except sqlite3.OperationalError:
                            raise          # busy / IO: whole batch
                        except Exception:
                            conn.execute("ROLLBACK TO ev")
                            conn.execute("RELEASE ev")
                            self.errors += 1
                            log.exception("ledger event skipped: %s", ev.get("type"))
                    conn.execute("COMMIT")
                    self.written += ok
                except Exception:
                    self.errors += len(batch)
                    log.exception("ledger batch failed (%d events)", len(batch))
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    except Exception:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
            finally:
                for _ in range(len(batch) + (1 if stop else 0)):
                    self._q.task_done()
                if stop:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    return

    def _apply(self, c: sqlite3.Connection, ev: dict) -> None:
        kind = str(ev.get("type") or "")
        seg_id = ev.get("id")
        if kind == "tm_hit":
            c.execute("UPDATE tm_units SET use_count = use_count + 1 WHERE id = ?", (int(ev["tm_id"]),))
            return
        if kind in _CAPTION_TYPES and seg_id and ev.get("room_id") and ev.get("session_id"):
            self._apply_caption(c, ev)
        elif kind == "caption_deleted":
            for sid in ev.get("ids") or ([seg_id] if seg_id else []):
                c.execute("UPDATE segments SET status='deleted' WHERE id=?", (str(sid),))
        if kind not in _CAPTION_TYPES or ev.get("status") in _EVENT_STATES:
            payload = {"status": ev.get("status"), "version": ev.get("version"), "translate_status": ev.get("translate_status")}
            c.execute(
                "INSERT INTO events(room_id, session_id, segment_id, kind, payload) VALUES (?,?,?,?,?)",
                (ev.get("room_id"), ev.get("session_id"), seg_id, f"live.{kind or 'unknown'}",
                 json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False)),
            )

    def _apply_caption(self, c: sqlite3.Connection, ev: dict) -> None:
        room, sess, seg = str(ev["room_id"]), str(ev["session_id"]), str(ev["id"])
        now = self._clock()
        c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
        c.execute(
            "INSERT OR IGNORE INTO sessions(id, room_id, session_ord, started_at) VALUES (?,?,?,?)",
            (sess, room, ev.get("session_ord"), now),
        )
        t0 = int(ev.get("t0_ms") or 0)
        t1 = max(t0, int(ev.get("t1_ms") or 0))
        c.execute(
            """INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status, speech_ratio)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 speech_ratio=COALESCE(excluded.speech_ratio, segments.speech_ratio),
                 t0_ms=CASE WHEN excluded.t0_ms > 0 THEN excluded.t0_ms ELSE segments.t0_ms END,
                 t1_ms=MAX(excluded.t1_ms, CASE WHEN excluded.t0_ms > 0 THEN excluded.t0_ms ELSE segments.t0_ms END)""",
            (seg, sess, room, int(ev.get("seq") or 0), t0, t1, segment_status(ev), _ratio(ev.get("speech_ratio"))),
        )
        zh = str(ev.get("zh") or "")
        if zh:
            cur = c.execute("SELECT text FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
            if not cur or cur[0] != zh:
                v = c.execute("SELECT COALESCE(MAX(version),0) FROM transcripts WHERE segment_id=?", (seg,)).fetchone()[0] + 1
                c.execute(
                    "INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni) VALUES (?,?,?,?,?)",
                    (seg, v, str(ev.get("zh_raw") or zh), zh, to_uni(zh)),
                )
        en = str(ev.get("en") or "")
        if en and (ev.get("translate_status") or "ok") == "ok":
            cur = c.execute(
                "SELECT text FROM translations WHERE segment_id=? AND tgt_lang='en' AND is_current=1", (seg,)
            ).fetchone()
            if not cur or cur[0] != en:
                v = c.execute(
                    "SELECT COALESCE(MAX(version),0) FROM translations WHERE segment_id=? AND tgt_lang='en'", (seg,)
                ).fetchone()[0] + 1
                tr = c.execute("SELECT id FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
                origin = ev.get("en_origin") if ev.get("en_origin") in _TRANSLATION_ORIGINS else "mt"
                engine = "tm" if origin == "tm_exact" else (self.engine or None)
                c.execute(
                    """INSERT INTO translations(segment_id, transcript_id, tgt_lang, version, text, origin, engine, model,
                                                glossary_version, term_flags, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (seg, tr[0] if tr else None, "en", v, en, origin, engine,
                     None if origin == "tm_exact" else (self.model or None), ev.get("glossary_version"),
                     json.dumps(ev.get("term_flags") or [], ensure_ascii=False), "ok"),
                )


def _ratio(value) -> float | None:
    try:
        return None if value is None else max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def segment_status(ev: dict) -> str:
    s = str(ev.get("status") or "")
    if s == "silent":
        return "silent"
    if s in _FAIL_STATUSES:
        return s
    if s == "ready":
        return "translated" if ev.get("en") else "asr_done"
    if ev.get("zh"):
        return "asr_done"
    return "received"
