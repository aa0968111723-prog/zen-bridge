#!/usr/bin/env python3
"""Offline ASR repetition / hallucination detector for final Chinese captions; stdlib only.

  python tools/asr_repetition_detector.py --db captions.sqlite3 [--session ID] [--json OUT]
  python tools/asr_repetition_detector.py --jsonl segments.jsonl [--json OUT]

Input (read-only, exactly one of):
* --db     CaptionStore captions.sqlite3 (app/store.py): rows of table captions
           with status ready / zh_ready / translate_failed, text column zh.
           The zen.sqlite3 ledger (app/admin/schema.sql) is also accepted: the
           current transcripts.text of segments with status asr_done/translated.
           Opened with a sqlite URI mode=ro (plus immutable=1 when no WAL file
           exists), so neither the database nor its sidecars are created or changed.
* --jsonl  UTF-8 JSON Lines; each line is a JSON string or an object with a
           text (or zh) string and optional id, session_id, seq, t0_ms, t1_ms.

Checks (stable issue codes):
* ngram_loop       — within one segment, a character n-gram (length 1–8)
                     repeats ≥ --min-repeats times consecutively (Whisper loop).
* boilerplate      — YouTube / livestream boilerplate such as 「謝謝收看」「請訂閱」
                     (half-width / full-width variants normalised).
* repeat_adjacent  — adjacent segments (same session, by seq/t0) with normalised
                     identical text.
* near_duplicate   — whole-segment near-duplicate of a recent previous segment
                     within --near-window segments (optional heuristic).

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


TOOL = "asr_repetition_detector"
ISSUE_CODES = (
    "ngram_loop",
    "boilerplate",
    "repeat_adjacent",
    "near_duplicate",
)
CAPTION_FINAL_STATUSES = ("ready", "zh_ready", "translate_failed")
LEDGER_FINAL_STATUSES = ("asr_done", "translated")

DEFAULTS = {
    "min_ngram": 1,
    "max_ngram": 8,
    "min_repeats": 3,
    "near_window": 3,
    "near_ratio": 0.9,
    "max_issue_rate": 0.1,
}

# Canonical Traditional / Simplified boilerplate phrases (matched after NFKC).
BOILERPLATE_PHRASES = (
    "謝謝收看",
    "谢谢收看",
    "請訂閱",
    "请订阅",
    "歡迎訂閱",
    "欢迎订阅",
    "記得打卡",
    "记得打卡",
    "下期再見",
    "下期再见",
    "我們下期見",
    "我们下期见",
    "謝謝大家",
    "谢谢大家",
    "請按讚",
    "请按赞",
    "點個讚",
    "点个赞",
    "訂閱頻道",
    "订阅频道",
    "開啟小鈴鐺",
    "开启小铃铛",
    "打開小鈴鐺",
    "打开小铃铛",
    "下次再見",
    "下次再见",
    "感謝收看",
    "感谢收看",
    "喜歡的話請",
    "喜欢的话请",
)

_WS_RE = re.compile(r"[\s\u3000]+")
_PUNCT_RE = re.compile(
    r"[!-\/:-@\[-`\{-~，。、；：！？「」『』（）【】《》〈〉…‥—–·￥￡￠]+"
)


def normalize_text(text: str) -> str:
    """NFKC, strip, collapse whitespace; keep CJK letters/digits for compare."""
    if text is None:
        return ""
    body = unicodedata.normalize("NFKC", str(text)).strip()
    body = _WS_RE.sub(" ", body)
    return body


def normalize_for_match(text: str) -> str:
    """Aggressive normalise: drop punctuation/space for identity / near-dup compare."""
    body = normalize_text(text)
    body = _PUNCT_RE.sub("", body)
    body = _WS_RE.sub("", body)
    return body.casefold()


def _boilerplate_needles() -> tuple[str, ...]:
    seen: list[str] = []
    for phrase in BOILERPLATE_PHRASES:
        needle = normalize_for_match(phrase)
        if needle and needle not in seen:
            seen.append(needle)
    return tuple(sorted(seen, key=len, reverse=True))


BOILERPLATE_NEEDLES = _boilerplate_needles()


def find_ngram_loop(
    text: str,
    *,
    min_n: int = 1,
    max_n: int = 8,
    min_repeats: int = 3,
) -> tuple[str, int, int] | None:
    """Return (gram, repeats, n) for the longest consecutive n-gram loop, else None.

    Prefer longer grams, then more repeats, then earlier start. Whitespace-only
    grams are ignored. A single CJK character looping (好好好好) counts when
    min_n allows it.
    """
    body = normalize_text(text)
    if not body:
        return None
    best: tuple[str, int, int] | None = None
    for n in range(max(1, max_n), max(0, min_n) - 1, -1):
        if n < min_n or n * min_repeats > len(body):
            continue
        limit = len(body) - n * min_repeats + 1
        i = 0
        while i < limit:
            gram = body[i : i + n]
            if not gram.strip() or _WS_RE.fullmatch(gram):
                i += 1
                continue
            count = 1
            j = i + n
            while j + n <= len(body) and body[j : j + n] == gram:
                count += 1
                j += n
            if count >= min_repeats:
                candidate = (gram, count, n)
                if best is None or (n, count) > (best[2], best[1]):
                    best = candidate
                i = j  # skip past this run
            else:
                i += 1
        if best is not None and best[2] == n:
            # Longer n already found; no need to fall to shorter unless none.
            return best
    return best


def find_boilerplate(text: str) -> str | None:
    """Return the matched canonical boilerplate needle, or None."""
    hay = normalize_for_match(text)
    if not hay:
        return None
    for needle in BOILERPLATE_NEEDLES:
        if needle in hay:
            return needle
    return None


def _similarity(a: str, b: str) -> float:
    """Character-level Jaccard on bigrams (1.0 if equal / both empty)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) == 1 and len(b) == 1:
        return 1.0 if a == b else 0.0

    def bigrams(s: str) -> set[str]:
        if len(s) < 2:
            return {s}
        return {s[i : i + 2] for i in range(len(s) - 1)}

    left, right = bigrams(a), bigrams(b)
    inter = len(left & right)
    union = len(left | right)
    return inter / union if union else 0.0


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
    out: dict = {}
    for key in ("min_ngram", "max_ngram", "min_repeats", "near_window"):
        value = values[key]
        if isinstance(value, bool) or int(value) != value or value < (0 if key == "near_window" else 1):
            raise ValueError(f"{key} must be a positive integer" if key != "near_window"
                             else "near_window must be a non-negative integer")
        out[key] = int(value)
    for key in ("near_ratio", "max_issue_rate"):
        rate = _number(key, values[key], allow_zero=True)
        if rate > 1:
            raise ValueError(f"{key} must be between 0 and 1")
        out[key] = rate
    if out["min_ngram"] > out["max_ngram"]:
        raise ValueError("min_ngram must not exceed max_ngram")
    if out["min_repeats"] < 2:
        raise ValueError("min_repeats must be at least 2")
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


