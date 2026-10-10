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
    def to_uni(text: str):
        return None     # DBA D10: NULL text_uni sends 1-2 char searches to the instr fallback

log = logging.getLogger("breeze.ledger")

_STOP = object()
_SKIP_TYPES = {"hello", "ping", "pong", "state", "listener_count"}
_FAIL_STATUSES = {"missing", "error", "timeout", "cancelled"}
_TRANSLATION_ORIGINS = {"mt", "post_edit", "human", "tm_exact", "import"}
_HUMAN_ORIGINS = {"human", "post_edit"}
_CAPTION_TYPES = {"caption", "final", "update"}
# Caption states worth an events row. In-progress versions (decoding, zh_ready) are not.
_EVENT_STATES = {"ready", "silent", "translate_failed", "missing", "error", "timeout", "cancelled"}
_REOPEN_AFTER_S = 30.0


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    msg = str(exc).lower()
    return "database is locked" in msg or "busy" in msg or "locked" in msg
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
    def __init__(self, path: str | Path | None, maxsize: int = 2000, connect=None, clock=time.time,
                 busy_retries: int = 8, sleep=time.sleep):
        self.enabled = bool(path)
        self.retries = 0
        self.busy_retries = busy_retries      # ~0.1+0.2+...+2 s of backoff plus busy_timeout per try
        self._sleep = sleep
        self.path = Path(path) if path else None
        self.dropped = 0
        self.spooled = 0
        self.replayed = 0
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
        self._spool_lock = threading.Lock()

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
        return {"ledger_dropped": self.dropped, "ledger_errors": self.errors, "ledger_written": self.written,
                "ledger_retries": self.retries,
                "ledger_spooled": self.spooled, "ledger_replayed": self.replayed}

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

    def close(self, timeout: float = 5.0) -> None:
        """全站 D7: whatever the writer could not finish within ``timeout`` is spooled to disk
        (replayed at the next start) instead of being lost with the process."""
        if self._closed:
            return
        self._closed = True
        thread = self._thread
        if thread is None:
            return
        try:
            self._q.put(_STOP, timeout=timeout)
        except queue.Full:
            pass
        thread.join(timeout)
        if thread.is_alive() or not self._q.empty():
            left = []
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP:
                    left.append(item)
                self._q.task_done()
            if left:
                self._spool(left)
                log.warning("ledger closed with %d events pending; spooled for the next start", len(left))

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
                        self._spool(batch)          # 全站 D6: keep it, replay after reopen
                        continue
                    try:
                        conn = self._open()
                        failed_at = 0.0
                    except Exception:
                        failed_at = self._clock()
                        self._spool(batch)
                        log.exception("ledger database unavailable: %s", self.path)
                        continue
                attempt = 0
                while True:
                    try:
                        self._write_batch(conn, batch)
                        self._replay(conn)
                        self._maybe_checkpoint(conn, len(batch))
                        break
                    except Exception as exc:
                        try:
                            if conn.in_transaction:
                                conn.execute("ROLLBACK")
                        except Exception:
                            pass
                        # QA 資料庫管理員 D1 / 實測專家 D3: BUSY/LOCKED keeps the batch and retries
                        # with backoff (new events keep queueing meanwhile); only after the total
                        # retry budget is it counted as errors.
                        if _is_busy(exc) and attempt < self.busy_retries and not stop:
                            self.retries += 1
                            self._sleep(min(2.0, 0.1 * (2 ** attempt)))
                            attempt += 1
                            continue
                        # No batch drop: spool to disk and replay after the next successful write.
                        self._spool(batch)
                        log.warning("ledger batch spooled (%d events): %s", len(batch), type(exc).__name__)
                        if not _is_busy(exc):
                            # I/O error / corruption: drop the connection, reopen later.
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = None
                            failed_at = self._clock()
                        break
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

    # ------------------------------------------------------------------ durable spool
    SPOOL_MAX_BYTES = 64 * 1024 * 1024

    @property
    def spool_path(self) -> Path | None:
        return self.path.with_name(self.path.name + ".spool.jsonl") if self.path else None

    def _spool(self, batch: list) -> None:
        """Append events that could not be written (BUSY budget spent, I/O error, DB not openable,
        close() timeout). Same sensitivity as the DB itself: it sits next to it. Only counted as
        errors if this fails."""
        sp = self.spool_path
        try:
            if sp is None:
                raise OSError("no path")
            with self._spool_lock:
                if sp.exists() and sp.stat().st_size > self.SPOOL_MAX_BYTES:
                    raise OSError("spool full")
                sp.parent.mkdir(parents=True, exist_ok=True)
                with open(sp, "a", encoding="utf-8") as fh:
                    for ev in batch:
                        fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self.spooled += len(batch)
        except Exception:
            self.errors += len(batch)
            log.exception("ledger spool failed; %d events lost", len(batch))

    def _replay(self, conn: sqlite3.Connection) -> None:
        """Write spooled events back (oldest first). Interrupted: the unwritten rest goes back."""
        sp = self.spool_path
        if sp is None or not sp.exists():
            return
        work = sp.with_name(sp.name + ".replay")
        try:
            os.replace(sp, work)
        except OSError:
            return
        lines = work.read_text(encoding="utf-8").splitlines()
        done = 0
        try:
            while done < len(lines):
                chunk = []
                for line in lines[done:done + BATCH_MAX]:
                    try:
                        chunk.append(json.loads(line))
                    except ValueError:
                        self.errors += 1
                if chunk:
                    self._write_batch(conn, chunk)
                done += len(lines[done:done + BATCH_MAX])
                self.replayed += len(chunk)
            work.unlink()
        except Exception:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except Exception:
                pass
            later = sp.read_text(encoding="utf-8") if sp.exists() else ""
            rest = "".join(line + "\n" for line in lines[done:])
            sp.write_text(rest + later, encoding="utf-8")
            work.unlink()
            log.warning("ledger spool replay interrupted; %d events kept for later", len(lines) - done)

    # QA dbtest P1: with a long reader open (admin search/export) the WAL grew to 216 MB over 2 h.
    # A TRUNCATE checkpoint every 300 events or 5 min capped it at 28 MB (max 177 ms on the 5600H).
    CHECKPOINT_EVERY = int(os.environ.get("ZEN_LEDGER_CHECKPOINT_EVERY") or 300)
    CHECKPOINT_S = float(os.environ.get("ZEN_LEDGER_CHECKPOINT_S") or 300)

    def _maybe_checkpoint(self, conn: sqlite3.Connection, n: int) -> None:
        self._since_ckpt = getattr(self, "_since_ckpt", 0) + n
        last = getattr(self, "_ckpt_at", None)
        now = self._clock()
        if last is None:
            self._ckpt_at = last = now
        if self._since_ckpt < self.CHECKPOINT_EVERY and now - last < self.CHECKPOINT_S:
            return
        self._since_ckpt, self._ckpt_at = 0, now
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.checkpoints = getattr(self, "checkpoints", 0) + 1
            if row and row[0]:
                log.info("ledger checkpoint busy (reader open): log=%s moved=%s", row[1], row[2])
        except sqlite3.Error:
            log.warning("ledger checkpoint failed", exc_info=True)

    def _write_batch(self, conn: sqlite3.Connection, batch: list) -> None:
        conn.execute("BEGIN IMMEDIATE")
        ok = skipped = 0
        for ev in batch:
            conn.execute("SAVEPOINT ev")
            try:
                self._apply(conn, ev)
                conn.execute("RELEASE ev")
                ok += 1
            except sqlite3.OperationalError:
                raise          # busy / IO: whole batch (retried by the caller)
            except Exception:
                conn.execute("ROLLBACK TO ev")
                conn.execute("RELEASE ev")
                skipped += 1
                log.exception("ledger event skipped: %s", ev.get("type"))
        conn.execute("COMMIT")
        self.written += ok
        self.errors += skipped

    def _apply(self, c: sqlite3.Connection, ev: dict) -> None:
        kind = str(ev.get("type") or "")
        seg_id = ev.get("id")
        if kind == "tm_hit":
            c.execute("UPDATE tm_units SET use_count = use_count + 1 WHERE id = ?", (int(ev["tm_id"]),))
            return
        sess = ev.get("session_id")
        if sess and c.execute("SELECT 1 FROM events WHERE kind='session_purged' AND session_id=? AND "
                              "(room_id IS NULL OR room_id=? OR ? IS NULL) LIMIT 1",
                              (str(sess), ev.get("room_id"), ev.get("room_id"))).fetchone() or (
                sess and ev.get("room_id") and c.execute(
                    "SELECT 1 FROM events WHERE kind='session_purged' AND session_id=? LIMIT 1",
                    (f"{ev['room_id']}:{sess}",)).fetchone()):
            return   # the admin purged this whole session: late live events must not resurrect it
        if kind in _CAPTION_TYPES and seg_id and ev.get("room_id") and ev.get("session_id"):
            self._apply_caption(c, ev)
        elif kind == "caption_deleted":
            # QA 全站測試長 D1: the host's delete reaches the archive. The segment row stays as a
            # tombstone (status='deleted', no text) so late updates cannot resurrect it; the text
            # itself is removed: transcripts/translations (FTS + embeddings via triggers), corrections.
            ids = [str(sid) for sid in (ev.get("ids") or ([seg_id] if seg_id else []))]
            for sid in ids:
                c.execute("UPDATE segments SET status='deleted' WHERE id=?", (sid,))
            self._erase_text(c, ids)
        elif kind == "captions_cleared" and ev.get("room_id"):
            ids = [r[0] for r in c.execute("SELECT id FROM segments WHERE room_id=? AND status<>'deleted'",
                                           (str(ev["room_id"]),))]
            c.execute("UPDATE segments SET status='deleted' WHERE room_id=? AND status<>'deleted'", (str(ev["room_id"]),))
            self._erase_text(c, ids)
        if kind not in _CAPTION_TYPES or ev.get("status") in _EVENT_STATES:
            payload = {"status": ev.get("status"), "version": ev.get("version"), "translate_status": ev.get("translate_status")}
            c.execute(
                "INSERT INTO events(room_id, session_id, segment_id, kind, payload) VALUES (?,?,?,?,?)",
                (ev.get("room_id"), ev.get("session_id"), seg_id, f"live.{kind or 'unknown'}",
                 json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False)),
            )

    @staticmethod
    def _erase_text(c: sqlite3.Connection, seg_ids: list[str]) -> None:
        for sid in seg_ids:
            c.execute("DELETE FROM corrections WHERE segment_id=?", (sid,))
            # architect A1: the host deleted this line - TM entries learned from it go too
            c.execute("DELETE FROM tm_units WHERE segment_id=?", (sid,))
            c.execute("DELETE FROM translations WHERE segment_id=?", (sid,))
            c.execute("DELETE FROM transcripts WHERE segment_id=?", (sid,))

    def _apply_caption(self, c: sqlite3.Connection, ev: dict) -> None:
        room, sess, seg = str(ev["room_id"]), str(ev["session_id"]), str(ev["id"])
        now = self._clock()
        c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
        # 全站 D8: sessions.id is global; a second room reusing the same session_id gets its own
        # namespaced row instead of every segment failing the (session_id, room_id) FK.
        owner = c.execute("SELECT room_id FROM sessions WHERE id=?", (sess,)).fetchone()
        if owner is not None and owner[0] != room:
            sess = f"{room}:{sess}"
        c.execute(
            "INSERT OR IGNORE INTO sessions(id, room_id, session_ord, started_at) VALUES (?,?,?,?)",
            (sess, room, ev.get("session_ord"), now),
        )
        t0 = int(ev.get("t0_ms") or 0)
        t1 = max(t0, int(ev.get("t1_ms") or 0))
        c.execute(
            """INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status, speech_ratio)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status=CASE WHEN segments.status='deleted' THEN 'deleted' ELSE excluded.status END,
                 speech_ratio=COALESCE(excluded.speech_ratio, segments.speech_ratio),
                 t0_ms=CASE WHEN excluded.t0_ms > 0 THEN excluded.t0_ms ELSE segments.t0_ms END,
                 t1_ms=MAX(excluded.t1_ms, CASE WHEN excluded.t0_ms > 0 THEN excluded.t0_ms ELSE segments.t0_ms END)""",
            (seg, sess, room, int(ev.get("seq") or 0), t0, t1, segment_status(ev), _ratio(ev.get("speech_ratio"))),
        )
        if c.execute("SELECT status FROM segments WHERE id=?", (seg,)).fetchone()[0] == "deleted":
            return                         # a late update never resurrects deleted text
        zh = str(ev.get("zh") or "")
        if zh:
            cur = c.execute("SELECT text, origin FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
            # QA 全端工程師 B2: an ASR replay never replaces a human transcript correction.
            if cur and cur[1] == "human" and cur[0] != zh:
                log.info("ledger: kept human transcript for %s (live sent different ASR text)", seg)
            elif not cur or cur[0] != zh:
                v = c.execute("SELECT COALESCE(MAX(version),0) FROM transcripts WHERE segment_id=?", (seg,)).fetchone()[0] + 1
                c.execute(
                    "INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni) VALUES (?,?,?,?,?)",
                    (seg, v, str(ev.get("zh_raw") or zh), zh, to_uni(zh)),
                )
        en = str(ev.get("en") or "")
        if en and (ev.get("translate_status") or "ok") == "ok":
            cur = c.execute(
                "SELECT text, origin FROM translations WHERE segment_id=? AND tgt_lang='en' AND is_current=1", (seg,)
            ).fetchone()
            incoming = ev.get("en_origin") if ev.get("en_origin") in _TRANSLATION_ORIGINS else "mt"
            # QA 全端工程師 B1: machine output never becomes current over a human correction.
            if cur and cur[1] in _HUMAN_ORIGINS and incoming not in _HUMAN_ORIGINS and cur[0] != en:
                log.info("ledger: kept human translation for %s (live sent %s)", seg, incoming)
            elif not cur or cur[0] != en:
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
                     json.dumps(ev.get("term_flags") or [], ensure_ascii=False),
                     # 全站 D4: EN covers earlier stale lines too; never pair it 1:1 with this zh.
                     "merged" if ev.get("en_merged_from") else "ok"),
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
