"""Fine-tuned Breeze (merged HF Whisper) -> whisper.cpp ggml .bin (+ optional quantize) for the app.

  python convert_asr_to_ggml.py --merged runs/asr1/merged --out runs/asr1/ggml \
      --whisper-cpp C:\\src\\whisper.cpp --openai-whisper C:\\src\\whisper [--quant q5_0]
  python convert_asr_to_ggml.py --merged ... --out ... --plan        # print the commands only

Steps: (1) whisper.cpp/models/convert-h5-to-ggml.py <merged> <openai-whisper repo> <out>  -> ggml-model.bin
       (2) whisper.cpp quantize tool: ggml-model.bin -> ggml-model-<quant>.bin (q5_0 / q8_0)
       (3) point the app at it: BREEZE_MODEL=<path> in .env (or replace the model file the installer placed).
The openai/whisper repo is only needed for its mel filters / tokenizer assets (Breeze uses the Whisper vocab).
Nothing is uploaded; all paths are local.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def plan(a) -> list[list[str]]:
    wc = Path(a.whisper_cpp or "whisper.cpp")
    out = Path(a.out)
    conv = [sys.executable, str(wc / "models" / "convert-h5-to-ggml.py"), str(a.merged), str(a.openai_whisper or "whisper"),
            str(out)]
    steps = [conv]
    if a.quant:
        q = None
        for name in ("whisper-quantize", "quantize"):
            for cand in (wc / "build" / "bin" / name, wc / "build" / "bin" / "Release" / f"{name}.exe"):
                if cand.exists():
                    q = cand
        steps.append([str(q or wc / "build" / "bin" / "whisper-quantize"), str(out / "ggml-model.bin"),
                      str(out / f"ggml-model-{a.quant}.bin"), a.quant])
    return steps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Breeze LoRA 合併模型 → whisper.cpp ggml")
    ap.add_argument("--merged", required=True, help="train_asr_lora.py 的 merged/ 資料夾")
    ap.add_argument("--out", required=True)
    ap.add_argument("--whisper-cpp")
    ap.add_argument("--openai-whisper")
    ap.add_argument("--quant", default="q5_0", help="q5_0 / q8_0 / 空字串 = 不量化")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    steps = plan(a)
    print("# convert steps (local only):")
    for s in steps:
        print(" ".join(f'"{x}"' if " " in x else x for x in s))
    print(f"# then: set BREEZE_MODEL={Path(a.out) / ('ggml-model-' + a.quant + '.bin' if a.quant else 'ggml-model.bin')} in .env")
    if a.plan:
        return 0
    if not (a.whisper_cpp and a.openai_whisper):
        print("需要 --whisper-cpp 與 --openai-whisper（本機原始碼資料夾）", file=sys.stderr)
        return 2
    Path(a.out).mkdir(parents=True, exist_ok=True)
    for s in steps:
        subprocess.run(s, check=True)
    shutil.copy2(Path(a.merged) / "config.json", Path(a.out) / "source-config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