def check_segment(
    text: str,
    limits: dict,
    *,
    prev_norm: str | None = None,
    recent_norms: list[str] | None = None,
) -> list[dict]:
    """Return issues for one final Chinese segment (plus adjacency / near-dup context)."""
    issues: list[dict] = []

    def add(code: str, message: str, **extra) -> None:
        item = {"code": code, "message_zh": message}
        item.update(extra)
        issues.append(item)

    loop = find_ngram_loop(
        text,
        min_n=limits["min_ngram"],
        max_n=limits["max_ngram"],
        min_repeats=limits["min_repeats"],
    )
    if loop is not None:
        gram, repeats, n = loop
        add(
            "ngram_loop",
            f"偵測到 n-gram 迴圈：「{gram}」連續重複 {repeats} 次（n={n}），疑似 Whisper 幻覺。",
            gram=gram,
            repeats=repeats,
            n=n,
        )

    phrase = find_boilerplate(text)
    if phrase is not None:
        add(
            "boilerplate",
            f"偵測到影音平台套話（正規化後含「{phrase}」），疑似非現場講者內容。",
            phrase=phrase,
        )

    norm = normalize_for_match(text)
    if prev_norm is not None and norm and norm == prev_norm:
        add(
            "repeat_adjacent",
            "與前一則片段正規化後文字完全相同（同句連發）。",
        )

    if recent_norms and norm and limits["near_window"] > 0:
        for older in recent_norms:
            if not older:
                continue
            # Exact adjacent match is already reported as repeat_adjacent.
            if older == prev_norm and norm == prev_norm:
                continue
            ratio = _similarity(norm, older)
            if ratio >= limits["near_ratio"]:
                add(
                    "near_duplicate",
                    f"與近期片段高度相似（相似度 {ratio:.0%}），疑似重複幻覺。",
                    similarity=round(ratio, 4),
                )
                break

    return issues


