"""corpus_export: zh-en / zh-ja parallel corpus export from zen.sqlite3 (DBA impl task)."""
import hashlib
import json
import sqlite3

import os

import pytest

from app.admin import corpus_export as ce
from app.admin import db
from app.tm import norm as tm_norm, src_hash

ALL_OK = {"default": "train-ok"}


# ---------------------------------------------------------------- fixture helpers
class Seeder:
    def __init__(self, path):
        self.c = db.connect(path)
        self.n = 0

    def session(self, sid, room="class", status="ended"):
        self.c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
        self.c.execute("INSERT OR IGNORE INTO sessions(id, room_id, started_at, status, ended_at) VALUES (?,?,?,?,?)",
                       (sid, room, 1000.0, status, None if status == "live" else 2000.0))

    def seg(self, zh, tgt, lang="en", sid="s1", room="class", origin="mt", qe=None, status="ok", seg_status="translated",
            stale=False, t_origin="asr"):
        self.session(sid, room)
        self.n += 1
        seg = f"{room}:{sid}:{self.n}"
        self.c.execute("INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status) VALUES (?,?,?,?,?,?,?)",
                       (seg, sid, room, self.n, 0, 1000, seg_status))
        tid = self.c.execute("INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni, origin) "
                             "VALUES (?,?,?,?,?,?)", (seg, 1, zh, zh, db.to_uni(zh), t_origin)).lastrowid
        if stale:   # new transcript version, translation still points at v1
            self.c.execute("UPDATE transcripts SET is_current=0 WHERE id=?", (tid,))
            self.c.execute("INSERT INTO transcripts(segment_id, version, text, text_uni) VALUES (?,?,?,?)",
                           (seg, 2, zh + "改", db.to_uni(zh + "改")))
        self.c.execute("INSERT INTO translations(segment_id, transcript_id, tgt_lang, version, text, origin, status, "
                       "qe_score, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (seg, tid, lang, 1, tgt, origin, status, qe, 1000.0 + self.n))
        return seg

    def tm(self, zh, tgt, lang="en", quality=4, origin="correction", room=None):
        if room:
            self.c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
        self.c.execute("INSERT INTO tm_units(src_lang, tgt_lang, src_text, src_norm, tgt_text, src_hash, origin, quality, "
                       "room_id) VALUES ('zh-TW',?,?,?,?,?,?,?,?)",
                       (lang, zh, tm_norm(zh), tgt, src_hash(zh, lang), origin, quality, room))

    def term(self, zh, en, status="active", ja=None, locked=0):
        gid = self.c.execute("SELECT id FROM glossaries WHERE name='g'").fetchone()
        gid = gid[0] if gid else self.c.execute("INSERT INTO glossaries(name) VALUES ('g')").lastrowid
        tid = self.c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, status, locked) VALUES (?,?,?,?,?)",
                             (gid, zh, en, status, locked)).lastrowid
        if ja:
            self.c.execute("INSERT INTO glossary_term_targets(term_id, tgt_lang, text, status) VALUES (?,?,?,?)",
                           (tid, "ja", ja, status))

    def close(self):
        self.c.commit()
        self.c.close()


@pytest.fixture
def dbpath(tmp_path):
    p = tmp_path / "zen.sqlite3"
    db.migrate(p)
    return p


def run(path, **kw):
    kw.setdefault("license_map", ALL_OK)
    conn = ce.open_readonly(path)
    try:
        return ce.run_export(conn, ce.ExportConfig(**kw))
    finally:
        conn.close()


def texts(out, pair="zh-en"):
    return sorted((r["src"], r["tgt"]) for r in out[pair])


# ---------------------------------------------------------------- license
def test_license_filter_default_excludes_unknown_and_disallowed(dbpath):
    s = Seeder(dbpath)
    s.seg("今天的課程開始了", "Today's class begins.", sid="ok1", room="r-ok")
    s.seg("我們來討論這本書", "Let us discuss this book.", sid="nt1", room="r-ok")
    s.seg("這是沒有授權的內容", "This content has no license.", sid="u1", room="r-unknown")
    s.tm("請大家坐好", "Please be seated, everyone.", origin="approved")
    s.close()
    lm = {"rooms": {"r-ok": "owner"}, "sessions": {"nt1": "no-train"}, "tm_origins": {"approved": "consented"}}
    out, st = run(dbpath, license_map=lm)
    assert texts(out) == [("今天的課程開始了", "Today's class begins."), ("請大家坐好", "Please be seated, everyone.")]
    assert st["dropped"]["zh-en"] == {"license_disallowed": 1, "license_unknown": 1}   # session beats room
    assert {r["license"] for r in out["zh-en"]} == {"owner", "consented"}
    out2, st2 = run(dbpath, license_map=lm, include_unknown=True)
    assert st2["output_total"]["zh-en"] == 3 and st2["by_license"]["zh-en"]["unknown"] == 1
    out3, _ = run(dbpath, license_map=lm, allowed_licenses=("owner",))
    assert texts(out3) == [("今天的課程開始了", "Today's class begins.")]


