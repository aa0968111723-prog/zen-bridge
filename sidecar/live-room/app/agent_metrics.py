"""Bounded operational history. Never stores audio, utterances or credentials."""
import json
import time
from collections import deque
from pathlib import Path
from threading import Lock

_KEEP = ("kind", "elapsed_ms", "ok", "at", "audio_seconds", "rtf", "route", "usage_known")

class AgentMetrics:
    def __init__(self, limit=100, path=None):
        self.started = time.monotonic()
        self.events = deque(maxlen=limit)
        self.lock = Lock()
        self.completed = 0
        self.failed = 0
        self.path = Path(path) if path else None
        self.persisted_completed = 0
        self.persisted_failed = 0
        self.usage_unknown = 0
        self.usage_known = 0
        self._load()

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-100:]
        except OSError:
            return
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            self.persisted_completed += int(bool(event.get("ok")))
            self.persisted_failed += int(not event.get("ok"))
            if event.get("kind") == "translate" and event.get("usage_known") is True:
                self.usage_known += 1
            elif event.get("kind") == "translate":
                self.usage_unknown += 1

    def record(self, kind, elapsed_ms, ok, audio_seconds=None, route=None, usage_known=None):
        event = {"kind": kind, "elapsed_ms": round(elapsed_ms), "ok": bool(ok), "at": time.time()}
        if audio_seconds is not None:
            event["audio_seconds"] = audio_seconds
            if audio_seconds > 0:
                event["rtf"] = round(elapsed_ms / 1000 / audio_seconds, 3)
        if route in ("local", "cloud"):
            event["route"] = route
        if usage_known is not None:
            event["usage_known"] = bool(usage_known)
            if kind == "translate":
                if usage_known:
                    self.usage_known += 1
                else:
                    self.usage_unknown += 1
        with self.lock:
            self.events.append(event)
            self.completed += int(bool(ok))
            self.failed += int(not ok)
        self._append(event)

    def _append(self, event):
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({k: event[k] for k in _KEEP if k in event}, ensure_ascii=False) + "\n")
        except OSError:
            return

    def snapshot(self):
        with self.lock:
            note = None if self.usage_known and self.usage_unknown == 0 else "沒有上游用量，不能把 0 當成免費"
            return {
                "uptime_s": round(time.monotonic() - self.started),
                "completed": self.completed,
                "failed": self.failed,
                "persisted_completed": self.persisted_completed,
                "persisted_failed": self.persisted_failed,
                "history": list(self.events),
                "cost_known": self.usage_known > 0 and self.usage_unknown == 0,
                "note": note,
            }
