"""round4 #7: optional Japanese target + kana reading on room glossary terms.

Wraps app.glossary.validate_terms from outside (glossary.py is Codex-owned and unchanged):
``ja`` / ``reading`` are taken off each item, the rest is validated by the existing rules, and the
checked Japanese fields are put back on the accepted term with the same ``zh``. The reading drives
the first-occurrence ruby on ja captions (pipeline.ja_ruby); ``ja`` is the target for the ja MT
glossary prompt (mt_backend).
"""
from __future__ import annotations

import re

from app.glossary import validate_terms

MAX_JA = 40
JA_FIELDS = ("ja", "reading")
_KANA = re.compile(r"[\u3041-\u309f\u30a0-\u30ff\u30fc\u30fb\s]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LABEL = {"ja": "日文", "reading": "讀音"}


def _ja_fields(item) -> tuple[dict, list[str]]:
    extra: dict = {}
    problems: list[str] = []
    if not isinstance(item, dict):
        return extra, problems
    for key in JA_FIELDS:
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str) or _CONTROL.search(raw):
            problems.append(f"{_LABEL[key]}必須是單行文字")
            continue
        value = raw.strip()
        if len(value) > MAX_JA:
            problems.append(f"{_LABEL[key]}超過 {MAX_JA} 字")
        elif key == "reading" and not _KANA.fullmatch(value):
            problems.append("讀音只能是平假名或片假名")
        elif value:
            extra[key] = value
    if "reading" in extra and "ja" not in extra:
        problems.append("有讀音時必須同時填日文")
    return extra, problems


def validate_terms_ja(raw) -> tuple[list[dict], list[dict]]:
    """Same contract as glossary.validate_terms, plus optional ja/reading per term."""
    if not isinstance(raw, list):
        return validate_terms(raw)
    stripped, extras, ja_rejected = [], {}, []
    for line, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            extra, problems = _ja_fields(item)
            ja_rejected += [{"line": line, "reason": p} for p in problems]
            if problems:
                continue                           # a bad ja field rejects the whole term
            zh = item.get("zh")
            if extra and isinstance(zh, str):
                extras[zh.strip()] = extra
            item = {k: v for k, v in item.items() if k not in JA_FIELDS}
        stripped.append(item)
    accepted, rejected = validate_terms(stripped)
    if ja_rejected:
        return [], ja_rejected + rejected           # all-or-nothing, like the base validator
    for term in accepted:
        term.update(extras.get(term.get("zh"), {}))
    return accepted, rejected
