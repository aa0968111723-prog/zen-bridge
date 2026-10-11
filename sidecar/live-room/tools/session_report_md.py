#!/usr/bin/env python3
"""Write a Traditional Chinese Markdown summary from a read-only caption database.

Usage: python tools/session_report_md.py --db captions.sqlite3 --out report.md [--session ID]
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
import sys
import unicodedata


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _percentile(values: list[float], p: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * p
    low = int(rank)
    fraction = rank - low
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * fraction


def _timestamp(value) -> str:
    if value is None:
        return "無資料"
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (OverflowError, OSError, ValueError):
            return "無資料"
    return _markdown(str(value))


def _markdown(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", r"\|").strip()


def _text_chars(text: str, lang: str) -> float:
    if lang == "en":
        return float(len(text.strip()))
    return sum(
        1 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
        for char in text.strip()
    )


def _session_rows(conn: sqlite3.Connection, tables: set[str], columns: dict[str, set[str]],
                  session: str | None) -> list[dict]:
    if "sessions" in tables and "id" in columns["sessions"]:
        available = columns["sessions"]
        fields = ("id", "started_at", "ended_at", "tgt_lang", "room_id")
        select = ", ".join(
            field if field in available else f"NULL AS {field}"
            for field in fields
        )
        query = f"SELECT {select} FROM sessions"
        params = ()
        if session is not None:
            query += " WHERE id=?"
            params = (session,)
        query += " ORDER BY id"
        rows = [dict(row) for row in conn.execute(query, params)]
        if "segments" in tables and "translations" in tables:
            segment_columns = columns["segments"]
            translation_columns = columns["translations"]
            if {"id", "session_id"} <= segment_columns and "tgt_lang" in translation_columns:
                current_filter = (
                    " AND t.is_current=1" if "is_current" in translation_columns else ""
                )
                for row in rows:
                    language = conn.execute(
                        f"""SELECT t.tgt_lang
                            FROM translations t JOIN segments s ON s.id=t.segment_id
                            WHERE s.session_id=? AND t.tgt_lang IS NOT NULL{current_filter}
                            GROUP BY t.tgt_lang ORDER BY COUNT(*) DESC, t.tgt_lang LIMIT 1""",
                        (row["id"],),
                    ).fetchone()
                    if language is not None:
                        row["tgt_lang"] = language[0]
        if rows or session is not None:
            return rows

    if "captions" in tables and "session_id" in columns["captions"]:
        if session is not None:
            return [{"id": session, "started_at": None, "ended_at": None,
                     "tgt_lang": "en", "room_id": None}]
        return [
            {"id": row[0], "started_at": None, "ended_at": None,
             "tgt_lang": "en", "room_id": None}
            for row in conn.execute(
                "SELECT DISTINCT session_id FROM captions WHERE session_id IS NOT NULL ORDER BY session_id"
            )
        ]
    if session is not None:
        return [{"id": session, "started_at": None, "ended_at": None,
                 "tgt_lang": None, "room_id": None}]
    return []


def _captions_for_session(conn: sqlite3.Connection, tables: set[str],
                          columns: dict[str, set[str]], metadata: dict,
                          notes: list[str]) -> tuple[int | None, int | None, list[dict], list[float]]:
    session_id = metadata["id"]
    if {"sessions", "segments"} <= tables:
        seg_columns = columns["segments"]
        if not {"id", "session_id"} <= seg_columns:
            notes.append("segments 缺少必要欄位，無法統計字幕。")
            return None, None, [], []
        expressions = [
            "id",
            "status" if "status" in seg_columns else "NULL AS status",
            "t0_ms" if "t0_ms" in seg_columns else "NULL AS t0_ms",
            "t1_ms" if "t1_ms" in seg_columns else "NULL AS t1_ms",
        ]
        segments = {
            row["id"]: dict(row)
            for row in conn.execute(
                f"SELECT {', '.join(expressions)} FROM segments WHERE session_id=?",
                (session_id,),
            )
        }
        if "status" not in seg_columns:
            notes.append("segments 缺少 status 欄位。")
        if not {"t0_ms", "t1_ms"} <= seg_columns:
            notes.append("segments 缺少音訊時間欄位，無法計算閱讀速度。")

        draft_count = None
        if "translations" not in tables:
            notes.append("translations 資料表不存在，定稿字幕與延遲無法統計。")
            return draft_count, None, [], []
        tr_columns = columns["translations"]
        needed = {"segment_id", "tgt_lang", "is_current", "text"}
        if not needed <= tr_columns:
            notes.append("translations 缺少必要欄位，定稿字幕與延遲無法統計。")
            return draft_count, None, [], []

        select = ["segment_id", "tgt_lang", "is_current", "text"]
        select.extend(
            field if field in tr_columns else f"NULL AS {field}"
            for field in ("latency_ms", "status")
        )
        lang = metadata.get("tgt_lang")
        final_rows = []
        latencies = []
        for row in conn.execute(
            f"""SELECT {', '.join(select)} FROM translations
                WHERE segment_id IN (SELECT id FROM segments WHERE session_id=?)""",
            (session_id,),
        ):
            translation = dict(row)
            segment = segments.get(translation["segment_id"])
            if not segment or not translation["is_current"] or translation["tgt_lang"] != lang:
                continue
            if translation["status"] is not None and translation["status"] not in ("ok", "merged"):
                continue
            if segment["status"] is not None and segment["status"] != "translated":
                continue
            final_rows.append({
                "text": translation["text"] or "",
                "t0_ms": segment["t0_ms"],
                "t1_ms": segment["t1_ms"],
            })
            latency = translation["latency_ms"]
            if isinstance(latency, (int, float)) and math.isfinite(latency) and latency >= 0:
                latencies.append(float(latency))
        if "latency_ms" not in tr_columns:
            notes.append("translations 缺少 latency_ms 欄位，翻譯延遲無法統計。")
        return draft_count, len(final_rows), final_rows, latencies

    if "captions" not in tables:
        notes.append("找不到支援的 sessions/segments/translations 或 captions 資料表。")
        return None, None, [], []
    caption_columns = columns["captions"]
    if "session_id" not in caption_columns:
        notes.append("captions 缺少 session_id 欄位，無法統計字幕。")
        return None, None, [], []
    selected = ["status" if "status" in caption_columns else "NULL AS status",
                "en" if "en" in caption_columns else "NULL AS en",
                "t0_ms" if "t0_ms" in caption_columns else "NULL AS t0_ms",
                "t1_ms" if "t1_ms" in caption_columns else "NULL AS t1_ms",
                "latency_ms" if "latency_ms" in caption_columns else "NULL AS latency_ms"]
    rows = [dict(row) for row in conn.execute(
        f"SELECT {', '.join(selected)} FROM captions WHERE session_id=?",
        (session_id,),
    )]
    if "status" not in caption_columns:
        notes.append("captions 缺少 status 欄位，草稿與定稿字幕數無法統計。")
    if "en" not in caption_columns:
        notes.append("captions 缺少 en 欄位，定稿字幕與閱讀速度無法統計。")
    if not {"t0_ms", "t1_ms"} <= caption_columns:
        notes.append("captions 缺少音訊時間欄位，無法計算閱讀速度。")
    draft_count = None
    finals = [
        {"text": row["en"] or "", "t0_ms": row["t0_ms"], "t1_ms": row["t1_ms"]}
        for row in rows
        if row["status"] is not None
        and str(row["status"]).lower() in ("ready", "final", "translated")
    ] if {"status", "en"} <= caption_columns else []
    latencies = [
        float(row["latency_ms"]) for row in rows
        if isinstance(row["latency_ms"], (int, float))
        and math.isfinite(row["latency_ms"]) and row["latency_ms"] >= 0
    ]
    if "latency_ms" not in caption_columns:
        notes.append("captions 缺少 latency_ms 欄位，延遲無法統計。")
    return draft_count, len(finals) if {"status", "en"} <= caption_columns else None, finals, latencies


def _glossary_terms(conn: sqlite3.Connection, tables: set[str],
                    columns: dict[str, set[str]], metadata: dict,
                    notes: list[str]) -> list[tuple[str, str, int]]:
    if "glossary_terms" not in tables:
        notes.append("glossary_terms 資料表不存在，無法列出常用術語。")
        return []
    if "glossaries" not in tables:
        notes.append("glossaries 資料表不存在，無法辨認本場次術語。")
        return []
    term_columns, glossary_columns = columns["glossary_terms"], columns["glossaries"]
    required_terms = {"glossary_id", "zh"}
    required_glossaries = {"id", "scope", "room_id", "session_id"}
    if not required_terms <= term_columns or not required_glossaries <= glossary_columns:
        notes.append("詞彙表缺少必要欄位，無法列出常用術語。")
        return []
    lang = metadata.get("tgt_lang")
    targets = columns.get("glossary_term_targets", set())
    if "glossary_term_targets" in tables:
        if lang not in ("en", "ja") or not {"term_id", "tgt_lang", "text", "hit_count"} <= targets:
            notes.append("glossary_term_targets 缺少目標語言或必要欄位，無法列出常用術語。")
            return []
        status_filter = "AND t.status='active'" if "status" in term_columns else ""
        target_status = "AND x.status='active'" if "status" in targets else ""
        rows = conn.execute(
            f"""SELECT t.zh, x.text, x.hit_count
                FROM glossary_terms t
                JOIN glossary_term_targets x ON x.term_id=t.id AND x.tgt_lang=?
                JOIN glossaries g ON g.id=t.glossary_id
                WHERE (g.scope='global'
                       OR (g.scope='room' AND g.room_id=?)
                       OR (g.scope='session' AND g.session_id=?))
                      {status_filter} {target_status}
                ORDER BY x.hit_count DESC, t.zh, x.text
                LIMIT 10""",
            (lang, metadata.get("room_id"), metadata["id"]),
        )
        return [(str(row[0] or ""), str(row[1] or ""), int(row[2] or 0)) for row in rows]

    if not {"en", "hit_count"} <= term_columns:
        notes.append("舊版 glossary_terms 缺少 en 或 hit_count 欄位，無法列出常用術語。")
        return []
    status_filter = "AND t.status='active'" if "status" in term_columns else ""
    rows = conn.execute(
        f"""SELECT t.zh, t.en, t.hit_count
            FROM glossary_terms t JOIN glossaries g ON g.id=t.glossary_id
            WHERE (g.scope='global'
                   OR (g.scope='room' AND g.room_id=?)
                   OR (g.scope='session' AND g.session_id=?))
                  {status_filter}
            ORDER BY t.hit_count DESC, t.zh, t.en
            LIMIT 10""",
        (metadata.get("room_id"), metadata["id"]),
    )
    return [(str(row[0] or ""), str(row[1] or ""), int(row[2] or 0)) for row in rows]


def _render_session(conn: sqlite3.Connection, tables: set[str], columns: dict[str, set[str]],
                    metadata: dict) -> str:
    notes: list[str] = []
    session_id = metadata.get("id")
    if session_id is None:
        notes.append("資料庫沒有可辨識的場次。")
    if "sessions" not in tables or not {"started_at", "ended_at"} <= columns.get("sessions", set()):
        notes.append("sessions 缺少開始或結束時間欄位。")
    if "sessions" not in tables or "tgt_lang" not in columns.get("sessions", set()):
        if "captions" in tables:
            metadata["tgt_lang"] = "en"
            notes.append("場次目標語言未儲存；CaptionStore 僅能以 en 報告。")
        else:
            notes.append("sessions 缺少 tgt_lang 欄位。")
    lang = metadata.get("tgt_lang")
    if lang not in ("en", "ja"):
        lang = None
        notes.append("目標語言不是支援的 en 或 ja，無法分類字幕。")

    draft_count, final_count, final_rows, latencies = _captions_for_session(
        conn, tables, columns, metadata, notes,
    )
    notes.append("資料庫未保留草稿事件，草稿字幕數無法統計。")
    notes.append("資料庫未保留草稿與定稿文字配對，草稿→定稿改寫率無法計算。")

    outliers = []
    if lang:
        threshold = 17 if lang == "en" else 8
        for row in final_rows:
            start, end = row["t0_ms"], row["t1_ms"]
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
                continue
            text = str(row["text"] or "").strip()
            cps = _text_chars(text, lang) / ((end - start) / 1000)
            if cps > threshold:
                outliers.append((cps, text))

    latency_known = (
        "translations" in tables and "latency_ms" in columns.get("translations", set())
    ) or ("captions" in tables and "latency_ms" in columns.get("captions", set()))
    if not latency_known:
        latency_summary = "無資料"
    else:
        p50, p95 = _percentile(latencies, 0.50), _percentile(latencies, 0.95)
        latency_summary = (
            f"p50 {_number(p50)} ms／p95 {_number(p95)} ms"
            if p50 is not None else "無可用數值"
        )

    terms = (
        _glossary_terms(conn, tables, columns, metadata, notes)
        if "sessions" in tables and {"segments", "translations"} <= tables else []
    )
    if "sessions" not in tables or not {"segments", "translations"} <= tables:
        notes.append("非 ledger 資料庫，沒有場次關聯的 glossary_terms 清單。")

    lines = [
        f"## 場次：{_markdown(str(session_id if session_id is not None else '未指定'))}",
        "",
        f"- 開始時間：{_timestamp(metadata.get('started_at'))}",
        f"- 結束時間：{_timestamp(metadata.get('ended_at'))}",
        f"- 目標語言：{lang or '無資料'}",
        f"- 草稿字幕數：{draft_count if draft_count is not None else '無資料'}",
        f"- 定稿字幕數：{final_count if final_count is not None else '無資料'}",
        "- 草稿→定稿改寫率：無法計算",
        f"- 翻譯延遲：{latency_summary}",
        "",
        f"### 閱讀速度超標字幕（{lang or '未知'}）",
    ]
    if outliers:
        for cps, text in outliers:
            excerpt = _markdown(text)
            if len(excerpt) > 160:
                excerpt = excerpt[:157] + "…"
            lines.append(f"- {cps:.1f} 字元／秒：{excerpt}")
    else:
        lines.append("- 無")

    if "sessions" in tables and {"segments", "translations"} <= tables:
        lines.extend(["", "### 最常用詞彙（最多 10 項）"])
        if terms:
            target_name = "英文" if lang == "en" else "日文"
            lines.extend([f"| 中文 | {target_name} | 使用次數 |", "| --- | --- | ---: |"])
            lines.extend(f"| {_markdown(zh)} | {_markdown(en)} | {count} |" for zh, en, count in terms)
        else:
            lines.append("- 無")
    if notes:
        lines.extend(["", "### 備註"])
        lines.extend(f"- {_markdown(note)}" for note in dict.fromkeys(notes))
    return "\n".join(lines)


def _number(value: float | None) -> str:
    return "無資料" if value is None else f"{value:.1f}"


def analyze(db: str | Path, session: str | None = None) -> str:
    path = Path(db).resolve()
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise ValueError("WAL without SHM: checkpoint the database before offline analysis")
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        known_tables = {"sessions", "segments", "translations", "captions",
                        "glossaries", "glossary_terms", "glossary_term_targets"}
        columns = {table: _columns(conn, table) for table in tables & known_tables}
        sessions = _session_rows(conn, tables, columns, session)
        if not sessions:
            sessions = [{"id": session, "started_at": None, "ended_at": None,
                         "tgt_lang": None, "room_id": None}]
        return "# 會議字幕摘要\n\n" + "\n\n".join(
            _render_session(conn, tables, columns, row) for row in sessions
        ) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--out", type=Path, metavar="OUT", required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.db, args.session)
        if args.out is not None:
            db = args.db.resolve()
            protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
            if args.out.resolve() in protected:
                raise ValueError("--out must not refer to the database or its SQLite sidecars")
            with args.out.open("x", encoding="utf-8", newline="\n") as output:
                output.write(report)
        print(report, end="")
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"session_report_md: cannot analyze {args.db}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    # Windows terminals and redirected pipes may otherwise use a legacy encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
