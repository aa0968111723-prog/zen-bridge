"""Tests for tools/en_ja_untranslated_span_check.py (queue item 41).

Every input is a small fake caption store / ledger SQLite file or caption JSONL
under tmp_path, written with explicit encodings.  The ledger is built from the
real app/admin/schema.sql DDL.  Nothing depends on the network, ports, POSIX
permission bits, the local time zone or the clock.
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
TOOL = ROOT / "tools" / "en_ja_untranslated_span_check.py"
SCHEMA = ROOT / "app" / "admin" / "schema.sql"

sys.path.insert(0, str(ROOT / "tools"))
import en_ja_untranslated_span_check as eju  # noqa: E402

REPORT_KEYS = {"tool", "schema_version", "status", "exit_code", "settings", "inputs", "sessions",
               "summary", "issues", "issues_total", "issues_truncated", "warnings", "notes",
               "suggestions", "summary_zh"}
SETTINGS = {"same_ratio": 0.85, "en_min_ratio": 1.0, "ja_min_ratio": 0.5, "min_src_chars": 6,
            "ja_min_copy": 8, "pinyin_run": 3}

EN_CLEAN = [
    ("今天我們一起坐禪。", "Today we sit in zazen together."),
    ("坐禪的時候要放鬆身體。", "Relax your body during zazen."),
    ("請大家慢慢地呼吸。", "Please breathe slowly, everyone."),
]
JA_CLEAN = [
    ("今天我們一起坐禪。", "今日は皆で坐禅をします。"),
    ("坐禪的時候要放鬆身體。", "坐禅のときは体の力を抜きましょう。"),
    ("請大家慢慢地呼吸。", "皆さん、ゆっくり呼吸してください。"),
]
EN_BAD = [
    ("今天我們一起坐禪。", "Today we sit in zazen together."),                 # 0 clean
    ("請老師開示公案。", "Please ask the teacher to explain the 公案."),      # 1 residual_zh
    ("我們一起坐禪。", "Let us wo men yi qi zuo chan now."),                  # 2 pinyin
    ("一切眾生皆有佛性。", "一切眾生皆有佛性。"),                              # 3 untranslated
    ("坐禪的時候要放鬆身體，慢慢地呼吸。", "Relax."),                          # 4 too_short
    ("請大家慢慢地呼吸。", ""),                                               # 5 empty
]
JA_BAD = [
    ("今天我們一起坐禪。", "今日は皆で坐禅をします。"),                         # 0 clean
    ("今天我們一起坐禪練習。", "今日は我們と坐禅の練習をします。"),             # 1 residual (們)
    ("一切眾生皆有佛性。", "一切眾生皆有佛性。"),                              # 2 untranslated
    ("坐禪的時候要放鬆身體，慢慢地呼吸。", "坐禅です。"),                      # 3 too_short
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
                ("e1", "en1", 1, "translated"), ("e2", "en1", 2, "translated"),
                ("e3", "en1", 3, "asr_done")]
        conn.executemany(
            "INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status, created_at)"
            " VALUES (?, ?, 'r', ?, 0, 1000, ?, 0)", segs)
        zh = {"j1": "今天我們一起坐禪。", "j2": "一切眾生皆有佛性。", "j3": "請大家慢慢地呼吸。",
              "j4": "這句被刪除了。", "e1": "今天我們一起坐禪。", "e2": "請老師開示公案。",
              "e3": "還沒翻譯。"}
        for sid, text in zh.items():
            conn.execute("INSERT INTO transcripts(segment_id, version, is_current, text, created_at)"
                         " VALUES (?, 1, 1, ?, 0)", (sid, text))
        conn.executemany(
            "INSERT INTO translations(segment_id, tgt_lang, version, is_current, text, status, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [("j1", "ja", 1, 0, "今天我們一起坐禪。", "ok", "mt"),                    # old version
             ("j1", "ja", 2, 1, "今日は皆で坐禅をします。", "ok", "post_edit"),
             ("j2", "ja", 1, 1, "一切眾生皆有佛性。", "merged", "mt"),               # untranslated
             ("j3", "ja", 1, 1, "皆さん、ゆっくり呼吸してください。", "error", "mt"),  # failed -> skipped
             ("j3", "ko", 1, 1, "여러분 천천히 숨을 쉬세요.", "ok", "mt"),             # other language
             ("j4", "ja", 1, 1, "這句被刪除了。", "ok", "mt"),                       # deleted segment
             ("e1", "en", 1, 1, "Today we sit in zazen together.", "ok", "mt"),
             ("e1", "ja", 1, 1, "今日は皆で坐禅をします。", "ok", "mt"),              # second target
             ("e2", "en", 1, 1, "Please ask the teacher about the 公案.", "ok", "mt"),
             ("e3", "en", 1, 1, "還沒翻譯。", "ok", "mt")])                          # segment not translated
        conn.commit()
    return path


def write_jsonl(path: Path, events: list, extra_lines: list[str] | None = None) -> Path:
    lines = [json.dumps(e, ensure_ascii=False) for e in events] + list(extra_lines or [])
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def bus(seq: int, zh: str, en: str, *, room: str = "r1", sess: str = "s1", version: int = 1,
        typ: str = "final", tgt_lang: str | None = None, **extra) -> dict:
    ev = {"type": typ, "id": f"{room}:{sess}:{seq}", "room_id": room, "session_id": sess, "seq": seq,
          "version": version, "zh": zh, "en": en, "status": "ready"}
    if tgt_lang is not None:
        ev["tgt_lang"] = tgt_lang
    ev.update(extra)
    return ev


def run(argv: list, capsys) -> tuple[int, str, str]:
    code = eju.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, out, err


def run_json(argv: list, tmp_path: Path, capsys, name: str = "report.json") -> tuple[int, dict, str]:
    out_path = tmp_path / name
    code, out, _err = run([*argv, "--json", out_path], capsys)
    return code, json.loads(out_path.read_text(encoding="utf-8")), out


def caption(src: str, tgt: str, lang: str) -> eju.Caption:
    return eju.Caption(cid="x", src=eju.nfkc(src), tgt=eju.visible_target(tgt), order=0, lang=lang)


def kinds(rep: dict) -> dict[str, list[str]]:
    return {i["id"]: i["kinds"] for i in rep["issues"]}


# --------------------------------------------------------------------------- tables / helpers
def test_character_tables_are_consistent():
    assert not eju.ZH_ONLY & eju.TRAD_FORM
    heads: dict[str, list[str]] = {}
    for ch, head in eju.FOLD.items():
        heads.setdefault(head, []).append(ch)
    for ch in eju.TRAD_FORM:                     # every traditional form has a different Japanese form
        assert ch in eju.FOLD and len(heads[eju.FOLD[ch]]) >= 2, ch
    seen: set[str] = set()
    for group in eju._VARIANT_GROUPS.split():
        assert len(group) >= 2
        for ch in group:
            assert ch not in seen
            seen.add(ch)
    assert eju.fold("坐禪") == eju.fold("坐禅") == eju.fold("座禅")
    assert eju.fold("說經") == eju.fold("説経") == eju.fold("说经")
    for word in ("一切眾生", "ざぜん", "abc"):
        assert len(eju.fold(word)) == len(word)


@pytest.mark.parametrize("text", [
    "今日は皆で坐禅をします。", "只管打坐とは、ただひたすら坐ることです。", "一切衆生悉有仏性という言葉があります。",
    "老師に仏性の開示をお願いします。", "私たちは毎朝六時に起きて、禅堂で坐禅を組みます。", "南無阿弥陀仏",
    "会議室の机の上に資料があります。", "準備ができたら始めましょう。", "東京駅から電車で一時間かかります。",
    "坐禪の会", "道元禅師は『正法眼蔵』を書きました。", "彼は誰ですか？", "臨済宗と曹洞宗の違いを説明します。",
    "経典を読誦する時間です。", "公案は禅の修行に使われます。", "人々は静かに坐っています。",
])
def test_ordinary_japanese_kanji_is_not_residual_chinese(text):
    c = caption("今天我們一起坐禪練習。", text, "ja")
    assert [i for i in eju.check_caption(c, [], SETTINGS) if i["kind"] != "too_short"] == []


def test_lang_tag():
    assert eju.lang_tag("ja") == "ja" and eju.lang_tag("ja-JP") == "ja" and eju.lang_tag("JA_jp") == "ja"
    assert eju.lang_tag("en") == "en" and eju.lang_tag("en-US") == "en"
    assert eju.lang_tag("ko") == "other"
    assert eju.lang_tag("") is None and eju.lang_tag(None) is None and eju.lang_tag(3) is None


def test_visible_target_and_content():
    assert eju.visible_target("{坐禅|ざぜん}を<wbr>続け<WBR/>ます ｻﾞｾﾞﾝ") == "坐禅を続けます ザゼン"
    assert eju.content("Hello, 世界！ 123") == "Hello世界123"


@pytest.mark.parametrize("word,expected", [
    ("Chán", True), ("zuò", True), ("Xiǎohuá", True), ("nǚ", True), ("lǜ", True),
    ("Dōgen", False), ("kōan", False), ("café", False), ("résumé", False), ("chan", False),
    ("Zen", False), ("naïve", False), ("Ōbaku", True),
])
def test_is_toned_pinyin(word, expected):
    assert eju.is_toned_pinyin(word) is expected


def test_pinyin_split():
    assert eju.pinyin_split("xiaohua") == ["xiao", "hua"]
    assert eju.pinyin_split("zuochan") == ["zuo", "chan"]
    assert eju.pinyin_split("dogen") is None


@pytest.mark.parametrize("text,lang,spans", [
    ("Let us wo men yi qi zuo chan now.", "en", [("wo men yi qi zuo chan", ["syllable_run"])]),
    ("He can run.", "en", []),                               # all common English words
    ("you can sit", "en", []),
    ("ni hao everyone", "en", []),                           # only two syllables
    ("This is Chán practice.", "en", [("Chán", ["tone_marks"])]),
    ("Dōgen wrote about kōan practice.", "en", []),
    ("Zen and Chan are the same.", "en", []),
    ("xiao-hua shuo hao", "en", [("xiao-hua shuo hao", ["syllable_run"])]),
    ("禅は zuo chan shi です", "ja", []),                     # toneless runs only matter in English
    ("禅は Chán です", "ja", [("Chán", ["tone_marks"])]),
])
def test_pinyin_spans(text, lang, spans):
    found = eju.pinyin_spans(lang, eju.nfkc(text), 3)
    assert [(s["text"], s["reasons"]) for s in found] == spans


def test_pinyin_run_length_option():
    assert eju.pinyin_spans("en", "ni hao everyone", 2)[0]["text"] == "ni hao"


def test_residual_spans_english():
    spans = eju.residual_spans("en", eju.nfkc("Sit 坐禪，打坐 then ㄅㄆ and 好."), "", 8)
    assert [(s["text"], s["reasons"]) for s in spans] == [("坐禪,打坐", ["han"]), ("ㄅㄆ", ["zhuyin"]),
                                                         ("好", ["han"])]


@pytest.mark.parametrize("tgt,reasons", [
    ("ㄗㄨㄛˋ禅をします", ["zhuyin"]),
    ("今日は我們と坐禅", ["zh_only_char"]),
    ("这是禅です", ["zh_only_char"]),
    ("坐禪會に参加", ["traditional_form"]),                   # two traditional forms
    ("大家放鬆身體です", ["zh_only_char", "traditional_form"]),  # 鬆 + one traditional form in a long run
    ("禅堂,坐禅道場です", ["zh_punct"]),
    ("只管打坐一如自在無礙です", ["copied_run"]),
])
def test_residual_spans_japanese_reasons(tgt, reasons):
    src_fold = eju.fold(eju.content("今天大家只管打坐一如自在無礙。"))
    (span,) = eju.residual_spans("ja", eju.nfkc(tgt), src_fold, 8)
    assert span["reasons"] == reasons


@pytest.mark.parametrize("tgt", ["坐禪の会", "坐禅をします", "只管打坐です", "公案、開示", "南無阿弥陀仏"])
def test_residual_spans_japanese_tolerates_kanji(tgt):
    src_fold = eju.fold(eju.content("今天坐禪只管打坐公案開示南無阿彌陀佛"))
    assert eju.residual_spans("ja", eju.nfkc(tgt), src_fold, 8) == []


def test_ja_min_copy_threshold():
    src_fold = eju.fold("只管打坐一如")
    assert eju.residual_spans("ja", "只管打坐一如です", src_fold, 8) == []
    assert eju.residual_spans("ja", "只管打坐一如です", src_fold, 6)[0]["reasons"] == ["copied_run"]


def test_mask_allowed_is_case_insensitive_and_length_preserving():
    out = eju.mask_allowed("Huineng met HUINENG and 慧能", ["huineng", "慧能"])
    assert len(out) == len("Huineng met HUINENG and 慧能")
    assert "Huineng" not in out and "HUINENG" not in out and "慧能" not in out and "met" in out


def test_untranslated_rules_en_and_ja():
    assert eju.check_caption(caption("一切眾生皆有佛性。", "一切眾生皆有佛性。", "en"), [], SETTINGS) == [
        {"kind": "untranslated", "similarity": 1.0}]
    # near-equal: one character changed
    (item,) = eju.check_caption(caption("一切眾生皆有佛性啊", "一切眾生皆有佛性呀", "en"), [], SETTINGS)
    assert item["kind"] == "untranslated" and 0.85 <= item["similarity"] < 1.0
    # Japanese pure-kanji rendering of a chant is fine, the unchanged Chinese is not
    assert eju.check_caption(caption("南無阿彌陀佛", "南無阿弥陀仏", "ja"), [], SETTINGS) == []
    assert eju.check_caption(caption("南無阿彌陀佛", "南無阿彌陀佛", "ja"), [], SETTINGS)[0]["kind"] == "untranslated"
    assert eju.check_caption(caption("只管打坐", "只管打坐", "ja"), [], SETTINGS) == []
    # a source without Han characters is never "untranslated"
    assert eju.check_caption(caption("OK OK OK", "OK OK OK", "en"), [], SETTINGS) == []


def test_too_short_and_empty_rules():
    (item,) = eju.check_caption(caption("坐禪的時候要放鬆身體，慢慢地呼吸。", "Relax.", "en"), [], SETTINGS)
    assert item["kind"] == "too_short" and item["src_chars"] == 15 and item["tgt_chars"] == 5
    assert item["length_ratio"] == round(5 / 15, 4) and item["min_ratio"] == 1.0
    assert eju.check_caption(caption("好。", "OK.", "en"), [], SETTINGS) == []           # below --min-src-chars
    assert eju.check_caption(caption("坐禪", "  ", "ja"), [], SETTINGS) == [{"kind": "empty"}]


# --------------------------------------------------------------------------- captions store
def test_captions_db_english_issues_by_kind(tmp_path, capsys):
    db = captions_db(tmp_path / "captions.sqlite3", {("r1", "s1"): EN_BAD})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == eju.EXIT_ISSUES == 1
    assert set(rep) == REPORT_KEYS
    assert rep["tool"] == "en_ja_untranslated_span_check" and rep["status"] == "issues"
    assert kinds(rep) == {"r1:s1:1": ["residual_zh"], "r1:s1:2": ["pinyin"], "r1:s1:3": ["untranslated"],
                          "r1:s1:4": ["too_short"], "r1:s1:5": ["empty"]}
    en = rep["summary"]["by_lang"]["en"]
    assert (en["captions"], en["flagged"], en["sessions"]) == (6, 5, 1)
    assert [en[k] for k in eju.KINDS] == [1, 1, 1, 1, 1]
    assert en["flag_rate"] == round(5 / 6, 4)
    assert rep["summary"]["by_lang"]["ja"]["captions"] == 0
    assert rep["issues_total"] == 5 and rep["issues_truncated"] == 0
    residual = rep["issues"][0]["details"][0]
    assert residual["spans"] == [{"text": "公案", "start": 38, "reasons": ["han"]}]
    assert rep["sessions"][0]["default_lang"] == "en" and rep["sessions"][0]["lang_basis"] == "kana_ratio"
    assert "發現 5 筆疑似問題字幕（exit 1）" in out
    assert "英文（en）：1 個場次、檢查 6 筆、有問題 5 筆（83.3%）" in out
    assert "殘留中文 1、拼音 1、整句未翻 1、譯文過短 1、譯文空白 1" in out
    assert "日文（ja）：沒有字幕" in out
    assert "「公案」（漢字）" in out and "相似度 1.00" in out
    assert "JSON 報告：" in out


def test_captions_db_clean_sessions_are_ok(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "en"): EN_CLEAN, ("r1", "ja"): JA_CLEAN})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == eju.EXIT_OK == 0
    assert rep["status"] == "ok" and rep["issues"] == [] and rep["warnings"] == []
    by_lang = rep["summary"]["by_lang"]
    assert by_lang["en"]["captions"] == 3 and by_lang["ja"]["captions"] == 3
    assert {s["session"]: s["default_lang"] for s in rep["sessions"]} == {"r1/en": "en", "r1/ja": "ja"}
    assert "沒有發現漏譯或殘留中文（exit 0）" in out


def test_captions_db_japanese_session_counts_separately(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "en"): EN_CLEAN, ("r1", "ja"): JA_BAD})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert kinds(rep) == {"r1:ja:1": ["residual_zh"], "r1:ja:2": ["untranslated"], "r1:ja:3": ["too_short"]}
    ja = rep["summary"]["by_lang"]["ja"]
    assert (ja["captions"], ja["flagged"], ja["residual_zh"], ja["untranslated"], ja["too_short"]) == (4, 3, 1, 1, 1)
    assert rep["summary"]["by_lang"]["en"]["flagged"] == 0
    assert all(i["lang"] == "ja" for i in rep["issues"])
    assert "日文不用的中文字" in out


def test_captions_db_ignores_rows_not_ready(tmp_path, capsys):
    rows = EN_CLEAN + [("好。", "好。", "error"), ("坐禪", None, "zh_ready")]
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["captions"] == 3 and rep["inputs"][0]["rows"] == 3


def test_null_target_is_skipped_blank_target_is_empty(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_CLEAN + [("坐禪。", None), ("坐禪。", "")]})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1 and kinds(rep) == {"r:s:4": ["empty"]}


def test_assume_lang_for_untagged_sessions(tmp_path, capsys):
    rows = [("坐禪", "坐禅"), ("一切眾生皆有佛性。", "一切眾生皆有佛性。")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys, "a.json")
    assert code == 1 and rep["sessions"][0]["default_lang"] == "en"     # no kana -> English rules
    assert kinds(rep) == {"r:s:0": ["untranslated"], "r:s:1": ["untranslated"]}
    code, rep, _ = run_json(["--db", db, "--assume-lang", "ja"], tmp_path, capsys, "b.json")
    assert rep["sessions"][0]["lang_basis"] == "assumed"
    assert kinds(rep) == {"r:s:1": ["untranslated"]}                    # 坐禅 is fine in Japanese


def test_lang_filter_and_room_session_filters(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "en"): EN_BAD, ("r1", "ja"): JA_CLEAN,
                                                ("r2", "ja"): JA_BAD})
    code, rep, out = run_json(["--db", db, "--lang", "ja"], tmp_path, capsys, "a.json")
    assert code == 1 and {i["session"] for i in rep["issues"]} == {"r2/ja"}
    assert rep["summary"]["by_lang"]["en"]["captions"] == 0
    assert "依 --lang ja 略過其他語言字幕 6 筆" in out
    code, rep, _ = run_json(["--db", db, "--room", "r1", "--session", "ja"], tmp_path, capsys, "b.json")
    assert code == 0 and [s["session"] for s in rep["sessions"]] == ["r1/ja"]
    assert rep["settings"]["room"] == "r1" and rep["settings"]["session"] == "ja"


def test_thresholds_are_configurable(tmp_path, capsys):
    rows = [("坐禪的時候要放鬆身體。", "Relax your body."),                     # 13 letters / 10 chars
            ("一切眾生皆有佛性啊", "一切眾生皆有佛性呀")]                        # similarity 0.889
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys, "a.json")
    assert kinds(rep) == {"r:s:1": ["untranslated"]}
    code, rep, _ = run_json(["--db", db, "--en-min-ratio", "1.5", "--same-ratio", "0.95"], tmp_path, capsys,
                            "b.json")
    assert kinds(rep)["r:s:0"] == ["too_short"]
    assert "untranslated" not in kinds(rep)["r:s:1"] and "residual_zh" in kinds(rep)["r:s:1"]
    code, rep, _ = run_json(["--db", db, "--en-min-ratio", "1.5", "--min-src-chars", "11"], tmp_path, capsys,
                            "c.json")
    assert "r:s:0" not in kinds(rep)


def test_allow_file_masks_spans(tmp_path, capsys):
    rows = EN_CLEAN + [("請老師開示公案。", "Please ask the teacher to explain the 公案."),
                       ("南無阿彌陀佛。", "南無阿彌陀佛"),
                       ("這是小華說的。", "Xiǎohuá said so.")]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, _ = run_json(["--db", db], tmp_path, capsys, "a.json")
    assert code == 1 and set(kinds(rep)) == {"r:s:3", "r:s:4", "r:s:5"}
    allow = tmp_path / "allow.txt"
    allow.write_bytes(b"\xef\xbb\xbf" + "# 保留\n\n公案\n南無阿彌陀佛\nxiǎohuá\n".encode("utf-8"))
    code, rep, out = run_json(["--db", db, "--allow", allow], tmp_path, capsys, "b.json")
    assert code == 0 and rep["issues"] == []
    assert "允許清單 3 項" in out


@pytest.mark.parametrize("content,needle", [
    ("# only comments\n\n".encode("utf-8"), "沒有任何項目"),
    ("公案\n".encode("cp950"), "不是 UTF-8"),
])
def test_bad_allow_file_is_input_error(tmp_path, capsys, content, needle):
    allow = tmp_path / "allow.txt"
    allow.write_bytes(content)
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_CLEAN})
    code, _out, err = run(["--db", db, "--allow", allow], capsys)
    assert code == eju.EXIT_INPUT == 3 and needle in err
    code, _out, err = run(["--db", db, "--allow", tmp_path / "nope.txt"], capsys)
    assert code == 3 and "找不到允許清單" in err


def test_db_without_rows_is_insufficient_data(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {})
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == eju.EXIT_WARN == 2 and rep["status"] == "warn"
    assert [w["code"] for w in rep["warnings"]] == ["no_captions"]
    assert "資料不足或有警告（exit 2）" in out


# --------------------------------------------------------------------------- ledger
def test_ledger_real_schema(tmp_path, capsys):
    db = ledger_db(tmp_path / "zen.sqlite3")
    code, rep, out = run_json(["--db", db], tmp_path, capsys)
    assert code == 1
    assert rep["inputs"][0]["schema"] == "ledger" and rep["inputs"][0]["captions"] == 5
    by_lang = rep["summary"]["by_lang"]
    assert (by_lang["en"]["captions"], by_lang["en"]["flagged"]) == (2, 1)
    assert (by_lang["ja"]["captions"], by_lang["ja"]["flagged"]) == (3, 1)
    assert by_lang["ja"]["sessions"] == 2                      # e1 also has a ja translation
    assert sorted((i["id"], i["lang"], tuple(i["kinds"])) for i in rep["issues"]) == [
        ("e2", "en", ("residual_zh",)), ("j2", "ja", ("untranslated",))]
    sessions = {s["session"]: s for s in rep["sessions"]}
    assert sessions["r/ja1"]["lang_basis"] == "tgt_lang" and sessions["r/ja1"]["default_lang"] == "ja"
    assert sessions["r/ja1"]["skipped_other_lang_rows"] == 1   # the ko row
    assert sessions["r/en1"]["by_lang"] == {"en": {"captions": 2, "flagged": 1}, "ja": {"captions": 1, "flagged": 0}}
    assert "略過非英文／日文" in out and "資料庫 ledger" in out


def test_ledger_redacted_rows_are_ignored(tmp_path, capsys):
    db = ledger_db(tmp_path / "zen.sqlite3")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE translations SET origin='redacted' WHERE segment_id IN ('j2', 'e2')")
        conn.commit()
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["captions"] == 3


def test_minimal_ledger_without_optional_columns(tmp_path, capsys):
    db = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE segments (id TEXT, room_id TEXT, session_id TEXT, seq INTEGER)")
        conn.execute("CREATE TABLE transcripts (segment_id TEXT, text TEXT)")
        conn.execute("CREATE TABLE translations (segment_id TEXT, text TEXT)")
        for i, (zh, ja) in enumerate(JA_BAD):
            conn.execute("INSERT INTO segments VALUES (?, 'r', 's', ?)", (f"g{i}", i))
            conn.execute("INSERT INTO transcripts VALUES (?, ?)", (f"g{i}", zh))
            conn.execute("INSERT INTO translations VALUES (?, ?)", (f"g{i}", ja))
        conn.commit()
    code, rep, _ = run_json(["--db", db], tmp_path, capsys)
    assert code == 1 and rep["sessions"][0]["lang_basis"] == "kana_ratio"
    assert rep["sessions"][0]["default_lang"] == "ja" and len(rep["issues"]) == 3


def test_wal_database_with_shm_is_readable(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_BAD})
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("UPDATE captions SET updated_at=2.0")
        holder.commit()
        assert Path(str(db) + "-wal").exists() and Path(str(db) + "-shm").exists()
        code, out, _err = run(["--db", db], capsys)
    finally:
        holder.close()
    assert code == 1 and "公案" in out


def test_inputs_not_modified_and_output_deterministic(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): EN_BAD})
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja, tgt_lang="ja") for i, (zh, ja) in enumerate(JA_BAD)])
    before = {p: (p.read_bytes(), os.stat(p).st_mtime_ns) for p in (db, js)}
    run(["--db", db, "--jsonl", js, "--json", tmp_path / "a.json"], capsys)
    run(["--db", db, "--jsonl", js, "--json", tmp_path / "b.json"], capsys)
    for p, (data, mtime) in before.items():
        assert p.read_bytes() == data and os.stat(p).st_mtime_ns == mtime
    assert not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists()
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()


# --------------------------------------------------------------------------- JSONL
def test_jsonl_bus_events_last_revision_wins_and_drafts_skipped(tmp_path, capsys):
    events = [bus(0, "今天我們一起坐禪。", "今日は我們と坐禅。", tgt_lang="ja", version=1),
              bus(0, "今天我們一起坐禪。", "今日は皆で坐禅をします。", tgt_lang="ja", version=2),   # corrected
              bus(1, "請大家慢慢地呼吸。", "請大家慢慢地呼吸。", typ="draft", tgt_lang="ja"),
              bus(2, "一切眾生皆有佛性。", "一切眾生皆有佛性。", tgt_lang="ja", version=3),
              bus(2, "一切眾生皆有佛性。", "一切衆生には仏性がある。", tgt_lang="ja", version=2),  # older, late
              bus(0, "今天我們一起坐禪。", "Today we sit in 坐禪.", sess="en"),                 # untagged en
              {"type": "captions_cleared", "room_id": "r1"}]
    js = write_jsonl(tmp_path / "events.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1
    assert sorted((i["id"], i["lang"], tuple(i["kinds"])) for i in rep["issues"]) == [
        ("r1:en:0", "en", ("residual_zh",)), ("r1:s1:2", "ja", ("untranslated",))]
    assert rep["inputs"][0]["skipped_events"] == 2
    assert {s["session"]: s["lang_basis"] for s in rep["sessions"]} == {"r1/en": "kana_ratio", "r1/s1": "tgt_lang"}


def test_jsonl_untagged_rows_follow_session_majority(tmp_path, capsys):
    events = [bus(0, "坐禪。", "坐禅です。", tgt_lang="ja"), bus(1, "坐禪。", "坐禅です。", tgt_lang="ja"),
              bus(2, "坐禪。", "坐禅")]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 0 and rep["summary"]["by_lang"]["ja"]["captions"] == 3


def test_jsonl_generic_keys_tag_language_and_one_session_per_file(tmp_path, capsys):
    a = write_jsonl(tmp_path / "a.jsonl", [{"source": zh, "ja": ja, "seq": i} for i, (zh, ja) in enumerate(JA_BAD)])
    b = write_jsonl(tmp_path / "b.jsonl", [{"source_text": zh, "en_text": en} for zh, en in EN_CLEAN])
    code, rep, out = run_json(["--jsonl", a, "--jsonl", b], tmp_path, capsys)
    assert code == 1
    assert sorted(s["session"] for s in rep["sessions"]) == ["a.jsonl", "b.jsonl"]
    assert {s["session"]: s["lang_basis"] for s in rep["sessions"]} == {"a.jsonl": "tgt_lang", "b.jsonl": "tgt_lang"}
    assert {i["id"] for i in rep["issues"]} == {"seq 1", "seq 2", "seq 3"}
    assert rep["summary"]["by_lang"]["en"]["captions"] == 3
    assert "a.jsonl seq 1" in out


def test_jsonl_other_language_tag_is_skipped(tmp_path, capsys):
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, en, tgt_lang="ko") for i, (zh, en) in enumerate(EN_BAD)])
    code, rep, out = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 2 and rep["sessions"][0]["skipped_other_lang_rows"] == 6
    assert "略過非英文／日文（tgt_lang 為其他語言）的字幕 6 筆" in out


def test_jsonl_missing_target_key_skipped_blank_target_empty(tmp_path, capsys):
    events = [{"zh": "坐禪的時候要放鬆身體。", "seq": 0}, {"zh": "坐禪的時候要放鬆身體。", "en": " ", "seq": 1}]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1 and rep["inputs"][0]["captions"] == 1 and kinds(rep) == {"seq 1": ["empty"]}


def test_jsonl_bom_crlf_and_line_separator(tmp_path, capsys):
    path = tmp_path / "e.jsonl"
    lines = [json.dumps(bus(i, zh, en + "\u2028"), ensure_ascii=False) for i, (zh, en) in enumerate(EN_CLEAN)]
    path.write_bytes(b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8") + b"\r\n")
    code, rep, _ = run_json(["--jsonl", path], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["bad_lines"] == 0 and rep["inputs"][0]["captions"] == 3


def test_jsonl_bad_lines_warn_but_issues_win(tmp_path, capsys):
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, en) for i, (zh, en) in enumerate(EN_BAD)],
                     extra_lines=["{not json", "[1, 2]", "   "])
    code, rep, out = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 1
    assert [w["code"] for w in rep["warnings"]] == ["bad_jsonl_lines"]
    assert rep["inputs"][0]["bad_lines"] == 1 and rep["inputs"][0]["skipped_events"] == 1
    assert "警告：JSONL 有 1 行不是合法 JSON" in out


def test_jsonl_bad_lines_without_issues_is_warn(tmp_path, capsys):
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, en) for i, (zh, en) in enumerate(EN_CLEAN)],
                     extra_lines=["{broken"])
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 2 and rep["status"] == "warn"


def test_jsonl_status_error_and_control_events_skipped(tmp_path, capsys):
    events = [bus(i, zh, en) for i, (zh, en) in enumerate(EN_CLEAN)]
    events += [{"type": "paused", "room_id": "r1", "session_id": "s1"},
               bus(9, "坐禪。", "坐禪。", status="error"),
               {"type": "caption_deleted", "room_id": "r1", "id": "r1:s1:0"}]
    js = write_jsonl(tmp_path / "e.jsonl", events)
    code, rep, _ = run_json(["--jsonl", js], tmp_path, capsys)
    assert code == 0 and rep["inputs"][0]["skipped_events"] == 3


def test_db_and_jsonl_together(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): EN_CLEAN})
    js = write_jsonl(tmp_path / "e.jsonl", [bus(i, zh, ja, tgt_lang="ja") for i, (zh, ja) in enumerate(JA_BAD)])
    code, rep, _ = run_json(["--db", db, "--jsonl", js], tmp_path, capsys)
    assert code == 1
    assert {i["source"] for i in rep["issues"]} == {"jsonl:e.jsonl"}
    assert [i["kind"] for i in rep["inputs"]] == ["db", "jsonl"]
    assert rep["summary"]["captions"] == 7


# --------------------------------------------------------------------------- errors / output
def test_json_refuses_overwrite(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_BAD})
    out_path = tmp_path / "exists.json"
    out_path.write_text("keep", encoding="utf-8")
    code, out, err = run(["--db", db, "--json", out_path], capsys)
    assert code == eju.EXIT_OUTPUT == 4 and "拒絕覆蓋" in err and out == ""
    assert out_path.read_text(encoding="utf-8") == "keep"


def test_json_unwritable_directory_is_output_error(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_BAD})
    code, _out, err = run(["--db", db, "--json", tmp_path / "missing" / "r.json"], capsys)
    assert code == 4 and "無法寫入" in err


def test_write_new_refuses_existing_file_directly(tmp_path):
    p = tmp_path / "r.json"
    assert eju._write_new(p, {"a": 1}) is None
    assert "拒絕覆蓋" in eju._write_new(p, {"a": 2})
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
    bad_ledger = tmp_path / "bad_ledger.sqlite3"
    with closing(sqlite3.connect(bad_ledger)) as conn:
        conn.execute("create table segments (id text)")
        conn.execute("create table transcripts (segment_id text, text text)")
        conn.execute("create table translations (segment_id text, text text)")
        conn.commit()
    wal_only = captions_db(tmp_path / "w.sqlite3", {("r", "s"): EN_BAD})
    Path(str(wal_only) + "-wal").write_bytes(b"")
    bad_utf8 = tmp_path / "bad.jsonl"
    bad_utf8.write_bytes(json.dumps({"zh": "坐禪", "en": "zazen"}, ensure_ascii=False).encode("cp950"))
    cases = [
        (["--db", tmp_path / "missing.sqlite3"], "找不到資料庫"),
        (["--db", not_db], "無法唯讀開啟資料庫"),
        (["--db", other], "不支援的資料庫結構"),
        (["--db", partial], "captions 表缺欄位"),
        (["--db", bad_ledger], "帳本表缺欄位"),
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
    ["--same-ratio", "0.4", "--jsonl", "x.jsonl"],
    ["--same-ratio", "1.1", "--jsonl", "x.jsonl"],
    ["--en-min-ratio", "0", "--jsonl", "x.jsonl"],
    ["--ja-min-ratio", "-1", "--jsonl", "x.jsonl"],
    ["--min-src-chars", "0", "--jsonl", "x.jsonl"],
    ["--ja-min-copy", "1", "--jsonl", "x.jsonl"],
    ["--pinyin-run", "1", "--jsonl", "x.jsonl"],
    ["--max-examples", "0", "--jsonl", "x.jsonl"],
    ["--max-issues", "-1", "--jsonl", "x.jsonl"],
    ["--lang", "ko", "--jsonl", "x.jsonl"],
    ["--assume-lang", "all", "--jsonl", "x.jsonl"],
    ["--same-ratio", "abc", "--jsonl", "x.jsonl"],
    ["--bogus", "--jsonl", "x.jsonl"],
    ["--jsonl", "x.jsonl", "--json", "x.jsonl"],
])
def test_argparse_errors_exit_3(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        eju.main(argv)
    assert exc.value.code == 3
    assert "參數錯誤" in capsys.readouterr().err


def test_max_examples_and_max_issues(tmp_path, capsys):
    rows = [("請老師開示公案。", f"Ask about the 公案 number {i}.") for i in range(7)]
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): rows})
    code, rep, out = run_json(["--db", db, "--max-examples", "2", "--max-issues", "3"], tmp_path, capsys)
    assert code == 1
    assert rep["issues_total"] == 7 and len(rep["issues"]) == 3 and rep["issues_truncated"] == 4
    assert rep["summary"]["flagged"] == 7
    assert out.count("[en] 殘留中文 r/s") == 2 and "另有 5 筆…" in out


def test_suggestions_follow_issue_kinds(tmp_path, capsys):
    db = captions_db(tmp_path / "c.sqlite3", {("r", "s"): EN_BAD})
    _code, rep, out = run_json(["--db", db], tmp_path, capsys)
    joined = " ".join(rep["suggestions"])
    assert "整句未翻" in joined and "--allow" in joined and "--en-min-ratio" in joined
    assert "建議：" in out


def test_cli_subprocess(tmp_path):
    db = captions_db(tmp_path / "c.sqlite3", {("r1", "s1"): EN_BAD})
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, str(TOOL), "--db", str(db)], capture_output=True,
                          env=env, cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 1
    out = proc.stdout.decode("utf-8")
    assert "en/ja 漏譯／殘留中文檢查" in out and "公案" in out


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


def test_read_only_and_no_repo_term_files():
    src = TOOL.read_text(encoding="utf-8")
    low = src.lower()
    assert "glossary" not in low and "dictionary" not in low
    assert '"?mode=ro"' in src and "uri=True" in src
    assert 'open(path, "x"' in src
    for word in ("INSERT", "UPDATE ", "DELETE", "DROP", "CREATE", "ALTER", "VACUUM", ".write_text(", ".unlink("):
        assert word not in src


def test_shebang_and_docstring_document_heuristics_and_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for needle in ("``ok``", "``issues``", "``warn``", "(input)", "(output)", "mode=ro",
                   "zh_only_char", "traditional_form", "zh_punct", "copied_run", "zhuyin", "pinyin",
                   "untranslated", "too_short", "empty"):
        assert needle in doc
    assert (eju.EXIT_OK, eju.EXIT_ISSUES, eju.EXIT_WARN, eju.EXIT_INPUT, eju.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
