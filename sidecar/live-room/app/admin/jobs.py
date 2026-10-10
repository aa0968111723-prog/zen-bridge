"""Long-running admin work: jobs table in zen.sqlite3 + one in-process asyncio worker.

POST endpoints return 202 + Location: /admin/api/v1/jobs/{id}. The worker claims one job
at a time (lease + heartbeat), runs its handler (sync handlers in a thread), and records
progress, result or a problem+json error. On start, running jobs whose lease expired are
re-queued (attempt < max_attempts) or failed ("interrupted"). Cancel: queued -> cancelled;
running -> cancel_requested, checked by the handler via ctx.cancelled().
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from app.admin import db as zdb

log = logging.getLogger("zen.admin.jobs")
KINDS = ("summarize", "export", "glossary_import", "embed_backfill", "backup", "retranslate_batch")
LEASE_S = 60.0


class JobDeferred(Exception):
    """Handler is not allowed to run now (e.g. embeddings while ASR is busy). Requeue later."""

    def __init__(self, delay_s: float = 30.0, reason: str = ""):
        super().__init__(reason or "deferred")
        self.delay_s = max(1.0, float(delay_s))
        self.reason = reason


class JobRetryable(Exception):
    """Raise (or wrap) a transient failure: the job is re-queued with backoff (QA 後端 B4)."""


def is_retryable(exc: BaseException) -> bool:
    import urllib.error
    if isinstance(exc, (JobRetryable, TimeoutError, ConnectionError, urllib.error.URLError)):
        return True
    if isinstance(exc, sqlite3.OperationalError) and any(w in str(exc).lower() for w in ("locked", "busy")):
        return True
    try:
        from app.admin.live_client import LiveDown
        if isinstance(exc, LiveDown):
            return True
    except Exception:          # pragma: no cover
        pass
    try:
        from app.embed import EmbedError
        if isinstance(exc, EmbedError):
            return True
    except Exception:          # pragma: no cover
        pass
    return False


def retry_delay_s(attempt: int, *, base: float = 5.0, cap: float = 300.0, jitter: float | None = None) -> float:
    j = (secrets.randbelow(1000) / 1000.0) if jitter is None else jitter
    return min(cap, base * (2 ** max(0, attempt - 1))) + j


class JobCancelled(Exception):
    pass


def new_job_id(clock=time.time) -> str:
    return f"job_{int(clock() * 1000):013x}{secrets.token_hex(5)}"


def params_hash(kind: str, target: dict, params: dict) -> str:
    raw = json.dumps({"k": kind, "t": target, "p": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def job_row(row) -> dict:
    if row is None:
        return None
    d = dict(row)
    return {
        "id": d["id"], "kind": d["kind"], "state": d["state"],
        "target": json.loads(d["target_json"] or "{}"), "params": json.loads(d["params_json"] or "{}"),
        "progress": {"done": d["progress_done"], "total": d["progress_total"], "stage": d["progress_stage"],
                     "message": d["progress_msg"]},
        "attempt": d["attempt"], "max_attempts": d["max_attempts"],
        "created_at": d["created_at"], "started_at": d["started_at"], "finished_at": d["finished_at"],
        "result": json.loads(d["result_json"]) if d["result_json"] else None,
        "error": json.loads(d["error_json"]) if d["error_json"] else None,
    }


class JobStore:
    def __init__(self, db_path: str | Path, clock=time.time):
        self.db_path = Path(db_path)
        self.clock = clock

    def _conn(self) -> sqlite3.Connection:
        return zdb.connect(self.db_path)

    def create(self, kind: str, target: dict | None = None, params: dict | None = None, *,
               created_by: int | None = None, idempotency_key: str | None = None, max_attempts: int = 3) -> tuple[dict, bool]:
        """Returns (job, created). An identical active job is returned instead of a duplicate."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind}")
        target, params = target or {}, params or {}
        ph = params_hash(kind, target, params)
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                "SELECT * FROM jobs WHERE kind=? AND params_hash=? AND state IN ('queued','running','cancel_requested')",
                (kind, ph)).fetchone()
            if existing:
                c.execute("COMMIT")
                return job_row(existing), False
            jid = new_job_id(self.clock)
            c.execute(
                """INSERT INTO jobs(id, kind, target_json, params_json, params_hash, max_attempts, idempotency_key,
                                    created_by, created_at, run_after) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (jid, kind, json.dumps(target, ensure_ascii=False), json.dumps(params, ensure_ascii=False), ph,
                 max_attempts, idempotency_key, created_by, self.clock(), self.clock()))
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            c.execute("COMMIT")
            return job_row(row), True
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def get(self, jid: str) -> dict | None:
        c = self._conn()
        try:
            return job_row(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        finally:
            c.close()

    def _write(self, sql: str, args: tuple) -> int:
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute(sql, args).rowcount
            c.execute("COMMIT")
            return n
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def cancel(self, jid: str) -> dict | None:
        now = self.clock()
        self._write("UPDATE jobs SET state='cancelled', finished_at=? WHERE id=? AND state='queued'", (now, jid))
        self._write("UPDATE jobs SET state='cancel_requested' WHERE id=? AND state='running'", (jid,))
        return self.get(jid)

    def recover(self, owner: str) -> int:
        now = self.clock()
        err = json.dumps({"type": "https://zen-bridge.local/problems/interrupted", "title": "interrupted",
                          "status": 500, "detail": "工作在執行中被中斷"}, ensure_ascii=False)
        expired = "(lease_until IS NULL OR lease_until<?)"
        n = self._write(f"UPDATE jobs SET state='cancelled', finished_at=? WHERE state='cancel_requested' AND {expired}",
                        (now, now))
        n += self._write(f"UPDATE jobs SET state='queued', lease_owner=NULL, lease_until=NULL "
                         f"WHERE state='running' AND {expired} AND attempt<max_attempts", (now,))
        n += self._write(f"UPDATE jobs SET state='failed', finished_at=?, error_json=? WHERE state='running' AND {expired}",
                         (now, err, now))
        return n

    def heartbeat(self, jid: str, owner: str) -> bool:
        """Extend the lease while the handler runs. False = this owner lost the job."""
        now = self.clock()
        return self._write("UPDATE jobs SET lease_until=?, heartbeat_at=? WHERE id=? AND lease_owner=? "
                           "AND state IN ('running','cancel_requested')", (now + LEASE_S, now, jid, owner)) == 1

    def claim(self, owner: str) -> dict | None:
        now = self.clock()
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT id FROM jobs WHERE state='queued' AND run_after<=? "
                            "ORDER BY priority DESC, created_at LIMIT 1", (now,)).fetchone()
            if not row:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE jobs SET state='running', lease_owner=?, lease_until=?, attempt=attempt+1, "
                      "started_at=COALESCE(started_at, ?), heartbeat_at=? WHERE id=?",
                      (owner, now + LEASE_S, now, now, row[0]))
            job = c.execute("SELECT * FROM jobs WHERE id=?", (row[0],)).fetchone()
            c.execute("COMMIT")
            return job_row(job)
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def progress(self, jid: str, done=None, total=None, stage=None, message=None) -> str:
        now = self.clock()
        self._write("UPDATE jobs SET progress_done=COALESCE(?, progress_done), progress_total=COALESCE(?, progress_total), "
                    "progress_stage=COALESCE(?, progress_stage), progress_msg=COALESCE(?, progress_msg), "
                    "heartbeat_at=?, lease_until=? WHERE id=?", (done, total, stage, message, now, now + LEASE_S, jid))
        job = self.get(jid)
        return job["state"] if job else "cancelled"

    def finish(self, jid: str, state: str, *, result=None, error=None, owner: str | None = None) -> bool:
        """owner given: only the current lease holder may finish (a stolen job is not overwritten)."""
        sql = ("UPDATE jobs SET state=?, finished_at=?, result_json=?, error_json=?, lease_owner=NULL, lease_until=NULL "
               "WHERE id=?")
        args = [state, self.clock(), json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(error, ensure_ascii=False) if error is not None else None, jid]
        if owner is not None:
            sql += " AND lease_owner=?"
            args.append(owner)
        return self._write(sql, tuple(args)) == 1

    def retry(self, jid: str, delay_s: float, error: dict) -> bool:
        """Re-queue a running job after a transient error while attempts remain. False = no attempts left."""
        now = self.clock()
        c = self._conn()
        try:
            cur = c.execute(
                "UPDATE jobs SET state='queued', lease_owner=NULL, lease_until=NULL, run_after=?, error_json=?, "
                "progress_stage='retry_wait' WHERE id=? AND state='running' AND attempt<max_attempts",
                (now + delay_s, json.dumps(error, ensure_ascii=False), jid))
            c.commit()
            return cur.rowcount == 1
        finally:
            c.close()

    def defer(self, jid: str, delay_s: float, reason: str = "") -> None:
        """Back to queued without spending an attempt; keeps progress so a UI can show why."""
        now = self.clock()
        self._write("UPDATE jobs SET state='queued', lease_owner=NULL, lease_until=NULL, run_after=?, "
                    "attempt=MAX(attempt-1, 0), progress_stage='waiting', progress_msg=? "
                    "WHERE id=? AND state='running'", (now + delay_s, reason[:200], jid))
        self._write("UPDATE jobs SET state='cancelled', finished_at=? WHERE id=? AND state='cancel_requested'",
                    (now, jid))

    def list(self, *, state: str | None = None, kind: str | None = None, before: tuple | None = None, limit: int = 50):
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if state:
            sql += " AND state=?"
            args.append(state)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if before:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args += [before[0], before[0], before[1]]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        c = self._conn()
        try:
            return [job_row(r) for r in c.execute(sql, args)]
        finally:
            c.close()


@dataclass
class JobContext:
    store: JobStore
    job: dict

    def progress(self, done=None, total=None, stage=None, message=None) -> None:
        state = self.store.progress(self.job["id"], done, total, stage, message)
        if state == "cancel_requested":
            raise JobCancelled()

    def cancelled(self) -> bool:
        job = self.store.get(self.job["id"])
        return bool(job and job["state"] == "cancel_requested")


class JobWorker:
    """Single asyncio worker. Handlers: {kind: callable(ctx) -> result dict} (sync or async)."""

    def __init__(self, store: JobStore, handlers: dict, *, poll_s: float = 1.0, heartbeat_s: float | None = None):
        self.store = store
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else LEASE_S / 4
        self.handlers = handlers
        self.poll_s = poll_s
        self.owner = f"admin-{os.getpid()}-{secrets.token_hex(3)}"
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.state = "stopped"

    def start(self) -> None:
        try:
            self.store.recover(self.owner)
        except Exception:
            log.exception("job recovery failed")
        self._task = asyncio.create_task(self._loop(), name="zen-admin-jobs")
        self.state = "idle"

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.state = "stopped"

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(self.store.claim, self.owner)
        if job is None:
            return False
        self.state = f"running:{job['id']}"
        handler = self.handlers.get(job["kind"])
        ctx = JobContext(self.store, job)
        # Lease heartbeat on its own thread, independent of the handler calling progress() and
        # of the event loop, so a long job (VACUUM INTO, embeddings) is not recovered and re-run.
        import threading
        stop_hb = threading.Event()
        lost = threading.Event()

        def beat():
            while not stop_hb.wait(self.heartbeat_s):
                try:
                    if not self.store.heartbeat(job["id"], self.owner):
                        lost.set()
                        log.warning("job %s lease lost; result will not be recorded", job["id"])
                        return
                except Exception:
                    log.debug("heartbeat failed", exc_info=True)
        hb = threading.Thread(target=beat, name=f"job-hb-{job['id']}", daemon=True)
        hb.start()
        try:
            if handler is None:
                raise NotImplementedError(f"沒有 {job['kind']} 的處理程式（後續版本加入）")
            if inspect.iscoroutinefunction(handler):
                result = await handler(ctx)
            else:
                result = await asyncio.to_thread(handler, ctx)
            stop_hb.set()
            await asyncio.to_thread(self.store.finish, job["id"], "succeeded", result=result or {}, owner=self.owner)
        except JobCancelled:
            await asyncio.to_thread(self.store.finish, job["id"], "cancelled", owner=self.owner)
        except JobDeferred as exc:
            await asyncio.to_thread(self.store.defer, job["id"], exc.delay_s, exc.reason)
        except Exception as exc:
            problem = {"type": "https://zen-bridge.local/problems/job-failed", "title": "job-failed", "status": 500,
                       "detail": str(exc)[:300] or type(exc).__name__}
            if is_retryable(exc):
                delay = retry_delay_s(int(job.get("attempt") or 1))
                if await asyncio.to_thread(self.store.retry, job["id"], delay, {**problem, "retry_in_s": round(delay, 1)}):
                    log.warning("job %s transient failure (%s); retry in %.0fs", job["id"], type(exc).__name__, delay)
                    return True
            log.exception("job %s failed", job["id"])
            await asyncio.to_thread(self.store.finish, job["id"], "failed", error=problem, owner=self.owner)
        finally:
            stop_hb.set()
            hb.join(timeout=2)
            self.state = "idle"
        return True

    async def _loop(self) -> None:
        idle = 0
        while True:
            try:
                idle += 1
                if idle >= 30:   # a crashed process's lease may expire later than our start
                    idle = 0
                    await asyncio.to_thread(self.store.recover, self.owner)
                ran = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job loop error")
                ran = False
            if not ran:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_s)
                except asyncio.TimeoutError:
                    pass


def backup_handler(db_path: Path, dest_dir_fn, identity_path: Path | None = None,
                   keep_daily: int = 14, keep_weekly: int = 8):
    """zen.sqlite3 + zen-identity.sqlite3 + manifest, then rotation (CTO-03)."""
    def run(ctx: JobContext) -> dict:
        ctx.progress(0, 2, "backup", "VACUUM INTO")
        dest = dest_dir_fn()
        out = zdb.backup_set(db_path, dest, identity_path)
        ctx.progress(1, 2, "rotate", out["file"])
        out["deleted"] = zdb.rotate_backups(dest, keep_daily=keep_daily, keep_weekly=keep_weekly)
        out["same_disk"] = zdb.same_disk(db_path, dest)
        ctx.progress(2, 2, "done", out["file"])
        return out
    return run
