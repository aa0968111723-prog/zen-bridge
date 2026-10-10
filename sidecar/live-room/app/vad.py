"""Silero VAD (ONNX, no torch) as an optional silence gate.

BREEZE_VAD=silero turns it on (default off, so main behaviour is unchanged). When the model
file or onnxruntime is missing, or a wav is not 16 kHz mono s16, the gate answers ``None``
and the pipeline falls back to the legacy RMS check (BREEZE_SILENCE_RMS).

Model: snakers4/silero-vad v6.2.3 ``silero_vad.onnx`` (MIT, 2,327,524 bytes). Fetch it with
``python scripts/fetch_silero_vad.py``; the sha256 below is verified on load. Tests never
download it; they inject a fake session.

I/O follows silero-vad utils_vad.OnnxWrapper: 16 kHz, 512-sample chunks with a 64-sample
context prefix, state shape [2, 1, 128].
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("breeze.vad")

MODEL = Path(__file__).with_name("vendor") / "silero_vad.onnx"
MODEL_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_BYTES = 2327524
SR, CHUNK, CTX = 16000, 512, 64


@dataclass(frozen=True)
class VadResult:
    speech_ratio: float          # 0..1 share of 32 ms chunks at or above threshold
    first_ms: int | None         # start of the first speech chunk
    last_ms: int | None          # end of the last speech chunk
    chunks: int = 0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class SileroVad:
    """Thread-safe wrapper; one ONNX session per process."""

    def __init__(self, path: Path = MODEL, threshold: float = 0.5, session_factory=None, threads: int = 1):
        self.threshold = threshold
        self._lock = threading.Lock()
        if session_factory is None:
            from app.windows_runtime import ensure_windows_runtime
            ensure_windows_runtime()
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            session_factory = lambda: ort.InferenceSession(  # noqa: E731
                str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._sess = session_factory()

    def probs(self, pcm) -> list[float]:
        import numpy as np
        state = np.zeros((2, 1, 128), dtype=np.float32)
        ctx = np.zeros((1, CTX), dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        out: list[float] = []
        with self._lock:
            for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
                x = np.concatenate([ctx, pcm[i:i + CHUNK][None, :]], axis=1).astype(np.float32)
                p, state = self._sess.run(None, {"input": x, "state": state, "sr": sr})
                ctx = x[:, -CTX:]
                out.append(float(np.asarray(p).reshape(-1)[0]))
        return out

    def analyze_pcm(self, pcm) -> VadResult:
        ps = self.probs(pcm)
        if not ps:
            return VadResult(0.0, None, None, 0)
        hits = [i for i, p in enumerate(ps) if p >= self.threshold]
        ms = CHUNK * 1000 // SR  # 32 ms
        return VadResult(
            len(hits) / len(ps),
            hits[0] * ms if hits else None,
            (hits[-1] + 1) * ms if hits else None,
            len(ps),
        )

    def analyze_wav(self, wav: Path) -> VadResult | None:
        import numpy as np
        try:
            with wave.open(str(wav), "rb") as w:
                if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
                    return None  # convert_to_wav emits 16k mono s16; anything else is not judged
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        except (wave.Error, EOFError, OSError):
            return None
        return self.analyze_pcm(pcm)


@dataclass
class VadGate:
    vad: SileroVad
    min_speech_ratio: float = 0.05
    skipped: int = 0
    errors: int = 0

    def check(self, wav: Path) -> VadResult | None:
        """None means "no opinion" (use RMS). Never raises."""
        try:
            return self.vad.analyze_wav(wav)
        except Exception:
            self.errors += 1
            log.exception("silero vad failed; falling back to RMS")
            return None

    def is_silent(self, res: VadResult | None) -> bool:
        return res is not None and res.chunks > 0 and res.speech_ratio < self.min_speech_ratio


def trim_wav(wav: Path, res: VadResult | None, *, pad_ms: int = 200, min_save_ms: int = 500,
             out: Path | None = None) -> Path | None:
    """optimization-round2 #6: cut leading/trailing silence before decoding.

    Returns the path of a trimmed copy (16 kHz mono s16), or None when there is nothing worth
    trimming (no VAD opinion, no speech found, or it would save < min_save_ms). Never raises.
    """
    if res is None or res.first_ms is None or res.last_ms is None:
        return None
    try:
        with wave.open(str(wav), "rb") as w:
            if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            n = w.getnframes()
            total_ms = n * 1000 // SR
            start_ms = max(0, res.first_ms - pad_ms)
            end_ms = min(total_ms, res.last_ms + pad_ms)
            if end_ms <= start_ms or total_ms - (end_ms - start_ms) < min_save_ms:
                return None
            w.setpos(start_ms * SR // 1000)
            frames = w.readframes((end_ms - start_ms) * SR // 1000)
        target = out or Path(str(wav) + ".trim.wav")
        with wave.open(str(target), "wb") as o:
            o.setnchannels(1)
            o.setsampwidth(2)
            o.setframerate(SR)
            o.writeframes(frames)
        return target
    except (wave.Error, EOFError, OSError):
        log.debug("vad trim failed", exc_info=True)
        return None


def _float(env: dict, name: str, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """全站 D11: NaN or out-of-range values fall back to the default (a ratio > 1 or threshold NaN
    would mark every clip silent and wipe all Chinese captions)."""
    import math
    try:
        v = float((env.get(name) or "").strip() or default)
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default
    if not math.isfinite(v) or not lo <= v <= hi:
        log.warning("%s=%s is outside %s..%s; using %s", name, v, lo, hi, default)
        return default
    return v


def vad_from_env(env: dict | None = None, session_factory=None) -> VadGate | None:
    """Build the gate, or None (RMS path) when off or unavailable. Never raises."""
    env = os.environ if env is None else env
    mode = (env.get("BREEZE_VAD") or "off").strip().lower()
    if mode in ("", "off", "0", "rms"):
        return None
    if mode != "silero":
        log.warning("BREEZE_VAD=%s is unknown; using RMS", mode)
        return None
    path = Path((env.get("BREEZE_VAD_MODEL") or "").strip() or MODEL)
    threshold = _float(env, "BREEZE_VAD_THRESHOLD", 0.5, 0.01, 0.99)
    ratio = _float(env, "BREEZE_VAD_MIN_SPEECH_RATIO", 0.05, 0.0, 0.9)
    if session_factory is None:
        if not path.is_file():
            log.warning("silero model missing at %s; run scripts/fetch_silero_vad.py. Using RMS", path)
            return None
        if (env.get("BREEZE_VAD_SKIP_SHA") or "") != "1":
            digest = sha256_file(path)
            if digest != MODEL_SHA256:
                log.warning("silero model sha256 %s does not match %s; using RMS", digest, MODEL_SHA256)
                return None
    try:
        vad = SileroVad(path, threshold=threshold, session_factory=session_factory)
    except Exception:
        log.exception("silero vad could not start (onnxruntime missing?); using RMS")
        return None
    return VadGate(vad, ratio)
