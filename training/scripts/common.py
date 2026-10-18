"""Shared helpers for training scripts: config loading, dataset reading, CER. Standard library only."""
from __future__ import annotations

import json
import re
import unicodedata
import wave
from array import array
from pathlib import Path

SPLITS = ("train", "validation", "test")


def _scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v in ("true", "false"):
        return v == "true"
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v.strip("'\"")


def load_config(path) -> dict:
    """PyYAML when installed; otherwise a small parser for the flat/nested mappings in configs/."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        pass
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        ind = len(line) - len(line.lstrip())
        k, _, v = line.strip().partition(":")
        while stack[-1][0] >= ind:
            stack.pop()
        cur = stack[-1][1]
        if v.strip():
            cur[k] = _scalar(v)
        else:
            cur[k] = {}
            stack.append((ind, cur[k]))
    return root


def read_split(data_dir, split: str, limit: int | None = None) -> list[dict]:
    """Rows of asr/<split>/metadata.jsonl with an absolute 'path'."""
    d = Path(data_dir) / split
    meta = d / "metadata.jsonl"
    if not meta.exists():
        return []
    rows = []
    for line in meta.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        r["path"] = str(d / r["file_name"])
        rows.append(r)
        if limit and len(rows) >= limit:
            break
    return rows


def load_wav(path):
    """16 kHz mono PCM16 -> list[float] in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        pcm = array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    return [x / 32768.0 for x in pcm]


def read_chat(mt_dir, pair: str, split: str, limit: int | None = None) -> list[dict]:
    p = Path(mt_dir) / f"{pair}.{split}.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[:limit] if limit else rows


_DROP = re.compile(r"[\s\u3000，。、！？；：「」『』（）《》〈〉【】…—\-–,.!?;:'\"()\[\]<>~·・]+")


def norm(t: str) -> str:
    return _DROP.sub("", unicodedata.normalize("NFKC", t or "").lower())


def _ed(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def corpus_cer(hyps, refs) -> float | None:
    e = n = 0
    for h, r in zip(hyps, refs):
        r = norm(r)
        if r:
            e += _ed(norm(h), r)
            n += len(r)
    return e / n if n else None


class ByteTokenizer:
    """Offline tokenizer for --dry-run only (no download): ids = utf-8 bytes + 4 specials."""
    pad_token_id, bos_token_id, eos_token_id, sot_id = 0, 1, 2, 3
    offset = 4
    vocab_size = 256 + 4

    def encode(self, text: str) -> list[int]:
        return [b + self.offset for b in text.encode("utf-8")]

    def decode(self, ids) -> str:
        return bytes(i - self.offset for i in ids if i >= self.offset).decode("utf-8", errors="ignore")


def training_args(cls, **kw):
    """Build (Seq2Seq)TrainingArguments across transformers 4.x/5.x: unknown keys are dropped, and
    warmup_ratio becomes a fractional warmup_steps where the ratio field no longer exists."""
    import dataclasses
    names = {f.name for f in dataclasses.fields(cls)}
    if "warmup_ratio" in kw and "warmup_ratio" not in names:
        r = kw.pop("warmup_ratio")
        if r:
            kw.setdefault("warmup_steps", r)
    if "eval_strategy" in kw and "eval_strategy" not in names and "evaluation_strategy" in names:
        kw["evaluation_strategy"] = kw.pop("eval_strategy")
    if "use_cpu" in kw and "use_cpu" not in names:
        kw.pop("use_cpu")
    return cls(**{k: v for k, v in kw.items() if k in names})
