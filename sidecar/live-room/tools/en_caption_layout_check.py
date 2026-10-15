#!/usr/bin/env python3
"""Offline English caption layout check for projector subtitles; stdlib only.

  python tools/en_caption_layout_check.py --db captions.sqlite3 [--session ID] [--json OUT]
  python tools/en_caption_layout_check.py --jsonl captions.jsonl [--json OUT]

Input (read-only, exactly one of):
* --db     CaptionStore captions.sqlite3 (app/store.py): rows of table captions
           with status ready, text column en. That legacy target column has no
           language tag, so rows containing kana are treated as Japanese and
           skipped (counted as skipped_non_english), never flagged as residue.
           The zen.sqlite3 ledger (app/admin/schema.sql) is also accepted: the
           current, successful (ok / merged) tgt_lang='en' translations of
           translated segments.
           Opened with a sqlite URI mode=ro (plus immutable=1 when no WAL file
           exists), so neither the database nor its sidecars are created or changed.
* --jsonl  UTF-8 JSON Lines; each line is a JSON string or an object with a
           text (or en) string and optional id, room_id, session_id, seq, lang.
           Objects whose lang does not start with "en" are skipped.

Checks per caption (codes):
  empty                       nothing to show
  line_too_long               a display line wider than --max-line-chars (42),
                              or a single unbreakable token wider than that
  too_many_lines              more than --max-lines (2) display lines
  residual_cjk                Han / kana / hangul / full-width CJK punctuation left over
  punctuation_spacing         space before , . ; : ! ?, missing space after them,
                              or padding inside brackets ( like this )
  extra_whitespace            leading/trailing or doubled spaces inside a line
  unbalanced_brackets         () [] {} “” ‘’ unmatched, or an odd number of "
  doubled_punctuation         ,, ;; :: .. ,. .,  (an ellipsis ... is fine)
  missing_final_punctuation   caption does not end in . ! ? or an ellipsis
                              (closing quotes/brackets after it are allowed)

Layout: when the caption already contains line breaks they are used as the
display lines; otherwise it is greedily word-wrapped at --max-line-chars.
Widths count one per character, two for East Asian wide/full-width characters
and zero for combining marks. --skip RULE disables a check (repeatable), e.g.
--skip missing_final_punctuation when segments are cut mid-sentence.

Report JSON goes to stdout (Traditional Chinese messages); --json also saves it
to a NEW file and never overwrites an existing one. Exit codes: 0 issue rate
within --max-issue-rate, 1 issue rate above it, 2 unreadable input or bad options.
The tool never imports the application, never uses the network and never
opens a listening port.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata


TOOL = "en_caption_layout_check"
ISSUE_CODES = (
    "empty", "line_too_long", "too_many_lines", "residual_cjk",
    "punctuation_spacing", "extra_whitespace", "unbalanced_brackets",
    "doubled_punctuation", "missing_final_punctuation",
)
DEFAULTS = {
    "max_line_chars": 42,
    "max_lines": 2,
    "max_issue_rate": 0.2,
}
CAPTION_FINAL_STATUSES = ("ready",)
LEDGER_TRANSLATION_STATUSES = ("ok", "merged")

BRACKETS = {"(": ")", "[": "]", "{": "}", "“": "”", "‘": "’"}
CLOSERS = {close: open_ for open_, close in BRACKETS.items()}
# Full-width / CJK punctuation that should never survive into English captions.
CJK_PUNCT = set("，。、；：！？「」『』（）【】《》〈〉〔〕　～")  # … and · are fine in English
FINAL_MARKS = (".", "!", "?", "…")
TRAILING_CLOSERS = set(")]}\"'”’")

SPACE_BEFORE = re.compile(r"(?<=\S)[ \t]+(?:[,;:!?]|\.(?![.\d]))")
MISSING_AFTER = re.compile(r"[,;!?](?=[A-Za-z])|(?<=[a-z]{2})\.(?=[A-Z][a-z])|(?<=[A-Za-z]):(?=[A-Za-z])")
INNER_PAD = re.compile(r"[(\[{][ \t]+|[ \t]+[)\]}]")
DOUBLED = re.compile(r"(?<!\.)\.\.(?!\.)|,,|;;|::|,\.|\.,")
KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def is_cjk(ch: str) -> bool:
    if ch in CJK_PUNCT:
        return True
    name = unicodedata.name(ch, "")
    return any(part in name for part in ("CJK UNIFIED", "CJK COMPATIBILITY", "HIRAGANA",
                                         "KATAKANA", "HANGUL", "BOPOMOFO", "FULLWIDTH"))


def wrap(text: str, width: int) -> tuple[list[str], list[str]]:
    """Greedy word wrap. Returns (lines, unbreakable tokens wider than width)."""
    lines: list[str] = []
    long_tokens: list[str] = []
    current = ""
    for word in text.split():
        if text_width(word) > width:
            long_tokens.append(word)
        if not current:
            current = word
        elif text_width(current) + 1 + text_width(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines, long_tokens


def display_lines(text: str, width: int) -> tuple[list[str], list[str], bool]:
    """Return (display lines, too-wide tokens, explicit) for one caption."""
    if "\n" in text or "\r" in text:
        lines = [line for line in re.split(r"\r\n|\r|\n", text)]
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        return lines, [], True
    lines, long_tokens = wrap(text, width)
    return lines, long_tokens, False


def unbalanced_brackets(text: str) -> list[str]:
    """Unmatched bracket/quote characters in order; a bare ’ is an apostrophe."""
    stack: list[str] = []
    stray: list[str] = []
    for ch in text:
        if ch in BRACKETS:
            stack.append(ch)
        elif ch in CLOSERS:
            if stack and stack[-1] == CLOSERS[ch]:
                stack.pop()
            elif ch != "’":
                stray.append(ch)
    if text.count('"') % 2:
        stray.append('"')
    return stray + stack


def has_final_punctuation(text: str) -> bool:
    body = text.rstrip()
    while body and body[-1] in TRAILING_CLOSERS:
        body = body[:-1].rstrip()
    return body.endswith(FINAL_MARKS)


def _thresholds(overrides: dict | None) -> dict:
    values = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown threshold {key}")
        if value is not None:
            values[key] = value
    out = {}
    for key in ("max_line_chars", "max_lines"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 1:
            raise ValueError(f"{key} must be a positive integer")
        out[key] = int(value)
    rate = values["max_issue_rate"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ValueError("max_issue_rate must be a number")
    rate = float(rate)
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError("max_issue_rate must be between 0 and 1")
    out["max_issue_rate"] = rate
    return out


def _skip_set(skip) -> frozenset:
    names = frozenset(name.strip() for name in (skip or ()) if name and name.strip())
    unknown = sorted(names - set(ISSUE_CODES))
    if unknown:
        raise ValueError(f"unknown rule(s) for --skip: {', '.join(unknown)}")
    return names


def check_caption(text: str, limits: dict | None = None, skip=()) -> tuple[list[dict], dict]:
    """Return (issues, metrics) for one English caption."""
    limits = _thresholds(limits)
    skipped = skip if isinstance(skip, frozenset) else _skip_set(skip)
    width = limits["max_line_chars"]
    issues: list[dict] = []

    def add(code: str, message: str, **extra) -> None:
        if code not in skipped:
            issues.append({"code": code, "message_zh": message, **extra})

    lines, long_tokens, explicit = display_lines(text, width)
    metrics = {
        "lines": len(lines),
        "longest_line": max((text_width(line.strip()) for line in lines), default=0),
        "explicit_breaks": explicit,
        "wrapped": lines,
    }
    if not text.strip():
        add("empty", "空白字幕：沒有任何英文文字。")
        return issues, metrics

    too_wide = [line.strip() for line in lines if text_width(line.strip()) > width]
    if long_tokens:
        add("line_too_long", f"有無法斷行的字詞超過每行 {width} 字元：{'、'.join(long_tokens[:3])}。",
            tokens=long_tokens[:5])
    elif too_wide:
        add("line_too_long", f"有 {len(too_wide)} 行超過每行 {width} 字元（最長 {metrics['longest_line']}）。")
    if len(lines) > limits["max_lines"]:
        add("too_many_lines",
            f"需要 {len(lines)} 行，超過上限 {limits['max_lines']} 行（每行 {width} 字元），建議切成兩則字幕。")

    residue = []
    for ch in text:
        if is_cjk(ch) and ch not in residue:
            residue.append(ch)
    if residue:
        add("residual_cjk", f"殘留中日韓文字或全形標點：{''.join(residue[:10])}。", chars=residue[:10])

    spacing = []
    for line in lines:
        for pattern, label in ((SPACE_BEFORE, "標點前多空白"), (MISSING_AFTER, "標點後缺空白"),
                               (INNER_PAD, "括號內側多空白")):
            for match in pattern.finditer(line):
                start = max(0, match.start() - 8)
                spacing.append(f"{label}「{line[start:match.end() + 8]}」")
    if spacing:
        add("punctuation_spacing", f"標點空白問題：{'；'.join(spacing[:3])}。", count=len(spacing))

    whitespace = 0
    for line in lines:
        if line.strip() and (line != line.strip() or re.search(r"\S[ \t]{2,}\S", line) or "\t" in line.strip()):
            whitespace += 1
    if not explicit and (text != text.strip() or re.search(r"\S[ \t]{2,}\S", text)):
        whitespace = max(whitespace, 1)
    if whitespace:
        add("extra_whitespace", "開頭、結尾或字詞之間有多餘空白。")

    stray = unbalanced_brackets(text)
    if stray:
        add("unbalanced_brackets", f"括號或引號不成對：{'、'.join(stray)}。", chars=stray)

    doubled = [match.group() for match in DOUBLED.finditer(text)]
    if doubled:
        add("doubled_punctuation", f"重複或衝突的標點：{'、'.join(doubled[:3])}。")

    if not has_final_punctuation(text):
        add("missing_final_punctuation", "句末沒有 . ! ? 或刪節號。")
    return issues, metrics


def _sqlite_uri(path: Path) -> str:
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise ValueError("WAL without SHM: checkpoint the database before offline analysis")
    return uri


def _read_db(db: Path, session: str | None) -> tuple[str, list[dict], int]:
    path = Path(db).resolve()
    if not path.is_file():
        raise ValueError(f"database not found: {path}")
    with closing(sqlite3.connect(_sqlite_uri(path), uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "captions" in tables:
            schema = "captions"
            marks = ",".join("?" * len(CAPTION_FINAL_STATUSES))
            query = f"""
                SELECT id, room_id, session_id, seq, en AS text
                FROM captions WHERE status IN ({marks})
            """
            params: tuple = CAPTION_FINAL_STATUSES
            if session is not None:
                query += " AND session_id=?"
                params += (session,)
            query += " ORDER BY room_id, session_id, seq, id"
        elif {"segments", "translations"} <= tables:
            schema = "ledger"
            marks = ",".join("?" * len(LEDGER_TRANSLATION_STATUSES))
            query = f"""
                SELECT s.id, s.room_id, s.session_id, s.seq, t.text
                FROM segments s
                JOIN translations t ON t.segment_id=s.id
                WHERE s.status='translated' AND t.tgt_lang='en' AND t.is_current=1
                      AND t.status IN ({marks})
            """
            params = LEDGER_TRANSLATION_STATUSES
            if session is not None:
                query += " AND s.session_id=?"
                params += (session,)
            query += " ORDER BY s.room_id, s.session_id, s.seq, s.id"
        else:
            raise ValueError("unsupported captions database schema (expected captions or ledger tables)")
        rows = [dict(row) for row in conn.execute(query, params)]
    kept = []
    skipped = 0
    for row in rows:
        if row["text"] is not None and not isinstance(row["text"], str):
            raise ValueError(f"caption {row['id']}: text must be a string")
        row["text"] = row["text"] or ""
        if schema == "captions" and KANA.search(row["text"]):
            skipped += 1   # untagged legacy column: a Japanese session, not English residue
            continue
        kept.append(row)
    return schema, kept, skipped


def _read_jsonl(path: Path, session: str | None) -> tuple[str, list[dict], int]:
    rows = []
    skipped = 0
    with Path(path).open(encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {number}: invalid JSON ({exc.msg})") from None
            row = {"id": number, "room_id": None, "session_id": None, "seq": None}
            if isinstance(value, str):
                row["text"] = value
            elif isinstance(value, dict):
                text = value.get("text", value.get("en"))
                if not isinstance(text, str):
                    raise ValueError(f"line {number}: expected a text (or en) string")
                row["text"] = text
                for key in ("id", "room_id", "session_id", "seq"):
                    if value.get(key) is not None:
                        if isinstance(value[key], bool) or not isinstance(value[key], (str, int)):
                            raise ValueError(f"line {number}: {key} must be a string or integer")
                        row[key] = value[key]
                lang = value.get("lang")
                if lang is not None:
                    if not isinstance(lang, str):
                        raise ValueError(f"line {number}: lang must be a string")
                    if not lang.lower().startswith("en"):
                        if session is None or row["session_id"] == session:
                            skipped += 1
                        continue
            else:
                raise ValueError(f"line {number}: expected a JSON string or object")
            if session is not None and row["session_id"] != session:
                continue
            rows.append(row)
    return "jsonl", rows, skipped


def analyze(*, db: Path | None = None, jsonl: Path | None = None,
            session: str | None = None, thresholds: dict | None = None,
            skip=(), max_findings: int | None = None) -> dict:
    if (db is None) == (jsonl is None):
        raise ValueError("provide exactly one of --db or --jsonl")
    if max_findings is not None and max_findings < 0:
        raise ValueError("--max-findings must be non-negative")
    limits = _thresholds(thresholds)
    skipped_rules = _skip_set(skip)
    schema, rows, skipped = (_read_db(Path(db), session) if db is not None
                             else _read_jsonl(Path(jsonl), session))
    counts = dict.fromkeys(ISSUE_CODES, 0)
    flagged = []
    for row in rows:
        issues, metrics = check_caption(row["text"], limits, skipped_rules)
        for issue in issues:
            counts[issue["code"]] += 1
        if issues:
            flagged.append({"id": row["id"], "session_id": row["session_id"], "seq": row["seq"],
                            "lines": metrics["lines"], "longest_line": metrics["longest_line"],
                            "wrapped": metrics["wrapped"][:limits["max_lines"] + 2],
                            "text": row["text"][:160], "issues": issues})
    total = len(rows)
    rate = len(flagged) / total if total else 0.0
    within = rate <= limits["max_issue_rate"]
    if not total:
        verdict = "沒有可檢查的英文字幕。"
    elif within:
        verdict = f"問題比例 {rate:.1%}，在門檻 {limits['max_issue_rate']:.0%} 以內。"
    else:
        verdict = f"問題比例 {rate:.1%}，超過門檻 {limits['max_issue_rate']:.0%}，請檢查翻譯斷句與排版。"
    return {
        "tool": TOOL,
        "schema": schema,
        "session": session,
        "thresholds": limits,
        "skipped_rules": sorted(skipped_rules),
        "summary": {
            "captions": total,
            "captions_with_issues": len(flagged),
            "issue_rate": round(rate, 4),
            "within_threshold": within,
            "skipped_non_english": skipped,
            "counts": counts,
            "verdict_zh": verdict,
        },
        "truncated": max_findings is not None and len(flagged) > max_findings,
        "captions": flagged if max_findings is None else flagged[:max_findings],
    }


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="captions.sqlite3 or zen.sqlite3 (read-only)")
    source.add_argument("--jsonl", type=Path, help="JSON Lines of English captions")
    parser.add_argument("--session", help="only check this session_id")
    parser.add_argument("--max-line-chars", type=int, default=DEFAULTS["max_line_chars"],
                        help="display width per line (default 42)")
    parser.add_argument("--max-lines", type=int, default=DEFAULTS["max_lines"],
                        help="display lines per caption (default 2)")
    parser.add_argument("--max-issue-rate", type=float, default=DEFAULTS["max_issue_rate"],
                        help="share of captions with issues allowed before exit 1 (0-1)")
    parser.add_argument("--skip", action="append", default=[], metavar="RULE", choices=ISSUE_CODES,
                        help="disable one check (repeatable)")
    parser.add_argument("--max-findings", type=int, help="limit the per-caption list (counts stay complete)")
    parser.add_argument("--json", type=Path, metavar="OUT", help="also save JSON to a NEW file")
    args = parser.parse_args(argv)
    label = args.db if args.db is not None else args.jsonl
    try:
        if args.json is not None:
            if args.db is not None:
                db = args.db.resolve()
                protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
            else:
                protected = {args.jsonl.resolve()}
            if args.json.resolve() in protected:
                raise ValueError("--json must not refer to the input or its SQLite sidecars")
            if args.json.exists() or args.json.is_symlink():
                raise ValueError(f"refusing to overwrite existing file {args.json}")
        report = analyze(db=args.db, jsonl=args.jsonl, session=args.session,
                         thresholds={"max_line_chars": args.max_line_chars,
                                     "max_lines": args.max_lines,
                                     "max_issue_rate": args.max_issue_rate},
                         skip=tuple(args.skip), max_findings=args.max_findings)
        payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.json is not None:
            with args.json.open("x", encoding="utf-8") as output:
                output.write(payload)
    except (OSError, sqlite3.Error, ValueError, UnicodeDecodeError) as exc:
        print(f"{TOOL}: cannot analyze {label}: {exc}", file=sys.stderr)
        return 2
    print(payload, end="")
    return 0 if report["summary"]["within_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
