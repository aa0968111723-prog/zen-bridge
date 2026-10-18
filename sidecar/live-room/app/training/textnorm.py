"""文字正規化與 CER（字元錯誤率）。"""
from __future__ import annotations

import re
import unicodedata

_DROP = re.compile(r"[\s\u3000，。、！？；：「」『』（）《》〈〉【】…—\-–,.!?;:'\"()\[\]<>~·・]+")


def norm(text: str | None) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return _DROP.sub("", t)


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(hyp: str | None, ref: str | None) -> float | None:
    """Character error rate of hyp against ref (both normalised). None when ref is empty."""
    r, h = norm(ref), norm(hyp)
    if not r:
        return None
    return edit_distance(h, r) / len(r)


def corpus_cer(pairs) -> float | None:
    errs = chars = 0
    for hyp, ref in pairs:
        r = norm(ref)
        if r:
            errs += edit_distance(norm(hyp), r)
            chars += len(r)
    return errs / chars if chars else None