def analyze(
    *,
    db: Path | None = None,
    jsonl: Path | None = None,
    session: str | None = None,
    thresholds: dict | None = None,
    max_findings: int | None = None,
) -> dict:
    if (db is None) == (jsonl is None):
        raise ValueError("provide exactly one of --db or --jsonl")
    if max_findings is not None and max_findings < 0:
        raise ValueError("--max-findings must be non-negative")
    limits = _thresholds(thresholds)
    schema, rows = _read_db(Path(db), session) if db is not None else _read_jsonl(Path(jsonl), session)

    counts = dict.fromkeys(ISSUE_CODES, 0)
    flagged: list[dict] = []
    prev_key: tuple | None = None
    prev_norm: str | None = None
    recent: list[str] = []  # normalised texts within near_window (same session key)

    for row in rows:
        key = (row.get("room_id"), row.get("session_id"))
        if key != prev_key:
            prev_norm = None
            recent = []
            prev_key = key

        issues = check_segment(
            row["text"],
            limits,
            prev_norm=prev_norm,
            recent_norms=list(recent),
        )
        for issue in issues:
            counts[issue["code"]] += 1
        if issues:
            flagged.append({
                "id": row["id"],
                "session_id": row["session_id"],
                "seq": row["seq"],
                "t0_ms": row["t0_ms"],
                "t1_ms": row["t1_ms"],
                "text": row["text"][:80],
                "issues": issues,
            })

        norm = normalize_for_match(row["text"])
        if norm:
            if limits["near_window"] > 0:
                recent.append(norm)
                if len(recent) > limits["near_window"]:
                    recent = recent[-limits["near_window"] :]
            else:
                recent = []
            prev_norm = norm
        else:
            # Empty / punctuation-only does not anchor adjacency.
            prev_norm = None

    total = len(rows)
    rate = len(flagged) / total if total else 0.0
    within = rate <= limits["max_issue_rate"]
    if not total:
        verdict = "沒有可檢查的中文定稿片段。"
    elif within:
        verdict = f"問題比例 {rate:.1%}，在門檻 {limits['max_issue_rate']:.0%} 以內。"
    else:
        verdict = f"問題比例 {rate:.1%}，超過門檻 {limits['max_issue_rate']:.0%}，請檢查 ASR 幻覺／重複。"
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
    parser.add_argument("--min-ngram", type=int, default=DEFAULTS["min_ngram"],
                        help="shortest character n-gram length for loop detection")
    parser.add_argument("--max-ngram", type=int, default=DEFAULTS["max_ngram"],
                        help="longest character n-gram length for loop detection")
    parser.add_argument("--min-repeats", type=int, default=DEFAULTS["min_repeats"],
                        help="consecutive repeats required to flag ngram_loop (default 3)")
    parser.add_argument("--near-window", type=int, default=DEFAULTS["near_window"],
                        help="how many prior segments to compare for near_duplicate (0=off)")
    parser.add_argument("--near-ratio", type=float, default=DEFAULTS["near_ratio"],
                        help="similarity threshold for near_duplicate (0-1)")
    parser.add_argument("--max-issue-rate", type=float, default=DEFAULTS["max_issue_rate"],
                        help="share of segments with issues allowed before exit 1 (0-1)")
    parser.add_argument("--max-findings", type=int,
                        help="limit the per-segment list (counts stay complete)")
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
        report = analyze(
            db=args.db,
            jsonl=args.jsonl,
            session=args.session,
            thresholds={
                "min_ngram": args.min_ngram,
                "max_ngram": args.max_ngram,
                "min_repeats": args.min_repeats,
                "near_window": args.near_window,
                "near_ratio": args.near_ratio,
                "max_issue_rate": args.max_issue_rate,
            },
            max_findings=args.max_findings,
        )
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
