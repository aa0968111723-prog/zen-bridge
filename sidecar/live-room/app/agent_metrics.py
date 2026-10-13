"""Bounded operational history. Never stores audio, utterances or credentials.

The JSONL keeps at most one backup (``*.jsonl.1``). Each file holds at most
``limit`` lines. ``persisted_*`` counts that saved window, not the process lifetime.
"""
import json
import time
from collections import deque
from pathlib import Path
from threading import Lock

_KEEP = ("kind", "elapsed_ms", "ok", "at", "audio_seconds", "rtf", "route", "usage_known")


class AgentMetrics:
    def __init__(self, limit=100, path=None):
        self.limit = max(1, int(limit))
        self.started = time.monotonic()
        self.events = deque(maxlen=self.limit)
        self.saved_events = deque(maxlen=self.limit)
        self.lock = Lock()
        self.completed = 0
        self.failed = 0
        self.path = Path(path) if path else None
        self.persisted_completed = 0
        self.persisted_failed = 0
        self.usage_unknown = 0
        self.usage_known = 0
        self.write_failures = 0
        self._active_lines = 0
        self._load()

    def _backup_path(self):
        return self.path.with_suffix(self.path.suffix + ".1")

    def _load(self):
        if self.path is None:
            return
        lines = deque(maxlen=self.limit)
        active_count = 0
        try:
            for candidate in (self._backup_path(), self.path):
                if not candidate.exists():
                    continue
                with candidate.open(encoding="utf-8", errors="replace") as handle:
                    while True:
                        line = handle.readline(65536)
                        if not line:
                            break
                        if candidate == self.path:
                            active_count += 1
                        if len(line) == 65536 and not line.endswith('\n'):
                            while line and not line.endswith('\n'):
                                line = handle.readline(65536)
                            continue
                        lines.append(line)
        except OSError:
            return
        for line in lines:
            event = self._clean(line)
            if event is not None:
                self.events.append(event)
                self.saved_events.append(event)
        self._recompute_window()
        self._active_lines = active_count
        if self._active_lines > self.limit and self.events:
            self._compact()

    def _clean(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("kind")
        elapsed = event.get("elapsed_ms")
        if not isinstance(kind, str) or not kind or len(kind) > 32:
            return None
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            return None
        clean = {"kind": kind, "elapsed_ms": elapsed, "ok": bool(event.get("ok"))}
        stamp = event.get("at")
        if not isinstance(stamp, bool) and isinstance(stamp, (int, float)):
            clean["at"] = stamp
        audio = event.get("audio_seconds")
        if not isinstance(audio, bool) and isinstance(audio, (int, float)):
            clean["audio_seconds"] = audio
        rtf = event.get("rtf")
        if not isinstance(rtf, bool) and isinstance(rtf, (int, float)):
            clean["rtf"] = rtf
        if event.get("route") in ("local", "cloud"):
            clean["route"] = event["route"]
        if isinstance(event.get("usage_known"), bool):
            clean["usage_known"] = event["usage_known"]
        return clean

    def _recompute_window(self):
        ok = sum(bool(event.get('ok')) for event in self.saved_events)
        fail = len(self.saved_events) - ok
        known = unknown = 0
        for event in self.events:
            if event.get("kind") == "translate":
                if event.get("usage_known") is True:
                    known += 1
                else:
                    unknown += 1
        self.persisted_completed = ok
        self.persisted_failed = fail
        self.usage_known = known
        self.usage_unknown = unknown

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
        with self.lock:
            self.events.append(event)
            self.completed += int(bool(ok))
            self.failed += int(not ok)
            self._append(event)
            self._recompute_window()

    def _line(self, event):
        return json.dumps({k: event[k] for k in _KEEP if k in event}, ensure_ascii=False) + "\n"

    def _append(self, event):
        """Caller holds ``self.lock``. Rotate before the active file passes ``limit`` lines."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._active_lines >= self.limit:
                self._rotate()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(self._line(event))
            self._active_lines += 1
            self.saved_events.append(event)
        except OSError:
            self.write_failures += 1

    def _rotate(self):
        backup = self._backup_path()
        if backup.exists():
            backup.unlink()
        if self.path.exists():
            self.path.replace(backup)
        self._active_lines = 0

    def _compact(self):
        """Rewrite a legacy oversized file down to the in-memory window."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("".join(self._line(event) for event in self.events), encoding="utf-8")
            tmp.replace(self.path)
            backup = self._backup_path()
            if backup.exists():
                backup.unlink()
            self._active_lines = len(self.events)
        except OSError:
            self.write_failures += 1
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                self.write_failures += 1

    def snapshot(self):
        with self.lock:
            note = None if self.usage_known and self.usage_unknown == 0 else "沒有上游用量，不能把 0 當成免費"
            return {
                "uptime_s": round(time.monotonic() - self.started),
                "completed": self.completed,
                "failed": self.failed,
                "persisted_completed": self.persisted_completed,
                "persisted_failed": self.persisted_failed,
                "persisted_window": self.limit,
                "persisted_note": f"persisted 統計只含保存視窗內最近 {self.limit} 筆，不是全部歷史",
                "write_failures": self.write_failures,
                "history": list(self.events),
                "cost_known": self.usage_known > 0 and self.usage_unknown == 0,
                "note": note,
            }
