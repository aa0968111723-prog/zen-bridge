"""Isolated, persistent whisper.cpp worker. Local PCM and JSON pipes only.

Tuning (optimization-round2 §0/§2; see app/asr_tuning.py for every env knob): flash attention
off, no temperature fallback, greedy best_of 1, audio_ctx derived from the clip length, a
max_tokens cap with a repetition-loop retry at full context, print_timings per clip and
system_info at start (both on stderr -> the parent's rotating log), and on Windows the worker
raises itself to ABOVE_NORMAL and opts out of EcoQoS. Transcripts never go to stderr.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import tempfile
import sys
import time
import wave
from pathlib import Path

from app.asr_tuning import FULL_CTX, audio_ctx_for, boost_current_process, max_tokens_for, repetition_loop

PREFIX = 'BREEZE_RESULT '
SR = 16000


def sampling_strategy(beam_size):
    # Zero preserves pywhispercpp's greedy default; beam search is opt-in.
    return 1 if beam_size > 1 else 0


def emit(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)


def note(msg):
    """Diagnostics only (no user text): stderr, which the parent writes to a rotating log."""
    print(msg, file=sys.stderr, flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--context', type=int, default=0)        # 0 = derive per clip
    parser.add_argument('--beam', type=int, default=0)
    parser.add_argument('--best', type=int, default=0)           # 0 -> 1 (no extra candidates)
    parser.add_argument('--flash-attn', type=int, default=0)
    parser.add_argument('--temperature-inc', type=float, default=0.0)
    parser.add_argument('--context-min', type=int, default=640)
    parser.add_argument('--max-tokens', type=int, default=0)     # 0 = derive per clip
    parser.add_argument('--repeat-guard', type=int, default=1)
    parser.add_argument('--timings', type=int, default=1)
    parser.add_argument('--priority', default='above_normal')
    parser.add_argument('--ecoqos-off', type=int, default=1)
    return parser.parse_args(argv)


def model_kwargs(args) -> dict:
    """Constructor parameters for pywhispercpp.Model (pure; unit-tested)."""
    return dict(
        context_params={'use_gpu': False, 'flash_attn': bool(args.flash_attn)},
        n_threads=args.threads, language='zh',
        audio_ctx=audio_ctx_for(6.0, args.context, args.context_min),
        params_sampling_strategy=sampling_strategy(args.beam),
        greedy={'best_of': args.best or 1},
        beam_search={'beam_size': args.beam or 5, 'patience': -1.0},
        temperature_inc=float(args.temperature_inc),
        print_progress=False, print_realtime=False, print_timestamps=False, no_context=True)


_TIMING = re.compile(r"\b(sample|encode|decode|batchd|prompt|total) time\s*=\s*([0-9.]+)\s*ms")
_FALLBACKS = re.compile(r"fallbacks\s*=\s*([0-9]+)\s*p\s*/\s*([0-9]+)\s*h")


def parse_whisper_timings(text: str) -> dict:
    """whisper_print_timings lines -> {"encode_ms", "decode_ms", ..., "fallbacks"} (missing keys omitted)."""
    out: dict = {}
    for name, ms in _TIMING.findall(text or ""):
        out[f"{name}_ms"] = round(float(ms), 2)
    m = _FALLBACKS.search(text or "")
    if m:
        out["fallbacks"] = int(m.group(1)) + int(m.group(2))
    return out


def capture_native_stderr(fn) -> str:
    """Run ``fn`` with C-level fd 2 pointed at a temp file and return what it wrote (then echo it to the
    real stderr so the rotating worker log keeps it). Root cause of null encode/decode ms in T4: the
    timings only ever went to stderr, never into the per-clip stats."""
    try:
        sys.stderr.flush()
    except Exception:
        pass
    saved = os.dup(2)
    with tempfile.TemporaryFile() as tmp:
        os.dup2(tmp.fileno(), 2)
        try:
            fn()
        finally:
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(saved, 2)
            os.close(saved)
        tmp.seek(0)
        text = tmp.read().decode("utf-8", "replace")
    if text:
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass
    return text


def _reset_timings(engine) -> None:
    try:
        import _pywhispercpp as pw
        ctx = getattr(engine, "_ctx", None)
        if ctx is not None and hasattr(pw, "whisper_reset_timings"):
            pw.whisper_reset_timings(ctx)
    except Exception:
        pass


def transcribe_clip(engine, pcm, prompt: str, language: str, args, clock=time.perf_counter) -> dict:
    seconds = len(pcm) / SR
    ctx = audio_ctx_for(seconds, args.context, args.context_min)
    cap = max_tokens_for(seconds, args.max_tokens)
    if args.timings:
        _reset_timings(engine)           # per-clip numbers, not process totals
    t0 = clock()
    segments = engine.transcribe(pcm, initial_prompt=prompt, language=language, audio_ctx=ctx, max_tokens=cap)
    text = ' '.join(s.text for s in segments).strip()
    retried = False
    if args.repeat_guard and ctx < FULL_CTX and repetition_loop(text):
        # A small audio_ctx can push whisper into a loop (#1951): one retry at full context.
        retried = True
        segments = engine.transcribe(pcm, initial_prompt=prompt, language=language, audio_ctx=FULL_CTX,
                                     max_tokens=cap)
        text = ' '.join(s.text for s in segments).strip()
        if repetition_loop(text):
            text = ''
    elapsed = clock() - t0
    stats = {'audio_s': round(seconds, 3), 'asr_s': round(elapsed, 3),
             'rtf': round(elapsed / seconds, 3) if seconds > 0 else None,
             'audio_ctx': ctx, 'max_tokens': cap, 'retried_full_ctx': retried}
    if args.timings:
        timings = getattr(engine, 'print_timings', None)
        if callable(timings):
            try:
                # whisper_print_timings -> C stderr; capture it so encode/decode ms reach the stats
                stats.update(parse_whisper_timings(capture_native_stderr(timings)))
            except Exception as exc:     # pragma: no cover
                note(f'print_timings failed: {type(exc).__name__}')
        note('BREEZE_TIMING ' + json.dumps(stats))
    return {'text': text, 'stats': stats}


def main():
    args = parse_args()
    import numpy as np
    from pywhispercpp.model import Model
    from app.native_paths import NativePaths
    boosted = boost_current_process(args.priority, bool(args.ecoqos_off))
    note('BREEZE_WORKER ' + json.dumps({'boost': boosted, 'pid': os.getpid()}))
    model = Path(args.model).resolve()
    if not model.is_file():
        raise FileNotFoundError('Local Breeze model is missing')
    paths = NativePaths(model)
    # The binding normally resolves back to an absolute UTF-8 path, which the
    # Windows CRT cannot always open. Override only that path before native init.
    class LocalModel(Model):
        def _init_model(self):
            self.model_path = paths.argument(model)
            super()._init_model()
    original = Path.cwd()
    try:
        if paths.cwd is not None:
            os.chdir(paths.cwd)
        engine = LocalModel(str(model), **model_kwargs(args))
    finally:
        os.chdir(original)
        paths.close()
    info = ''
    if args.timings:
        try:
            info = str(Model.system_info())
            note('BREEZE_SYSTEM_INFO ' + info)
        except Exception as exc:   # pragma: no cover
            note(f'system_info failed: {type(exc).__name__}')
    emit({'ready': True, 'system_info': info[:500], 'boost': boosted})
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('close'):
            break
        try:
            with wave.open(request['path'], 'rb') as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SR):
                    raise ValueError('Expected 16 kHz mono PCM16 audio')
                pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
            language = request.get('language', 'zh')
            if language not in ('zh', 'en'):
                raise ValueError('Unsupported language')
            out = transcribe_clip(engine, pcm, request.get('prompt', ''), language, args)
            emit({'ok': True, 'text': out['text'], 'stats': out['stats']})
        except Exception as exc:
            emit({'ok': False, 'error': type(exc).__name__ + ': local inference failed'})

if __name__ == '__main__':
    main()
