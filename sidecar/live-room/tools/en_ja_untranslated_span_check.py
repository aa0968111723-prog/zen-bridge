#!/usr/bin/env python3
"""Offline check for untranslated spans in en / ja captions (queue item 41).

  python tools/en_ja_untranslated_span_check.py --db captions.sqlite3
  python tools/en_ja_untranslated_span_check.py --db zen.sqlite3 --lang ja --json OUT.json
  python tools/en_ja_untranslated_span_check.py --jsonl events.jsonl --allow names.txt

Finds captions whose English or Japanese text still carries Chinese: left-over
Chinese / zhuyin / pinyin spans, whole captions that were not translated (the
target equals or nearly equals the Chinese source) and targets that are much
shorter than their source (possible omissions).  English and Japanese are
counted separately.

Read-only, stdlib only, no network, no ports, does not import the app package.
Input files are never modified.

Inputs (at least one; both may be given):

* ``--db`` — SQLite opened with a URI ``mode=ro`` (plus ``immutable=1`` when
  there is no ``-wal`` file; ``-wal`` without ``-shm`` must be checkpointed
  first).  Either the live-room caption store (``captions.sqlite3``, table
  ``captions``: ``id, room_id, session_id, seq, zh, en, status``; ``status =
  'ready'`` rows; the target text travels in ``en`` without a language tag) or
  the admin ledger (``zen.sqlite3``: ``segments`` with ``status =
  'translated'`` joined with the current ``transcripts.text`` and every current
  ``translations`` row with ``status`` ``ok`` / ``merged``, tagged by
  ``tgt_lang``; redacted rows are skipped; older ledgers without the optional
  columns are read with fewer filters).
* ``--jsonl`` — UTF-8 JSON Lines (repeatable).  Live-room caption events
  (``type`` ``final`` / ``update`` / ``caption`` with ``id, room_id,
  session_id, seq, version, zh, en, status, tgt_lang``) or generic lines whose
  source text is in ``zh`` / ``source`` / ``source_text`` / ``src_text`` /
  ``src`` / ``asr_text`` and whose target is in ``translation`` / ``target`` /
  ``target_text`` / ``tgt_text`` / ``ja`` / ``ja_text`` / ``en_text`` /
  ``en``.  The last revision of each ``id`` (else ``session``+``seq``) wins
  (higher ``version``, else later line); drafts and control events are
  skipped.  Lines without ``room_id`` / ``session_id`` form one session per
  file.

Language of a caption: its ``tgt_lang`` tag (``en*`` / ``ja*``; other tags
are skipped and counted).  Untagged captions take the majority tag of their
session; in a session with no tags at all they are Japanese when at least
30 % of the session's target lines contain hiragana / katakana, otherwise
English (``--assume-lang`` overrides the guess).

Heuristics (text is NFKC-normalised first, ``{base|reading}`` ruby shows
``base``, ``<wbr>`` is dropped; spans listed in ``--allow`` are masked):

* **residual_zh, en** — any run of Han characters or zhuyin (bopomofo) in an
  English caption; runs separated only by Chinese punctuation or spaces are
  reported as one span.
* **residual_zh, ja** — Japanese is written with kanji, so a Han run alone is
  *not* evidence.  A run of Han characters / zhuyin (kana and 、。 end a run;
  ``,`` ``;`` ``:`` — NFKC of the Chinese full-width ，；： — join it) is
  reported only when at least one of these holds:
  ``zhuyin`` — it contains bopomofo;
  ``zh_only_char`` — it contains a character modern Japanese does not use
  (Chinese particles / pronouns such as 們 這 嗎 呢 吧 您 她 很, or
  simplified-only forms such as 们 这 说 时 经);
  ``traditional_form`` — it contains two or more traditional forms that
  Japanese writes in shinjitai (禪→禅, 佛→仏, 經→経, 說→説 …), or one in a
  run of at least four Han characters;
  ``zh_punct`` — Chinese comma / semicolon / colon between Han characters
  in a run of at least four Han characters;
  ``copied_run`` — its longest common substring with the source (after
  folding traditional / simplified / shinjitai variants) is at least
  ``--ja-min-copy`` (8) characters.
  A single traditional form in a short run (坐禪の) or a kanji compound
  shared with the source (坐禅, 公案, 只管打坐) is never reported by itself.
* **pinyin** — words with pinyin tone marks that split into valid pinyin
  syllables (a short list of English loanwords such as café is ignored; a
  Hepburn word with macrons that also splits into pinyin, e.g. Ōsaka, can be
  a false positive — use ``--allow``); in English captions also runs of at
  least ``--pinyin-run`` (3) consecutive toneless syllables, at least two of
  which are not common English words (``wo men yi qi zuo chan``; single
  words like Zen / Chan are not reported).
* **untranslated** — the source has Han characters and the target, folded
  and stripped of spaces / punctuation, has a ``difflib`` similarity of at
  least ``--same-ratio`` (0.85) with the source.  For Japanese the target
  must also contain no kana and either a ``zh_only_char`` /
  ``traditional_form`` / zhuyin character or at least ``--ja-min-copy``
  characters, so a pure-kanji Japanese rendering (南無阿弥陀仏) is not
  flagged while an unchanged 南無阿彌陀佛 is.  An untranslated caption is not
  also reported as residual / too short.
* **too_short** — sources with at least ``--min-src-chars`` (6) letters /
  digits / Han characters whose target has fewer than ``--en-min-ratio``
  (1.0) letters per source character in English, or ``--ja-min-ratio``
  (0.5) characters per source character in Japanese.
* **empty** — the target field is present but blank while the source is not.

=================  ============================================================  ====
status             condition                                                     exit
=================  ============================================================  ====
``ok``             at least one caption checked, no issue, no warning            0
``issues``         some caption was flagged (wins over warnings, still listed)   1
``warn``           insufficient data: no en / ja caption with a source text,     2
                   corrupt JSONL lines
(input)            missing / unreadable / undecodable / unknown-schema input,    3
                   bad ``--allow`` file, invalid options (argparse errors too)
(output)           ``--json`` path already exists (never overwritten) or         4
                   cannot be written
=================  ============================================================  ====

Human output is Traditional Chinese.  ``--json OUT`` writes one JSON object to
a *new* file only (exclusive create); the same input always gives the same
report (no clock, locale or time-zone dependence).
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass, field
import difflib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

TOOL_NAME = "en_ja_untranslated_span_check"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_WARN = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

LANGS = ("en", "ja")
KINDS = ("residual_zh", "pinyin", "untranslated", "too_short", "empty")
KIND_ZH = {"residual_zh": "殘留中文", "pinyin": "拼音", "untranslated": "整句未翻",
           "too_short": "譯文過短", "empty": "譯文空白"}
LANG_ZH = {"en": "英文（en）", "ja": "日文（ja）"}
REASON_ZH = {"han": "漢字", "zhuyin": "注音", "zh_only_char": "日文不用的中文字",
             "traditional_form": "繁體舊字形", "zh_punct": "中文標點", "copied_run": "整段照抄原文",
             "tone_marks": "帶聲調拼音", "syllable_run": "連續拼音"}

DEFAULT_SAME_RATIO = 0.85
DEFAULT_EN_MIN_RATIO = 1.0
DEFAULT_JA_MIN_RATIO = 0.5
DEFAULT_MIN_SRC_CHARS = 6
DEFAULT_JA_MIN_COPY = 8
DEFAULT_PINYIN_RUN = 3
DEFAULT_MAX_EXAMPLES = 5
DEFAULT_MAX_ISSUES = 500
KANA_RATIO_JA = 0.30
SHOW_CHARS = 40

SOURCE_KEYS = ("zh", "source", "source_text", "src_text", "src", "asr_text")
TARGET_KEYS = ("translation", "target", "target_text", "tgt_text", "ja", "ja_text", "en_text", "en")
KEY_LANG = {"ja": "ja", "ja_text": "ja", "en_text": "en"}
LANG_KEYS = ("tgt_lang", "target_lang", "lang")
SKIP_TYPES = frozenset({"draft", "start", "end", "pause", "resume", "paused", "resumed",
                        "vad", "speech", "meta", "hello", "ping", "pong", "error",
                        "captions_cleared", "caption_deleted"})
SKIP_STATUS = frozenset({"error", "missing", "timeout", "cancelled", "deleted", "silent",
                         "paused", "queued", "zh_ready", "failed"})

# Characters modern Japanese does not write: Chinese particles / pronouns and
# simplified-only forms (simplified forms shared with shinjitai, e.g. 会 国 学
# 写 万 与 机 准, are deliberately left out).
ZH_ONLY = frozenset(
    "們這麼麽嗎呢吧啊嘛您妳她牠祂很哪喔呀啦咱嗯哇裡甭倆啥誒欸唄噢喲冇哦囉咧怎找鬆"
    "们这么吗说让从对个还时为东车门问间见长开关听话语经觉释净归圆严斋实处观气传读发单证晓广应乐"
    "录济众隐显继龙忏华钵莲师顿渐灵缘萨译岁举历边乡龟两亚儿劝坏变专带恶战摄择检权欢满总转钟铁"
    "题齐剑县轮讲义诵论识认诸谓请调愿须头风养亲现环练纪结线续习执报场尘压梦层虑忆怀戏扩书极决"
    "况减测汉湾烧尔毕异盘祸种稳笔节范简类细终组纲紧缚闻职肃兴盖药术补计记设诀诗诚误课谈谛谢贪"
    "责贵资贤质轻较农进运过达远适选针镜闭闲阴阳际队虽离电响颂领频顾飞饮馆腾惊鸣齿耻烦恼无产"
)
# Traditional forms that Japanese writes differently (shinjitai); one in a short
# run is tolerated (temple names, older spellings), two or more, or one in a
# run of four or more Han characters, are not.
TRAD_FORM = frozenset(
    "禪佛經覺釋淨參歸圓嚴齋實處觀學氣藏寶傳德惠會來與國圖稱讀說發對單獨證禮燈曉靜聽廣應樂萬錄"
    "濟眾乘隱盡顯斷繼譯壽歲從隨舉步歷黑默點邊鄉體黃龜兩亂亞假兒勸卷壞變奧將專帶彌徑惡戰拜攝擇"
    "數條檢權歡殘每沒淺溫滿爭狀畫當盜眞祕禱絕總聲腦蟲裝覽觸譽賣贊轉辭遲醉錢險雜雙顏餘驗鹽"
    "關靈緣讚鐵齊劍號縣續壓寫擔據擴爐獻產穩竊肅脫舊藥輕齒惱戲懷稅銳閱悅內吳綠爲"
)

# Variant folding (traditional / Japanese / simplified) for source-target
# comparisons only; the first character of each group is the fold key.
_VARIANT_GROUPS = (
    "禪禅 佛仏 經経经 覺覚觉 釋釈释 淨浄净 參参 歸帰归 圓円圆 嚴厳严 齋斎斋 實実实 處処处 觀観观 "
    "學学 氣気气 藏蔵 寶宝 傳伝传 德徳 惠恵 會会 來来 與与 國国 圖図图 稱称 讀読读 說説说 發発发 "
    "對対对 單単单 獨独 證証证 禮礼 燈灯 曉暁晓 靜静 聽聴听 廣広广 應応应 樂楽乐 萬万 錄録录 濟済济 "
    "眾衆众 乘乗 隱隠隐 盡尽 顯顕显 斷断 繼継继 龍竜龙 譯訳译 語语 壽寿 歲歳岁 時时 間间 無无 為爲为 "
    "從従从 隨随 舉挙举 步歩 歷歴历 黑黒 默黙 點点 邊辺边 鄉郷乡 體体 黃黄 龜亀龟 兩両两 亂乱 亞亜亚 "
    "假仮 兒児儿 勸勧劝 卷巻 壞壊坏 變変变 奧奥 將将 專専专 帶帯带 彌弥 徑径 惡悪恶 戰戦战 拜拝 "
    "攝摂摄 擇択择 數数 條条 檢検检 權権权 歡歓欢 殘残 每毎 沒没 淺浅 溫温 滿満满 爭争 狀状 畫画 "
    "當当 盜盗 眞真 祕秘 禱祷 絕絶 總総总 聲声 腦脳 舍舎 蟲虫 裝装 覽覧 觸触 譽誉 賣売 贊賛 轉転转 "
    "辭辞 遲遅 醉酔 錢銭 險険 雜雑 雙双 顏顔 餘余 驗験 鹽塩 關関关 靈霊灵 緣縁缘 讚讃赞 鐵鉄铁 "
    "齊斉齐 劍剣剑 號号 縣県县 續続续 壓圧压 寫写 擔担 據拠据 擴拡扩 爐炉 獻献 產産产 穩穏稳 竊窃 "
    "肅粛肃 脫脱 舊旧 藥薬药 輕軽轻 齒歯齿 惱悩恼 戲戯戏 懷懐怀 稅税 銳鋭 閱閲 悅悦 內内 吳呉 綠緑 "
    "坐座 們们 這这 個个 讓让 還还 東东 車车 門门 問问 見见 長长 開开 話话 師师 華华 頭头 風风 "
    "書书 電电 講讲 義义 論论 識识 認认 請请 謂谓 諸诸 現现 結结 線线 習习 報报 場场 夢梦 層层 "
    "記记 計计 設设 詩诗 誠诚 誤误 課课 談谈 諦谛 謝谢 貴贵 進进 運运 過过 達达 遠远 選选 飛飞 鳴鸣"
)


def _build_fold() -> dict[str, str]:
    table: dict[str, str] = {}
    for group in _VARIANT_GROUPS.split():
        head = group[0]
        for ch in group:
            if ch in table:
                raise RuntimeError(f"variant listed twice: {ch}")
            table[ch] = head
    return table


FOLD = _build_fold()
_FOLD_TRANS = str.maketrans(FOLD)

_HAN = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3005"
_ZHUYIN = "\u3100-\u312f\u31a0-\u31bf\u02ca\u02c7\u02cb\u02d9"
EN_RUN = re.compile(f"[{_HAN}{_ZHUYIN}]+(?:[,;:、。!?\\s]{{1,3}}[{_HAN}{_ZHUYIN}]+)*")
JA_RUN = re.compile(f"[{_HAN}{_ZHUYIN}]+(?:[,;:]\\s?[{_HAN}{_ZHUYIN}]+)*")
ZHUYIN_CHAR = re.compile(f"[{_ZHUYIN}]")
LATIN_WORD = re.compile(r"[A-Za-z\u00c0-\u024f]+")
RUBY = re.compile(r"\{([^{}|]+)\|([^{}|]+)\}")
WBR = re.compile(r"<wbr\s*/?>", re.IGNORECASE)
MASK = "\x00"

# Every standard pinyin syllable (plus the v / ue spellings typed for ü).
PINYIN_SYLLABLES = frozenset("""
a ai an ang ao ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu
ma mai man mang mao me mei men meng mi mian miao mie min ming miu mo mou mu
fa fan fang fei fen feng fo fou fu
da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du duan dui dun duo
ta tai tan tang tao te teng ti tian tiao tie ting tong tou tu tuan tui tun tuo
na nai nan nang nao ne nei nen neng ni nian niang niao nie nin ning niu nong nou nu nuan nuo nü nüe nv nve nue
la lai lan lang lao le lei leng li lia lian liang liao lie lin ling liu lo long lou lu luan lun luo lü lüe lv lve lue
ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan guang gui gun guo
ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan kuang kui kun kuo
ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang hui hun huo
ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun
qi qia qian qiang qiao qie qin qing qiong qiu qu quan que qun
xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun
zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu zhua zhuai zhuan zhuang zhui zhun zhuo
cha chai chan chang chao che chen cheng chi chong chou chu chua chuai chuan chuang chui chun chuo
sha shai shan shang shao she shei shen sheng shi shou shu shua shuai shuan shuang shui shun shuo
ran rang rao re ren reng ri rong rou ru rua ruan rui run ruo
za zai zan zang zao ze zei zen zeng zi zong zou zu zuan zui zun zuo
ca cai can cang cao ce cen ceng ci cong cou cu cuan cui cun cuo
sa sai san sang sao se sen seng si song sou su suan sui sun suo
ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun
wa wai wan wang wei wen weng wo wu e ei en eng er o ou
""".split())
# English words that are also pinyin syllables: they never count towards a run.
COMMON_EN = frozenset("""
a an ban bang bin bo can chan chi dang dao den die ding dong e fan fang gang gong gun ha hang he
hen hong la lie lo long ma man me men mi mo mu nu o pa pan pang pen pi pie pin ping ran rang run
sang she song sun tan tang tao tie ting wan wang wo ya ye yo you zen
""".split())
LOANWORDS = frozenset("café cafés résumé resumé cliché clichés fiancé fiancée décor rosé sauté touché "
                      "protégé entrée matinée élan éclair née exposé soufflé purée déjà señor".split())
_TONES = {"ā": "a", "á": "a", "ǎ": "a", "à": "a", "ē": "e", "é": "e", "ě": "e", "è": "e",
          "ī": "i", "í": "i", "ǐ": "i", "ì": "i", "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
          "ū": "u", "ú": "u", "ǔ": "u", "ù": "u", "ǖ": "ü", "ǘ": "ü", "ǚ": "ü", "ǜ": "ü"}
_TONE_TRANS = str.maketrans(_TONES)
_PINYIN_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzü") | frozenset(_TONES)


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
    """Map traditional / simplified / shinjitai variants to one key (length-preserving)."""
    return text.translate(_FOLD_TRANS)


def is_han(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or ch == "々"


def is_kana(ch: str) -> bool:
    o = ord(ch)
    return 0x3041 <= o <= 0x3096 or 0x30A1 <= o <= 0x30FA or 0x31F0 <= o <= 0x31FF


def has_kana(text: str) -> bool:
    return any(is_kana(c) for c in text)


def content(text: str) -> str:
    """Letters, digits, Han and kana only (spaces and punctuation dropped)."""
    return "".join(c for c in text if c.isalnum())


def visible_target(raw: str) -> str:
    """NFKC target text with ruby markup reduced to its base and <wbr> removed."""
    return RUBY.sub(lambda m: m.group(1), WBR.sub("", nfkc(raw)))


def lang_tag(value: object) -> str | None:
    """'en' / 'ja' / 'other' for a tag, None when there is no usable tag."""
    if not isinstance(value, str) or not value.strip():
        return None
    head = value.strip().lower().replace("_", "-").split("-")[0]
    if head in ("ja", "jp", "jpn"):
        return "ja"
    if head in ("en", "eng"):
        return "en"
    return "other"


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def longest_common(a: str, b: str) -> int:
    if not a or not b:
        return 0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def pinyin_split(word: str) -> list[str] | None:
    """Split a lower-case toneless word into pinyin syllables (fewest pieces), else None."""
    n = len(word)
    best: list[list[str] | None] = [None] * (n + 1)
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(i + 1, min(n, i + 6) + 1):
            piece = word[i:j]
            if piece in PINYIN_SYLLABLES:
                cand = best[i] + [piece]
                if best[j] is None or len(cand) < len(best[j]):
                    best[j] = cand
    return best[n]


def is_toned_pinyin(word: str) -> bool:
    low = word.lower()
    if low in LOANWORDS or not any(c in _TONES for c in low):
        return False
    if any(c not in _PINYIN_LETTERS for c in low):
        return False
    parts = pinyin_split(low.translate(_TONE_TRANS))
    return parts is not None and sum(1 for c in low if c in _TONES) <= len(parts)


def shorten(text: str, limit: int = SHOW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- span detection
def mask_allowed(text: str, allowed: list[str]) -> str:
    if not allowed:
        return text
    low = text.lower()
    use_low = len(low) == len(text)
    chars = list(text)
    for entry in allowed:
        hay, needle = (low, entry.lower()) if use_low else (text, entry)
        i = hay.find(needle)
        while i != -1:
            for k in range(i, i + len(needle)):
                chars[k] = MASK
            i = hay.find(needle, i + 1)
    return "".join(chars)


def residual_spans(lang: str, tgt: str, src_fold: str, ja_min_copy: int) -> list[dict]:
    spans = []
    if lang == "en":
        for m in EN_RUN.finditer(tgt):
            reasons = ["zhuyin"] if ZHUYIN_CHAR.search(m.group()) else []
            if any(is_han(c) for c in m.group()):
                reasons.insert(0, "han")
            spans.append({"text": m.group(), "start": m.start(), "reasons": reasons})
        return spans
    for m in JA_RUN.finditer(tgt):
        run = m.group()
        han = "".join(c for c in run if is_han(c))
        reasons = []
        if ZHUYIN_CHAR.search(run):
            reasons.append("zhuyin")
        if any(c in ZH_ONLY for c in run):
            reasons.append("zh_only_char")
        trad = sum(1 for c in run if c in TRAD_FORM)
        if trad >= 2 or (trad and len(han) >= 4):
            reasons.append("traditional_form")
        if len(han) >= 4 and re.search(f"[{_HAN}][,;:]", run):
            reasons.append("zh_punct")
        if longest_common(fold(han), src_fold) >= ja_min_copy:
            reasons.append("copied_run")
        if reasons:
            spans.append({"text": run, "start": m.start(), "reasons": reasons})
    return spans


def pinyin_spans(lang: str, tgt: str, run_min: int) -> list[dict]:
    words = [(m.start(), m.end(), m.group()) for m in LATIN_WORD.finditer(tgt)]
    spans: list[dict] = []
    i = 0
    while i < len(words):
        j = i
        while j < len(words):
            low = words[j][2].lower()
            if not (low in PINYIN_SYLLABLES or is_toned_pinyin(words[j][2])):
                break
            if j > i and not re.fullmatch(r"[\s\-]+", tgt[words[j - 1][1]:words[j][0]]):
                break
            j += 1
        run = words[i:j]
        if not run:
            i += 1
            continue
        uncommon = sum(1 for w in run if w[2].lower() not in COMMON_EN or is_toned_pinyin(w[2]))
        if lang == "en" and len(run) >= run_min and uncommon >= 2:
            spans.append({"text": tgt[run[0][0]:run[-1][1]], "start": run[0][0], "reasons": ["syllable_run"]})
        else:
            for start, end, word in run:
                if is_toned_pinyin(word):
                    spans.append({"text": word, "start": start, "reasons": ["tone_marks"]})
        i = j
    return spans


def ja_untranslated_marker(tgt: str) -> bool:
    return any(c in ZH_ONLY or c in TRAD_FORM for c in tgt) or bool(ZHUYIN_CHAR.search(tgt))


# --------------------------------------------------------------------------- data
@dataclass
class Caption:
    cid: str
    src: str
    tgt: str
    order: int
    tag: str | None = None
    lang: str = ""


@dataclass
class Session:
    source: str
    room_id: str
    session_id: str
    label: str
    captions: list[Caption] = field(default_factory=list)
    skipped_other_lang: int = 0
    default_lang: str = ""
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


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _add(sess: Session, cid: str, src: str, tgt: str | None, order: int, tag: str | None) -> bool:
    if tag == "other":
        sess.skipped_other_lang += 1
        return False
    if not src.strip() or tgt is None:
        return False
    sess.captions.append(Caption(cid=cid, src=nfkc(src), tgt=visible_target(tgt), order=order, tag=tag))
    return True


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
    multi_lang = schema == "ledger"
    for order, (r_room, r_sess, _seq, r_id, zh, tgt, tag) in enumerate(rows):
        room_s, sess_s = str(r_room if r_room is not None else ""), str(r_sess if r_sess is not None else "")
        sess = out.session(f"db:{label}", room_s, sess_s, f"{room_s}/{sess_s}")
        t = lang_tag(tag) if multi_lang else None
        if _add(sess, str(r_id), _text(zh) or "", _text(tgt) if tgt is not None else None, order, t):
            used += 1
    out.inputs.append({"path": str(path), "kind": "db", "schema": schema, "rows": len(rows), "captions": used})


def _first_str(obj: dict, keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    """First key holding a non-blank string; else the first key holding any string."""
    blank = None
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str):
            if v.strip():
                return k, v
            if blank is None:
                blank = k
    return (blank, "") if blank is not None else (None, None)


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
        tag = None
        for k in LANG_KEYS:
            if k in obj:
                tag = lang_tag(obj.get(k))
                if tag is not None:
                    break
        _sk, src = _first_str(obj, SOURCE_KEYS)
        tk, tgt = _first_str(obj, TARGET_KEYS)
        if tag is None and tk in KEY_LANG:
            tag = KEY_LANG[tk]
        cid = obj.get("id")
        if not isinstance(cid, (str, int)) or isinstance(cid, bool) or not str(cid):
            seq = obj.get("seq")
            cid = f"seq {seq}" if isinstance(seq, int) and not isinstance(seq, bool) else f"line {number}"
        if _add(sess, str(cid), src or "", tgt, first, tag):
            used += 1
    out.bad_lines += bad
    out.inputs.append({"path": str(path), "kind": "jsonl", "lines": lines, "bad_lines": bad,
                       "skipped_events": skipped, "captions": used})


def read_allow(path: Path) -> list[str]:
    path = Path(path)
    if not path.is_file():
        raise InputError(f"找不到允許清單：{path}")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise InputError(f"無法讀取允許清單：{path}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"允許清單不是 UTF-8：{path}（位元組 {exc.start}）") from exc
    entries: list[str] = []
    for line in text.splitlines():
        line = nfkc(line).strip()
        if not line or line.startswith("#"):
            continue
        if line not in entries:
            entries.append(line)
    if not entries:
        raise InputError(f"允許清單沒有任何項目：{path}")
    return sorted(entries, key=lambda e: (-len(e), e))


# --------------------------------------------------------------------------- analysis
def decide_language(sess: Session, assume: str | None) -> None:
    tags = [c.tag for c in sess.captions if c.tag in LANGS]
    targets = [c.tgt for c in sess.captions if c.tgt.strip()]
    sess.kana_ratio = round(sum(1 for t in targets if has_kana(t)) / len(targets), 4) if targets else None
    if tags:
        en, ja = tags.count("en"), tags.count("ja")
        sess.default_lang, sess.lang_basis = ("ja" if ja > en else "en"), "tgt_lang"
    elif assume:
        sess.default_lang, sess.lang_basis = assume, "assumed"
    else:
        sess.default_lang = "ja" if (sess.kana_ratio or 0.0) >= KANA_RATIO_JA else "en"
        sess.lang_basis = "kana_ratio"
    for c in sess.captions:
        c.lang = c.tag if c.tag in LANGS else sess.default_lang


def check_caption(c: Caption, allowed: list[str], settings: dict) -> list[dict]:
    """Issues of one caption: dicts with kind / spans / measures."""
    src_c = content(c.src)
    tgt_c = content(c.tgt)
    if not c.tgt.strip():
        return [{"kind": "empty"}]
    src_fold = fold(src_c)
    src_han = sum(1 for ch in src_c if is_han(ch))
    whole_allowed = bool(allowed) and c.tgt.strip().lower() in {a.lower() for a in allowed}
    sim = round(similarity(src_fold, fold(tgt_c)), 4)
    if src_han and not whole_allowed and sim >= settings["same_ratio"]:
        if c.lang == "en" or (not has_kana(c.tgt) and (ja_untranslated_marker(c.tgt)
                                                      or len(tgt_c) >= settings["ja_min_copy"])):
            return [{"kind": "untranslated", "similarity": sim}]
    issues = []
    masked = mask_allowed(c.tgt, allowed)
    spans = residual_spans(c.lang, masked, src_fold, settings["ja_min_copy"])
    if spans:
        issues.append({"kind": "residual_zh", "spans": spans})
    pys = pinyin_spans(c.lang, masked, settings["pinyin_run"])
    if pys:
        issues.append({"kind": "pinyin", "spans": pys})
    if len(src_c) >= settings["min_src_chars"]:
        ratio = round(len(tgt_c) / len(src_c), 4)
        limit = settings["en_min_ratio"] if c.lang == "en" else settings["ja_min_ratio"]
        if ratio < limit:
            issues.append({"kind": "too_short", "length_ratio": ratio, "min_ratio": limit,
                           "src_chars": len(src_c), "tgt_chars": len(tgt_c)})
    return issues


def _empty_lang_stats() -> dict:
    return {"captions": 0, "flagged": 0, "flag_rate": None, "sessions": 0, **{k: 0 for k in KINDS}}


def analyze(loaded: Loaded, allowed: list[str], settings: dict) -> dict:
    by_lang = {lang: _empty_lang_stats() for lang in LANGS}
    sessions_out = []
    issues: list[dict] = []
    warnings: list[dict] = []
    notes: list[str] = []
    skipped_other = 0
    skipped_filter = 0
    for key in sorted(loaded.sessions, key=lambda k: (k[0], k[1], k[2])):
        sess = loaded.sessions[key]
        decide_language(sess, settings["assume_lang"])
        skipped_other += sess.skipped_other_lang
        per = {lang: {"captions": 0, "flagged": 0} for lang in LANGS}
        for c in sess.captions:
            if settings["lang"] != "all" and c.lang != settings["lang"]:
                skipped_filter += 1
                continue
            stats = by_lang[c.lang]
            stats["captions"] += 1
            per[c.lang]["captions"] += 1
            found = check_caption(c, allowed, settings)
            if not found:
                continue
            stats["flagged"] += 1
            per[c.lang]["flagged"] += 1
            for item in found:
                stats[item["kind"]] += 1
            issues.append({"session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
                           "source": sess.source, "id": c.cid, "lang": c.lang,
                           "kinds": [i["kind"] for i in found], "details": found,
                           "src": c.src, "tgt": c.tgt})
        for lang in LANGS:
            if per[lang]["captions"]:
                by_lang[lang]["sessions"] += 1
        sessions_out.append({"session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
                             "source": sess.source, "captions": len(sess.captions),
                             "default_lang": sess.default_lang, "lang_basis": sess.lang_basis,
                             "kana_ratio": sess.kana_ratio, "skipped_other_lang_rows": sess.skipped_other_lang,
                             "by_lang": per})
    for stats in by_lang.values():
        if stats["captions"]:
            stats["flag_rate"] = round(stats["flagged"] / stats["captions"], 4)
    checked = sum(s["captions"] for s in by_lang.values())
    if loaded.bad_lines:
        warnings.append({"code": "bad_jsonl_lines",
                         "message_zh": f"JSONL 有 {loaded.bad_lines} 行不是合法 JSON，已略過。"})
    if checked == 0:
        warnings.append({"code": "no_captions",
                         "message_zh": "沒有可檢查的英文／日文字幕（需要同時有中文原文與譯文欄位），資料不足以判斷。"})
    if skipped_other:
        notes.append(f"略過非英文／日文（tgt_lang 為其他語言）的字幕 {skipped_other} 筆。")
    if skipped_filter:
        notes.append(f"依 --lang {settings['lang']} 略過其他語言字幕 {skipped_filter} 筆。")
    guessed = [s["session"] for s in sessions_out if s["lang_basis"] == "kana_ratio" and s["captions"]]
    if guessed:
        notes.append(f"{len(guessed)} 個場次沒有 tgt_lang 標記，依假名比例推定語言（≥30 % 譯文含假名＝日文）："
                     + "、".join(guessed[:5]) + ("…" if len(guessed) > 5 else ""))
    if allowed:
        notes.append(f"允許清單 {len(allowed)} 項：這些片段不算殘留中文／拼音。")
    notes.append("日文本來就用漢字：只有含注音、日文不用的中文字、繁體舊字形（兩個以上，或在四字以上的漢字片段裡）、中文標點連接，"
                 "或與原文最長共同子字串達門檻的漢字片段才算殘留中文。")
    total_issues = len(issues)
    shown = issues[:settings["max_issues"]]
    if issues:
        status, code = "issues", EXIT_ISSUES
    elif warnings:
        status, code = "warn", EXIT_WARN
    else:
        status, code = "ok", EXIT_OK
    report = {
        "tool": TOOL_NAME, "schema_version": SCHEMA_VERSION, "status": status, "exit_code": code,
        "settings": dict(settings), "inputs": loaded.inputs, "sessions": sessions_out,
        "summary": {"captions": checked, "flagged": total_issues, "by_lang": by_lang},
        "issues": shown, "issues_total": total_issues, "issues_truncated": total_issues - len(shown),
        "warnings": warnings, "notes": notes,
        "suggestions": _suggestions(by_lang, warnings, settings),
    }
    report["summary_zh"] = render_text(report, issues, settings)
    return report


def _suggestions(by_lang: dict, warnings: list[dict], settings: dict) -> list[str]:
    out = []
    total = {k: sum(by_lang[lang][k] for lang in LANGS) for k in KINDS}
    if total["untranslated"] or total["empty"]:
        out.append("整句未翻／譯文空白：檢查該時段翻譯引擎是否逾時或回傳原文，於後台重新翻譯或人工補譯；本工具不修改任何檔案。")
    if total["residual_zh"] or total["pinyin"]:
        out.append("殘留中文／拼音：多半是專有名詞或人名沒譯出；在後台詞彙表補上該詞的英文／日文譯法（含讀音），"
                   "確定要保留的寫法可放進 --allow 清單。")
    if total["too_short"]:
        out.append(f"譯文過短：抽查原文是否被截斷或漏譯一半（門檻 en {settings['en_min_ratio']}、"
                   f"ja {settings['ja_min_ratio']} 字／原文字，可用 --en-min-ratio／--ja-min-ratio 調整）。")
    codes = {w["code"] for w in warnings}
    if "no_captions" in codes:
        out.append("確認輸入的是已翻譯的字幕（字幕庫 status='ready'、帳本 segments.status='translated'）。")
    if "bad_jsonl_lines" in codes:
        out.append("JSONL 有壞行：確認檔案沒有在寫入中被截斷。")
    return out


def _describe(issue: dict, kind: str) -> str:
    detail = next(d for d in issue["details"] if d["kind"] == kind)
    head = f"  [{issue['lang']}] {KIND_ZH[kind]} {issue['session']} {issue['id']}"
    if kind in ("residual_zh", "pinyin"):
        parts = "、".join(f"「{shorten(s['text'], 20)}」（{'／'.join(REASON_ZH[r] for r in s['reasons'])}）"
                         for s in detail["spans"][:3])
        return f"{head}：{parts}｜譯文「{shorten(issue['tgt'])}」"
    if kind == "untranslated":
        return f"{head}：相似度 {detail['similarity']:.2f}｜原文「{shorten(issue['src'])}」→ 譯文「{shorten(issue['tgt'])}」"
    if kind == "too_short":
        return (f"{head}：{detail['tgt_chars']}/{detail['src_chars']} 字（比例 {detail['length_ratio']:.2f} < "
                f"{detail['min_ratio']}）｜原文「{shorten(issue['src'])}」→ 譯文「{shorten(issue['tgt'])}」")
    return f"{head}：原文「{shorten(issue['src'])}」沒有譯文"


def render_text(r: dict, all_issues: list[dict], settings: dict) -> str:
    s = r["summary"]
    head = {"ok": "沒有發現漏譯或殘留中文（exit 0）", "issues": f"發現 {s['flagged']} 筆疑似問題字幕（exit 1）",
            "warn": "資料不足或有警告（exit 2）"}[r["status"]]
    lines = [f"en/ja 漏譯／殘留中文檢查：{head}"]
    lines.append("輸入：" + "、".join(
        f"{Path(i['path']).name}（{'資料庫 ' + i['schema'] if i['kind'] == 'db' else 'JSONL'}，字幕 {i['captions']} 筆）"
        for i in r["inputs"]))
    for lang in LANGS:
        st = s["by_lang"][lang]
        if not st["captions"]:
            lines.append(f"{LANG_ZH[lang]}：沒有字幕")
            continue
        kinds = "、".join(f"{KIND_ZH[k]} {st[k]}" for k in KINDS)
        lines.append(f"{LANG_ZH[lang]}：{st['sessions']} 個場次、檢查 {st['captions']} 筆、有問題 {st['flagged']} 筆"
                     f"（{st['flag_rate'] * 100:.1f}%）；{kinds}")
    if all_issues:
        lines.append(f"問題清單（每種語言每類最多 {settings['max_examples']} 筆）：")
        for lang in LANGS:
            for kind in KINDS:
                hits = [i for i in all_issues if i["lang"] == lang and kind in i["kinds"]]
                for issue in hits[:settings["max_examples"]]:
                    lines.append(_describe(issue, kind))
                if len(hits) > settings["max_examples"]:
                    lines.append(f"  [{lang}] {KIND_ZH[kind]} 另有 {len(hits) - settings['max_examples']} 筆…")
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
    ap = _Parser(prog="en_ja_untranslated_span_check.py",
                 description="唯讀檢查英文／日文字幕的殘留中文／拼音、整句未翻與疑似漏譯（en/ja 分開統計）。")
    ap.add_argument("--db", type=Path, metavar="PATH", help="字幕資料庫（captions.sqlite3 或 zen.sqlite3 帳本，唯讀開啟）")
    ap.add_argument("--jsonl", type=Path, action="append", default=[], metavar="PATH", help="字幕事件 JSONL（可重複）")
    ap.add_argument("--allow", type=Path, metavar="PATH",
                    help="允許保留的片段（UTF-8，每行一項，# 註解；例如人名、刻意保留的漢字或拼音）")
    ap.add_argument("--lang", choices=("all", "en", "ja"), default="all", help="只檢查某一種譯文語言（預設 all）")
    ap.add_argument("--assume-lang", choices=LANGS, help="沒有 tgt_lang 標記的場次一律視為這個語言")
    ap.add_argument("--room", help="只看這個 room_id")
    ap.add_argument("--session", help="只看這個 session_id")
    ap.add_argument("--same-ratio", type=float, default=DEFAULT_SAME_RATIO,
                    help="整句未翻：譯文與原文相似度門檻（預設 0.85）")
    ap.add_argument("--en-min-ratio", type=float, default=DEFAULT_EN_MIN_RATIO,
                    help="譯文過短：英文字母數／原文字數下限（預設 1.0）")
    ap.add_argument("--ja-min-ratio", type=float, default=DEFAULT_JA_MIN_RATIO,
                    help="譯文過短：日文字數／原文字數下限（預設 0.5）")
    ap.add_argument("--min-src-chars", type=int, default=DEFAULT_MIN_SRC_CHARS,
                    help="原文至少幾個字才檢查譯文過短（預設 6）")
    ap.add_argument("--ja-min-copy", type=int, default=DEFAULT_JA_MIN_COPY,
                    help="日文：與原文相同的連續漢字達幾個字算照抄（預設 8）")
    ap.add_argument("--pinyin-run", type=int, default=DEFAULT_PINYIN_RUN,
                    help="英文：連續幾個無聲調拼音音節算殘留拼音（預設 3）")
    ap.add_argument("--max-examples", type=int, default=DEFAULT_MAX_EXAMPLES, help="摘要每類列出幾筆（預設 5）")
    ap.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES, help="JSON 最多列出幾筆問題字幕（預設 500）")
    ap.add_argument("--json", type=Path, metavar="OUT", help="另寫 JSON 報告到新檔（已存在就拒絕，不覆蓋）")
    return ap


def _check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.db is None and not args.jsonl:
        ap.error("請給 --db 或 --jsonl（至少一個）")
    if not 0.5 <= args.same_ratio <= 1.0:
        ap.error("--same-ratio 必須介於 0.5 與 1.0")
    if not 0.0 < args.en_min_ratio <= 10.0:
        ap.error("--en-min-ratio 必須大於 0 且不超過 10")
    if not 0.0 < args.ja_min_ratio <= 10.0:
        ap.error("--ja-min-ratio 必須大於 0 且不超過 10")
    if args.min_src_chars < 1:
        ap.error("--min-src-chars 至少為 1")
    if args.ja_min_copy < 2:
        ap.error("--ja-min-copy 至少為 2")
    if args.pinyin_run < 2:
        ap.error("--pinyin-run 至少為 2")
    if args.max_examples < 1:
        ap.error("--max-examples 至少為 1")
    if args.max_issues < 0:
        ap.error("--max-issues 不能是負數")
    if args.json is not None:
        target = os.path.abspath(str(args.json))
        inputs = [p for p in [args.db, args.allow, *args.jsonl] if p is not None]
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
    settings = {"lang": args.lang, "assume_lang": args.assume_lang, "room": args.room, "session": args.session,
                "same_ratio": args.same_ratio, "en_min_ratio": args.en_min_ratio,
                "ja_min_ratio": args.ja_min_ratio, "min_src_chars": args.min_src_chars,
                "ja_min_copy": args.ja_min_copy, "pinyin_run": args.pinyin_run,
                "max_examples": args.max_examples, "max_issues": args.max_issues}
    loaded = Loaded()
    try:
        allowed = read_allow(args.allow) if args.allow is not None else []
        if args.db is not None:
            read_db(args.db, loaded, args.room, args.session)
        for p in args.jsonl:
            read_jsonl(p, loaded, args.room, args.session)
    except InputError as exc:
        sys.stderr.write(f"輸入錯誤：{exc}\n")
        return EXIT_INPUT
    report = analyze(loaded, allowed, settings)
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
