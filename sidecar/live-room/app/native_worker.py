"""Isolated, persistent whisper.cpp worker. Local PCM and JSON pipes only."""
from __future__ import annotations
import argparse
import json
import os
import sys
import wave
from pathlib import Path

PREFIX = 'BREEZE_RESULT '

def sampling_strategy(beam_size):
    # Zero preserves pywhispercpp's greedy default; beam search is opt-in.
    return 1 if beam_size > 1 else 0

def emit(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--context', type=int, default=0)
    parser.add_argument('--beam', type=int, default=0)
    parser.add_argument('--best', type=int, default=0)
    args = parser.parse_args()
    import numpy as np
    from pywhispercpp.model import Model
    from app.native_paths import NativePaths
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
        engine = LocalModel(str(model), context_params={'use_gpu': False, 'flash_attn': True},
            n_threads=args.threads, language='zh', audio_ctx=args.context,
            params_sampling_strategy=sampling_strategy(args.beam),
            greedy={'best_of': args.best or 5}, beam_search={'beam_size': args.beam or 5, 'patience': -1.0},
            print_progress=False, print_realtime=False, print_timestamps=False, no_context=True)
    finally:
        os.chdir(original)
        paths.close()
    emit({'ready': True})
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('close'):
            break
        try:
            with wave.open(request['path'], 'rb') as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                    raise ValueError('Expected 16 kHz mono PCM16 audio')
                pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768.0
            segments = engine.transcribe(pcm, initial_prompt=request.get('prompt', ''))
            emit({'ok': True, 'text': ' '.join(s.text for s in segments).strip()})
        except Exception as exc:
            emit({'ok': False, 'error': type(exc).__name__ + ': local inference failed'})

if __name__ == '__main__':
    main()
