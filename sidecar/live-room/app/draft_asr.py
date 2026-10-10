"""Streaming *draft* ASR interface (design stub, CTO-01 / ARCH-01). NOT wired into the pipeline.

Problem: Breeze-ASR-25 on the 5600H CPU needs ~40-48 s per 6 s slice (README, RTF ~7), so
live captions cannot be produced by Breeze alone.

Proposed architecture (two-pass):
  1. Draft pass (live): a true streaming recognizer emits partial Chinese every ~0.6 s.
     Candidate: sherpa-onnx ``streaming-paraformer-bilingual-zh-en`` int8 (Apache-2.0,
     ~0.24 GB, documented RTF ~0.15 on unknown hardware), output converted from Simplified to
     Taiwan Traditional with OpenCC ``s2twp``. Drafts are shown as grey "草稿" text and are
     never written to the ledger, TM or glossary.
  2. Final pass (Breeze): the 6 s slice still goes to Breeze. When it returns, the final line
     replaces the draft (same seq). If Breeze falls behind (RTF > 1), finalisation moves to
     a post-session job (``post_session_finalize``) and the live room keeps the drafts.
  3. Translation runs on *final* lines by default; an opt-in may translate drafts that have
     been stable for N partials.

Interface (this file): ``DraftAsr.feed(pcm16) -> list[DraftPartial]``, ``finish()``,
``reset()``. ``draft_from_env()`` returns ``NullDraft`` unless ``BREEZE_DRAFT_ASR=sherpa``.
Nothing is downloaded here; model paths come from ``BREEZE_DRAFT_MODEL_DIR``.
Acceptance before wiring: zbench layer 1 must show draft RTF p95 < 0.5 with Breeze idle,
and draft+final CER on the user's own recordings must be measured (UNKNOWN today).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

SAMPLE_RATE = 16000


class DraftAsrUnavailable(RuntimeError):
    """sherpa-onnx / OpenCC / model files are missing. Callers fall back to NullDraft."""


@dataclass(frozen=True)
class DraftPartial:
    text: str            # Traditional Chinese (after s2twp)
    is_endpoint: bool    # recognizer detected an utterance end
    t_ms: int            # stream time of this partial
    final: bool = False  # always False: drafts are never final; Breeze finalises


class DraftAsr(Protocol):
    def feed(self, pcm16: bytes) -> list[DraftPartial]: ...
    def finish(self) -> list[DraftPartial]: ...
    def reset(self) -> None: ...


class NullDraft:
    """Default: no draft pass (current behaviour)."""

    def feed(self, pcm16: bytes) -> list[DraftPartial]:
        return []

    def finish(self) -> list[DraftPartial]:
        return []

    def reset(self) -> None:
        return None


def opencc_s2twp() -> Callable[[str], str]:
    try:
        import opencc  # type: ignore
    except ImportError as exc:
        raise DraftAsrUnavailable("OpenCC 未安裝（pip install opencc-python-reimplemented 或 opencc）") from exc
    conv = opencc.OpenCC("s2twp")
    return conv.convert


class SherpaParaformerDraft:
    """sherpa-onnx OnlineRecognizer (streaming paraformer) + OpenCC s2twp.

    ``recognizer`` and ``convert`` are injectable for tests; production builds them from
    ``model_dir`` (encoder.int8.onnx, decoder.int8.onnx, tokens.txt).
    """

    def __init__(self, model_dir: str | Path | None = None, *, recognizer=None, convert=None, threads: int = 1):
        self.convert = convert or opencc_s2twp()
        if recognizer is None:
            try:
                import sherpa_onnx  # type: ignore
            except ImportError as exc:
                raise DraftAsrUnavailable("sherpa-onnx 未安裝") from exc
            d = Path(model_dir or "")
            files = [d / "encoder.int8.onnx", d / "decoder.int8.onnx", d / "tokens.txt"]
            missing = [str(f) for f in files if not f.is_file()]
            if missing:
                raise DraftAsrUnavailable("缺少草稿模型檔：" + ", ".join(missing))
            recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=str(files[2]), encoder=str(files[0]), decoder=str(files[1]), num_threads=threads,
                sample_rate=SAMPLE_RATE, feature_dim=80, enable_endpoint_detection=True)
        self.rec = recognizer
        self.stream = self.rec.create_stream()
        self.samples = 0
        self.last = ""

    def _partials(self) -> list[DraftPartial]:
        out = []
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
        text = self.rec.get_result(self.stream)
        text = text.text if hasattr(text, "text") else str(text)
        endpoint = bool(self.rec.is_endpoint(self.stream))
        if text and (text != self.last or endpoint):
            out.append(DraftPartial(self.convert(text), endpoint, int(self.samples * 1000 / SAMPLE_RATE)))
            self.last = text
        if endpoint:
            self.rec.reset(self.stream)
            self.last = ""
        return out

    def feed(self, pcm16: bytes) -> list[DraftPartial]:
        import array
        a = array.array("h")
        a.frombytes(pcm16[: len(pcm16) // 2 * 2])
        self.samples += len(a)
        self.stream.accept_waveform(SAMPLE_RATE, [x / 32768.0 for x in a])
        return self._partials()

    def finish(self) -> list[DraftPartial]:
        self.stream.input_finished()
        return self._partials()

    def reset(self) -> None:
        self.stream = self.rec.create_stream()
        self.samples = 0
        self.last = ""


XASR_FILES = {"int8": ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"),
              "fp32": ("encoder.onnx", "decoder.onnx", "joiner.onnx")}
# round3/round4 endpoint rules (cer_eval.py): 2.4 s silence before any text, 0.8 s after text, 12 s cap.
ENDPOINT_RULES = dict(rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=0.8,
                      rule3_min_utterance_length=12.0)


def xasr_precision(model_dir: str | Path, prefer: str = "auto") -> str:
    """int8 when its files are present (or asked for), else fp32. Raises if neither is complete."""
    d = Path(model_dir or "")
    order = {"auto": ("int8", "fp32"), "int8": ("int8",), "fp32": ("fp32",)}.get(prefer, ("int8", "fp32"))
    for p in order:
        if all((d / f).is_file() for f in XASR_FILES[p]) and (d / "tokens.txt").is_file():
            return p
    raise DraftAsrUnavailable(f"缺少 X-ASR 模型檔（{d}）；程式不會自動下載")


class XAsrDraft(SherpaParaformerDraft):
    """round4 #5: sherpa-onnx X-ASR 480 ms streaming zipformer transducer (zh-en, punct).

    ``num_threads`` defaults to **1** (round4 D1: int8 at 4 threads - and once at 2 - gave
    non-deterministic / garbled output on the box). ``precision`` auto picks int8 if present.
    Nothing is downloaded: the folder comes from ``BREEZE_DRAFT_MODEL_DIR``.
    """

    def __init__(self, model_dir: str | Path | None = None, *, recognizer=None, convert=None,
                 threads: int = 1, precision: str = "auto"):
        self.precision = None
        if recognizer is None:
            try:
                import sherpa_onnx  # type: ignore
            except ImportError as exc:
                raise DraftAsrUnavailable("sherpa-onnx 未安裝") from exc
            d = Path(model_dir or "")
            self.precision = xasr_precision(d, precision)
            enc, dec, join = (str(d / f) for f in XASR_FILES[self.precision])
            recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(d / "tokens.txt"), encoder=enc, decoder=dec, joiner=join,
                num_threads=max(1, int(threads)), sample_rate=SAMPLE_RATE, feature_dim=80,
                enable_endpoint_detection=True, **ENDPOINT_RULES)
        super().__init__(recognizer=recognizer, convert=convert, threads=threads)
        self.threads = max(1, int(threads))


def draft_from_env(env: dict | None = None) -> DraftAsr:
    env = os.environ if env is None else env
    mode = (env.get("BREEZE_DRAFT_ASR") or "off").strip().lower()
    if mode in ("", "off", "0"):
        return NullDraft()
    if mode not in ("sherpa", "xasr"):
        raise ValueError("BREEZE_DRAFT_ASR 只能是 off、xasr 或 sherpa")
    try:
        threads = max(1, int(env.get("BREEZE_DRAFT_THREADS") or 1))
    except ValueError:
        threads = 1
    try:
        if mode == "xasr":
            return XAsrDraft(env.get("BREEZE_DRAFT_MODEL_DIR"), threads=threads,
                             precision=(env.get("BREEZE_DRAFT_PRECISION") or "auto").strip().lower())
        return SherpaParaformerDraft(env.get("BREEZE_DRAFT_MODEL_DIR"), threads=threads)
    except DraftAsrUnavailable:
        return NullDraft()        # fail soft: live captions keep working without drafts
