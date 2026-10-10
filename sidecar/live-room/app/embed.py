"""Embeddings (qwen3-embedding:0.6b on Ollama) — only when the live pipeline is idle.

hardware.md §5.1 rule 3 / §7.4: embeddings never compete with live captions. A batch
(ZEN_EMBED_BATCH, default 16) runs only when
  * the live room is not running (no ASR at all — e.g. after the session ended), or
  * its ASR and translation queues have both been empty for ZEN_EMBED_IDLE_S (default 10 s).
The gate is re-checked before every batch; when busy the admin job is deferred
(JobDeferred), not failed, and does not spend an attempt.

Ollama's runner process priority cannot be set from here (it is Ollama's process), so
the load is capped with options.num_thread (ZEN_EMBED_NUM_THREAD, default 2).
"""
from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
import urllib.request
from dataclasses import dataclass, field

from app.runtime_tuning import env_float, env_int

log = logging.getLogger("zen.embed")

DEFAULT_MODEL = "qwen3-embedding:0.6b"
DEFAULT_BASE = "http://127.0.0.1:11434"
BUSY_KEYS = ("pending", "inflight", "translate_queued", "translate_busy", "listeners")


def _refused(exc: BaseException) -> bool:
    """Connection refused = the live room is not running. Everything else is 'state unknown'."""
    import errno
    if isinstance(exc, ConnectionRefusedError):
        return True
    try:
        from app.admin.live_client import LiveRefused
        if isinstance(exc, LiveRefused):
            return True
    except Exception:          # pragma: no cover
        pass
    if isinstance(exc, OSError) and (exc.errno in (errno.ECONNREFUSED, 10061) or "refused" in str(exc).lower()):
        return True
    return False


class EmbedError(RuntimeError):
    pass


