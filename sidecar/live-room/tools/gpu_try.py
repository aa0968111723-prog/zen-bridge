"""Run a few WAV clips through the app's ASR worker (app.asr_gpu.build_asr) and print RTF + GPU status.

  set BREEZE_ASR_GPU=vulkan
  set BREEZE_WHISPER_DIR=%LOCALAPPDATA%\\ZenBridge\\tools\\whisper-vulkan
  python -m tools.gpu_try --model <ggml-breeze-asr-25-q5_0.bin> <a.wav> [b.wav ...]

Without BREEZE_ASR_GPU it measures the CPU worker, so the two are directly comparable.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--cpu-threads", type=int, default=4)
    ap.add_argument("wavs", nargs="+", type=Path)
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from app.asr_gpu import build_asr, gpu_status
    from app.native_asr import NativeResidentAsr

    def cpu():
        return NativeResidentAsr(args.model, threads=args.cpu_threads, audio_context=640, beam_size=1)

    t0 = time.monotonic()
    asr, started = build_asr(cpu, args.model)
    print(json.dumps({"start_ok": started.ok, "start_s": round(time.monotonic() - t0, 2),
                      "start_error": started.error, "gpu": gpu_status()}, ensure_ascii=False))
    rows = []
    try:
        for wav in args.wavs:
            with wave.open(str(wav), "rb") as w:
                secs = w.getnframes() / float(w.getframerate())
            t = time.monotonic()
            res = asr.transcribe(wav, "")
            wall = time.monotonic() - t
            row = {"wav": wav.name, "audio_s": round(secs, 2), "wall_s": round(wall, 3),
                   "rtf": round(wall / secs, 3) if secs else None, "ok": res.ok, "text": res.text, "error": res.error}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))
    finally:
        asr.close()
    ok = [r["rtf"] for r in rows if r["ok"] and r["rtf"] is not None]
    print(json.dumps({"n": len(rows), "ok": len(ok), "rtf_max": max(ok) if ok else None,
                      "rtf_mean": round(sum(ok) / len(ok), 3) if ok else None,
                      "active": gpu_status()["active"]}, ensure_ascii=False))
    return 0 if ok and len(ok) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
