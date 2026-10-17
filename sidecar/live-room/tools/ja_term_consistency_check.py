#!/usr/bin/env python3
"""Offline Japanese term-consistency check for ja caption sessions (queue item 40).

  python tools/ja_term_consistency_check.py --db captions.sqlite3
  python tools/ja_term_consistency_check.py --db zen.sqlite3 --terms zen_terms.txt --json OUT.json
  python tools/ja_term_consistency_check.py --jsonl events.jsonl --session s1 --min-count 2

Within one session the same Chinese term (Zen / Buddhist proper nouns such as
坐禪, 公案, 開示) should keep one Japanese spelling on the projector.  This tool
finds terms that were rendered with *different* Japanese spellings in the same
session — 坐禅 vs 座禅, 仏性 vs 佛性, 禅 vs ゼン, ざぜん vs 坐禅 — and lists
them with counts, caption ids and a suggested spelling.

Read-only, stdlib only, no network, no ports, does not import the app package.
Input files are never modified.  Term lists come only from the captions
themselves and from an optional user-supplied ``--terms`` text file; no term
files inside the repository are read.

Inputs (at least one; both may be given):

* ``--db`` — SQLite opened with a URI ``mode=ro`` (plus ``immutable=1`` when
  there is no ``-wal`` file; ``-wal`` without ``-shm`` must be checkpointed
  first).  Either the live-room caption store (``captions.sqlite3``, table
  ``captions``: ``id, room_id, session_id, seq, zh, en, status``; ``status =
  'ready'`` rows; the target text travels in ``en`` without a language tag) or
  the admin ledger (``zen.sqlite3``: ``segments`` joined with the current
  ``transcripts.text`` and the current ``translations`` row with ``tgt_lang =
  'ja'`` and ``status`` ``ok`` / ``merged``; ``segments.status = 'translated'``).
* ``--jsonl`` — UTF-8 JSON Lines (repeatable).  Live-room caption events
  (``type`` ``final`` / ``update`` / ``caption`` with ``id, room_id,
  session_id, seq, version, zh, en, status, tgt_lang, ruby``) or generic lines
  whose source text is in ``zh`` / ``source`` / ``source_text`` / ``src_text``
  / ``src`` / ``asr_text`` and whose Japanese text is in ``ja`` / ``ja_text`` /
  ``target`` / ``target_text`` / ``tgt_text`` / ``translation`` / ``en``.  The
  last revision of each ``id`` (else ``session``+``seq``) wins (higher
  ``version``, else later line); drafts and control events are skipped.
  Lines without ``room_id`` / ``session_id`` form one session per file.

Which sessions are Japanese: rows tagged ``tgt_lang`` (``ja*`` = Japanese,
anything else is skipped); untagged sessions count as Japanese when at least
30 % of their target lines contain hiragana / katakana (``--assume-ja`` skips
the check).

Terms:

* **auto** (default) — CJK n-grams of ``--min-len``..``--max-len`` characters
  (2..4) that occur in at least ``--min-count`` (3) captions' Chinese text of
  the session *and* whose kanji form shows up in the Japanese text; n-grams
  that only ever appear inside one longer candidate are dropped.
* **list** — ``--terms PATH`` (UTF-8, one term per line, ``#`` comments).  A
  line may add known Japanese spellings after ``=`` or a TAB, separated by
  ``|``, e.g. ``坐禪 = 坐禅|座禅|ざぜん``.  Listed terms are checked whatever
  their count.  ``--no-auto-terms`` checks only the list.

Matching: text is NFKC-normalised; a Japanese window matches a term when both
fold to the same characters under a built-in kanji-variant table
(traditional / simplified / Japanese shinjitai and common itaiji such as
坐↔座, 禪↔禅, 佛↔仏, 經↔経).  Kana spellings are matched from the
``--terms`` spellings and from ruby readings carried by the captions
(``{base|reading}`` markup or the ``ruby`` event list); hiragana and katakana
forms of a reading both count.  ``{base|reading}`` shows ``base`` on screen.
A (session, term) pair is *checked* once at least two matching Japanese
occurrences exist; it is *inconsistent* when two or more different spellings
were used.  Captions whose Chinese has the term but whose Japanese has no
matching spelling are counted as possible paraphrases / omissions (info only).

=================  ============================================================  ====
status             condition                                                     exit
=================  ============================================================  ====
``ok``             at least one term checked, no inconsistency, no warning       0
``inconsistent``   some term has two or more Japanese spellings in a session     1
                   (wins over warnings, which are still listed)
``warn``           insufficient data: no Japanese session, no term with two      2
                   matching occurrences, corrupt JSONL lines
(input)            missing / unreadable / undecodable / unknown-schema input,    3
                   bad ``--terms`` file, invalid options (argparse errors too)
(output)           ``--json`` path already exists (never overwritten) or         4
                   cannot be written
=================  ============================================================  ====

Human output is Traditional Chinese.  ``--json OUT`` writes one JSON object to
a *new* file only (exclusive create); the same input always gives the same
report (no clock, locale or time-zone dependence).
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

TOOL_NAME = "ja_term_consistency_check"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_INCONSISTENT = 1
EXIT_WARN = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

DEFAULT_MIN_COUNT = 3
DEFAULT_MIN_LEN = 2
DEFAULT_MAX_LEN = 4
DEFAULT_MAX_EXAMPLES = 3
KANA_RATIO_JA = 0.30
MIN_READING_LEN = 2

SOURCE_KEYS = ("zh", "source", "source_text", "src_text", "src", "asr_text")
TARGET_KEYS = ("ja", "ja_text", "target", "target_text", "tgt_text", "translation", "en")
LANG_KEYS = ("tgt_lang", "target_lang", "lang")
SKIP_TYPES = frozenset({"draft", "start", "end", "pause", "resume", "paused", "resumed",
                        "vad", "speech", "meta", "hello", "ping", "pong", "error"})
SKIP_STATUS = frozenset({"error", "missing", "timeout", "cancelled", "deleted", "silent",
                         "paused", "queued", "zh_ready", "failed"})

# Each group lists characters treated as the same kanji when matching a Chinese
# term against Japanese text.  The first character is the fold key.  Kept small
# and generic on purpose (traditional / Japanese / simplified forms).
_KANJI_VARIANT_GROUPS = (
    "禪禅 坐座 佛仏 經経经 覺覚觉 釋釈释 淨浄净 參参 歸帰归 圓円圆 嚴厳严 齋斎斋 實実实 處処处 "
    "觀観观 學学 氣気气 藏蔵 寶宝 傳伝传 德徳 惠恵 會会 來来 與与 國国 圖図图 稱称 讀読读 說説说 "
    "發発发 對対对 單単单 獨独 證証证 禮礼 燈灯 曉暁晓 靜静 聽聴听 廣広广 應応应 樂楽乐 萬万 錄録录 "
    "濟済济 眾衆众 乘乗 隱隠隐 盡尽 顯顕显 斷断 繼継继 龍竜龙 懺忏 華华 鉢缽钵 蓮莲 師师 門门 開开 "
    "關関关 頓顿 漸渐 靈霊灵 業业 緣縁缘 薩萨 剎刹 譯訳译 語语 壽寿 歲歳岁 時时 間间 無无 為爲为 "
    "從従从 隨随 舉挙举 步歩 歷歴历 黑黒 默黙 點点 邊辺边 鄉郷乡 體体 黃黄 龜亀龟 兩両两 亂乱 亞亜亚 "
    "假仮 兒児儿 勸勧劝 卷巻 壞壊坏 變変变 奧奥 將将 專専专 帶帯带 彌弥 徑径 惡悪恶 戰戦战 拜拝 "
    "攝摂摄 擇択择 數数 條条 檢検检 權権权 歡歓欢 殘残 每毎 沒没 淺浅 溫温 滿満满 爭争 狀状 畫画 "
    "當当 盜盗 眞真 祕秘 禱祷 絕絶 總総总 聲声 腦脳 舍舎 蟲虫 裝装 覽覧 觸触 譽誉 賣売 贊賛 轉転转 "
    "辭辞 遲遅 醉酔 錢銭 險険 雜雑 雙双 顏顔 餘余 驗験 鹽塩 衛衞 恆恒 曆暦 讚讃赞 鐘钟 鐵鉄铁 問问 "
    "題题 齊斉齐 劍剣剑 礙碍 號号 縣県县 輪轮 講讲 義义 誦诵 論论 識识 認认 諸诸 謂谓 話话 請请 "
    "調调 願愿 須须 頭头 風风 養养 東东 車车 長长 見见 親亲 現现 環环 練练 紀纪 結结 線线 續続续 "
    "習习 執执 報报 場场 塵尘 壓圧压 夢梦 寫写 層层 慮虑 憶忆 懷懐怀 戲戯戏 擔担 據拠据 擴拡扩 "
    "教敎 書书 極极 樓楼 機机 決决 況况 減减 測测 準准 漢汉 灣湾 燒焼烧 爐炉 爾尔 獻献 產産产 "
    "畢毕 異异 癡痴 盤盘 禍祸 種种 穩穏稳 竊窃 筆笔 節节 範范 簡简 類类 細细 終终 組组 綱纲 緊紧 "
    "縛缚 聞闻 職职 肅粛肃 脫脱 興兴 舊旧 蓋盖 藥薬药 術术 補补 計计 記记 設设 訣诀 詩诗 誠诚 "
    "誤误 課课 談谈 諦谛 謝谢 貪贪 責责 貴贵 資资 賢贤 質质 輕軽轻 較较 農农 這这 進进 運运 "
    "過过 達达 遠远 適适 選选 還还 針针 鏡镜 閉闭 閑闲 陰阴 陽阳 際际 隊队 雖虽 離离 電电 響响 "
    "頌颂 領领 頻频 顧顾 飛飞 飲飮饮 館馆 騰腾 驚惊 鳴鸣 麼么 黨党 齒歯齿 恥耻 煩烦 惱悩恼"
)


def _build_fold() -> dict[str, str]:
    table: dict[str, str] = {}
    for group in _KANJI_VARIANT_GROUPS.split():
        head = group[0]
        for ch in group:
            if ch in table:
                raise RuntimeError(f"kanji variant listed twice: {ch}")
            table[ch] = head
    return table


FOLD = _build_fold()
_FOLD_TRANS = str.maketrans(FOLD)

CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
RUBY = re.compile(r"\{([^{}|]+)\|([^{}|]+)\}")
WBR = re.compile(r"<wbr\s*/?>", re.IGNORECASE)


class InputError(Exception):
    """Unreadable / invalid input (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means "warn" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 參數錯誤：{message}\n")
        raise SystemExit(EXIT_INPUT)


# --------------------------------------------------------------------------- text helpers
def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def fold(text: str) -> str:
    """Map kanji variants to one key (length-preserving)."""
    return text.translate(_FOLD_TRANS)


def is_kanji(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or ch == "々"


def is_hiragana(ch: str) -> bool:
    return 0x3041 <= ord(ch) <= 0x309F


def is_katakana(ch: str) -> bool:
    return 0x30A1 <= ord(ch) <= 0x30FA or ch in "ーヽヾ" or 0x31F0 <= ord(ch) <= 0x31FF


def has_kana(text: str) -> bool:
    return any(is_hiragana(c) or (is_katakana(c) and c != "ー") for c in text)


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in text)


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in text)


