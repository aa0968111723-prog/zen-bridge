"""round4 #3: per-stage latency (round4 §3.2, A2-A6) per segment, p50/p95 in /api/metrics.

Stages (milliseconds):
  A2 upload   t_recv - t_slice_end   (host sends ``t1_wall_ms`` = Date.now() at slice end; host page
                                      and server are the same machine, so the clocks agree)
  A3 queue    t_asr0 - t_recv        (includes decode/VAD and the wait for a recognizer slot)
  A4 decode   webm->wav decode + Silero VAD (+ trim)
  A5 asr      t_asr1 - t_asr0
  A6 mt       t_mt1 - t_mt0          (the translator call only; queue wait is not included)

Only durations are kept - never text or audio. Samples are a bounded window per stage
(``BREEZE_LATENCY_WINDOW``, default 512) so a long class does not grow memory.
"""
from __future__ import annotations

import os
import threading
from collections import deque

STAGES = ("A2", "A3", "A4", "A5", "A6")
NAMES = {"A2": "upload", "A3": "queue", "A4": "decode_vad", "A5": "asr", "A6": "mt"}
A2_MAX_MS = 60_000          # a host clock that is clearly off is not an upload sample


def _window() -> int:
    try:
        return max(16, min(10_000, int(os.getenv("BREEZE_LATENCY_WINDOW") or 512)))
    except ValueError:
        return 512


def _q(sorted_xs: list[float], p: float) -> float:
    k = (len(sorted_xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


class StageLatency:
    def __init__(self, window: int | None = None):
        n = window or _window()
        self._lock = threading.Lock()
        self._xs = {s: deque(maxlen=n) for s in STAGES}

    def note(self, segment, stage: str, ms: float | None) -> None:
        if ms is None or stage not in self._xs:
            return
        ms = float(ms)
        if ms < 0 or ms != ms:
            return
        if stage == "A2" and ms > A2_MAX_MS:
            return
        lat = getattr(segment, "lat", None)
        if isinstance(lat, dict):
            lat[stage] = int(round(ms))
        with self._lock:
            self._xs[stage].append(ms)

    def snapshot(self) -> dict:
        out = {}
        with self._lock:
            items = {s: sorted(xs) for s, xs in self._xs.items()}
        for s, xs in items.items():
            out[s] = {"name": NAMES[s], "n": len(xs),
                      "p50_ms": None if not xs else int(round(_q(xs, 0.50))),
                      "p95_ms": None if not xs else int(round(_q(xs, 0.95)))}
        return out


def upload_ms(t1_wall_ms: int | None, received_at_s: float) -> float | None:
    if not t1_wall_ms:
        return None
    return received_at_s * 1000.0 - float(t1_wall_ms)
