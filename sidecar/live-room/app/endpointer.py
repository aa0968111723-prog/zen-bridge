"""Streaming utterance endpointer for the fast two-pass caption path (grok/fast-draft-asr).

The host streams 16 kHz mono PCM16 (100 ms packets) over ``/ws/draft``. The draft recognizer
shows grey text right away; this module decides where one *final* utterance ends so Breeze can
finalise it in ~2-4 s pieces instead of fixed 6 s slices.

Rules (all in seconds, overridable through ``BREEZE_ENDPOINT_*``):

* a 30 ms frame is speech when its RMS is above ``max(abs_floor, noise_floor * ratio)``; the
  noise floor follows the quietest frames (slow up, fast down);
* an utterance starts at the first speech frame (``pre_roll`` of audio before it is kept);
* **pause cut**: speech has lasted ``min_utt`` and the last ``end_silence`` is silent;
* **soft cut**: the utterance is longer than ``soft_max`` and a shorter pause (``soft_silence``)
  shows up;
* **hard cut**: the utterance reaches ``hard_max``; the cut goes to the quietest frame inside the
  last ``search_back`` window so it does not land in the middle of a syllable. The remainder is
  carried into the next utterance (nothing is dropped);
* ``flush()`` closes whatever speech is left (end of stream).

Every cut keeps ``post_roll`` of trailing audio when available. Pure Python + ``array``; no model,
no numpy needed, so the host never pays for it when the feature is off.
"""
from __future__ import annotations

import array
import io
import math
import os
import wave
from dataclasses import dataclass

SR = 16000
FRAME = 480                     # 30 ms


@dataclass(frozen=True)
class EndpointConfig:
    min_utt: float = 0.8
    end_silence: float = 0.35
    soft_max: float = 3.0
    soft_silence: float = 0.15
    hard_max: float = 4.0
    search_back: float = 0.8
    pre_roll: float = 0.15
    post_roll: float = 0.2
    lone_silence: float = 1.0   # a short word followed by this much silence is finalised on its own
    lone_min: float = 0.25      # ...if it was at least this long; shorter blips are dropped
    abs_floor: float = 0.006    # RMS on a -1..1 scale (~ -44 dBFS)
    ratio: float = 3.0

    def validate(self) -> "EndpointConfig":
        ok = (0.2 <= self.min_utt <= self.soft_max <= self.hard_max <= 15.0
              and 0.1 <= self.end_silence <= 2.0 and 0.05 <= self.soft_silence <= self.end_silence
              and 0.1 <= self.search_back < self.hard_max and 0 <= self.pre_roll <= 1.0
              and 0 <= self.post_roll <= 1.0 and self.end_silence <= self.lone_silence <= 5.0
              and 0.05 <= self.lone_min <= self.min_utt and 0 < self.abs_floor < 0.5 and 1.0 <= self.ratio <= 20.0)
        if not ok:
            raise ValueError("BREEZE_ENDPOINT_* 數值不合理")
        return self


_ENV = {"min_utt": "BREEZE_ENDPOINT_MIN_S", "end_silence": "BREEZE_ENDPOINT_SILENCE_S",
        "soft_max": "BREEZE_ENDPOINT_SOFT_MAX_S", "soft_silence": "BREEZE_ENDPOINT_SOFT_SILENCE_S",
        "hard_max": "BREEZE_ENDPOINT_MAX_S", "abs_floor": "BREEZE_ENDPOINT_RMS_FLOOR"}


def config_from_env(env: dict | None = None) -> EndpointConfig:
    """Bad or out-of-range values fall back to the defaults (never raises)."""
    env = os.environ if env is None else env
    vals = {}
    for field, name in _ENV.items():
        raw = (env.get(name) or "").strip()
        if not raw:
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if math.isfinite(v):
            vals[field] = v
    try:
        return EndpointConfig(**vals).validate()
    except ValueError:
        return EndpointConfig()


@dataclass(frozen=True)
class Utterance:
    pcm: bytes          # PCM16 mono 16 kHz
    t0_ms: int          # stream time of the first sample
    t1_ms: int          # stream time after the last sample
    reason: str         # "pause" | "soft" | "hard" | "flush"

    def wav(self) -> bytes:
        return pcm16_to_wav(self.pcm)


def pcm16_to_wav(pcm: bytes, rate: int = SR) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def _rms(frame: array.array) -> float:
    if not frame:
        return 0.0
    return math.sqrt(sum(x * x for x in frame) / len(frame)) / 32768.0


