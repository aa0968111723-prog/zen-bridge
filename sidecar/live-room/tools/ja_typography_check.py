#!/usr/bin/env python3
"""Offline Japanese projector-caption checks (stdlib only).

Usage: python tools/ja_typography_check.py --db captions.sqlite3
       python tools/ja_typography_check.py --text captions.txt [--max-findings N]

CaptionStore's legacy target column is named en and has no language tag:
ready rows are treated as Japanese candidates, not identified as Japanese.
Ledger input selects current, successful ja translations of translated segments.
Text input is UTF-8, one caption per line. .jsonl/.ndjson files contain JSON
strings or objects with a text string and optional string/integer id.
Embedded newlines in JSON/database captions are checked as separate display lines.

Chinese residue is advisory, not language detection: only the small simplified
glyph list 这们说为对汉语吗 is flagged (Japanese shares most Han characters).
The full-width Chinese comma ， is flagged only between adjacent Han/kana
characters; numeric commas, Latin contexts, shared/traditional kanji and proper
names are not inferred to be untranslated Chinese.
ASCII punctuation inside Latin/numeric tokens (including U.S.) is ignored.
Widths use app.ja_linebreak's ruby-aware helper: {base|reading} counts as base.
Counts include all occurrences; --max-findings limits only the findings list.
Use a stable offline database; WAL without SHM must be checkpointed first.
Exit codes: 0 clean, 1 findings, 2 invalid input (diagnostic on stderr).
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ja_linebreak import _width


RULES = (
    "ascii_punctuation", "unbalanced_brackets", "chinese_residue",
    "doubled_punctuation", "kinsoku_line_start", "line_too_long",
)
CHINESE_GLYPHS = frozenset("这们说为对汉语吗")
BRACKETS = {"「": "」", "『": "』", "（": "）"}
RUBY = re.compile(r"\{([^{}|]+)\|[^{}|]+\}")
LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[,.?!:;][A-Za-z0-9]+)*")
ACRONYM = re.compile(r"(?:[A-Za-z]\.){2,}")


def _japanese(char: str) -> bool:
    name = unicodedata.name(char, "")
    return any(part in name for part in ("CJK UNIFIED", "CJK COMPATIBILITY", "HIRAGANA", "KATAKANA"))


def check_line(text: str) -> list[tuple[str, int]]:
    """Return (rule, offset) occurrences in visible text, not ruby readings."""
    text = RUBY.sub(lambda match: match.group(1), text)
    found = []
    if any(_japanese(char) for char in text):
        protected = set()
        for pattern in (LATIN_TOKEN, ACRONYM):
            for match in pattern.finditer(text):
                protected.update(range(match.start(), match.end()))
        for index, char in enumerate(text):
            if char in ",.?!:;" and index not in protected:
                found.append(("ascii_punctuation", index))
    stack = []
    for index, char in enumerate(text):
        if char in BRACKETS:
            stack.append((char, index))
        elif char in BRACKETS.values():
            if stack and BRACKETS[stack[-1][0]] == char:
                stack.pop()
            else:
                found.append(("unbalanced_brackets", index))
        if char in CHINESE_GLYPHS or (
            char == "，" and 0 < index < len(text) - 1
            and _japanese(text[index - 1]) and _japanese(text[index + 1])
        ):
            found.append(("chinese_residue", index))
    found.extend(("unbalanced_brackets", index) for _, index in stack)
    found.extend(("doubled_punctuation", match.start())
                 for match in re.finditer(r"(?=(。。|、、))", text))
    stripped = text.lstrip()
    if stripped.startswith(("、", "。")):
        found.append(("kinsoku_line_start", len(text) - len(stripped)))
    if _width(text) > 16:
        found.append(("line_too_long", 0))
    return sorted(found, key=lambda item: (item[1], item[0]))


def _database(path: Path) -> list[tuple[str, str]]:
    path = path.resolve()
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise ValueError("WAL without SHM: checkpoint the database before offline analysis")
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "captions" in tables:
            query = """
                SELECT id, en FROM captions WHERE status='ready'
                ORDER BY room_id, session_id, seq, id
            """
        elif {"segments", "translations"} <= tables:
            query = """
                SELECT s.id, t.text FROM segments s
                JOIN translations t ON t.segment_id=s.id
                WHERE s.status='translated' AND t.tgt_lang='ja'
                      AND t.is_current=1 AND t.status IN ('ok', 'merged')
                ORDER BY s.room_id, s.session_id, s.seq, s.id
            """
        else:
            raise ValueError("unsupported captions database schema")
        rows = list(conn.execute(query))
    for caption_id, text in rows:
        if text is not None and not isinstance(text, str):
            raise ValueError(f"caption {caption_id}: text must be a string")
    return [(caption_id, text or "") for caption_id, text in rows]


def _text(path: Path) -> list[tuple[str | int, str, int]]:
    rows = []
    is_jsonl = path.suffix.lower() in (".jsonl", ".ndjson")
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            text = line.rstrip("\r\n")
            caption_id = number
            if is_jsonl:
                if not text.strip():
                    continue
                value = json.loads(text)
                if isinstance(value, dict):
                    caption_id = value.get("id", number)
                    text = value.get("text")
                else:
                    text = value
                if not isinstance(text, str):
                    raise ValueError(f"line {number}: expected a JSON string or object with text")
                if isinstance(caption_id, bool) or not isinstance(caption_id, (str, int)):
                    raise ValueError(f"line {number}: id must be a string or integer")
            rows.append((caption_id, text, number))
    return rows


def analyze(*, db: Path | None = None, text: Path | None = None,
            max_findings: int | None = None) -> dict:
    if (db is None) == (text is None):
        raise ValueError("provide exactly one of --db or --text")
    if max_findings is not None and max_findings < 0:
        raise ValueError("--max-findings must be non-negative")
    rows = (_text(Path(text)) if text is not None else
            [(caption_id, value, None) for caption_id, value in _database(Path(db))])
    counts = dict.fromkeys(RULES, 0)
    findings = []
    lines_checked = 0
    for caption_id, value, source_line in rows:
        for number, line in enumerate(value.splitlines(), 1):
            lines_checked += 1
            visible = RUBY.sub(lambda match: match.group(1), line)
            for rule, offset in check_line(line):
                counts[rule] += 1
                if max_findings is None or len(findings) < max_findings:
                    finding = {
                        "id": caption_id, "line": number if source_line is None else source_line,
                        "rule": rule, "snippet": visible[max(0, offset - 20):offset + 40],
                    }
                    if source_line is not None:
                        finding["caption_line"] = number
                    findings.append(finding)
    total = sum(counts.values())
    return {"lines_checked": lines_checked, "counts": counts,
            "finding_count": total, "truncated": len(findings) < total,
            "findings": findings}


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path)
    source.add_argument("--text", type=Path)
    parser.add_argument("--max-findings", type=int)
    args = parser.parse_args(argv)
    try:
        report = analyze(db=args.db, text=args.text, max_findings=args.max_findings)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"ja_typography_check: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["finding_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