def test_no_license_map_means_everything_unknown(dbpath):
    s = Seeder(dbpath)
    s.seg("今天的課程開始了", "Today's class begins.")
    s.close()
    out, st = run(dbpath, license_map={})
    assert out["zh-en"] == [] and st["dropped"]["zh-en"] == {"license_unknown": 1}


def test_provenance_session_is_hashed(dbpath):
    s = Seeder(dbpath)
    s.seg("今天的課程開始了", "Today's class begins.", sid="secret-session-42")
    s.close()
    out, _ = run(dbpath)
    rec = out["zh-en"][0]
    assert set(rec) == {"id", "src", "tgt", "src_lang", "tgt_lang", "source", "origin", "license", "quality", "session"}
    assert rec["source"] == "ledger" and rec["session"] != "secret-session-42"
    assert rec["session"] == hashlib.sha256(b"zen-corpus-v1|secret-session-42").hexdigest()[:16]
    out2, _ = run(dbpath, hash_salt="other")
    assert out2["zh-en"][0]["session"] != rec["session"]


# ---------------------------------------------------------------- dedupe
def test_exact_dedupe_normalises_width_space_punct_and_keeps_best(dbpath):
    s = Seeder(dbpath)
    s.seg("你好，世界！", "Hello, world!", origin="mt")
    s.seg("你好,  世界!", "Hello,   world!", origin="human")          # half-width + extra spaces
    s.seg("你好，世界！", "ＨＥＬＬＯ， world！", origin="mt")             # full-width latin, case
    s.tm("你好，世界！", "Hello, world!", quality=4)                 # human (correction) but quality 0.8
    s.close()
    out, st = run(dbpath)
    assert st["dedup"]["zh-en"]["exact_removed"] == 3 and len(out["zh-en"]) == 1
    assert out["zh-en"][0]["origin"] == "human"           # human edit, then quality, then latest
    _, st = run(dbpath, sources=("ledger",))
    assert st["dedup"]["zh-en"]["exact_removed"] == 2


def test_near_dup_optional_keeps_human_then_quality(dbpath):
    s = Seeder(dbpath)
    s.seg("我們開始上課吧", "Let's start class.", origin="mt", qe=0.9)
    s.seg("我們開始上課吧", "Let us begin the class.", origin="post_edit")
    s.tm("我們開始上課吧", "Let's begin class now.", quality=3)
    s.close()
    out, st = run(dbpath)
    assert len(out["zh-en"]) == 3 and st["dedup"]["zh-en"]["near_removed"] == 0
    out, st = run(dbpath, near_dup=True)
    assert st["dedup"]["zh-en"]["near_removed"] == 2
    assert [r["tgt"] for r in out["zh-en"]] == ["Let us begin the class."]


# ---------------------------------------------------------------- PII
@pytest.mark.parametrize("raw,tag,key", [
    ("請寄信到 chen.dawen@example.com 謝謝", "[EMAIL]", "email"),
    ("我的手機是0912-345-678請回電", "[PHONE]", "phone"),
    ("台北辦公室 (02)2345-6789 有人", "[PHONE]", "phone"),
    ("國際電話 +886 912 345 678 可以", "[PHONE]", "phone"),
    ("日本の携帯 090-1234-5678 です", "[PHONE]", "phone"),
    ("東京 +81 3 1234 5678 へ", "[PHONE]", "phone"),
    ("美國 +1 415 555 0123 號碼", "[PHONE]", "phone"),
    ("全形號碼０９１２３４５６７８也算", "[PHONE]", "phone"),
    ("身分證字號A123456789不要念", "[ID]", "tw_id"),
    ("連結 https://x.example.com/a?token=s3cr3t&x=1 打開", "[URL]", "url"),
    ("連結 https://user:pw@host.example.com/a 打開", "[URL]", "url"),
])
def test_redactor_each_pattern(raw, tag, key):
    out, hits = ce.Redactor().redact(raw)
    assert tag in out and hits == {key: 1}
    assert ce.Redactor().redact(raw) == (out, hits)       # deterministic


@pytest.mark.parametrize("raw", ["西元2024年開會", "價格1,234,567元", "10:30-11:45 上課", "看 https://example.org/page 介紹",
                                 "編號 B12345678X 是產品"])