class Endpointer:
    def __init__(self, config: EndpointConfig | None = None):
        self.cfg = (config or EndpointConfig()).validate()
        self._buf = array.array("h")       # samples since self._base (pre-roll + current utterance)
        self._base = 0                     # absolute sample index of _buf[0]
        self._total = 0                    # absolute samples received
        self._pending = array.array("h")   # < 1 frame not yet analysed
        self._rms: list[float] = []        # per analysed frame inside _buf (frame-aligned with _buf)
        self._noise = self.cfg.abs_floor
        self._start: int | None = None     # frame index (in _rms) where speech began
        self._last_speech: int | None = None
        self.cuts = 0

    # --------------------------------------------------------------- helpers
    def _frames(self, seconds: float) -> int:
        return max(1, int(round(seconds * SR / FRAME)))

    def _threshold(self) -> float:
        return max(self.cfg.abs_floor, self._noise * self.cfg.ratio)

    def _is_speech(self, r: float) -> bool:
        return r >= self._threshold()

    def _track_noise(self, r: float) -> None:
        if r < self._noise:
            self._noise = 0.7 * self._noise + 0.3 * r
        elif not self._is_speech(r):
            self._noise = 0.995 * self._noise + 0.005 * r
        self._noise = max(self._noise, 1e-4)

    @property
    def in_speech(self) -> bool:
        return self._start is not None

    def utterance_s(self) -> float:
        if self._start is None:
            return 0.0
        return (len(self._rms) - self._start) * FRAME / SR

    # --------------------------------------------------------------- core
    def feed(self, pcm16: bytes) -> list[Utterance]:
        a = array.array("h")
        a.frombytes(pcm16[: len(pcm16) // 2 * 2])
        self._total += len(a)
        self._pending.extend(a)
        out: list[Utterance] = []
        while len(self._pending) >= FRAME:
            frame = self._pending[:FRAME]
            del self._pending[:FRAME]
            self._buf.extend(frame)
            r = _rms(frame)
            self._rms.append(r)
            speech = self._is_speech(r)
            self._track_noise(r)
            idx = len(self._rms) - 1
            if speech:
                if self._start is None:
                    self._start = idx
                self._last_speech = idx
            cut = self._decide(idx)
            if cut is not None:
                out.append(cut)
            elif self._start is None:
                self._trim_idle()
        return out

    def _decide(self, idx: int) -> Utterance | None:
        if self._start is None or self._last_speech is None:
            return None
        n = idx + 1 - self._start                       # frames since speech began
        voiced = self._last_speech + 1 - self._start    # speech span (without trailing silence)
        silent = idx - self._last_speech                # trailing silent frames
        if voiced >= self._frames(self.cfg.min_utt) and silent >= self._frames(self.cfg.end_silence):
            return self._cut(self._last_speech + 1, "pause")
        if silent >= self._frames(self.cfg.lone_silence):
            if voiced >= self._frames(self.cfg.lone_min):
                return self._cut(self._last_speech + 1, "pause")   # a short reply ("好。") still gets a final
            self._start = self._last_speech = None                  # a click or cough: forget it
            return None
        if n >= self._frames(self.cfg.soft_max) and silent >= self._frames(self.cfg.soft_silence):
            return self._cut(self._last_speech + 1, "soft")
        if n >= self._frames(self.cfg.hard_max):
            lo = max(self._start + self._frames(self.cfg.min_utt), idx + 1 - self._frames(self.cfg.search_back))
            seg = self._rms[lo: idx + 1]
            best = lo + min(range(len(seg)), key=lambda i: (seg[i], -i)) if seg else idx
            return self._cut(best + 1, "hard")
        return None

    def _cut(self, end_frame: int, reason: str) -> Utterance:
        """Emit [start - pre_roll, end_frame + post_roll) and keep the rest for the next utterance."""
        assert self._start is not None
        pre = self._frames(self.cfg.pre_roll) if self.cfg.pre_roll > 0 else 0
        post = self._frames(self.cfg.post_roll) if self.cfg.post_roll > 0 else 0
        first = max(0, self._start - pre)
        last = min(len(self._rms), end_frame + (post if reason != "hard" else 0))
        pcm = self._buf[first * FRAME: last * FRAME].tobytes()
        t0 = int((self._base + first * FRAME) * 1000 / SR)
        t1 = int((self._base + last * FRAME) * 1000 / SR)
        # keep from end_frame (hard cut: the remainder is real speech) - post roll is shared audio
        keep_from = end_frame
        self._drop_frames(keep_from)
        self._start = None
        self._last_speech = None
        # the carried remainder may already contain speech: re-scan it so a hard cut continues
        for i, r in enumerate(self._rms):
            if self._is_speech(r):
                if self._start is None:
                    self._start = i
                self._last_speech = i
        self.cuts += 1
        return Utterance(pcm, t0, t1, reason)

    def _drop_frames(self, n: int) -> None:
        n = max(0, min(n, len(self._rms)))
        del self._buf[: n * FRAME]
        del self._rms[:n]
        self._base += n * FRAME
        if self._start is not None:
            self._start = max(0, self._start - n)
        if self._last_speech is not None:
            self._last_speech = self._last_speech - n if self._last_speech >= n else None

    def _trim_idle(self) -> None:
        keep = self._frames(self.cfg.pre_roll) if self.cfg.pre_roll > 0 else 0
        extra = len(self._rms) - keep
        if extra > 0:
            self._drop_frames(extra)

    def flush(self) -> list[Utterance]:
        if self._pending:
            self._buf.extend(self._pending)
            self._rms.append(_rms(self._pending))
            self._pending = array.array("h")
        if self._start is None or self._last_speech is None:
            out = []
        else:
            out = [self._cut(self._last_speech + 1, "flush")]
        self._drop_frames(len(self._rms))           # end of stream: nothing is carried over
        self._start = self._last_speech = None
        return out

    def remainder_pcm(self) -> bytes:
        """Audio already received after the last cut (fed back into a reset draft engine)."""
        return self._buf.tobytes() + self._pending.tobytes()
