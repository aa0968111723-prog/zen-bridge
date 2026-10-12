"""Fast final ASR for endpoint-cut lines (grok/fast-draft-asr, opt-in ``BREEZE_FAST_FINAL``).

Why: on the 5600H iGPU Breeze-ASR-25 (Vulkan, audio_ctx 640) needs ~3.2 s for *any* line, even a
1 s one, so an endpoint-cut final can never land before ~3.5-4 s after the speaker stops. An
offline (non-streaming) sherpa-onnx model decodes the same line in ~0.3 s on 4 CPU threads.

Flow when ``BREEZE_FAST_FINAL=sense_voice`` (or ``paraformer``) and ``BREEZE_ENDPOINT_FINAL=1``:
  1. The endpoint-cut line goes through the normal pipeline (same seq, admission, glossary,
     translation), but its ASR call is answered by the fast model -> final + translation
     arrive ~1-1.5 s after the speaker stops.
  2. Unless ``BREEZE_FAST_FINAL_CORRECT=0``, Breeze then re-hears the same audio in the
     background. If its text differs, the line is replaced through ``pipeline.retranslate``
     (zh replaced, translation redone) -> the accuracy of the line ends up Breeze's again.
     Corrections run one at a time; when the backlog is deep the oldest pending ones are
     skipped so Breeze never falls behind the room (the fast line stays).

Routing is by a ContextVar set only around endpoint-final submissions, so the 6 s fallback
slices, retries and every other caller still reach Breeze directly. Nothing is downloaded:
``BREEZE_FAST_FINAL_MODEL_DIR`` must point at an extracted sherpa-onnx model folder.
"""
from __future__ import annotations

import array
import asyncio
import contextvars
import logging
import os
import threading
import tempfile
import wave
from pathlib import Path
from typing import Callable

from app.asr import AsrResult

log = logging.getLogger("breeze.fast_final")

SAMPLE_RATE = 16000
KINDS = ("sense_voice", "paraformer")

# "fast" while an endpoint-final line is inside pipeline.submit; asyncio.to_thread copies it.
ROUTE: contextvars.ContextVar[str] = contextvars.ContextVar("breeze_final_route", default="")


class FastFinalUnavailable(RuntimeError):
    pass