def test_redactor_leaves_non_pii(raw):
    assert ce.Redactor().redact(raw) == (raw, {})


def test_redactor_names_list_and_all_urls():
    r = ce.Redactor(["陳大文", "Chen Dawen"], mask_all_urls=True)
    assert r.redact("陳大文老師說 Chen Dawen 會來 https://example.org/p") == ("[NAME]老師說 [NAME] 會來 [URL]", {"name": 2, "url": 1})


def test_pii_mask_and_drop_in_pipeline(dbpath):
    s = Seeder(dbpath)
    s.seg("請寄信到 a@b.com 給陳大文老師", "Please email a@b.com to teacher Chen.")
    s.seg("請打電話 0912345678 給我們", "Please call us at 0912345678.")
    s.seg("今天天氣很好我們出去", "The weather is nice today, let's go out.")
    s.close()
    out, st = run(dbpath, names=["陳大文"])
    assert st["pii"]["rows_redacted"] == 2 and st["pii"]["matches"]["email"] == 2
    assert st["pii"]["matches"]["phone"] == 2 and st["pii"]["matches"]["name"] == 1
    blob = json.dumps(out, ensure_ascii=False)
    assert "a@b.com" not in blob and "0912345678" not in blob and "陳大文" not in blob
    out, st = run(dbpath, pii_mode="drop")
    assert len(out["zh-en"]) == 1 and st["pii"]["rows_dropped"] == 2 and st["dropped"]["zh-en"] == {"pii": 2}


def test_never_reads_speakers_or_identity(dbpath, tmp_path):
    s = Seeder(dbpath)
    seg = s.seg("今天的課程開始了", "Today's class begins.")
    s.c.execute("INSERT INTO speakers(id, session_id, label) VALUES (7,'s1','SPEAKER_07')")
    s.c.execute("UPDATE segments SET speaker_id=7 WHERE id=?", (seg,))
    s.close()
    out, _ = run(dbpath)
    assert "SPEAKER_07" not in json.dumps(out, ensure_ascii=False)   # no speakers join at all
    ident = tmp_path / "zen-identity.sqlite3"
    db.migrate_identity(ident)
    with pytest.raises(ce.IdentityDbRefused):
        ce.open_readonly(ident)
    renamed = tmp_path / "copy.sqlite3"
    renamed.write_bytes(ident.read_bytes())
    with pytest.raises(ce.IdentityDbRefused):
        ce.open_readonly(renamed)


# ---------------------------------------------------------------- quality filters
def test_quality_filters(dbpath):
    s = Seeder(dbpath)
    s.seg("今天的課程開始了", "Today's class begins.")                                   # keep
    s.seg("請看這邊", " ")                                                               # empty
    s.seg("這是一段非常長的中文句子內容", "Hi")                                          # ratio too small
    s.seg("好的", "OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK")  # short src: ratio skipped
    s.seg("iPhone 15", "iPhone 15")                                                     # src==tgt (also no zh)
    s.seg("這句是一樣的", "這句是一樣的")                                                # src==tgt
    s.seg("翻譯沒有完成的句子", "partial", status="timeout")                             # not ok
    s.seg("這句已經被遮蔽了", "[redacted]", origin="redacted")
    s.seg("這是舊版本的翻譯句子", "Old version of the sentence.", stale=True)
    s.seg("品質分數很低的機器翻譯", "Low quality machine translation.", qe=0.1)
    s.seg("這句英文裡混了中文", "This sentence 混了很多中文字在裡面")                     # wrong script
    s.tm("低品質翻譯記憶", "Low quality TM unit.", quality=2)
    s.tm("高品質翻譯記憶", "High quality TM unit.", quality=4)
    s.close()
    out, st = run(dbpath, min_quality=0.3)
    assert texts(out) == sorted([("今天的課程開始了", "Today's class begins."),
                                 ("好的", "OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK OK"),
                                 ("高品質翻譯記憶", "High quality TM unit.")])
    assert st["dropped"]["zh-en"] == {"empty": 1, "length_ratio": 1, "src_equals_tgt": 2, "translation_not_ok": 1,
                                      "redacted": 1, "stale_pair": 1, "low_quality": 1, "wrong_script": 1,
                                      "low_tm_quality": 1}
    _, st = run(dbpath, min_tm_quality=2)
    assert "low_tm_quality" not in st["dropped"]["zh-en"]