@dataclass
class OllamaEmbedder:
    base_url: str = DEFAULT_BASE
    model: str = DEFAULT_MODEL
    num_thread: int = 2
    keep_alive: str | int = "5m"      # unload soon after; the translate model stays resident
    timeout_s: float = 60.0
    opener: object | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {"model": self.model, "input": texts, "keep_alive": self.keep_alive, "truncate": True}
        if self.num_thread > 0:
            body["options"] = {"num_thread": self.num_thread}
        req = urllib.request.Request(self.base_url.rstrip("/") + "/api/embed", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        from app.net import safe_opener
        open_url = self.opener or safe_opener()           # 資安長 #1: no redirects, no proxy
        try:
            with open_url(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            raise EmbedError(f"embedding request failed: {type(exc).__name__}") from exc
        vecs = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise EmbedError("embedding reply has the wrong shape")
        return vecs


def embedder_from_env(env=None, opener=None) -> OllamaEmbedder:
    import os
    from app.translate_config import validate_base_url
    env = os.environ if env is None else env
    allow_remote = (env.get("BREEZE_TRANSLATE_ALLOW_REMOTE") or "").strip() == "1"
    base = validate_base_url(env.get("ZEN_EMBED_BASE_URL") or DEFAULT_BASE, allow_remote=allow_remote)
    if base.endswith("/v1"):
        base = base[:-3]
    return OllamaEmbedder(base_url=base, model=(env.get("ZEN_EMBED_MODEL") or DEFAULT_MODEL).strip(),
                          num_thread=env_int("ZEN_EMBED_NUM_THREAD", 2, env), opener=opener)


@dataclass
class IdleGate:
    """probe() -> live metrics dict, or None when the live room is not running."""

    probe: object
    idle_s: float = 10.0
    clock: object = time.monotonic
    poll_s: float = 1.0
    sleep: object = time.sleep
    _busy_at: float | None = field(default=None, init=False)

    def busy(self, metrics: dict) -> bool:
        if not isinstance(metrics, dict):
            return True
        if any(int(metrics.get(k) or 0) > 0 for k in BUSY_KEYS):
            return True
        idle = metrics.get("asr_idle_s")
        if idle is not None and float(idle) < self.idle_s:
            return True                     # an upload / ASR result happened inside the window
        return float(metrics.get("backlog_s") or 0) > 0

    def _sample(self):
        """('down'|'busy'|'quiet'|'error', reason)."""
        try:
            metrics = self.probe()
        except Exception as exc:
            if _refused(exc):
                return "down", "live_down"
            return "error", f"probe_error_{type(exc).__name__}"
        if metrics is None:
            return "down", "live_down"
        if self.busy(metrics):
            return "busy", "asr_or_translate_busy"
        return "quiet", "idle"

    def check(self) -> tuple[bool, str]:
        now = self.clock()
        kind, why = self._sample()
        if kind == "down":
            return True, why
        if kind in ("busy", "error"):
            # Fail closed (QA 效能長 P0-7): timeout / 5xx / breaker open count as busy.
            self._busy_at = now
            return False, why if kind == "error" else "asr_or_translate_busy"
        if self._busy_at is None:
            # First look: require a full quiet window before the first batch.
            self._busy_at = now
        quiet = now - self._busy_at
        if quiet >= self.idle_s:
            return True, "idle"
        return False, f"quiet_{quiet:.0f}s_of_{self.idle_s:.0f}s"

    def confirm(self) -> tuple[bool, str]:
        """check(), then keep polling every poll_s for a whole idle_s window (QA 效能長 P0-6).

        A single instant sample can land in the gap between two 6-s slices; this sustained
        window catches any busy moment (or probe error) and defers. When the live room is not
        running, one re-check after poll_s is enough (it cannot be busy without starting).
        """
        ok, why = self.check()
        if not ok:
            return ok, why
        polls = 1 if why == "live_down" else max(1, int(-(-self.idle_s // max(self.poll_s, 0.01))))
        for _ in range(polls):
            self.sleep(self.poll_s)
            kind, reason = self._sample()
            if kind in ("busy", "error"):
                self._busy_at = self.clock()
                return False, reason
            if why == "live_down" and kind == "quiet":
                # the App just started: require a full quiet window from now on
                self._busy_at = self.clock()
                return False, "live_started"
        return True, why


def gate_from_env(probe, env=None) -> IdleGate:
    return IdleGate(probe=probe, idle_s=env_float("ZEN_EMBED_IDLE_S", 10.0, env))


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def text_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def pending_transcripts(c, model: str, limit: int, session_id: str | None = None) -> list[tuple[int, str]]:
    sql = ("SELECT t.id, t.text FROM transcripts t JOIN segments g ON g.id = t.segment_id "
           "WHERE t.is_current = 1 AND length(t.text) > 0 AND g.status <> 'deleted' AND NOT EXISTS ("
           "  SELECT 1 FROM embeddings e WHERE e.owner_type='transcript' AND e.owner_id = CAST(t.id AS TEXT) AND e.model = ?)")
    args: list = [model]
    if session_id:
        sql += " AND g.session_id = ?"
        args.append(session_id)
    sql += " ORDER BY t.id LIMIT ?"
    args.append(limit)
    return [(int(r[0]), str(r[1])) for r in c.execute(sql, args)]


def store_batch(c, model: str, rows: list[tuple[int, str]], vecs: list[list[float]]) -> int:
    c.execute("BEGIN IMMEDIATE")
    try:
        for (tid, text), vec in zip(rows, vecs):
            c.execute(
                "INSERT INTO embeddings(owner_type, owner_id, model, dim, vector, text_sha1) VALUES ('transcript',?,?,?,?,?) "
                "ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET dim=excluded.dim, vector=excluded.vector, "
                "text_sha1=excluded.text_sha1, created_at=unixepoch('subsec')",
                (str(tid), model, len(vec), pack(vec), text_sha1(text)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return len(rows)


def backfill_handler(connect, embedder: OllamaEmbedder, gate: IdleGate, *, batch: int = 16,
                     max_batches: int = 1000, defer_s: float = 30.0):
    """Admin job handler (kind embed_backfill). Checks the idle gate before every batch."""
    from app.admin.jobs import JobDeferred

    def run(ctx) -> dict:
        session_id = (ctx.job.get("target") or {}).get("session_id")
        done = 0
        for _ in range(max_batches):
            ok, why = gate.confirm()
            if not ok:
                if done:
                    ctx.progress(done, None, "waiting", why)
                raise JobDeferred(defer_s, why)
            c = connect()
            try:
                rows = pending_transcripts(c, embedder.model, batch, session_id)
                if not rows:
                    return {"embedded": done, "model": embedder.model}
                vecs = embedder.embed([t for _, t in rows])
                done += store_batch(c, embedder.model, rows, vecs)
            finally:
                c.close()
            ctx.progress(done, None, "embedding", f"{done} 句")
        return {"embedded": done, "model": embedder.model, "more": True}
    return run
