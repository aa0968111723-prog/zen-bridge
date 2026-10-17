"""Fine-tuned MT model -> GGUF for llama.cpp / Ollama, so the app can use it.

  python convert_mt_to_gguf.py --merged runs/mt1/merged --out runs/mt1/gguf --llama-cpp C:\\src\\llama.cpp \
      [--quant Q4_K_M] [--ollama-name zen-mt-qwen3-4b-lora] [--create]
  python convert_mt_to_gguf.py --merged ... --out ... --plan

Steps: (1) llama.cpp/convert_hf_to_gguf.py <merged> --outtype f16 --outfile <out>/model-f16.gguf
       (2) llama-quantize model-f16.gguf model-<quant>.gguf <quant>
       (3a) Ollama: write Modelfile and `ollama create <name> -f Modelfile`; then BREEZE_TRANSLATE_MODEL=<name>
       (3b) llama-server: BREEZE_MT_GGUF_EN / BREEZE_MT_GGUF_JA=<out>/model-<quant>.gguf (same switch the Hy-MT backend uses)
Alternative without merging: llama.cpp/convert_lora_to_gguf.py <adapter> --base <base> -> ADAPTER line in the
Modelfile or `llama-server --lora`. Everything stays on this machine.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SYSTEM = ("You translate Traditional Chinese dharma talks and Zen club announcements into natural English or "
          "Japanese as asked. Keep Buddhist terms consistent with the club glossary. Output only the translation.")


def steps(a) -> list[list[str]]:
    lc = Path(a.llama_cpp or "llama.cpp")
    out = Path(a.out)
    f16 = out / "model-f16.gguf"
    q = out / f"model-{a.quant}.gguf"
    quant_bin = next((p for p in (lc / "build" / "bin" / "llama-quantize", lc / "build" / "bin" / "Release" /
                                  "llama-quantize.exe") if p.exists()), lc / "build" / "bin" / "llama-quantize")
    s = [[sys.executable, str(lc / "convert_hf_to_gguf.py"), str(a.merged), "--outtype", "f16", "--outfile", str(f16)],
         [str(quant_bin), str(f16), str(q), a.quant]]
    if a.create:
        s.append(["ollama", "create", a.ollama_name, "-f", str(out / "Modelfile")])
    return s


def modelfile(a) -> str:
    return (f"FROM ./model-{a.quant}.gguf\n"
            f'SYSTEM """{SYSTEM}"""\n'
            "PARAMETER temperature 0.2\nPARAMETER num_ctx 2048\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="翻譯 LoRA 合併模型 → GGUF（llama.cpp／Ollama）")
    ap.add_argument("--merged", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--llama-cpp")
    ap.add_argument("--quant", default="Q4_K_M")
    ap.add_argument("--ollama-name", default="zen-mt-qwen3-4b-lora")
    ap.add_argument("--create", action="store_true", help="最後執行 ollama create（本機）")
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    print("# convert steps (local only):")
    for s in steps(a):
        print(" ".join(s))
    print(f"# then: BREEZE_TRANSLATE_MODEL={a.ollama_name} (Ollama) or BREEZE_MT_GGUF_EN / BREEZE_MT_GGUF_JA={Path(a.out) / f'model-{a.quant}.gguf'} (llama-server)")
    if a.plan:
        return 0
    if not a.llama_cpp:
        print("需要 --llama-cpp（本機 llama.cpp 原始碼＋已編譯的 llama-quantize）", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "Modelfile").write_text(modelfile(a), encoding="utf-8")
    for s in steps(a):
        subprocess.run(s, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