def test_live_session_excluded_unless_opted_in(dbpath):
    s = Seeder(dbpath)
    s.session("live1", status="live")
    s.seg("直播中的句子內容", "A sentence from a live session.", sid="live1")
    s.close()
    assert run(dbpath)[1]["dropped"]["zh-en"] == {"session_live": 1}
    assert run(dbpath, include_live=True)[1]["output_total"]["zh-en"] == 1


# ---------------------------------------------------------------- zh-ja
def test_zh_ja_script_check(dbpath):
    s = Seeder(dbpath)
    s.seg("今天的課程開始了", "今日の授業が始まりました。", lang="ja")                   # kana: keep
    s.seg("我們來討論這本書", "Let's discuss this book.", lang="ja")                    # latin only
    s.seg("請大家注意安全事項", "請大家注意安全事項規則", lang="ja")                      # kanji-only, long: untranslated
    s.tm("東京大學", "東京大学", lang="ja", quality=4)                                    # short kanji-only: keep
    s.term("佛法", "Dharma", ja="仏法")
    s.close()
    out, st = run(dbpath)
    assert texts(out, "zh-ja") == [("今天的課程開始了", "今日の授業が始まりました。"), ("佛法", "仏法"), ("東京大學", "東京大学")]
    assert st["dropped"]["zh-ja"] == {"wrong_script": 1, "ja_no_kana": 1}
    assert all(r["tgt_lang"] == "ja" and r["src_lang"] == "zh" for r in out["zh-ja"])
    assert texts(out, "zh-en") == [("佛法", "Dharma")]
    assert run(dbpath, pairs=("zh-ja",))[0].keys() == {"zh-ja"}


def test_glossary_inactive_dropped(dbpath):
    s = Seeder(dbpath)
    s.term("因緣", "causes and conditions", locked=1)
    s.term("建議詞", "suggested term", status="proposed")
    s.close()
    out, st = run(dbpath, sources=("glossary",))
    assert texts(out) == [("因緣", "causes and conditions")] and out["zh-en"][0]["quality"] == 1.0
    assert st["dropped"]["zh-en"] == {"glossary_not_active": 1}


# ---------------------------------------------------------------- output / stats
def _seed_mixed(path):
    s = Seeder(path)
    s.seg("今天的課程開始了", "Today's class begins.")
    s.seg("今天的課程開始了", "Today's class begins.")      # exact dup
    s.seg("請看這邊", "")                                   # empty
    s.seg("今天的課程開始了", "今日の授業が始まりました。", lang="ja")
    s.tm("請大家坐好", "Please be seated, everyone.")
    s.tm("請大家坐好", "皆さん、座ってください。", lang="ja", quality=1)
    s.term("佛法", "Dharma")
    s.close()