def script_kind(surface: str) -> str:
    if surface and all(is_kanji(c) for c in surface):
        return "kanji"
    if surface and all(is_hiragana(c) or c == "ー" for c in surface):
        return "hiragana"
    if surface and all(is_katakana(c) for c in surface):
        return "katakana"
    return "mixed"


KIND_ZH = {"kanji": "漢字", "hiragana": "平假名", "katakana": "片假名", "mixed": "混寫"}


def visible_target(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """NFKC target text with ruby markup reduced to its base; also returns (base, reading)."""
    text = WBR.sub("", nfkc(raw))
    readings = [(m.group(1).strip(), m.group(2).strip()) for m in RUBY.finditer(text)]
    return RUBY.sub(lambda m: m.group(1), text), readings


def is_ja_tag(value: object) -> bool | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("_", "-").split("-")[0] in ("ja", "jp", "jpn")


# --------------------------------------------------------------------------- data
@dataclass
class Caption:
    cid: str
    src: str
    tgt: str
    order: int
    lang_ja: bool | None = None
    readings: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Session:
    source: str
    room_id: str
    session_id: str
    label: str
    captions: list[Caption] = field(default_factory=list)
    skipped_non_ja: int = 0
    is_ja: bool = False
    lang_basis: str = ""
    kana_ratio: float | None = None


@dataclass
class Loaded:
    sessions: dict[tuple[str, str, str], Session] = field(default_factory=dict)
    inputs: list[dict] = field(default_factory=list)
    bad_lines: int = 0

    def session(self, source: str, room: str, sess: str, label: str) -> Session:
        key = (source, room, sess)
        if key not in self.sessions:
            self.sessions[key] = Session(source=source, room_id=room, session_id=sess, label=label)
        return self.sessions[key]


# --------------------------------------------------------------------------- inputs
def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise InputError("有 -wal 但沒有 -shm：請先 checkpoint 資料庫再離線分析")
    return sqlite3.connect(uri, uri=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_db(db: Path, out: Loaded, room: str | None, session: str | None) -> None:
    path = Path(db).resolve()
    if not path.is_file():
        raise InputError(f"找不到資料庫：{path}")
    label = path.name
    try:
        with closing(_connect_ro(path)) as conn:
            conn.execute("BEGIN")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            where, params = [], []
            if "captions" in tables:
                schema = "captions"
                cols = _columns(conn, "captions")
                need = {"room_id", "session_id", "seq", "zh", "en"}
                if not need <= cols:
                    raise InputError(f"captions 表缺欄位：{', '.join(sorted(need - cols))}")
                cid = "id" if "id" in cols else "(room_id || ':' || session_id || ':' || seq)"
                if "status" in cols:
                    where.append("status='ready'")
                if room is not None:
                    where.append("room_id=?")
                    params.append(room)
                if session is not None:
                    where.append("session_id=?")
                    params.append(session)
                sql = (f"SELECT room_id, session_id, seq, {cid}, zh, en, NULL FROM captions"
                       + (" WHERE " + " AND ".join(where) if where else "")
                       + f" ORDER BY room_id, session_id, seq, {cid}")
                rows = list(conn.execute(sql, params))
            elif {"segments", "translations", "transcripts"} <= tables:
                schema = "ledger"
                scols = _columns(conn, "segments")
                tcols = _columns(conn, "translations")
                rcols = _columns(conn, "transcripts")
                need_s = {"id", "room_id", "session_id", "seq"}
                need_t = {"segment_id", "text"}
                if not need_s <= scols or not need_t <= tcols or not need_t <= rcols:
                    raise InputError("帳本表缺欄位（需要 segments.id/room_id/session_id/seq、"
                                     "translations.segment_id/text、transcripts.segment_id/text）")
                tjoin = ["t.segment_id = s.id"]
                if "is_current" in tcols:
                    tjoin.append("t.is_current = 1")
                if "status" in tcols:
                    tjoin.append("t.status IN ('ok', 'merged')")
                if "origin" in tcols:
                    tjoin.append("t.origin <> 'redacted'")
                rjoin = ["r.segment_id = s.id"]
                if "is_current" in rcols:
                    rjoin.append("r.is_current = 1")
                if "origin" in rcols:
                    rjoin.append("r.origin <> 'redacted'")
                lang = "t.tgt_lang" if "tgt_lang" in tcols else "NULL"
                if "status" in scols:
                    where.append("s.status = 'translated'")
                if room is not None:
                    where.append("s.room_id=?")
                    params.append(room)
                if session is not None:
                    where.append("s.session_id=?")
                    params.append(session)
                sql = ("SELECT s.room_id, s.session_id, s.seq, s.id, r.text, t.text, " + lang +
                       " FROM segments s JOIN translations t ON " + " AND ".join(tjoin) +
                       " LEFT JOIN transcripts r ON " + " AND ".join(rjoin)
                       + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY s.room_id, s.session_id, s.seq, s.id"
                       + (", t.tgt_lang" if "tgt_lang" in tcols else ""))
                rows = list(conn.execute(sql, params))
            else:
                raise InputError("不支援的資料庫結構（需要 captions 表，或 segments＋transcripts＋translations 帳本表）")
    except sqlite3.Error as exc:
        raise InputError(f"無法唯讀開啟資料庫：{path}（{exc.__class__.__name__}: {exc}）") from exc
    used = 0
    for order, (r_room, r_sess, _seq, r_id, zh, tgt, tag) in enumerate(rows):
        room_s, sess_s = str(r_room if r_room is not None else ""), str(r_sess if r_sess is not None else "")
        sess = out.session(f"db:{label}", room_s, sess_s, f"{room_s}/{sess_s}")
        lang = is_ja_tag(tag) if schema == "ledger" else None
        if lang is False:
            sess.skipped_non_ja += 1
            continue
        src, tgt_raw = _text(zh), _text(tgt)
        if not src.strip() or not tgt_raw.strip():
            continue
        visible, readings = visible_target(tgt_raw)
        sess.captions.append(Caption(cid=str(r_id), src=nfkc(src), tgt=visible, order=order,
                                     lang_ja=lang, readings=readings))
        used += 1
    out.inputs.append({"path": str(path), "kind": "db", "schema": schema, "rows": len(rows), "captions": used})


def _first_str(obj: dict, keys: tuple[str, ...]) -> tuple[str | None, str]:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return k, v
    return None, ""


def _ruby_list(value: object) -> list[tuple[str, str]]:
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                base, reading = item.get("text"), item.get("reading")
                if isinstance(base, str) and isinstance(reading, str) and base.strip() and reading.strip():
                    out.append((nfkc(base.strip()), nfkc(reading.strip())))
    return out


def read_jsonl(path: Path, out: Loaded, room: str | None, session: str | None) -> None:
    path = Path(path)
    if not path.is_file():
        raise InputError(f"找不到 JSONL：{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"無法讀取 JSONL：{path}（{exc}）") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"JSONL 不是 UTF-8：{path}（位元組 {exc.start}）") from exc
    label = path.name
    lines = bad = skipped = 0
    latest: dict[tuple, tuple[int, int, dict, int]] = {}
    for number, line in enumerate(text.split("\n"), 1):   # not splitlines(): U+2028 may sit inside a string
        line = line.rstrip("\r")
        if not line.strip():
            continue
        lines += 1
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        typ = str(obj.get("type") or "").strip().lower()
        status = str(obj.get("status") or "").strip().lower()
        if typ in SKIP_TYPES or status in SKIP_STATUS:
            skipped += 1
            continue
        r = obj.get("room_id")
        s = obj.get("session_id")
        r = "" if r is None else str(r)
        s = "" if s is None else str(s)
        if room is not None and r != room:
            continue
        if session is not None and s != session:
            continue
        cid = obj.get("id")
        seq = obj.get("seq")
        if isinstance(cid, (str, int)) and not isinstance(cid, bool) and str(cid):
            key = (r, s, "id", str(cid))
        elif isinstance(seq, int) and not isinstance(seq, bool):
            key = (r, s, "seq", seq)
        else:
            key = (r, s, "line", number)
        version = obj.get("version")
        version = version if isinstance(version, int) and not isinstance(version, bool) else 0
        prev = latest.get(key)
        first = prev[3] if prev else number
        if prev is None or version >= prev[0]:
            latest[key] = (version, number, obj, first)
    used = 0
    for key, (_v, number, obj, first) in sorted(latest.items(), key=lambda kv: kv[1][3]):
        r, s = key[0], key[1]
        if r or s:
            sess = out.session(f"jsonl:{label}", r, s, f"{r}/{s}" if r else s)
        else:
            sess = out.session(f"jsonl:{label}", "", "", label)
        lang = None
        for k in LANG_KEYS:
            if k in obj:
                lang = is_ja_tag(obj.get(k))
                if lang is not None:
                    break
        if lang is False:
            sess.skipped_non_ja += 1
            continue
        _sk, src = _first_str(obj, SOURCE_KEYS)
        _tk, tgt = _first_str(obj, TARGET_KEYS)
        if not src.strip() or not tgt.strip():
            continue
        visible, readings = visible_target(tgt)
        readings.extend(_ruby_list(obj.get("ruby")))
        cid = obj.get("id")
        if not isinstance(cid, (str, int)) or isinstance(cid, bool) or not str(cid):
            seq = obj.get("seq")
            cid = f"seq {seq}" if isinstance(seq, int) and not isinstance(seq, bool) else f"line {number}"
        sess.captions.append(Caption(cid=str(cid), src=nfkc(src), tgt=visible, order=first,
                                     lang_ja=lang, readings=readings))
        used += 1
    out.bad_lines += bad
    out.inputs.append({"path": str(path), "kind": "jsonl", "lines": lines, "bad_lines": bad,
                       "skipped_events": skipped, "captions": used})


def read_terms(path: Path) -> dict[str, list[str]]:
    path = Path(path)
    if not path.is_file():
        raise InputError(f"找不到詞語清單：{path}")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise InputError(f"無法讀取詞語清單：{path}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"詞語清單不是 UTF-8：{path}（位元組 {exc.start}）") from exc
    terms: dict[str, list[str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = nfkc(line).strip()
        if not line or line.startswith("#"):
            continue
        head, sep, tail = line.partition("\t") if "\t" in line else line.partition("=")
        term = head.strip()
        if not term or not any(is_kanji(c) for c in term):
            raise InputError(f"詞語清單第 {number} 行沒有中文詞：{line!r}")
        spellings = []
        if sep:
            for part in re.split(r"[|,、]", tail):
                part = part.strip()
                if part:
                    spellings.append(part)
        bucket = terms.setdefault(term, [])
        bucket.extend(s for s in spellings if s not in bucket)
    if not terms:
        raise InputError(f"詞語清單沒有任何詞：{path}")
    return terms


# --------------------------------------------------------------------------- analysis
def decide_language(sess: Session, assume_ja: bool) -> None:
    tags = [c.lang_ja for c in sess.captions if c.lang_ja is not None]
    targets = [c.tgt for c in sess.captions if c.tgt.strip()]
    sess.kana_ratio = round(sum(1 for t in targets if has_kana(t)) / len(targets), 4) if targets else None
    if not sess.captions:
        sess.is_ja, sess.lang_basis = False, "empty"
    elif tags:
        sess.is_ja, sess.lang_basis = True, "tgt_lang"
    elif assume_ja:
        sess.is_ja, sess.lang_basis = True, "assumed"
    else:
        sess.is_ja = (sess.kana_ratio or 0.0) >= KANA_RATIO_JA
        sess.lang_basis = "kana_ratio"


def _ngrams(text: str, lo: int, hi: int) -> set[str]:
    grams = set()
    for run in CJK_RUN.findall(text):
        for n in range(lo, hi + 1):
            for i in range(0, len(run) - n + 1):
                grams.add(run[i:i + n])
    return grams


def _session_readings(sess: Session) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for c in sess.captions:
        for base, reading in c.readings:
            if base and reading:
                out.setdefault(fold(base), set()).add(reading)
    return out


def _explicit_spellings(term: str, listed: list[str], readings: dict[str, set[str]]) -> list[str]:
    raw = list(listed) + sorted(readings.get(fold(term), ()))
    out: list[str] = []
    for s in raw:
        forms = [s]
        if all(is_hiragana(c) or is_katakana(c) for c in s):
            if len(s) < MIN_READING_LEN:
                continue
            forms = [s, to_hiragana(s), to_katakana(s)]
        for f in forms:
            if f and f not in out:
                out.append(f)
    return out


def match_caption(term: str, key: str, spellings: list[str], tgt: str, ftgt: str) -> list[str]:
    """Surfaces of ``term`` in one Japanese caption, in text order, overlaps removed."""
    spans: list[tuple[int, int, str]] = []
    n = len(term)
    i = ftgt.find(key)
    while i != -1:
        spans.append((i, i + n, tgt[i:i + n]))
        i = ftgt.find(key, i + 1)
    for s in spellings:
        j = tgt.find(s)
        while j != -1:
            spans.append((j, j + len(s), s))
            j = tgt.find(s, j + 1)
    spans.sort(key=lambda sp: (sp[0], -(sp[1] - sp[0]), sp[2]))
    kept: list[tuple[int, int, str]] = []
    end = -1
    for sp in spans:
        if sp[0] >= end:
            kept.append(sp)
            end = sp[1]
    return [sp[2] for sp in kept]


@dataclass
class TermResult:
    term: str
    origin: str
    surfaces: Counter = field(default_factory=Counter)
    first_seen: dict = field(default_factory=dict)
    captions_by_surface: dict = field(default_factory=dict)
    captions_with_term: int = 0
    missing: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.surfaces.values())


def check_term(sess: Session, term: str, origin: str, spellings: list[str], folded: list[str],
               indices: list[int] | None = None) -> TermResult:
    res = TermResult(term=term, origin=origin)
    key = fold(term)
    for idx in (range(len(sess.captions)) if indices is None else indices):
        c = sess.captions[idx]
        if term not in c.src:
            continue
        res.captions_with_term += 1
        found = match_caption(term, key, spellings, c.tgt, folded[idx])
        if not found:
            res.missing.append(c.cid)
            continue
        for s in found:
            res.surfaces[s] += 1
            res.first_seen.setdefault(s, idx)
            ids = res.captions_by_surface.setdefault(s, [])
            if c.cid not in ids:
                ids.append(c.cid)
    return res


def auto_terms(sess: Session, folded: list[str], lo: int, hi: int, min_count: int) -> dict[str, list[int]]:
    """Frequent CJK n-grams of the session's Chinese text that have a kanji match in the Japanese."""
    counts: Counter = Counter()
    holders: dict[str, list[int]] = {}
    for idx, c in enumerate(sess.captions):
        for g in _ngrams(c.src, lo, hi):
            counts[g] += 1
            holders.setdefault(g, []).append(idx)
    cands = []
    for g, k in counts.items():
        if k < min_count:
            continue
        key = fold(g)
        if any(key in folded[i] for i in holders[g]):
            cands.append(g)
    cand_set = set(cands)
    absorbed: set[str] = set()
    for longer in cands:
        for n in range(lo, len(longer)):
            for i in range(0, len(longer) - n + 1):
                sub = longer[i:i + n]
                if sub in cand_set and holders[sub] == holders[longer]:
                    absorbed.add(sub)
    return {g: holders[g] for g in cands if g not in absorbed}


def _dominant(res: TermResult) -> str:
    return sorted(res.surfaces, key=lambda s: (-res.surfaces[s], res.first_seen[s], s))[0]


def analyze_session(sess: Session, listed: dict[str, list[str]], settings: dict) -> tuple[list[dict], int, int]:
    folded = [fold(c.tgt) for c in sess.captions]
    readings = _session_readings(sess)
    terms: dict[str, str] = {}
    for t in listed:
        terms[t] = "list"
    found_auto: dict[str, list[int]] = {}
    if settings["auto_terms"]:
        found_auto = auto_terms(sess, folded, settings["min_len"], settings["max_len"], settings["min_count"])
        for t in found_auto:
            terms.setdefault(t, "auto")
    checked = 0
    results: list[TermResult] = []
    for term in sorted(terms, key=lambda t: (-len(t), t)):
        spellings = _explicit_spellings(term, listed.get(term, []), readings)
        indices = found_auto.get(term) if terms[term] == "auto" else None
        res = check_term(sess, term, terms[term], spellings, folded, indices)
        if res.total >= 2:
            checked += 1
            if len(res.surfaces) >= 2:
                results.append(res)
    # drop a shorter term whose deviating captions all sit inside a longer reported term
    reported: list[TermResult] = []
    for res in results:
        dom = _dominant(res)
        deviating = {cid for s, ids in res.captions_by_surface.items() if s != dom for cid in ids}
        covered = False
        for big in reported:
            if res.term in big.term:
                theirs = {cid for ids in big.captions_by_surface.values() for cid in ids}
                if deviating <= theirs:
                    covered = True
                    break
        if not covered:
            reported.append(res)
    out = []
    for res in reported:
        dom = _dominant(res)
        variants = []
        for s in sorted(res.surfaces, key=lambda s: (-res.surfaces[s], res.first_seen[s], s)):
            ids = res.captions_by_surface[s]
            variants.append({
                "surface": s, "kind": script_kind(s), "count": res.surfaces[s],
                "captions": len(ids), "caption_ids": ids[:settings["max_examples"]],
            })
        to_fix = [cid for v in variants if v["surface"] != dom for cid in res.captions_by_surface[v["surface"]]]
        kinds = sorted({v["kind"] for v in variants})
        out.append({
            "session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
            "source": sess.source, "term": res.term, "term_origin": res.origin,
            "occurrences": res.total, "captions_with_term": res.captions_with_term,
            "variants": variants, "suggested": dom, "suggested_count": res.surfaces[dom],
            "fix_caption_ids": to_fix[:settings["max_examples"] * 3], "fix_count": len(to_fix),
            "mixed_script": len(kinds) > 1, "kinds": kinds,
            "unmatched_captions": len(res.missing),
        })
    out.sort(key=lambda d: (-(d["occurrences"] - d["suggested_count"]), -d["occurrences"], d["term"]))
    return out, checked, len(terms)


def analyze(loaded: Loaded, listed: dict[str, list[str]], settings: dict) -> dict:
    sessions_out = []
    inconsistencies: list[dict] = []
    warnings: list[dict] = []
    notes: list[str] = []
    checked_total = 0
    ja_sessions = 0
    skipped_sessions = []
    captions_total = 0
    seen_listed: set[str] = set()
    for key in sorted(loaded.sessions, key=lambda k: (k[0], k[1], k[2])):
        sess = loaded.sessions[key]
        decide_language(sess, settings["assume_ja"])
        entry = {"session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
                 "source": sess.source, "captions": len(sess.captions), "ja": sess.is_ja,
                 "lang_basis": sess.lang_basis, "kana_ratio": sess.kana_ratio,
                 "skipped_non_ja_rows": sess.skipped_non_ja,
                 "terms_considered": 0, "terms_checked": 0, "inconsistencies": 0}
        if sess.is_ja:
            ja_sessions += 1
            captions_total += len(sess.captions)
            found, checked, considered = analyze_session(sess, listed, settings)
            entry.update(terms_considered=considered, terms_checked=checked, inconsistencies=len(found))
            checked_total += checked
            inconsistencies.extend(found)
            for t in listed:
                if any(t in c.src for c in sess.captions):
                    seen_listed.add(t)
        elif sess.captions or sess.skipped_non_ja:
            skipped_sessions.append(sess.label)
        sessions_out.append(entry)
    if loaded.bad_lines:
        warnings.append({"code": "bad_jsonl_lines",
                         "message_zh": f"JSONL 有 {loaded.bad_lines} 行不是合法 JSON，已略過。"})
    if ja_sessions == 0:
        warnings.append({"code": "no_ja_session",
                         "message_zh": "找不到日文場次（沒有 tgt_lang=ja，譯文也幾乎沒有假名）；可用 --assume-ja 強制視為日文。"})
    elif checked_total == 0:
        warnings.append({"code": "insufficient_terms",
                         "message_zh": "沒有任何詞在同一場次出現至少 2 次可比對的日文寫法，資料不足以判斷一致性。"})
    if skipped_sessions:
        notes.append(f"略過非日文場次 {len(skipped_sessions)} 個：{'、'.join(skipped_sessions[:5])}"
                     + ("…" if len(skipped_sessions) > 5 else ""))
    unseen = sorted(t for t in listed if t not in seen_listed)
    if listed and unseen:
        notes.append(f"清單中有 {len(unseen)} 個詞沒出現在任何日文場次的中文字幕：{'、'.join(unseen[:8])}"
                     + ("…" if len(unseen) > 8 else ""))
    notes.append("比對依 NFKC＋內建異體字對照（例：坐／座、禪／禅、佛／仏）；假名寫法只認 --terms 與字幕附帶的讀音（ruby）。")
    if inconsistencies:
        status, code = "inconsistent", EXIT_INCONSISTENT
    elif warnings:
        status, code = "warn", EXIT_WARN
    else:
        status, code = "ok", EXIT_OK
    suggestions = _suggestions(inconsistencies, warnings, settings)
    report = {
        "tool": TOOL_NAME, "schema_version": SCHEMA_VERSION, "status": status, "exit_code": code,
        "settings": dict(settings), "inputs": loaded.inputs, "sessions": sessions_out,
        "summary": {"ja_sessions": ja_sessions, "captions": captions_total,
                    "terms_listed": len(listed), "term_pairs_checked": checked_total,
                    "inconsistencies": len(inconsistencies)},
        "inconsistencies": inconsistencies, "warnings": warnings, "notes": notes,
        "suggestions": suggestions,
    }
    report["summary_zh"] = render_text(report)
    return report


def _suggestions(items: list[dict], warnings: list[dict], settings: dict) -> list[str]:
    out = []
    if items:
        out.append("每個不一致詞挑一個日文寫法（預設建議＝該場次最常用者），回頭修正列出的字幕，並在後台詞彙表鎖定該譯法（含讀音），之後場次就會一致；本工具不修改任何檔案。")
        if any(d["mixed_script"] for d in items):
            out.append("同一詞時而漢字、時而假名：投影字幕建議統一用漢字，首次出現時加讀音（ruby），不要混用。")
        if any(d["unmatched_captions"] for d in items):
            out.append("部分字幕中文有該詞、日文卻找不到對應寫法（可能意譯或漏譯），可人工抽查。")
    codes = {w["code"] for w in warnings}
    if "insufficient_terms" in codes:
        out.append(f"可降低 --min-count（目前 {settings['min_count']}）或用 --terms 提供要檢查的禪學專有名詞（可附已知日文寫法與讀音）。")
    if "no_ja_session" in codes:
        out.append("確認輸入的是日文場次（tgt_lang=ja）的字幕；若譯文確定是日文可加 --assume-ja。")
    if "bad_jsonl_lines" in codes:
        out.append("JSONL 有壞行：確認檔案沒有在寫入中被截斷。")
    return out


def render_text(r: dict) -> str:
    s = r["summary"]
    head = {"ok": "日文譯名一致（exit 0）", "inconsistent": f"發現 {s['inconsistencies']} 個譯名不一致（exit 1）",
            "warn": "資料不足或有警告（exit 2）"}[r["status"]]
    lines = [f"日文譯名一致性檢查：{head}"]
    lines.append("輸入：" + "、".join(
        f"{Path(i['path']).name}（{'資料庫 ' + i['schema'] if i['kind'] == 'db' else 'JSONL'}，字幕 {i['captions']} 筆）"
        for i in r["inputs"]))
    lines.append(f"日文場次 {s['ja_sessions']} 個、字幕 {s['captions']} 筆；可比對的（場次，詞）組合 {s['term_pairs_checked']} 個"
                 + (f"；清單詞 {s['terms_listed']} 個" if s["terms_listed"] else ""))
    if r["inconsistencies"]:
        lines.append("不一致清單：")
        for d in r["inconsistencies"]:
            vs = "、".join(f"{v['surface']}×{v['count']}（{KIND_ZH[v['kind']]}）" for v in d["variants"])
            origin = "清單" if d["term_origin"] == "list" else "自動"
            lines.append(f"  [{d['session']}] {d['term']}（{origin}）→ {vs}")
            fix = "、".join(d["fix_caption_ids"])
            more = "…" if d["fix_count"] > len(d["fix_caption_ids"]) else ""
            lines.append(f"     建議統一為「{d['suggested']}」（{d['suggested_count']}/{d['occurrences']} 次）；"
                         f"需檢查字幕 {d['fix_count']} 筆：{fix}{more}"
                         + (f"；另有 {d['unmatched_captions']} 筆日文找不到對應寫法" if d["unmatched_captions"] else ""))
    for w in r["warnings"]:
        lines.append(f"警告：{w['message_zh']}")
    for n in r["notes"]:
        lines.append(f"附註：{n}")
    if r["suggestions"]:
        lines.append("建議：")
        lines.extend(f"  - {x}" for x in r["suggestions"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI
def _write_new(path: Path, report: dict) -> str | None:
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    except FileExistsError:
        return f"拒絕覆蓋既有檔案：{path}（只寫新檔）"
    except OSError as exc:
        return f"無法寫入 JSON 報告：{path}（{exc}）"
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="ja_term_consistency_check.py",
                 description="唯讀檢查同一場次中，同一中文詞（禪學專有名詞）是否被譯成不同的日文寫法。")
    ap.add_argument("--db", type=Path, metavar="PATH", help="字幕資料庫（captions.sqlite3 或 zen.sqlite3 帳本，唯讀開啟）")
    ap.add_argument("--jsonl", type=Path, action="append", default=[], metavar="PATH",
                    help="字幕事件 JSONL（可重複）")
    ap.add_argument("--terms", type=Path, metavar="PATH",
                    help="使用者提供的詞語清單（UTF-8，每行一詞，可用 = 附已知日文寫法，以 | 分隔）")
    ap.add_argument("--no-auto-terms", action="store_true", help="不自動從字幕抽詞，只檢查 --terms 清單")
    ap.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT, help="自動抽詞：同場次至少出現在幾筆字幕（預設 3）")
    ap.add_argument("--min-len", type=int, default=DEFAULT_MIN_LEN, help="自動抽詞最短字數（預設 2）")
    ap.add_argument("--max-len", type=int, default=DEFAULT_MAX_LEN, help="自動抽詞最長字數（預設 4）")
    ap.add_argument("--assume-ja", action="store_true", help="沒有 tgt_lang 標記的場次一律視為日文")
    ap.add_argument("--room", help="只看這個 room_id")
    ap.add_argument("--session", help="只看這個 session_id")
    ap.add_argument("--max-examples", type=int, default=DEFAULT_MAX_EXAMPLES, help="每種寫法列出幾個字幕 id（預設 3）")
    ap.add_argument("--json", type=Path, metavar="OUT", help="另寫 JSON 報告到新檔（已存在就拒絕，不覆蓋）")
    return ap


def _check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.db is None and not args.jsonl:
        ap.error("請給 --db 或 --jsonl（至少一個）")
    if args.no_auto_terms and args.terms is None:
        ap.error("--no-auto-terms 需要搭配 --terms")
    if args.min_count < 1:
        ap.error("--min-count 至少為 1")
    if args.min_len < 1:
        ap.error("--min-len 至少為 1")
    if args.max_len < args.min_len:
        ap.error("--max-len 不能小於 --min-len")
    if args.max_len > 8:
        ap.error("--max-len 最多 8")
    if args.max_examples < 1:
        ap.error("--max-examples 至少為 1")
    if args.json is not None:
        target = os.path.abspath(str(args.json))
        inputs = [p for p in [args.db, args.terms, *args.jsonl] if p is not None]
        if any(os.path.abspath(str(p)) == target for p in inputs):
            ap.error("--json 不能指向輸入檔")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    _check_args(ap, args)
    if args.json is not None and (args.json.exists() or args.json.is_symlink()):
        sys.stderr.write(f"拒絕覆蓋既有檔案：{args.json}（只寫新檔）\n")
        return EXIT_OUTPUT
    settings = {"auto_terms": not args.no_auto_terms, "min_count": args.min_count,
                "min_len": args.min_len, "max_len": args.max_len, "assume_ja": args.assume_ja,
                "max_examples": args.max_examples, "room": args.room, "session": args.session}
    loaded = Loaded()
    try:
        listed = read_terms(args.terms) if args.terms is not None else {}
        if args.db is not None:
            read_db(args.db, loaded, args.room, args.session)
        for p in args.jsonl:
            read_jsonl(p, loaded, args.room, args.session)
    except InputError as exc:
        sys.stderr.write(f"輸入錯誤：{exc}\n")
        return EXIT_INPUT
    report = analyze(loaded, listed, settings)
    if args.json is not None:
        err = _write_new(args.json, report)
        if err:
            sys.stderr.write(err + "\n")
            return EXIT_OUTPUT
    print(report["summary_zh"])
    if args.json is not None:
        print(f"JSON 報告：{args.json}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
