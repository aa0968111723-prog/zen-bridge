"""MT LoRA on a small LLM (default Qwen3-4B) from the chat-format JSONL that the dataset builder exports.

  python training/scripts/train_mt_lora.py --data <export>/mt --out runs/mt1
  python training/scripts/train_mt_lora.py --data <export>/mt --out /tmp/drymt --dry-run   # CPU, tiny random Qwen2, offline

Uses unsloth (4-bit QLoRA) when installed and use_unsloth is auto/true; otherwise transformers + peft.
Loss is computed on the assistant turn only. Writes adapter/, merged/ (real runs) and report.json
with exact-match / chrF-lite on the test split before and after.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ByteTokenizer, training_args, load_config, read_chat  # noqa: E402

HERE = Path(__file__).resolve().parents[1]


def char_f(h: str, r: str, n: int = 3) -> float:
    """chrF-like char n-gram F1 (no dependency); enough to see before/after movement."""
    def grams(s):
        s = "".join(s.split())
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    a, b = grams(h), grams(r)
    if not a or not b:
        return 0.0
    p, rc = len(a & b) / len(a), len(a & b) / len(b)
    return 0.0 if p + rc == 0 else 2 * p * rc / (p + rc)


class _ByteChat:
    """Dry-run tokenizer with a trivial chat template."""

    def __init__(self):
        self.b = ByteTokenizer()
        self.eos_token_id, self.pad_token_id = self.b.eos_token_id, self.b.pad_token_id

    def prompt_ids(self, msgs):
        txt = "".join(f"<{m['role']}>{m['content']}\n" for m in msgs if m["role"] != "assistant") + "<assistant>"
        return [self.b.bos_token_id] + self.b.encode(txt)

    def answer_ids(self, text):
        return self.b.encode(text) + [self.b.eos_token_id]

    def decode(self, ids):
        return self.b.decode(ids)


class _HFChat:
    def __init__(self, tok):
        self.t = tok
        self.eos_token_id = tok.eos_token_id
        self.pad_token_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    def prompt_ids(self, msgs):
        m = [x for x in msgs if x["role"] != "assistant"]
        kw = {"enable_thinking": False}
        try:
            return self.t.apply_chat_template(m, add_generation_prompt=True, tokenize=True, **kw)
        except TypeError:
            return self.t.apply_chat_template(m, add_generation_prompt=True, tokenize=True)

    def answer_ids(self, text):
        return self.t(text, add_special_tokens=False).input_ids + [self.eos_token_id]

    def decode(self, ids):
        return self.t.decode(ids, skip_special_tokens=True)


def build(cfg, dry):
    if dry:
        from transformers import Qwen2Config, Qwen2ForCausalLM
        d = cfg.get("dry_run", {})
        tok = _ByteChat()
        conf = Qwen2Config(vocab_size=ByteTokenizer.vocab_size, hidden_size=d.get("hidden", 32), intermediate_size=64,
                           num_hidden_layers=d.get("layers", 1), num_attention_heads=d.get("heads", 2),
                           num_key_value_heads=1, max_position_embeddings=1024, eos_token_id=tok.eos_token_id,
                           pad_token_id=tok.pad_token_id, bos_token_id=1)
        return Qwen2ForCausalLM(conf), tok, False
    want = str(cfg.get("use_unsloth", "auto")).lower()
    if want in ("auto", "true"):
        try:
            from unsloth import FastLanguageModel
            model, tok = FastLanguageModel.from_pretrained(cfg["base_model"], max_seq_length=cfg["max_seq_len"],
                                                           load_in_4bit=bool(cfg["train"].get("load_in_4bit", True)))
            lc = cfg["lora"]
            model = FastLanguageModel.get_peft_model(model, r=lc["r"], lora_alpha=lc["alpha"], lora_dropout=lc["dropout"],
                                                     target_modules=lc["target_modules"], use_gradient_checkpointing="unsloth")
            return model, _HFChat(tok), True
        except ImportError:
            if want == "true":
                raise
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(cfg["base_model"])
    kw = {"torch_dtype": "auto"}
    if cfg["train"].get("load_in_4bit"):
        from transformers import BitsAndBytesConfig
        kw.update(quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4"), device_map="auto")
    return AutoModelForCausalLM.from_pretrained(cfg["base_model"], **kw), _HFChat(tok), False


def encode(rows, tok, max_len):
    out = []
    for r in rows:
        msgs = r["messages"]
        p = list(tok.prompt_ids(msgs))
        a = tok.answer_ids(msgs[-1]["content"])
        ids = (p + a)[:max_len]
        labels = ([-100] * len(p) + a)[:max_len]
        out.append({"input_ids": ids, "labels": labels, "prompt": p, "ref": msgs[-1]["content"]})
    return out


def collate(batch, pad):
    import torch
    n = max(len(b["input_ids"]) for b in batch)
    ids = torch.full((len(batch), n), pad, dtype=torch.long)
    lab = torch.full((len(batch), n), -100, dtype=torch.long)
    att = torch.zeros((len(batch), n), dtype=torch.long)
    for i, b in enumerate(batch):
        k = len(b["input_ids"])
        ids[i, :k] = torch.tensor(b["input_ids"])
        lab[i, :k] = torch.tensor(b["labels"])
        att[i, :k] = 1
    return {"input_ids": ids, "labels": lab, "attention_mask": att}


def evaluate(model, items, tok, max_new):
    import torch
    model.eval()
    dev = next(model.parameters()).device
    hyps = []
    with torch.no_grad():
        for it in items:
            x = torch.tensor([it["prompt"]], device=dev)
            y = model.generate(input_ids=x, attention_mask=torch.ones_like(x), max_new_tokens=max_new, do_sample=False,
                               pad_token_id=tok.pad_token_id, eos_token_id=tok.eos_token_id)
            hyps.append(tok.decode(y[0, x.shape[1]:].tolist()).strip())
    refs = [it["ref"] for it in items]
    if not refs:
        return {"n": 0}
    return {"n": len(refs), "exact": sum(h == r for h, r in zip(hyps, refs)) / len(refs),
            "chrf3": sum(char_f(h, r) for h, r in zip(hyps, refs)) / len(refs)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="翻譯 LoRA 微調（中文→英文／日文）")
    ap.add_argument("--config", default=str(HERE / "configs" / "mt_qwen_lora.yaml"))
    ap.add_argument("--data", required=True, help="匯出資料夾裡的 mt/")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-steps", type=int, default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.dry_run:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import torch
    from transformers import Trainer, TrainingArguments
    tr = cfg["train"]
    torch.manual_seed(tr.get("seed", 42))
    t0 = time.time()
    lim = cfg.get("dry_run", {}).get("max_pairs", 8) if args.dry_run else None
    pairs = cfg.get("pairs", ["zh-en", "zh-ja"])
    rows = {s: sum((read_chat(args.data, p, s, lim) for p in pairs), []) for s in ("train", "validation", "test")}
    if not rows["train"]:
        print("沒有 MT 訓練配對（mt/zh-en.train.jsonl）", file=sys.stderr)
        return 2
    model, tok, is_unsloth = build(cfg, args.dry_run)
    max_len = 128 if args.dry_run else cfg["max_seq_len"]
    data = {s: encode(r, tok, max_len) for s, r in rows.items()}
    max_new = 8 if args.dry_run else 256
    test = data["test"] or data["validation"]
    before = evaluate(model, test, tok, max_new)
    if not is_unsloth:
        from peft import LoraConfig, get_peft_model
        lc = cfg["lora"]
        model = get_peft_model(model, LoraConfig(r=lc["r"], lora_alpha=lc["alpha"], lora_dropout=lc["dropout"],
                                                 target_modules=lc["target_modules"], bias="none", task_type="CAUSAL_LM"))
    steps = args.max_steps or (cfg.get("dry_run", {}).get("steps", 2) if args.dry_run else -1)
    targs = training_args(TrainingArguments, output_dir=str(Path(args.out) / "checkpoints"),
                              per_device_train_batch_size=2 if args.dry_run else tr["batch_size"],
                              gradient_accumulation_steps=1 if args.dry_run else tr["grad_accum"],
                              learning_rate=tr["learning_rate"], num_train_epochs=tr["epochs"], max_steps=steps,
                              warmup_ratio=0 if args.dry_run else tr["warmup_ratio"],
                              bf16=bool(tr.get("bf16")) and torch.cuda.is_available() and torch.cuda.is_bf16_supported(),
                              logging_steps=1 if args.dry_run else 20, save_strategy="no" if args.dry_run else "epoch",
                              report_to=[], remove_unused_columns=False, use_cpu=args.dry_run,
                              dataloader_pin_memory=False, seed=tr.get("seed", 42))
    trainer = Trainer(model=model, args=targs, train_dataset=data["train"],
                      data_collator=lambda b: collate(b, tok.pad_token_id))
    res = trainer.train()
    after = evaluate(model, test, tok, max_new)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out / "adapter")
    if not args.dry_run:
        if is_unsloth:
            model.save_pretrained_merged(str(out / "merged"), tok.t, save_method="merged_16bit")
        else:
            model.merge_and_unload().save_pretrained(out / "merged")
            tok.t.save_pretrained(out / "merged")
    rep = {"base_model": "dry-run-tiny-qwen2" if args.dry_run else cfg["base_model"], "dry_run": args.dry_run,
           "unsloth": is_unsloth, "pairs": {s: len(v) for s, v in data.items()}, "before": before, "after": after,
           "train_loss": res.training_loss, "steps": res.global_step, "seconds": round(time.time() - t0, 1)}
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
