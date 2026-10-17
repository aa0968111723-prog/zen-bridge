"""CER of a model on the held-out split. Two backends:

  HF (base or merged LoRA):  python eval_asr_cer.py --data <export>/asr --model runs/asr1/merged [--adapter runs/asr1/adapter]
  whisper.cpp (the app's):   python eval_asr_cer.py --data <export>/asr --whisper-cli C:\\...\\whisper-cli.exe --ggml ggml-breeze-ft.bin

Run it with the base model and with the fine-tuned one to compare before/after on the same split.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import corpus_cer, read_split  # noqa: E402


def whisper_cpp(cli: str, ggml: str, rows, lang="zh") -> list[str]:
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, r in enumerate(rows):
            base = Path(td) / f"o{i}"
            subprocess.run([cli, "-m", ggml, "-l", lang, "-nt", "-otxt", "-of", str(base), "-f", r["path"]],
                           check=True, capture_output=True)
            out.append(base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip())
    return out


def hf(model_id: str, adapter: str | None, rows, lang="zh") -> list[str]:
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import load_wav
    proc = WhisperProcessor.from_pretrained(model_id, language=lang, task="transcribe")
    model = WhisperForConditionalGeneration.from_pretrained(model_id)
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(dev).eval()
    out = []
    with torch.no_grad():
        for r in rows:
            x = proc.feature_extractor(load_wav(r["path"]), sampling_rate=16000, return_tensors="pt").input_features.to(dev)
            ids = model.generate(input_features=x, language=lang, task="transcribe", max_new_tokens=200)
            out.append(proc.tokenizer.decode(ids[0], skip_special_tokens=True))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--split", default="test")
    ap.add_argument("--model")
    ap.add_argument("--adapter")
    ap.add_argument("--whisper-cli")
    ap.add_argument("--ggml")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rows = read_split(a.data, a.split)
    if not rows:
        print("切分是空的", file=sys.stderr)
        return 2
    hyps = whisper_cpp(a.whisper_cli, a.ggml, rows) if a.whisper_cli else hf(a.model, a.adapter, rows)
    refs = [r["transcription"] for r in rows]
    per = {}
    for r, h in zip(rows, hyps):
        per.setdefault(r["speaker_id"], ([], []))
        per[r["speaker_id"]][0].append(h)
        per[r["speaker_id"]][1].append(r["transcription"])
    rep = {"split": a.split, "clips": len(rows), "cer": corpus_cer(hyps, refs),
           "cer_per_speaker": {k: corpus_cer(*v) for k, v in per.items()}}
    if a.out:
        Path(a.out).write_text(json.dumps({**rep, "hyps": hyps}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
