"""Tests for tools/ja_term_consistency_check.py (queue item 40).

Every input is a small fake caption store / ledger SQLite file or caption JSONL
under tmp_path, written with explicit encodings.  Nothing depends on the
network, ports, POSIX permission bits, the local time zone or the clock, and
no term file of the repository is read.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ja_term_consistency_check.py"
SCHEMA = ROOT / "app" / "admin" / "schema.sql"

sys.path.insert(0, str(ROOT / "tools"))
import ja_term_consistency_check as jtc  # noqa: E402

REPORT_KEYS = {"tool", "schema_version", "status", "exit_code", "settings", "inputs", "sessions",
               "summary", "inconsistencies", "warnings", "notes", "suggestions", "summary_zh"}

ZAZEN = [
    ("今天我們一起坐禪。", "今日は皆で坐禅をします。"),
    ("坐禪的時候要放鬆。", "坐禅のときはリラックスしましょう。"),
    ("坐禪不是睡覺。", "座禅は眠ることではありません。"),
    ("我們繼續坐禪。", "坐禅を続けましょう。"),
]
CONSISTENT = [
    ("今天我們一起坐禪。", "今日は皆で坐禅をします。"),
    ("坐禪的時候要放鬆。", "坐禅のときはリラックスしましょう。"),
    ("坐禪不是睡覺。", "坐禅は眠ることではありません。"),
]
ENGLISH = [
    ("今天我們一起坐禪。", "Today we sit in zazen together."),
    ("坐禪的時候要放鬆。", "Relax during zazen."),
    ("坐禪不是睡覺。", "Zazen is not sleeping."),
]


# --------------------------------------------------------------------------- builders
def captions_db(path: Path, sessions: dict[tuple[str, str], list], status: str = "ready") -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """create table captions (
                id text primary key, room_id text not null, session_id text not null,
                seq integer not null, version integer not null, zh text, zh_raw text, en text,
                status text, t0_ms integer, t1_ms integer, updated_at real not null,
                session_ord integer, term_flags text)""")
        for (room, sess), rows in sessions.items():
            for seq, row in enumerate(rows):
                zh, en = row[0], row[1]
                st = row[2] if len(row) > 2 else status
                conn.execute("insert into captions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"{room}:{sess}:{seq}", room, sess, seq, 1, zh, zh, en, st,
                              seq * 1000, seq * 1000 + 900, 1.0, 1, None))
        conn.commit()
    return path


def _ddl(table: str) -> str:
    schema = SCHEMA.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r" \([\s\S]*?\n\);", schema)
    assert m is not None, table
    return m.group()


def ledger_db(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        for table in ("rooms", "sessions", "segments", "transcripts", "translations"):
            conn.execute(_ddl(table))
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('r', 0)")
        conn.executemany("INSERT INTO sessions(id, room_id, started_at, tgt_lang) VALUES (?, 'r', 0, ?)",
                         [("ja1", "ja"), ("en1", "en")])
        segs = [("j1", "ja1", 1, "translated"), ("j2", "ja1", 2, "translated"),
                ("j3", "ja1", 3, "translated"), ("j4", "ja1", 4, "deleted"),
                ("j5", "ja1", 5, "translated"), ("e1", "en1", 1, "translated"),
                ("e2", "en1", 2, "translated")]
        conn.executemany(
            "INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)"
            " VALUES (?, ?, 'r', ?, 0, 1000, ?, 0)", segs)
        zh = {"j1": "請老師開示佛性。", "j2": "佛性人人本具。", "j3": "一切眾生皆有佛性。",
              "j4": "佛性被刪除。", "j5": "佛性不增不減。", "e1": "佛性。", "e2": "佛性人人本具。"}
        for sid, text in zh.items():
            conn.execute("INSERT INTO transcripts(segment_id, version, is_current, text, created_at)"
                         " VALUES (?, 1, 1, ?, 0)", (sid, text))
        conn.executemany(
            "INSERT INTO translations(segment_id, tgt_lang, version, is_current, text, status, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [("j1", "ja", 1, 0, "老師に佛性の開示をお願いします。", "ok", "mt"),     # old version
             ("j1", "ja", 2, 1, "老師に仏性の開示をお願いします。", "ok", "post_edit"),
             ("j2", "ja", 1, 1, "仏性は誰にでも本来備わっています。", "merged", "mt"),
             ("j3", "ja", 1, 1, "一切衆生には皆佛性があります。", "ok", "mt"),
             ("j4", "ja", 1, 1, "佛性佛性佛性", "ok", "mt"),                    # deleted segment
             ("j5", "ja", 1, 1, "佛性は増えも減りもしない。", "error", "mt"),      # failed translation
             ("j5", "en", 1, 1, "Buddha nature neither grows nor shrinks.", "ok", "mt"),
             ("e1", "en", 1, 1, "Buddha nature.", "ok", "mt"),
             ("e2", "en", 1, 1, "Buddha nature is inherent.", "ok", "mt")])
        conn.commit()
    return path


def write_jsonl(path: Path, events: list, encoding: str = "utf-8", bom: bool = False,
                extra_lines: list[str] | None = None) -> Path:
    lines = [json.dumps(e, ensure_ascii=False) for e in events] + list(extra_lines or [])
    data = ("\n".join(lines) + "\n").encode(encoding)
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)
    return path


def bus(seq: int, zh: str, en: str, *, room: str = "r1", sess: str = "s1", version: int = 1,
        typ: str = "final", tgt_lang: str | None = "ja", **extra) -> dict:
    ev = {"type": typ, "id": f"{room}:{sess}:{seq}", "room_id": room, "session_id": sess, "seq": seq,
          "version": version, "zh": zh, "en": en, "status": "ready"}
    if tgt_lang is not None:
        ev["tgt_lang"] = tgt_lang
    ev.update(extra)
    return ev


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = jtc.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def run_json(argv: list[str], tmp_path: Path, capsys, name: str = "report.json") -> tuple[int, dict, str]:
    out_path = tmp_path / name
    code, out, _err = run([*argv, "--json", out_path], capsys)
    return code, json.loads(out_path.read_text(encoding="utf-8")), out


# --------------------------------------------------------------------------- helpers
def test_variant_table_has_no_duplicates_and_folds_key_pairs():
    seen: set[str] = set()
    for group in jtc._KANJI_VARIANT_GROUPS.split():
        assert len(group) >= 2
        for ch in group:
            assert ch not in seen
            seen.add(ch)
    assert jtc.fold("座禅") == jtc.fold("坐禪") == jtc.fold("坐禅")
    assert jtc.fold("仏性") == jtc.fold("佛性")
    assert jtc.fold("経典") == jtc.fold("經典") == jtc.fold("经典")
    assert jtc.fold("公案") == "公案"
    for word in ("坐禪會", "ざぜん", "abc"):
        assert len(jtc.fold(word)) == len(word)


@pytest.mark.parametrize("surface,kind", [
    ("坐禅", "kanji"), ("ざぜん", "hiragana"), ("ザゼン", "katakana"), ("ゼンー", "katakana"),
    ("禅の", "mixed"), ("Zen", "mixed"), ("人々", "kanji"),
])
def test_script_kind(surface, kind):
    assert jtc.script_kind(surface) == kind


def test_kana_helpers():
    assert jtc.to_katakana("ざぜん") == "ザゼン"
    assert jtc.to_hiragana("ザゼン") == "ざぜん"
    assert jtc.has_kana("坐禅をします")
    assert not jtc.has_kana("坐禅")
    assert not jtc.has_kana("ー")
    assert not jtc.has_kana("Zazen")


def test_visible_target_strips_ruby_and_wbr_and_nfkc():
    text, readings = jtc.visible_target("{坐禅|ざぜん}を<wbr>続け<wbr/>ます ｻﾞｾﾞﾝ")
    assert text == "坐禅を続けます ザゼン"
    assert readings == [("坐禅", "ざぜん")]


def test_is_ja_tag():
    assert jtc.is_ja_tag("ja") is True
    assert jtc.is_ja_tag("ja-JP") is True
    assert jtc.is_ja_tag("JA_jp") is True
    assert jtc.is_ja_tag("en") is False
    assert jtc.is_ja_tag("") is None
    assert jtc.is_ja_tag(None) is None


def test_match_caption_dedupes_overlaps_and_keeps_order():
    tgt = "座禅と坐禅、ざぜん、ザゼン"
    found = jtc.match_caption("坐禪", jtc.fold("坐禪"), ["坐禅", "ざぜん", "ザゼン"], tgt, jtc.fold(tgt))
    assert found == ["座禅", "坐禅", "ざぜん", "ザゼン"]


def test_explicit_spellings_add_both_kana_scripts_and_skip_one_kana():
    out = jtc._explicit_spellings("坐禪", ["座禅", "ざぜん", "ぜ"], {jtc.fold("坐禪"): {"ザゼン"}})
    assert out == ["座禅", "ざぜん", "ザゼン"]


# --------------------------------------------------------------------------- captions store
def test_captions_db_inconsistent_kanji_variants(tmp_path, capsys):
    db = captions_db(tmp_path / "captions.sqlite3", {("r1", "s1"): ZAZEN})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == jtc.EXIT_INCONSISTENT == 1
    assert set(rep) == REPORT_KEYS
    assert rep["status"] == "inconsistent" and rep["exit_code"] == 1
    assert rep["tool"] == "ja_term_consistency_check"
    (item,) = rep["inconsistencies"]
    assert item["term"] == "坐禪" and item["term_origin"] == "auto"
    assert item["session"] == "r1/s1" and item["room_id"] == "r1" and item["session_id"] == "s1"
    assert [(v["surface"], v["count"], v["kind"]) for v in item["variants"]] == [
        ("坐禅", 3, "kanji"), ("座禅", 1, "kanji")]
    assert item["suggested"] == "坐禅" and item["suggested_count"] == 3
    assert item["fix_caption_ids"] == ["r1:s1:2"] and item["fix_count"] == 1
    assert item["mixed_script"] is False
    assert rep["inputs"][0]["schema"] == "captions"
    assert rep["sessions"][0]["lang_basis"] == "kana_ratio"
    assert "發現 1 個譯名不一致（exit 1）" in out
    assert "坐禪（自動）→ 坐禅×3（漢字）、座禅×1（漢字）" in out
    assert "建議統一為「坐禅」" in out
    assert "JSON 報告：" in out


def test_captions_db_consistent_is_ok(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): CONSISTENT})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == jtc.EXIT_OK == 0
    assert rep["status"] == "ok" and rep["inconsistencies"] == [] and rep["warnings"] == []
    assert rep["summary"]["term_pairs_checked"] >= 1
    assert "日文譯名一致（exit 0）" in out


def test_captions_db_ignores_rows_not_ready(tmp_path, capsys):
    rows = CONSISTENT + [("坐禪很重要。", "座禅は大切です。", "error"),
                         ("坐禪很重要。", "座禅は大切です。", "zh_ready")]
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 0
    assert rep["inputs"][0]["captions"] == 3


def test_english_session_is_skipped_and_only_english_warns(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "en"): ENGLISH})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == jtc.EXIT_WARN == 2
    assert [w["code"] for w in rep["warnings"]] == ["no_ja_session"]
    assert rep["sessions"][0]["ja"] is False and rep["sessions"][0]["kana_ratio"] == 0.0
    assert "略過非日文場次 1 個" in out
    assert "--assume-ja" in out


def test_mixed_sessions_only_japanese_is_checked(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "en"): ENGLISH, ("r1", "ja"): ZAZEN})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert {s["session"]: s["ja"] for s in rep["sessions"]} == {"r1/en": False, "r1/ja": True}
    assert [i["session"] for i in rep["inconsistencies"]] == ["r1/ja"]


def test_assume_ja_treats_untagged_as_japanese(tmp_path, capsys):
    rows = [("坐禪。", "坐禅。"), ("坐禪。", "座禅。"), ("坐禪。", "坐禅。")]   # no kana at all
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys, "a.json")
    assert code == 2 and rep["sessions"][0]["ja"] is False
    code, rep, _ = run_json(["--db", db, "--assume-ja"], tmp_path, capsys, "b.json")
    assert code == 1 and rep["sessions"][0]["lang_basis"] == "assumed"


def test_same_term_different_spelling_in_different_sessions_is_fine(tmp_path, capsys):
    s1 = [("坐禪。", "坐禅です。")] * 3
    s2 = [("坐禪。", "座禅です。")] * 3
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s1"): s1, ("r", "s2"): s2})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 0
    assert all(s["terms_checked"] >= 1 for s in rep["sessions"])


def test_room_and_session_filters(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "bad"): ZAZEN, ("r1", "good"): CONSISTENT,
                                                ("r2", "bad"): ZAZEN})
    code, rep, _ = run_json(["--db", db, "--session", "good"], tmp_path, capsys, "a.json")
    assert code == 0 and [s["session"] for s in rep["sessions"]] == ["r1/good"]
    code, rep, _ = run_json(["--db", db, "--room", "r2"], tmp_path, capsys, "b.json")
    assert code == 1 and [i["session"] for i in rep["inconsistencies"]] == ["r2/bad"]
    assert rep["settings"]["room"] == "r2"


def test_traditional_vs_shinjitai_and_tie_break_first_seen(tmp_path, capsys):
    rows = [("一切眾生皆有佛性。", "一切衆生には皆佛性があります。"),
            ("佛性人人本具。", "仏性は誰にでも備わっています。"),
            ("見性就是見佛性。", "見性とは仏性を見ることです。"),
            ("佛性不生不滅。", "佛性は生じも滅しもしません。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db, "--min-count", "4"], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert item["term"] == "佛性"
    assert [(v["surface"], v["count"]) for v in item["variants"]] == [("佛性", 2), ("仏性", 2)]
    assert item["suggested"] == "佛性"                       # tie -> first seen
    assert item["fix_caption_ids"] == ["r:s:1", "r:s:2"]


def test_min_count_controls_auto_extraction(tmp_path, capsys):
    rows = [("公案。", "公案です。"), ("公案。", "公按です。"),
            ("坐禪。", "坐禅です。"), ("坐禪。", "座禅です。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db], tmp_path, capsys, "a.json")
    assert code == 2 and [w["code"] for w in rep["warnings"]] == ["insufficient_terms"]
    assert "--min-count（目前 3）" in out
    code, rep, _ = run_json(["--db", db, "--min-count", "2"], tmp_path, capsys, "b.json")
    assert code == 1 and [i["term"] for i in rep["inconsistencies"]] == ["坐禪"]


def test_auto_terms_drop_ngrams_always_inside_a_longer_candidate(tmp_path, capsys):
    rows = [("參加坐禪會。", "坐禅会に参加。"), ("坐禪會開始。", "座禅会が始まる。"),
            ("坐禪會結束。", "坐禅会が終わる。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert [i["term"] for i in rep["inconsistencies"]] == ["坐禪會"]
    sess = jtc.Session(source="x", room_id="r", session_id="s", label="r/s")
    for i, (zh, ja) in enumerate(rows):
        sess.captions.append(jtc.Caption(cid=str(i), src=zh, tgt=ja, order=i))
    terms = jtc.auto_terms(sess, [jtc.fold(c.tgt) for c in sess.captions], 2, 4, 3)
    assert "坐禪會" in terms and "坐禪" not in terms and "禪會" not in terms


def test_shorter_term_report_dropped_when_longer_covers_it(tmp_path, capsys):
    rows = [("坐禪會。", "坐禅会です。"), ("坐禪會。", "座禅会です。"), ("坐禪會。", "坐禅会です。"),
            ("坐禪。", "坐禅。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert [i["term"] for i in rep["inconsistencies"]] == ["坐禪會"]


def test_unmatched_captions_are_counted_not_inconsistent(tmp_path, capsys):
    rows = ZAZEN + [("坐禪就是安靜地坐著。", "静かに座ることです。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert item["unmatched_captions"] == 1 and item["captions_with_term"] == 5
    assert "另有 1 筆日文找不到對應寫法" in out
    assert any("意譯或漏譯" in s for s in rep["suggestions"])


def test_halfwidth_katakana_is_not_a_separate_spelling(tmp_path, capsys):
    rows = [("禪修。", "ゼンシュウ。"), ("禪修。", "ｾﾞﾝｼｭｳ。"), ("禪修。", "ゼンシュウ。")]
    terms = tmp_path / "terms.txt"
    terms.write_text("禪修 = ゼンシュウ\n", encoding="utf-8")
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db, "--terms", terms], tmp_path, capsys)
    assert code == 0 and rep["summary"]["term_pairs_checked"] == 1


def test_inline_ruby_reading_finds_kana_spelling(tmp_path, capsys):
    rows = [("我們坐禪。", "{坐禅|ざぜん}をします。"), ("坐禪結束。", "ざぜんは終わりです。"),
            ("繼續坐禪。", "坐禅を続けます。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert [(v["surface"], v["kind"]) for v in item["variants"]] == [("坐禅", "kanji"), ("ざぜん", "hiragana")]
    assert item["mixed_script"] is True and item["kinds"] == ["hiragana", "kanji"]
    assert any("時而漢字、時而假名" in s for s in rep["suggestions"])
    assert "（平假名）" in out


def test_single_occurrence_is_insufficient_data(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): [("坐禪。", "坐禅です。")]})
    code, rep, _ = run_json(["--db", db, "--min-count", "1"], tmp_path, capsys)
    assert code == 2 and rep["status"] == "warn"
    assert rep["summary"]["term_pairs_checked"] == 0


def test_db_without_captions_rows_warns(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 2 and rep["summary"]["ja_sessions"] == 0


# --------------------------------------------------------------------------- ledger
def test_ledger_real_schema_selects_current_ok_ja_translations(tmp_path, capsys):
    db = ledger_db(tmp_path / "zen.sqlite3")
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert rep["inputs"][0]["schema"] == "ledger"
    assert rep["inputs"][0]["captions"] == 3
    sessions = {s["session"]: s for s in rep["sessions"]}
    assert sessions["r/ja1"]["ja"] is True and sessions["r/ja1"]["lang_basis"] == "tgt_lang"
    assert sessions["r/ja1"]["skipped_non_ja_rows"] == 1          # j5 en row
    assert sessions["r/en1"]["ja"] is False and sessions["r/en1"]["skipped_non_ja_rows"] == 2
    (item,) = rep["inconsistencies"]
    assert item["term"] == "佛性"
    assert [(v["surface"], v["count"]) for v in item["variants"]] == [("仏性", 2), ("佛性", 1)]
    assert item["fix_caption_ids"] == ["j3"]
    assert "資料庫 ledger" in out


def test_ledger_redacted_rows_are_ignored(tmp_path, capsys):
    db = ledger_db(tmp_path / "zen.sqlite3")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE translations SET origin='redacted' WHERE segment_id='j3'")
        conn.commit()
    code, rep, _ = run_json(["--db", db, "--min-count", "2"], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["captions"] == 2


def test_minimal_ledger_without_optional_columns(tmp_path, capsys):
    db = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE segments (id TEXT, room_id TEXT, session_id TEXT, seq INTEGER)")
        conn.execute("CREATE TABLE transcripts (segment_id TEXT, text TEXT)")
        conn.execute("CREATE TABLE translations (segment_id TEXT, text TEXT)")
        for i, (zh, ja) in enumerate(ZAZEN):
            conn.execute("INSERT INTO segments VALUES (?, 'r', 's', ?)", (f"g{i}", i))
            conn.execute("INSERT INTO transcripts VALUES (?, ?)", (f"g{i}", zh))
            conn.execute("INSERT INTO translations VALUES (?, ?)", (f"g{i}", ja))
        conn.commit()
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1 and rep["sessions"][0]["lang_basis"] == "kana_ratio"


def test_wal_database_with_shm_is_readable(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("UPDATE captions SET updated_at=2.0")
        holder.commit()
        assert Path(str(db) + "-wal").exists() and Path(str(db) + "-shm").exists()
        code, out, _err = run(["--db", db], capsys)
    finally:
        holder.close()
    assert code == 1 and "座禅×1" in out


def test_inputs_not_modified_and_output_deterministic(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): ZAZEN})
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja) for i, (zh, ja) in enumerate(ZAZEN)])
    before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (db, js)}
    run(["--db", db, "--jsonl", js, "--json", tmp_path / "a.json"], capsys)
    run(["--db", db, "--jsonl", js, "--json", tmp_path / "b.json"], capsys)
    for p, (data, mtime) in before.items():
        assert p.read_bytes() == data and os.stat(p).st_mtime_ns == mtime
    assert not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists()
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


# --------------------------------------------------------------------------- JSONL
def test_jsonl_bus_events_last_revision_wins_and_drafts_skipped(tmp_path, capsys):
    events = [bus(0, "坐禪。", "坐禅です。"), bus(1, "坐禪。", "座禅です。", version=1),
              bus(1, "坐禪。", "坐禅です。", version=2),                 # corrected later
              bus(2, "坐禪。", "座禅です。", typ="draft"),
              bus(3, "坐禪。", "坐禅です。"),
              bus(4, "坐禪。", "座禅です。", version=3), bus(4, "坐禪。", "坐禅です。", version=2)]  # older arrives late
    js = write_jsonl(tmp_path / "events.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert [(v["surface"], v["count"]) for v in item["variants"]] == [("坐禅", 3), ("座禅", 1)]
    assert item["fix_caption_ids"] == ["r1:s1:4"]
    assert rep["inputs"][0]["skipped_events"] == 1
    assert rep["sessions"][0]["lang_basis"] == "tgt_lang"


def test_jsonl_equal_version_later_line_wins(tmp_path, capsys):
    events = [bus(0, "坐禪。", "座禅です。"), bus(0, "坐禪。", "坐禅です。"),
              bus(1, "坐禪。", "坐禅です。"), bus(2, "坐禪。", "坐禅です。")]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["captions"] == 3


def test_jsonl_english_tagged_events_are_skipped(tmp_path, capsys):
    events = [bus(i, zh, en, tgt_lang="en") for i, (zh, en) in enumerate(ENGLISH)]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 2 and rep["sessions"][0]["skipped_non_ja_rows"] == 3


def test_jsonl_ruby_event_list_supplies_kana_reading(tmp_path, capsys):
    events = [bus(0, "坐禪。", "坐禅です。", ruby=[{"text": "坐禅", "reading": "ざぜん"}]),
              bus(1, "坐禪。", "ザゼンです。"), bus(2, "坐禪。", "坐禅です。")]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert [(v["surface"], v["kind"]) for v in item["variants"]] == [("坐禅", "kanji"), ("ザゼン", "katakana")]


def test_jsonl_generic_keys_and_one_session_per_file(tmp_path, capsys):
    a = write_jsonl(tmp_path / "a.jsonl", [{"source": zh, "ja": ja, "seq": i} for i, (zh, ja) in enumerate(ZAZEN)])
    b = write_jsonl(tmp_path / "b.jsonl", [{"source_text": zh, "translation": ja}
                                           for zh, ja in CONSISTENT])
    code, rep, out = run_json(["--jsonl", a, "--jsonl", b], tmp_path, capsys)
    assert code == 1
    assert sorted(s["session"] for s in rep["sessions"]) == ["a.jsonl", "b.jsonl"]
    (item,) = rep["inconsistencies"]
    assert item["session"] == "a.jsonl" and item["fix_caption_ids"] == ["seq 2"]
    assert "[a.jsonl]" in out


def test_jsonl_bom_and_crlf_are_accepted(tmp_path, capsys):
    path = tmp_path / "e.jsonl"
    lines = [json.dumps(bus(i, zh, ja), ensure_ascii=False) for i, (zh, ja) in enumerate(ZAZEN)]
    path.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8") + b"\r\n")
    code, _out, _err = run(["--jsonl", path], capsys)
    assert code == 1


def test_jsonl_line_separator_inside_string_is_not_a_line_break(tmp_path, capsys):
    events = [bus(i, zh, ja + "\u2028") for i, (zh, ja) in enumerate(CONSISTENT)]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    assert "\u2028" in js.read_text(encoding="utf-8")
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["bad_lines"] == 0 and rep["inputs"][0]["captions"] == 3


def test_jsonl_bad_lines_warn_but_inconsistency_wins(tmp_path, capsys):
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja) for i, (zh, ja) in enumerate(ZAZEN)],
                     extra_lines=["{not json", "[1, 2]", "   "])
    code, rep, out = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1
    assert [w["code"] for w in rep["warnings"]] == ["bad_jsonl_lines"]
    assert rep["inputs"][0]["bad_lines"] == 1 and rep["inputs"][0]["skipped_events"] == 1
    assert "警告：JSONL 有 1 行不是合法 JSON" in out


def test_jsonl_bad_lines_without_inconsistency_is_warn(tmp_path, capsys):
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja) for i, (zh, ja) in enumerate(CONSISTENT)],
                     extra_lines=["{broken"])
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 2 and rep["status"] == "warn"


def test_jsonl_status_error_and_control_events_skipped(tmp_path, capsys):
    events = [bus(i, zh, ja) for i, (zh, ja) in enumerate(CONSISTENT)]
    events += [{"type": "paused", "room_id": "r1", "session_id": "s1"},
               bus(9, "坐禪。", "座禅です。", status="error")]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["skipped_events"] == 2


def test_db_and_jsonl_together(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): CONSISTENT})
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja) for i, (zh, ja) in enumerate(ZAZEN)])
    code, rep, _ = run_json(["--db", db, "--jsonl", js], tmp_path, capsys)
    assert code == 1
    assert [i["source"] for i in rep["inconsistencies"]] == ["jsonl:e.jsonl"]
    assert [i["kind"] for i in rep["inputs"]] == ["db", "jsonl"]


# --------------------------------------------------------------------------- --terms
def test_terms_file_spellings_comments_bom_and_tab(tmp_path, capsys):
    rows = [("禪是什麼？", "禅とは何か。"), ("禪就是禪。", "ゼンはゼンです。"), ("禪。", "禅。")]
    terms = tmp_path / "terms.txt"
    terms.write_bytes(b"\xef\xbb\xbf" + "# 禪學詞語\n\n禪\tぜん\n公案 = 公案 | こうあん\n".encode("utf-8"))
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db, "--terms", terms], tmp_path, capsys)
    assert code == 1
    (item,) = rep["inconsistencies"]
    assert item["term"] == "禪" and item["term_origin"] == "list"
    assert [(v["surface"], v["count"]) for v in item["variants"]] == [("禅", 2), ("ゼン", 2)]
    assert rep["summary"]["terms_listed"] == 2
    assert "公案" in " ".join(rep["notes"])               # listed but never seen
    assert "禪（清單）" in out


def test_listed_term_checked_below_min_count(tmp_path, capsys):
    rows = [("公案。", "公案です。"), ("公案。", "公按です。")]
    terms = tmp_path / "t.txt"
    terms.write_text("公案=公按\n", encoding="utf-8")
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db, "--terms", terms, "--no-auto-terms"], tmp_path, capsys)
    assert code == 1 and rep["settings"]["auto_terms"] is False
    assert [v["surface"] for v in rep["inconsistencies"][0]["variants"]] == ["公案", "公按"]


def test_no_auto_terms_ignores_frequent_ngrams(tmp_path, capsys):
    terms = tmp_path / "t.txt"
    terms.write_text("公案\n", encoding="utf-8")
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    code, rep, _ = run_json(["--db", db, "--terms", terms, "--no-auto-terms"], tmp_path, capsys)
    assert code == 2 and rep["inconsistencies"] == []


@pytest.mark.parametrize("content,needle", [
    ("# only comments\n\n", "沒有任何詞"),
    ("zazen = 坐禅\n", "第 1 行沒有中文詞"),
    ("坐禪\n= 坐禅\n", "第 2 行沒有中文詞"),
])
def test_bad_terms_file_is_input_error(tmp_path, capsys, content, needle):
    terms = tmp_path / "t.txt"
    terms.write_text(content, encoding="utf-8")
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    code, _out, err = run(["--db", db, "--terms", terms], capsys)
    assert code == jtc.EXIT_INPUT == 3 and needle in err


def test_terms_file_not_utf8_or_missing(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    terms = tmp_path / "t.txt"
    terms.write_bytes("坐禪\n".encode("cp950"))
    code, _out, err = run(["--db", db, "--terms", terms], capsys)
    assert code == 3 and "不是 UTF-8" in err
    code, _out, err = run(["--db", db, "--terms", tmp_path / "nope.txt"], capsys)
    assert code == 3 and "找不到詞語清單" in err


# --------------------------------------------------------------------------- errors / output
def test_json_refuses_overwrite(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    out_path = tmp_path / "exists.json"
    out_path.write_text("keep", encoding="utf-8")
    code, out, err = run(["--db", db, "--json", out_path], capsys)
    assert code == jtc.EXIT_OUTPUT == 4 and "拒絕覆蓋" in err and out == ""
    assert out_path.read_text(encoding="utf-8") == "keep"


def test_json_unwritable_directory_is_output_error(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): ZAZEN})
    code, _out, err = run(["--db", db, "--json", tmp_path / "missing" / "r.json"], capsys)
    assert code == 4 and "無法寫入" in err


def test_write_new_refuses_existing_file_directly(tmp_path):
    p = tmp_path / "r.json"
    assert jtc._write_new(p, {"a": 1}) is None
    assert "拒絕覆蓋" in jtc._write_new(p, {"a": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


def test_input_errors(tmp_path, capsys):
    not_db = tmp_path / "x.sqlite3"
    not_db.write_bytes(b"this is not sqlite" * 100)
    other = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("create table foo (a)")
        conn.commit()
    partial = tmp_path / "partial.sqlite3"
    with closing(sqlite3.connect(partial)) as conn:
        conn.execute("create table captions (id text, room_id text, session_id text, seq int)")
        conn.commit()
    wal_only = captions_db(tmp_path / "w.sqlite3", {("r", "s"): ZAZEN})
    Path(str(wal_only) + "-wal").write_bytes(b"")
    bad_utf8 = tmp_path / "bad.jsonl"
    bad_utf8.write_bytes(json.dumps({"zh": "坐禪", "ja": "坐禅"}, ensure_ascii=False).encode("cp932"))
    cases = [
        (["--db", tmp_path / "missing.sqlite3"], "找不到資料庫"),
        (["--db", not_db], "無法唯讀開啟資料庫"),
        (["--db", other], "不支援的資料庫結構"),
        (["--db", partial], "captions 表缺欄位"),
        (["--db", wal_only], "checkpoint"),
        (["--jsonl", tmp_path / "missing.jsonl"], "找不到 JSONL"),
        (["--jsonl", bad_utf8], "不是 UTF-8"),
        (["--jsonl", tmp_path], "找不到 JSONL"),
    ]
    for argv, needle in cases:
        code, _out, err = run(argv, capsys)
        assert code == 3, argv
        assert needle in err, (argv, err)


@pytest.mark.parametrize("argv", [
    [],
    ["--db"],
    ["--min-count", "0", "--jsonl", "x.jsonl"],
    ["--min-len", "0", "--jsonl", "x.jsonl"],
    ["--min-len", "3", "--max-len", "2", "--jsonl", "x.jsonl"],
    ["--max-len", "9", "--jsonl", "x.jsonl"],
    ["--max-examples", "0", "--jsonl", "x.jsonl"],
    ["--min-count", "abc", "--jsonl", "x.jsonl"],
    ["--no-auto-terms", "--jsonl", "x.jsonl"],
    ["--bogus", "--jsonl", "x.jsonl"],
    ["--jsonl", "x.jsonl", "--json", "x.jsonl"],
])
def test_argparse_errors_exit_3(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        jtc.main(argv)
    assert exc.value.code == 3
    assert "參數錯誤" in capsys.readouterr().err


def test_max_examples_limits_caption_ids(tmp_path, capsys):
    rows = [("坐禪。", "坐禅です。")] * 5 + [("坐禪。", "座禅です。")] * 4
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db, "--max-examples", "1"], tmp_path, capsys)
    assert code == 1
    item = rep["inconsistencies"][0]
    assert all(len(v["caption_ids"]) == 1 for v in item["variants"])
    assert item["fix_count"] == 4 and len(item["fix_caption_ids"]) == 3
    assert "需檢查字幕 4 筆" in out and "…" in out


def test_cli_subprocess(tmp_path):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): ZAZEN})
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, str(TOOL), "--db", str(db)], capture_output=True,
                          env=env, cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 1
    out = proc.stdout.decode("utf-8")
    assert "日文譯名一致性檢查" in out and "座禅" in out


# --------------------------------------------------------------------------- static checks
def test_stdlib_only_no_network_no_app_no_clock():
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert mods <= stdlib | {"__future__"}, mods - stdlib
    assert not mods & {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "ssl",
                       "subprocess", "asyncio", "smtplib", "xmlrpc", "app", "time", "datetime"}
    assert "8645" not in src
    for word in ("urlopen", "http://", "https://", ".now(", "utcnow", "localtime", "time.time"):
        assert word not in src


def test_does_not_touch_repo_term_files_and_opens_db_read_only():
    src = TOOL.read_text(encoding="utf-8")
    low = src.lower()
    assert "glossary" not in low and "dictionary" not in low
    assert "dictionary-data" not in low
    assert '"?mode=ro"' in src and "uri=True" in src
    assert 'open(path, "x"' in src
    for word in ("INSERT", "UPDATE ", "DELETE", "DROP", "CREATE", "ALTER", "VACUUM", ".write_text(", ".unlink("):
        assert word not in src


def test_shebang_and_docstring_document_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for code in ("``ok``", "``inconsistent``", "``warn``", "(input)", "(output)", "mode=ro"):
        assert code in doc
    assert (jtc.EXIT_OK, jtc.EXIT_INCONSISTENT, jtc.EXIT_WARN, jtc.EXIT_INPUT, jtc.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