def test_stats_are_consistent(dbpath, tmp_path):
    _seed_mixed(dbpath)
    st = ce.export(dbpath, tmp_path / "out", ce.ExportConfig(license_map=ALL_OK))
    for p in ce.PAIRS:
        n_in = sum(st["input"][p].values())
        n_drop = sum(st["dropped"][p].values())
        n_dup = st["dedup"][p]["exact_removed"] + st["dedup"][p]["near_removed"]
        assert n_in == n_drop + n_dup + st["output_total"][p]
        assert st["output_total"][p] == sum(st["output"][p].values()) == sum(st["by_license"][p].values())
    assert st["input"]["zh-en"] == {"ledger": 3, "tm": 1, "glossary": 1}
    assert st["output"]["zh-en"] == {"ledger": 1, "tm": 1, "glossary": 1}
    assert st["dropped"]["zh-ja"] == {"low_tm_quality": 1}
    on_disk = json.loads((tmp_path / "out" / "corpus_stats.json").read_text(encoding="utf-8"))
    assert on_disk["output_total"] == st["output_total"] and on_disk["meta"]["hash_salt"] == "default"
    lines = (tmp_path / "out" / "corpus.zh-en.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(json.loads(x)["tgt_lang"] == "en" for x in lines)


def test_output_deterministic(dbpath, tmp_path):
    _seed_mixed(dbpath)
    cfg = ce.ExportConfig(license_map=ALL_OK)
    ce.export(dbpath, tmp_path / "a", cfg)
    ce.export(dbpath, tmp_path / "b", cfg)
    for name in ("corpus.zh-en.jsonl", "corpus.zh-ja.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_atomic_write_keeps_old_file_on_failure(dbpath, tmp_path, monkeypatch):
    _seed_mixed(dbpath)
    out = tmp_path / "out"
    out.mkdir()
    (out / "corpus.zh-en.jsonl").write_text("OLD\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(ce.os, "replace", boom)
    with pytest.raises(OSError):
        ce.export(dbpath, out, ce.ExportConfig(license_map=ALL_OK))
    assert (out / "corpus.zh-en.jsonl").read_text(encoding="utf-8") == "OLD\n"
    assert sorted(p.name for p in out.iterdir()) == ["corpus.zh-en.jsonl"]   # no temp left behind
    monkeypatch.undo()
    ce.export(dbpath, out, ce.ExportConfig(license_map=ALL_OK))
    assert (out / "corpus.zh-en.jsonl").read_text(encoding="utf-8").count("\n") == 3


def test_empty_db(dbpath, tmp_path):
    st = ce.export(dbpath, tmp_path / "out", ce.ExportConfig(license_map=ALL_OK))
    assert st["output_total"] == {"zh-en": 0, "zh-ja": 0} and st["dropped"] == {"zh-en": {}, "zh-ja": {}}
    assert (tmp_path / "out" / "corpus.zh-en.jsonl").read_bytes() == b""
    assert (tmp_path / "out" / "corpus.zh-ja.jsonl").read_bytes() == b""


# ---------------------------------------------------------------- read-only
def _fingerprint(path):
    """Main file bytes + WAL frames. A reader may create an empty -wal/-shm (SQLite WAL index), never content."""
    wal = path.with_name(path.name + "-wal")
    return hashlib.sha256(path.read_bytes()).hexdigest(), (wal.read_bytes() if wal.exists() else b"")


def test_readonly_never_writes(dbpath, tmp_path):
    _seed_mixed(dbpath)
    before = _fingerprint(dbpath)
    ce.export(dbpath, tmp_path / "out", ce.ExportConfig(license_map=ALL_OK))
    assert _fingerprint(dbpath) == before
    conn = ce.open_readonly(dbpath)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO rooms(id) VALUES ('x')")
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    conn.close()


def test_works_while_writer_holds_transaction(dbpath, tmp_path):
    _seed_mixed(dbpath)
    w = db.connect(dbpath)
    assert w.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    w.execute("BEGIN IMMEDIATE")
    s = Seeder.__new__(Seeder)
    s.c, s.n = w, 100
    s.seg("寫入中尚未提交的句子", "An uncommitted sentence being written.")
    try:
        st = ce.export(dbpath, tmp_path / "out", ce.ExportConfig(license_map=ALL_OK))
        assert st["output_total"]["zh-en"] == 3            # committed snapshot only
    finally:
        w.execute("ROLLBACK")
        w.close()


def test_ro_uri_windows_and_odd_paths(tmp_path):
    from pathlib import PureWindowsPath
    assert ce.ro_uri(PureWindowsPath(r"C:\Users\柏能 陳\AppData\Local\ZenBridge\data\zen.sqlite3")) == (
        "file:///C:/Users/%E6%9F%8F%E8%83%BD%20%E9%99%B3/AppData/Local/ZenBridge/data/zen.sqlite3?mode=ro")
    # '?' is not a legal Windows filename character; keep it only on POSIX.
    odd = tmp_path / ("資料 #1 x" if os.name == "nt" else "資料 #1?x")
    odd.mkdir()
    p = odd / "zen.sqlite3"
    db.migrate(p)
    conn = ce.open_readonly(p)                       # '#', '?' and spaces must not break the URI
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    conn.close()


def test_missing_db_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        ce.open_readonly(tmp_path / "nope.sqlite3")
    assert not (tmp_path / "nope.sqlite3").exists()


# ---------------------------------------------------------------- CLI
def test_cli_dry_run_and_full(dbpath, tmp_path, capsys):
    _seed_mixed(dbpath)
    lm = tmp_path / "lic.json"
    lm.write_text(json.dumps(ALL_OK), encoding="utf-8")
    out = tmp_path / "out"
    assert ce.main(["--db", str(dbpath), "--out", str(out), "--license-map", str(lm), "--dry-run"]) == 0
    assert not out.exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed["output_total"] == {"zh-en": 3, "zh-ja": 1}
    assert ce.main(["--db", str(dbpath), "--out", str(out), "--license-map", str(lm), "--pairs", "zh-ja",
                    "--min-tm-quality", "1"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["corpus.zh-ja.jsonl", "corpus_stats.json"]
    assert len((out / "corpus.zh-ja.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert ce.main(["--db", str(dbpath), "--out", str(out)]) == 0          # no map: all unknown -> 0 rows
    assert (out / "corpus.zh-en.jsonl").read_bytes() == b""
    with pytest.raises(SystemExit):
        ce.main(["--db", str(dbpath), "--out", str(out), "--pairs", "zh-ko"])
    assert ce.main(["--db", str(tmp_path / "missing.sqlite3"), "--dry-run"]) == 2
