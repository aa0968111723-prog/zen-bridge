"""Breeze-ASR-25 LoRA fine-tune (HF transformers + peft) with CER before/after on the held-out test split.

  python training/scripts/train_asr_lora.py --data <export>/asr --out runs/asr1 [--config training/configs/asr_breeze_lora.yaml]
  python training/scripts/train_asr_lora.py --data <export>/asr --out /tmp/dry --dry-run     # CPU, tiny random model, offline

--dry-run builds a 1-layer random Whisper locally (no download) and a byte tokenizer, runs a couple of optimizer
steps on CPU and evaluates CER, so CI can exercise the full code path. Real training needs an NVIDIA GPU
(see training/README.md); the laptop's Radeon iGPU cannot train this model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ByteTokenizer, training_args, corpus_cer, load_config, load_wav, read_split  # noqa: E402

HERE = Path(__file__).resolve().parents[1]


class _Tok:
    """Uniform encode/decode over the real Whisper tokenizer or the dry-run byte tokenizer."""

    def __init__(self, processor=None):
        self.p = processor
        self.b = None if processor else ByteTokenizer()

    def labels(self, text: str) -> list[int]:
        if self.b:
            return self.b.encode(text) + [self.b.eos_token_id]
        ids = self.p.tokenizer(text).input_ids
        return ids

    def decode(self, ids) -> str:
        if self.b:
            return self.b.decode(ids)
        return self.p.tokenizer.decode(ids, skip_special_tokens=True)


def build(cfg: dict, dry: bool):
    import torch  # noqa: F401
    from transformers import WhisperConfig, WhisperFeatureExtractor, WhisperForConditionalGeneration
    if dry:
        d = cfg.get("dry_run", {})
        bt = ByteTokenizer()
        conf = WhisperConfig(vocab_size=bt.vocab_size, d_model=d.get("d_model", 32), encoder_layers=d.get("layers", 1),
                             decoder_layers=d.get("layers", 1), encoder_attention_heads=d.get("heads", 2),
                             decoder_attention_heads=d.get("heads", 2), encoder_ffn_dim=64, decoder_ffn_dim=64,
                             num_mel_bins=80, max_source_positions=1500, max_target_positions=448,
                             pad_token_id=bt.pad_token_id, bos_token_id=bt.bos_token_id, eos_token_id=bt.eos_token_id,
                             decoder_start_token_id=bt.sot_id, suppress_tokens=[], begin_suppress_tokens=[])
        model = WhisperForConditionalGeneration(conf)
        model.generation_config.forced_decoder_ids = None
        model.generation_config.decoder_start_token_id = bt.sot_id
        model.generation_config.eos_token_id = bt.eos_token_id
        model.generation_config.pad_token_id = bt.pad_token_id
        return model, WhisperFeatureExtractor(), _Tok(None)
    from transformers import WhisperProcessor
    proc = WhisperProcessor.from_pretrained(cfg["base_model"], language=cfg.get("language", "zh"), task=cfg.get("task", "transcribe"))
    kw = {}
    if cfg["train"].get("load_in_8bit"):
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
        kw["device_map"] = "auto"
    model = WhisperForConditionalGeneration.from_pretrained(cfg["base_model"], **kw)
    model.generation_config.language = cfg.get("language", "zh")
    model.generation_config.task = cfg.get("task", "transcribe")
    model.generation_config.forced_decoder_ids = None
    return model, proc.feature_extractor, _Tok(proc)


def featurize(rows, fe, tok, max_label: int):
    out = []
    for r in rows:
        feats = fe(load_wav(r["path"]), sampling_rate=16000, return_tensors="np").input_features[0]
        out.append({"input_features": feats, "labels": tok.labels(r["transcription"])[:max_label],
                    "text": r["transcription"]})
    return out


def collate(batch, decoder_start: int):
    import numpy as np
    import torch
    feats = torch.tensor(np.stack([b["input_features"] for b in batch]))
    n = max(len(b["labels"]) for b in batch)
    labels = torch.full((len(batch), n), -100, dtype=torch.long)
    for i, b in enumerate(batch):
        ids = b["labels"]
        if ids and ids[0] == decoder_start:          # the model prepends decoder_start itself
            ids = ids[1:]
        labels[i, :len(ids)] = torch.tensor(ids, dtype=torch.long)
    return {"input_features": feats, "labels": labels}


def transcribe(model, items, tok, max_new: int, batch: int = 4) -> list[str]:
    import numpy as np
    import torch
    model.eval()
    dev = next(model.parameters()).device
    dtype = next(model.parameters()).dtype
    out = []
    with torch.no_grad():
        for i in range(0, len(items), batch):
            x = torch.tensor(np.stack([it["input_features"] for it in items[i:i + batch]])).to(dev, dtype)
            ids = model.generate(input_features=x, max_new_tokens=max_new)
            out += [tok.decode(s.tolist()) for s in ids]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Breeze-ASR-25 LoRA 微調（只在本機／自己的 GPU）")
    ap.add_argument("--config", default=str(HERE / "configs" / "asr_breeze_lora.yaml"))
    ap.add_argument("--data", required=True, help="匯出資料夾裡的 asr/")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="CPU＋迷你隨機模型，不下載，CI 用")
    ap.add_argument("--max-steps", type=int, default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.dry_run:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import Seq2SeqTrainer, Seq2SeqTrainingArguments
    torch.manual_seed(cfg["train"].get("seed", 42))
    t0 = time.time()
    lim = cfg.get("dry_run", {}).get("max_clips", 4) if args.dry_run else None
    rows = {s: read_split(args.data, s, lim) for s in ("train", "validation", "test")}
    if not rows["train"] or not rows["test"]:
        print("需要 train 與 test 切分（先用 python -m app.training build 匯出）", file=sys.stderr)
        return 2
    model, fe, tok = build(cfg, args.dry_run)
    tr = cfg["train"]
    max_new = 8 if args.dry_run else tr.get("eval_max_new_tokens", 200)
    data = {s: featurize(r, fe, tok, tr.get("max_label_tokens", 200)) for s, r in rows.items()}
    refs = [x["text"] for x in data["test"]]
    cer_before = corpus_cer(transcribe(model, data["test"], tok, max_new), refs)
    lc = cfg["lora"]
    model = get_peft_model(model, LoraConfig(r=lc["r"], lora_alpha=lc["alpha"], lora_dropout=lc["dropout"],
                                             target_modules=lc["target_modules"], bias="none"))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    if tr.get("gradient_checkpointing") and not args.dry_run:
        model.base_model.model.gradient_checkpointing_enable()
        model.base_model.model.enable_input_require_grads()
    dstart = model.base_model.model.config.decoder_start_token_id
    steps = args.max_steps or (cfg.get("dry_run", {}).get("steps", 2) if args.dry_run else -1)
    targs = training_args(Seq2SeqTrainingArguments, 
        output_dir=str(Path(args.out) / "checkpoints"), per_device_train_batch_size=2 if args.dry_run else tr["batch_size"],
        gradient_accumulation_steps=1 if args.dry_run else tr["grad_accum"], learning_rate=tr["learning_rate"],
        num_train_epochs=tr["epochs"], max_steps=steps, warmup_ratio=0 if args.dry_run else tr["warmup_ratio"],
        fp16=bool(tr.get("fp16")) and torch.cuda.is_available(), logging_steps=1 if args.dry_run else 25,
        save_strategy="no" if args.dry_run else "epoch", eval_strategy="no" if args.dry_run or not data["validation"] else "epoch",
        report_to=[], remove_unused_columns=False, label_names=["labels"], use_cpu=args.dry_run,
        dataloader_pin_memory=False, seed=tr.get("seed", 42))
    trainer = Seq2SeqTrainer(model=model, args=targs, train_dataset=data["train"],
                             eval_dataset=data["validation"] or None, data_collator=lambda b: collate(b, dstart))
    res = trainer.train()
    cer_after = corpus_cer(transcribe(model, data["test"], tok, max_new), refs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out / "adapter")
    if not args.dry_run:
        merged = model.merge_and_unload()
        merged.save_pretrained(out / "merged")
        tok.p.save_pretrained(out / "merged")
    report = {"base_model": "dry-run-tiny-whisper" if args.dry_run else cfg["base_model"], "dry_run": args.dry_run,
              "clips": {s: len(v) for s, v in data.items()}, "cer_before": cer_before, "cer_after": cer_after,
              "train_loss": res.training_loss, "steps": res.global_step, "trainable_params": trainable,
              "total_params": total, "seconds": round(time.time() - t0, 1)}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
