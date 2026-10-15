#!/usr/bin/env python3
"""Offline segmentation-quality check for final Chinese captions; stdlib only.

  python tools/zh_segment_quality_check.py --db captions.sqlite3 [--session ID] [--json OUT]
  python tools/zh_segment_quality_check.py --jsonl segments.jsonl [--json OUT]

Input (read-only, exactly one of):
* --db     CaptionStore captions.sqlite3 (app/store.py): rows of table captions
           with status ready / zh_ready / translate_failed, text column zh.
           The zen.sqlite3 ledger (app/admin/schema.sql) is also accepted: the
           current transcripts.text of segments with status asr_done/translated.
           Opened with a sqlite URI mode=ro (plus immutable=1 when no WAL file
           exists), so neither the database nor its sidecars are created or changed.
* --jsonl  UTF-8 JSON Lines; each line is a JSON string or an object with a
           text (or zh) string and optional id, session_id, seq, t0_ms, t1_ms.

Checks per segment (codes): empty, edge_whitespace, too_short, too_long,
duration_too_short, duration_too_long, dangling_connective, unbalanced_brackets.
Character counts use letters and digits only (CJK characters, kana, Latin
letters, numbers); punctuation and whitespace are not counted. Durations come
from t0_ms/t1_ms audio offsets; missing or non-positive spans skip the
duration checks and are counted as duration_unknown. Single-character
connectives (那、而、但…) are flagged only after punctuation or as the whole
segment, so 「放在那」 is not flagged.

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
import sqlite3
import sys
import unicodedata


TOOL = "zh_segment_quality_check"
ISSUE_CODES = (
    "empty", "edge_whitespace", "too_short", "too_long",
    "duration_too_short", "duration_too_long", "dangling_connective",
    "unbalanced_brackets",
)
CAPTION_FINAL_STATUSES = ("ready", "zh_ready", "translate_failed")
LEDGER_FINAL_STATUSES = ("asr_done", "translated")

DEFAULTS = {
    "min_chars": 2,
    "max_chars": 40,
    "min_duration_s": 0.4,
    "max_duration_s": 15.0,
    "min_cps": 1.0,
    "max_cps": 9.0,
    "max_issue_rate": 0.2,
}

# Multi-character connectives match at the very end of the segment.
CONNECTIVES = (
    "然後", "但是", "可是", "不過", "所以", "因此", "因為", "而且", "並且",
    "還有", "就是", "那麼", "如果", "假如", "要是", "雖然", "或者", "還是",
    "接著", "於是", "然而", "甚至", "以及", "另外", "而是", "不然", "否則",
    "比如", "例如", "像是", "尤其是", "除了", "只要", "只是", "然后", "因为",
    "还有", "那么", "虽然", "于是", "并且", "还是", "接着",
)
# Single-character connectives are ambiguous (就在那), so they count only when
# they stand alone after punctuation/whitespace or form the whole segment.
SINGLE_CONNECTIVES = ("那", "而", "但", "跟", "和", "與", "或", "且", "及", "与")
# Trailing characters ignored when looking for a dangling connective.
SOFT_TAIL = set(" \t\r\n\u3000，,、；;：:…‥—–-~～·。.！!？?")
BRACKETS = {"（": "）", "(": ")", "「": "」", "『": "』", "《": "》",
            "【": "】", "〈": "〉", "“": "”", "‘": "’"}
CLOSERS = {close: open_ for open_, close in BRACKETS.items()}


def char_count(text: str) -> int:
    """Count letters and digits (CJK, kana, Latin, numbers); skip punctuation/space."""
    return sum(1 for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def dangling_connective(text: str, extra: tuple[str, ...] = ()) -> str | None:
    body = text.rstrip()
    while body and body[-1] in SOFT_TAIL:
        body = body[:-1]
    for word in sorted(set(CONNECTIVES) | set(extra), key=len, reverse=True):
        if word and body.endswith(word):
            return word
    for word in SINGLE_CONNECTIVES:
        if body.endswith(word):
            before = body[:-len(word)]
            if not before or unicodedata.category(before[-1])[0] not in ("L", "N"):
                return word
    return None


def unbalanced_brackets(text: str) -> list[str]:
    """Return the unmatched bracket/quote characters in order of appearance."""
    stack: list[str] = []
    stray: list[str] = []
    for ch in text:
        if ch in BRACKETS:
            stack.append(ch)
        elif ch in CLOSERS:
            if stack and stack[-1] == CLOSERS[ch]:
                stack.pop()
            else:
                stray.append(ch)
    if text.count('"') % 2:
        stray.append('"')
    return stray + stack


def _number(name: str, value, *, allow_zero: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{name} must be finite and positive")
    return number


def _thresholds(overrides: dict | None) -> dict:
    values = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown threshold {key}")
        if value is not None:
            values[key] = value
    out = {}
    for key, value in values.items():
        if key in ("min_chars", "max_chars"):
            if isinstance(value, bool) or int(value) != value or value < 1:
                raise ValueError(f"{key} must be a positive integer")
            out[key] = int(value)
        elif key == "max_issue_rate":
            rate = _number(key, value, allow_zero=True)
            if rate > 1:
                raise ValueError("max_issue_rate must be between 0 and 1")
            out[key] = rate
        else:
            out[key] = _number(key, value)
    if out["min_chars"] > out["max_chars"]:
        raise ValueError("min_chars must not exceed max_chars")
    if out["min_duration_s"] > out["max_duration_s"]:
        raise ValueError("min_duration_s must not exceed max_duration_s")
    if out["min_cps"] > out["max_cps"]:
        raise ValueError("min_cps must not exceed max_cps")
    return out


def _read_db(db: Path, session: str | None) -> tuple[str, list[dict]]:
    path = Path(db).resolve()
    if not path.is_file():
        raise ValueError(f"database not found: {path}")
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise ValueError("WAL without SHM: checkpoint the database before offline analysis")
    params: tuple = () if session is None else (session,)
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "captions" in tables:
            schema = "captions"
            marks = ",".join("?" * len(CAPTION_FINAL_STATUSES))
            query = f"""
                SELECT id, room_id, session_id, seq, zh AS text, t0_ms, t1_ms
                FROM captions WHERE status IN ({marks})
            """
            params = CAPTION_FINAL_STATUSES + params
            if session is not None:
                query += " AND session_id=?"
            query += " ORDER BY room_id, session_id, seq, id"
        elif {"segments", "transcripts"} <= tables:
            schema = "ledger"
            marks = ",".join("?" * len(LEDGER_FINAL_STATUSES))
            query = f"""
                SELECT s.id, s.room_id, s.session_id, s.seq, t.text, s.t0_ms, s.t1_ms
                FROM segments s
                JOIN transcripts t ON t.segment_id=s.id AND t.is_current=1
                WHERE s.status IN ({marks})
            """
            params = LEDGER_FINAL_STATUSES + params
            if session is not None:
                query += " AND s.session_id=?"
            query += " ORDER BY s.room_id, s.session_id, s.seq, s.id"
        else:
            raise ValueError("unsupported captions database schema (expected captions or ledger tables)")
        rows = [dict(row) for row in conn.execute(query, params)]
    for row in rows:
        if row["text"] is not None and not isinstance(row["text"], str):
            raise ValueError(f"segment {row['id']}: text must be a string")
        row["text"] = row["text"] or ""
    return schema, rows


def _read_jsonl(path: Path, session: str | None) -> tuple[str, list[dict]]:
    rows = []
    with Path(path).open(encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {number}: invalid JSON ({exc.msg})") from None
            row = {"id": number, "room_id": None, "session_id": None, "seq": None,
                   "t0_ms": None, "t1_ms": None}
            if isinstance(value, str):
                row["text"] = value
            elif isinstance(value, dict):
                text = value.get("text", value.get("zh"))
                if not isinstance(text, str):
                    raise ValueError(f"line {number}: expected a text (or zh) string")
                row["text"] = text
                for key in ("id", "room_id", "session_id", "seq"):
                    if value.get(key) is not None:
                        if isinstance(value[key], bool) or not isinstance(value[key], (str, int)):
                            raise ValueError(f"line {number}: {key} must be a string or integer")
                        row[key] = value[key]
                for key in ("t0_ms", "t1_ms"):
                    item = value.get(key)
                    if item is not None:
                        if isinstance(item, bool) or not isinstance(item, (int, float)):
                            raise ValueError(f"line {number}: {key} must be a number")
                        row[key] = item
            else:
                raise ValueError(f"line {number}: expected a JSON string or object")
            if session is not None and row["session_id"] != session:
                continue
            rows.append(row)
    return "jsonl", rows


def check_segment(text: str, t0_ms, t1_ms, limits: dict,
                  extra_connectives: tuple[str, ...] = ()) -> tuple[list[dict], dict]:
    """Return (issues, metrics) for one final Chinese segment."""
    issues: list[dict] = []

    def add(code: str, message: str) -> None:
        issues.append({"code": code, "message_zh": message})

    chars = char_count(text)
    duration = None
    if (isinstance(t0_ms, (int, float)) and isinstance(t1_ms, (int, float))
            and math.isfinite(t0_ms) and math.isfinite(t1_ms) and t1_ms > t0_ms):
        duration = (t1_ms - t0_ms) / 1000
    metrics = {"chars": chars,
               "duration_s": None if duration is None else round(duration, 3),
               "cps": None if duration is None or not chars else round(chars / duration, 3)}
    if not text.strip():
        add("empty", "空白片段：沒有任何文字。")
        return issues, metrics
    if text != text.strip():
        add("edge_whitespace", "片段開頭或結尾有多餘空白。")
    if chars < limits["min_chars"]:
        add("too_short", f"片段過短：{chars} 字（下限 {limits['min_chars']}）。")
    elif chars > limits["max_chars"]:
        add("too_long", f"片段過長：{chars} 字（上限 {limits['max_chars']}），建議再切分。")
    if duration is not None:
        cps = chars / duration
        if duration < limits["min_duration_s"] or cps > limits["max_cps"]:
            add("duration_too_short",
                f"時長太短：{duration:.2f} 秒 {chars} 字（每秒 {cps:.1f} 字，上限 {limits['max_cps']:g}）。")
        elif duration > limits["max_duration_s"] or cps < limits["min_cps"]:
            add("duration_too_long",
                f"時長太長：{duration:.2f} 秒 {chars} 字（每秒 {cps:.1f} 字，下限 {limits['min_cps']:g}）。")
    word = dangling_connective(text, extra_connectives)
    if word:
        add("dangling_connective", f"結尾停在連接詞「{word}」，句子可能被切斷。")
    stray = unbalanced_brackets(text)
    if stray:
        add("unbalanced_brackets", f"括號或引號不成對：{'、'.join(stray)}。")
    return issues, metrics


def analyze(*, db: Path | None = None, jsonl: Path | None = None,
            session: str | None = None, thresholds: dict | None = None,
            extra_connectives: tuple[str, ...] = (), max_findings: int | None = None) -> dict:
    if (db is None) == (jsonl is None):
        raise ValueError("provide exactly one of --db or --jsonl")
    if max_findings is not None and max_findings < 0:
        raise ValueError("--max-findings must be non-negative")
    limits = _thresholds(thresholds)
    extra = tuple(word.strip() for word in extra_connectives if word and word.strip())
    schema, rows = _read_db(Path(db), session) if db is not None else _read_jsonl(Path(jsonl), session)
    counts = dict.fromkeys(ISSUE_CODES, 0)
    flagged = []
    duration_unknown = 0
    for row in rows:
        issues, metrics = check_segment(row["text"], row["t0_ms"], row["t1_ms"], limits, extra)
        if metrics["duration_s"] is None:
            duration_unknown += 1
        for issue in issues:
            counts[issue["code"]] += 1
        if issues:
            flagged.append({"id": row["id"], "session_id": row["session_id"], "seq": row["seq"],
                            "t0_ms": row["t0_ms"], "t1_ms": row["t1_ms"], **metrics,
                            "text": row["text"][:80], "issues": issues})
    total = len(rows)
    rate = len(flagged) / total if total else 0.0
    within = rate <= limits["max_issue_rate"]
    if not total:
        verdict = "沒有可檢查的中文定稿片段。"
    elif within:
        verdict = f"問題比例 {rate:.1%}，在門檻 {limits['max_issue_rate']:.0%} 以內。"
    else:
        verdict = f"問題比例 {rate:.1%}，超過門檻 {limits['max_issue_rate']:.0%}，請檢查斷句設定。"
    return {
        "tool": TOOL,
        "schema": schema,
        "session": session,
        "thresholds": limits,
        "summary": {
            "segments": total,
            "segments_with_issues": len(flagged),
            "issue_rate": round(rate, 4),
            "within_threshold": within,
            "duration_unknown": duration_unknown,
            "counts": counts,
            "verdict_zh": verdict,
        },
        "truncated": max_findings is not None and len(flagged) > max_findings,
        "segments": flagged if max_findings is None else flagged[:max_findings],
    }


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="captions.sqlite3 or zen.sqlite3 (read-only)")
    source.add_argument("--jsonl", type=Path, help="JSON Lines of final Chinese segments")
    parser.add_argument("--session", help="only check this session_id")
    parser.add_argument("--min-chars", type=int, default=DEFAULTS["min_chars"])
    parser.add_argument("--max-chars", type=int, default=DEFAULTS["max_chars"])
    parser.add_argument("--min-duration", type=float, default=DEFAULTS["min_duration_s"], metavar="SECONDS")
    parser.add_argument("--max-duration", type=float, default=DEFAULTS["max_duration_s"], metavar="SECONDS")
    parser.add_argument("--min-cps", type=float, default=DEFAULTS["min_cps"], help="chars per second floor")
    parser.add_argument("--max-cps", type=float, default=DEFAULTS["max_cps"], help="chars per second ceiling")
    parser.add_argument("--max-issue-rate", type=float, default=DEFAULTS["max_issue_rate"],
                        help="share of segments with issues allowed before exit 1 (0-1)")
    parser.add_argument("--connective", action="append", default=[], metavar="WORD",
                        help="extra dangling connective (repeatable)")
    parser.add_argument("--max-findings", type=int, help="limit the per-segment list (counts stay complete)")
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
                         thresholds={"min_chars": args.min_chars, "max_chars": args.max_chars,
                                     "min_duration_s": args.min_duration,
                                     "max_duration_s": args.max_duration,
                                     "min_cps": args.min_cps, "max_cps": args.max_cps,
                                     "max_issue_rate": args.max_issue_rate},
                         extra_connectives=tuple(args.connective), max_findings=args.max_findings)
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