def _pcm_floats(wav: Path) -> list[float]:
    with wave.open(str(wav), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise ValueError("fast final expects 16 kHz mono 16-bit WAVE")
        if not 0 < w.getnframes() <= SAMPLE_RATE * 30:
            raise ValueError("fast final audio must be between 0 and 30 seconds")
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    return [x / 32768.0 for x in a]


class SherpaOfflineFinal:
    """Offline sherpa-onnx recognizer + OpenCC s2twp. ``transcribe(wav) -> str``."""

    def __init__(self, kind: str, model_dir: str | Path | None, *, threads: int = 4,
                 recognizer=None, convert: Callable[[str], str] | None = None, language: str = 'zh'):
        if kind not in KINDS:
            raise ValueError(f"BREEZE_FAST_FINAL 只能是 off、{'、'.join(KINDS)}")
        self.kind = kind
        if language not in ('zh','en','auto'):
            raise ValueError('Invalid fast ASR language')
        self._lock = threading.Lock()          # one OfflineRecognizer, decoded one line at a time
        if convert is None:
            from app.draft_asr import DraftAsrUnavailable, opencc_s2twp
            try:
                convert = opencc_s2twp()
            except DraftAsrUnavailable as exc:
                raise FastFinalUnavailable(str(exc)) from exc
        self.convert = convert
        if recognizer is None:
            from app.speech_extras import bootstrap
            bootstrap()
            try:
                import sherpa_onnx  # type: ignore
            except ImportError as exc:
                raise FastFinalUnavailable("sherpa-onnx 未安裝") from exc
            d = Path(model_dir or "")
            model = next((d / n for n in ("model.int8.onnx", "model.onnx") if (d / n).is_file()), None)
            if model is None or not (d / "tokens.txt").is_file():
                raise FastFinalUnavailable(f"找不到快速定稿模型：{d}")
            R = sherpa_onnx.OfflineRecognizer
            if kind == "sense_voice":
                recognizer = R.from_sense_voice(model=str(model), tokens=str(d / "tokens.txt"),
                                                num_threads=threads, use_itn=True, language=language)
            else:
                recognizer = R.from_paraformer(paraformer=str(model), tokens=str(d / "tokens.txt"),
                                               num_threads=threads)
        self.rec = recognizer

    def transcribe(self, wav: Path) -> str:
        samples = _pcm_floats(wav)
        with self._lock:
            s = self.rec.create_stream()
            s.accept_waveform(SAMPLE_RATE, samples)
            self.rec.decode_stream(s)
            text = s.result.text
        return self.convert(text or "").strip()


class FastFinalAsr:
    """Pipeline ASR wrapper: endpoint-final lines (ROUTE == "fast") use the fast model.

    Everything else, and any fast-model failure, falls through to the wrapped Breeze ASR.
    Other attributes (warm-up, health, ...) are forwarded so the pipeline cannot tell.
    """

    def __init__(self, primary, fast: SherpaOfflineFinal):
        self.primary = primary
        self.fast = fast

    def transcribe(self, wav: Path, prompt: str, *args, **kwargs):
        if ROUTE.get() == "fast":
            try:
                text = self.fast.transcribe(Path(wav))
                return AsrResult(ok=True, text=text, blank=not text)
            except Exception:
                log.exception("fast final failed; Breeze takes this line")
        return self.primary.transcribe(wav, prompt, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.primary, name)


def _norm(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isalnum()).lower()


class Corrector:
    """Background Breeze re-hearing of fast finals, one at a time, newest first when behind."""

    def __init__(self, primary, prompt: str, tmp_dir: Path, apply, *, max_pending: int = 3):
        self.primary = primary
        self.prompt = prompt
        self.tmp_dir = Path(tmp_dir)
        self.apply = apply                     # async (room, session, seq, zh) -> None
        self.max_pending = max(1, int(max_pending))
        self._pending: list[tuple] = []
        self._task: asyncio.Task | None = None
        self.skipped = 0
        self.corrected = 0
        self.kept = 0
        self._closed = False

    def submit(self, room_id: str, session_id: str, seq: int, wav_bytes: bytes, fast_text: str,
               *, expected_epoch: int | None = None) -> None:
        if self._closed:
            return
        self._pending.append((room_id, session_id, seq, wav_bytes, fast_text, expected_epoch))
        while len(self._pending) > self.max_pending:
            self._pending.pop(0)               # the room has moved on; that line keeps the fast text
            self.skipped += 1
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            room_id, session_id, seq, wav_bytes, fast_text, expected_epoch = self._pending.pop(0)
            fd, name = tempfile.mkstemp(prefix="correct-", suffix=f"-{seq}.wav", dir=self.tmp_dir)
            path = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(wav_bytes)
                def transcribe():
                    from app.runtime_activity import work
                    with work('transcribe'):
                        return self.primary.transcribe(path, self.prompt)
                res = await asyncio.to_thread(transcribe)
                if self._closed:
                    continue
                text = (getattr(res, "text", "") or "").strip()
                if not getattr(res, "ok", False) or not text:
                    continue
                if _norm(text) == _norm(fast_text):
                    self.kept += 1
                    continue
                if expected_epoch is None:
                    await self.apply(room_id, session_id, seq, text)
                else:
                    await self.apply(room_id, session_id, seq, text, fast_text, expected_epoch)
                self.corrected += 1
            except Exception:
                log.exception("Breeze correction failed room=%s seq=%s", room_id, seq)
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass

    def stats(self) -> dict:
        return {"pending": len(self._pending), "corrected": self.corrected, "kept": self.kept,
                "skipped": self.skipped}

    async def aclose(self) -> None:
        self._closed = True
        self._pending.clear()
        # Do not cancel to_thread and unlink a WAV while native ASR still reads it.
        if self._task is not None:
            await asyncio.shield(self._task)


def fast_final_from_env(env: dict | None = None) -> SherpaOfflineFinal | None:
    env = os.environ if env is None else env
    kind = (env.get("BREEZE_FAST_FINAL") or "off").strip().lower()
    if kind in ("", "off", "0"):
        return None
    try:
        threads = max(1, min(4, int(env.get("BREEZE_FAST_FINAL_THREADS") or 4)))
    except ValueError:
        threads = 4
    try:
        return SherpaOfflineFinal(kind, env.get("BREEZE_FAST_FINAL_MODEL_DIR"), threads=threads)
    except FastFinalUnavailable as exc:
        log.warning("fast final off: %s", exc)
        return None                       # fail soft: endpoint finals keep using Breeze
