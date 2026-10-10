"""Export zh-en / zh-ja parallel corpora from zen.sqlite3 (ledger + TM + glossary) for LoRA fine-tuning.

python -m app.admin.corpus_export --db PATH --out DIR [--pairs zh-en,zh-ja] [--license-map map.json]
       [--min-quality 0.0] [--min-tm-quality 3] [--include-unknown-license] [--near-dup] [--dry-run]

Design (database-review §8, QA D4/D11):
* Read-only: ``file:...?mode=ro`` + ``PRAGMA query_only=ON``, one read transaction for a consistent
  snapshot, so it is safe while the live ledger writes in WAL mode. Nothing is ever written to the DB.
* Never opens the identity DB (zen-identity.sqlite3): refuses that file, and refuses any DB that
  contains identity tables. No query joins ``speakers``; only text columns are read.
* The schema has no license/consent column. Licenses come from an optional JSON license map
  (``--license-map``) keyed by room, session, TM origin or glossary; anything unmapped is
  ``unknown`` and excluded unless ``--include-unknown-license``. See README for the format.
* Pipeline per record: select -> license -> PII (mask or drop) -> quality filters -> exact dedupe
  -> optional near-dup. Every drop is counted under exactly one reason (the first that applies).
* Output: ``corpus.<pair>.jsonl`` per pair + ``corpus_stats.json``, each written to a temp file in
  the same directory, fsynced, then ``os.replace``d (atomic on the same filesystem).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PAIRS = ("zh-en", "zh-ja")
SOURCES = ("ledger", "tm", "glossary")
UNKNOWN = "unknown"
# Licenses that allow training by default (configurable with --allow-license).
DEFAULT_ALLOWED = ("train-ok", "owner", "consented", "public-domain", "cc0", "cc-by", "cc-by-4.0",
                   "cc-by-sa", "cc-by-sa-4.0")
DEFAULT_SALT = "zen-corpus-v1"        # not secret; set --hash-salt / ZEN_CORPUS_HASH_SALT for unguessable hashes
IDENTITY_TABLES = ("user_accounts", "speaker_identities", "voiceprints", "api_tokens")
HUMAN_ORIGINS = {"human", "post_edit", "correction", "approved"}
LEDGER_QUALITY = {"human": 1.0, "post_edit": 0.9, "tm_exact": 0.8, "import": 0.6, "mt": 0.5}
RATIO_BOUNDS = {"zh-en": (0.3, 10.0), "zh-ja": (0.3, 4.0)}    # len(tgt)/len(src), applied when len(src) >= 4
MAX_CHARS = 1000

# ---------------------------------------------------------------- normalisation
_PUNCT = str.maketrans({
    "。": ".", "、": ",", "，": ",", "．": ".", "：": ":", "；": ";", "！": "!", "？": "?",
    "「": '"', "」": '"', "『": '"', "』": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
    "（": "(", "）": ")", "【": "[", "】": "]", "〈": "<", "〉": ">", "《": "<", "》": ">",
    "～": "~", "〜": "~", "－": "-", "—": "-", "–": "-", "‧": "·", "・": "·", "…": "...",
})
_WS = re.compile(r"\s+")
# a space next to CJK or punctuation carries no meaning for dedupe ("你好, 世界" == "你好，世界")
_SOFT_WS = re.compile(r" (?=[^0-9A-Za-z])|(?<=[^0-9A-Za-z]) ")


def clean(text: str | None) -> str:
    """Output form: whitespace collapsed, original punctuation kept."""
    return _WS.sub(" ", (text or "").replace("\u3000", " ")).strip()


def norm_key(text: str | None) -> str:
    """Dedupe form: NFKC, full/half-width punctuation unified, whitespace collapsed, casefolded."""
    t = unicodedata.normalize("NFKC", text or "").translate(_PUNCT)
    return _SOFT_WS.sub("", _WS.sub(" ", t).strip()).casefold()


# ---------------------------------------------------------------- PII
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'，。、）)\]]+")
_URL_SECRET = re.compile(r"(?i)([?&#;](?:[a-z_]*token|key|apikey|api_key|sig|signature|secret|password|pwd|auth|code|session|sid|"
                         r"x-amz-[a-z-]+)=)|//[^/@\s]+:[^/@\s]+@")
_TW_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][1289]\d{8}(?![A-Za-z0-9])")
_PHONE = re.compile(
    r"(?<![0-9A-Za-z+])(?:"
    r"\+?886[\s\-]?\(?0?\)?9\d{2}[\s\-]?\d{3}[\s\-]?\d{3}"            # TW mobile intl
    r"|09\d{2}[\s\-]?\d{3}[\s\-]?\d{3}"                                # TW mobile
    r"|\+?886[\s\-]?\(?0?\)?[2-8][\s\-]?\d{3,4}[\s\-]?\d{4}"           # TW landline intl
    r"|\(?0[2-8]\)?[\s\-]?\d{3,4}[\s\-]?\d{4}"                         # TW landline / JP 0X-XXXX-XXXX
    r"|\+?81[\s\-]?\(?0?\)?[1-9]\d{0,3}[\s\-]?\d{1,4}[\s\-]?\d{4}"     # JP intl
    r"|0[5-9]0[\s\-]?\d{4}[\s\-]?\d{4}"                                # JP mobile / IP phone
    r"|0\d{1,4}[\s\-]\d{1,4}[\s\-]\d{4}"                               # JP landline with separators
    r"|\+\d{1,3}[\s\-]?(?:\(?\d{1,4}\)?[\s\-]?){2,4}\d{2,4}"           # generic international
    r")(?![0-9A-Za-z])")
PII_TAGS = ("email", "url", "phone", "tw_id", "name")


def _digits(s: str) -> int:
    return sum(ch.isdigit() for ch in s)


class Redactor:
    """Deterministic masking: each match becomes a fixed tag ([EMAIL], [URL], [PHONE], [ID], [NAME])."""

    def __init__(self, names: list[str] | None = None, mask_all_urls: bool = False):
        names = sorted({n.strip() for n in (names or []) if len(n.strip()) >= 2}, key=lambda n: (-len(n), n))
        self._names = re.compile("|".join(re.escape(n) for n in names)) if names else None
        self.mask_all_urls = mask_all_urls

    def redact(self, text: str) -> tuple[str, dict[str, int]]:
        hits: dict[str, int] = {}

        def sub(rx, tag, key, s, keep=None):
            def rep(m):
                if keep is not None and keep(m.group(0)):
                    return m.group(0)
                hits[key] = hits.get(key, 0) + 1
                return tag
            return rx.sub(rep, s)

        t = unicodedata.normalize("NFKC", text)    # full-width digits/@ become ASCII so patterns match
        if t != text and not any(rx.search(t) for rx in (_EMAIL, _URL, _TW_ID, _PHONE)) and not (
                self._names and self._names.search(t)):
            return text, hits                      # keep original (NFKC) form when nothing to mask
        t = sub(_URL, "[URL]", "url", t, keep=lambda u: not self.mask_all_urls and not _URL_SECRET.search(u))
        t = sub(_EMAIL, "[EMAIL]", "email", t)
        t = sub(_TW_ID, "[ID]", "tw_id", t)
        t = sub(_PHONE, "[PHONE]", "phone", t, keep=lambda p: _digits(p) < 8)
        if self._names:
            t = sub(self._names, "[NAME]", "name", t)
        return (t if hits else text), hits


# ---------------------------------------------------------------- scripts
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]")


def script_problem(text: str, lang: str, *, ja_require_kana_len: int = 6) -> str | None:
    if lang == "zh":
        return None if _HAN.search(text) else "src_not_zh"
    if lang == "ja":
        kana, han = len(_KANA.findall(text)), len(_HAN.findall(text))
        if not kana and not han:
            return "wrong_script"
        if not kana and ja_require_kana_len and han >= ja_require_kana_len:
            return "ja_no_kana"                    # long kanji-only target: likely untranslated Chinese
        return None
    if lang == "en":
        letters, han = len(_LATIN.findall(text)), len(_HAN.findall(text)) + len(_KANA.findall(text))
        if not letters or han > max(2, letters // 3):
            return "wrong_script"
    return None


# ---------------------------------------------------------------- config / records
@dataclass
class ExportConfig:
    pairs: tuple[str, ...] = PAIRS
    sources: tuple[str, ...] = SOURCES
    allowed_licenses: tuple[str, ...] = DEFAULT_ALLOWED
    include_unknown: bool = False
    license_map: dict = field(default_factory=dict)
    min_quality: float = 0.0
    min_tm_quality: int = 3
    near_dup: bool = False
    pii_mode: str = "mask"                 # mask | drop
    names: list[str] = field(default_factory=list)
    mask_all_urls: bool = False
    include_live: bool = False
    ja_require_kana_len: int = 6
    hash_salt: str = DEFAULT_SALT


@dataclass
class Rec:
    pair: str
    src: str
    tgt: str
    source: str
    origin: str
    license: str
    quality: float
    human: bool
    ts: float
    session: str | None = None
    extra_drop: str | None = None          # reason decided while selecting (e.g. stale pair)


def _hash(salt: str, value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()[:16]


def load_license_map(path: str | Path | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("license map must be a JSON object")
    return data


def resolve_license(lm: dict, source: str, *, room: str | None = None, session: str | None = None,
                    origin: str | None = None) -> str:
    """Most specific wins: session > room > (tm origin | glossary) > per-source default > default."""
    def get(section, key):
        sec = lm.get(section) or {}
        return sec.get(key) if key is not None and isinstance(sec, dict) else None
    if source == "ledger":
        lic = get("sessions", session) or get("rooms", room)
    elif source == "tm":
        lic = get("tm_origins", origin) or get("rooms", room)
    else:
        lic = lm.get("glossary") if isinstance(lm.get("glossary"), str) else None
    lic = lic or get("source_defaults", source) or lm.get("default") or UNKNOWN
    return str(lic).strip().lower() or UNKNOWN


# ---------------------------------------------------------------- DB access
class IdentityDbRefused(RuntimeError):
    pass


def ro_uri(path) -> str:
    """Absolute path -> SQLite read-only URI. pathlib percent-encodes spaces/#/? and gives
    file:///C:/... for Windows paths, which SQLite's URI parser accepts."""
    return f"{path.as_uri()}?mode=ro"


def open_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"database not found: {p}")
    if "identity" in p.name.lower():
        raise IdentityDbRefused(f"refusing to read an identity database: {p.name}")
    conn = sqlite3.connect(ro_uri(p.resolve()), uri=True, isolation_level=None,
                           timeout=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.OperationalError as exc:
        conn.close()
        if "readonly" in str(exc):
            # WAL readers need <db>-shm; SQLite cannot create it in a read-only directory.
            raise sqlite3.OperationalError(
                f"{exc}: WAL 模式的唯讀連線需要可建立 {p.name}-shm；請讓資料夾可寫或先用備份副本匯出") from exc
        raise
    if names & set(IDENTITY_TABLES):
        conn.close()
        raise IdentityDbRefused("database contains identity tables; corpus export only reads zen.sqlite3")
    return conn


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _cols(c, table) -> set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def select_records(c: sqlite3.Connection, cfg: ExportConfig, stats: dict) -> list[Rec]:
    tables = _tables(c)
    tgts = {p: p.split("-")[1] for p in cfg.pairs}
    recs: list[Rec] = []
    lm = cfg.license_map
    if "ledger" in cfg.sources and {"transcripts", "translations", "segments", "sessions"} <= tables:
        sql = """
          SELECT s.id AS sid, s.room_id, s.status AS sess_status, g.status AS seg_status,
                 t.id AS tid, t.text AS zh, t.origin AS t_origin,
                 tr.tgt_lang, tr.text AS tgt, tr.origin, tr.status AS tr_status, tr.transcript_id, tr.qe_score,
                 tr.created_at
          FROM translations tr
          JOIN segments g    ON g.id = tr.segment_id
          JOIN sessions s    ON s.id = g.session_id
          JOIN transcripts t ON t.segment_id = g.id AND t.is_current = 1
          WHERE tr.is_current = 1 AND tr.tgt_lang IN ({})
          ORDER BY tr.id""".format(",".join("?" * len(tgts)))
        for r in c.execute(sql, list(tgts.values())):
            pair = f"zh-{r['tgt_lang']}"
            origin = r["origin"] or "mt"
            q = LEDGER_QUALITY.get(origin, 0.5)
            if origin == "mt" and r["qe_score"] is not None:
                q = max(0.0, min(1.0, float(r["qe_score"])))
            drop = None
            if "redacted" in (origin, r["t_origin"]):
                drop = "redacted"
            elif r["seg_status"] in ("deleted", "error", "timeout", "cancelled", "missing", "silent"):
                drop = "segment_status"
            elif (r["tr_status"] or "ok") != "ok":
                drop = "translation_not_ok"
            elif r["transcript_id"] is not None and r["transcript_id"] != r["tid"]:
                drop = "stale_pair"            # translation was made from an older transcript version
            elif r["sess_status"] == "live" and not cfg.include_live:
                drop = "session_live"
            recs.append(Rec(pair, r["zh"] or "", r["tgt"] or "", "ledger", origin,
                            resolve_license(lm, "ledger", room=r["room_id"], session=r["sid"]),
                            q, origin in HUMAN_ORIGINS, float(r["created_at"] or 0),
                            _hash(cfg.hash_salt, r["sid"]), drop))
    if "tm" in cfg.sources and "tm_units" in tables:
        sql = """SELECT src_lang, tgt_lang, src_text, tgt_text, origin, quality, room_id, updated_at
                 FROM tm_units WHERE tgt_lang IN ({}) ORDER BY id""".format(",".join("?" * len(tgts)))
        for r in c.execute(sql, list(tgts.values())):
            drop = None
            if not str(r["src_lang"] or "").lower().startswith("zh"):
                drop = "src_not_zh"
            elif int(r["quality"]) < cfg.min_tm_quality:
                drop = "low_tm_quality"
            recs.append(Rec(f"zh-{r['tgt_lang']}", r["src_text"] or "", r["tgt_text"] or "", "tm", r["origin"],
                            resolve_license(lm, "tm", room=r["room_id"], origin=r["origin"]),
                            int(r["quality"]) / 5.0, r["origin"] in HUMAN_ORIGINS, float(r["updated_at"] or 0),
                            None, drop))
    if "glossary" in cfg.sources and "glossary_terms" in tables:
        if "glossary_term_targets" in tables:              # schema v3
            sql = """SELECT g.zh, x.tgt_lang, x.text AS tgt, x.locked, x.status, x.source, x.updated_at
                     FROM glossary_term_targets x JOIN glossary_terms g ON g.id = x.term_id
                     WHERE x.tgt_lang IN ({}) ORDER BY x.id""".format(",".join("?" * len(tgts)))
            rows = c.execute(sql, list(tgts.values()))
        elif "en" in tgts.values():                         # schema v2: en only
            rows = c.execute("SELECT zh, 'en' AS tgt_lang, en AS tgt, locked, status, source, updated_at "
                             "FROM glossary_terms ORDER BY id")
        else:
            rows = []
        for r in rows:
            drop = None if r["status"] == "active" else "glossary_not_active"
            recs.append(Rec(f"zh-{r['tgt_lang']}", r["zh"] or "", r["tgt"] or "", "glossary", r["source"],
                            resolve_license(lm, "glossary"), 1.0 if r["locked"] else 0.9,
                            r["source"] in ("manual", "correction"), float(r["updated_at"] or 0), None, drop))
    for rec in recs:
        stats["input"][rec.pair][rec.source] += 1
    return recs


# ---------------------------------------------------------------- pipeline
def _new_stats(cfg: ExportConfig) -> dict:
    return {
        "input": {p: {s: 0 for s in SOURCES} for p in cfg.pairs},
        "output": {p: {s: 0 for s in SOURCES} for p in cfg.pairs},
        "output_total": {p: 0 for p in cfg.pairs},
        "by_license": {p: {} for p in cfg.pairs},
        "dropped": {p: {} for p in cfg.pairs},
        "dedup": {p: {"exact_removed": 0, "near_removed": 0} for p in cfg.pairs},
        "pii": {"rows_redacted": 0, "rows_dropped": 0, "matches": {t: 0 for t in PII_TAGS}},
    }


def _drop(stats, pair, reason):
    d = stats["dropped"][pair]
    d[reason] = d.get(reason, 0) + 1


def _rank(r: Rec) -> tuple:
    return (r.human, r.quality, r.ts)


def run_export(conn: sqlite3.Connection, cfg: ExportConfig) -> tuple[dict[str, list[dict]], dict]:
    stats = _new_stats(cfg)
    allowed = {a.strip().lower() for a in cfg.allowed_licenses}
    red = Redactor(cfg.names, cfg.mask_all_urls)
    conn.execute("BEGIN")                     # one read snapshot for all sources
    try:
        recs = select_records(conn, cfg, stats)
    finally:
        conn.execute("COMMIT")
    kept: dict[str, dict[tuple, Rec]] = {p: {} for p in cfg.pairs}
    for r in recs:
        p = r.pair
        if r.extra_drop:
            _drop(stats, p, r.extra_drop); continue
        if r.license == UNKNOWN:
            if not cfg.include_unknown:
                _drop(stats, p, "license_unknown"); continue
        elif r.license not in allowed:
            _drop(stats, p, "license_disallowed"); continue
        src, sh = red.redact(r.src)
        tgt, th = red.redact(r.tgt)
        if sh or th:
            if cfg.pii_mode == "drop":
                stats["pii"]["rows_dropped"] += 1
                for k, v in {**sh}.items():
                    stats["pii"]["matches"][k] += v
                for k, v in th.items():
                    stats["pii"]["matches"][k] += v
                _drop(stats, p, "pii"); continue
            stats["pii"]["rows_redacted"] += 1
            for h in (sh, th):
                for k, v in h.items():
                    stats["pii"]["matches"][k] += v
        src, tgt = clean(src), clean(tgt)
        reason = _quality_problem(src, tgt, p, r, cfg)
        if reason:
            _drop(stats, p, reason); continue
        r.src, r.tgt = src, tgt
        key = (p, norm_key(src), norm_key(tgt))
        old = kept[p].get(key)
        if old is not None:
            stats["dedup"][p]["exact_removed"] += 1
            if _rank(r) > _rank(old):
                kept[p][key] = r
            continue
        kept[p][key] = r
    out: dict[str, list[dict]] = {}
    for p in cfg.pairs:
        rows = list(kept[p].values())
        if cfg.near_dup:
            best: dict[str, Rec] = {}
            for r in rows:
                k = norm_key(r.src)
                if k in best:
                    stats["dedup"][p]["near_removed"] += 1
                    if _rank(r) > _rank(best[k]):
                        best[k] = r
                else:
                    best[k] = r
            rows = list(best.values())
        rows.sort(key=lambda r: (r.source, norm_key(r.src), norm_key(r.tgt)))
        lines = []
        for r in rows:
            sl, tl = "zh", p.split("-")[1]
            rid = hashlib.sha256(f"{p}|{norm_key(r.src)}|{norm_key(r.tgt)}".encode("utf-8")).hexdigest()[:24]
            lines.append({"id": rid, "src": r.src, "tgt": r.tgt, "src_lang": sl, "tgt_lang": tl,
                          "source": r.source, "origin": r.origin, "license": r.license,
                          "quality": round(r.quality, 3), "session": r.session})
            stats["output"][p][r.source] += 1
            stats["by_license"][p][r.license] = stats["by_license"][p].get(r.license, 0) + 1
        stats["output_total"][p] = len(lines)
        out[p] = lines
    return out, stats


def _quality_problem(src: str, tgt: str, pair: str, r: Rec, cfg: ExportConfig) -> str | None:
    def body(s):    # text without mask tags
        return re.sub(r"\[(?:EMAIL|URL|PHONE|ID|NAME)\]", "", s).strip()
    if not body(src) or not body(tgt):
        return "empty"
    if len(src) > MAX_CHARS or len(tgt) > MAX_CHARS * 4:
        return "too_long"
    if norm_key(src) == norm_key(tgt):
        return "src_equals_tgt"
    sp = script_problem(src, "zh")
    if sp:
        return sp
    tp = script_problem(tgt, pair.split("-")[1], ja_require_kana_len=cfg.ja_require_kana_len)
    if tp:
        return tp
    lo, hi = RATIO_BOUNDS.get(pair, (0.0, float("inf")))
    if r.source != "glossary" and len(src) >= 4:
        ratio = len(tgt) / len(src)
        if ratio < lo or ratio > hi:
            return "length_ratio"
    if r.quality < cfg.min_quality:
        return "low_quality"
    return None


# ---------------------------------------------------------------- output
def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_outputs(out_dir: str | Path, corpora: dict[str, list[dict]], stats: dict) -> list[Path]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for pair, rows in corpora.items():
        data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")
        p = d / f"corpus.{pair}.jsonl"
        _atomic_write(p, data)
        written.append(p)
    sp = d / "corpus_stats.json"
    _atomic_write(sp, json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    written.append(sp)
    return written


def export(db_path: str | Path, out_dir: str | Path | None, cfg: ExportConfig, *, dry_run: bool = False) -> dict:
    conn = open_readonly(db_path)
    try:
        corpora, stats = run_export(conn, cfg)
    finally:
        conn.close()
    stats["meta"] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "pairs": list(cfg.pairs), "sources": list(cfg.sources),
        "allowed_licenses": sorted(cfg.allowed_licenses), "include_unknown_license": cfg.include_unknown,
        "min_quality": cfg.min_quality, "min_tm_quality": cfg.min_tm_quality, "near_dup": cfg.near_dup,
        "pii_mode": cfg.pii_mode, "hash_salt": "default" if cfg.hash_salt == DEFAULT_SALT else "custom",
        "dry_run": dry_run,
    }
    if not dry_run:
        if out_dir is None:
            raise ValueError("--out is required unless --dry-run")
        write_outputs(out_dir, corpora, stats)
    return stats


# ---------------------------------------------------------------- CLI
def _csv(v: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in v.split(",") if x.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="從 zen.sqlite3（ledger/TM/詞彙）匯出 zh-en、zh-ja 平行語料（JSONL）")
    ap.add_argument("--db", required=True, help="zen.sqlite3 路徑（唯讀開啟；不可是 zen-identity.sqlite3）")
    ap.add_argument("--out", help="輸出資料夾（--dry-run 時可省略）")
    ap.add_argument("--pairs", default=",".join(PAIRS), help="zh-en,zh-ja")
    ap.add_argument("--sources", default=",".join(SOURCES), help="ledger,tm,glossary")
    ap.add_argument("--license-map", help="授權對照 JSON（見 README）；未對照者為 unknown")
    ap.add_argument("--allow-license", default=",".join(DEFAULT_ALLOWED), help="允許訓練的授權值（逗號分隔）")
    ap.add_argument("--include-unknown-license", action="store_true", help="納入授權不明的列（預設排除）")
    ap.add_argument("--min-quality", type=float, default=0.0, help="整體品質下限 0..1（預設 0）")
    ap.add_argument("--min-tm-quality", type=int, default=3, help="TM quality 下限 1..5（預設 3）")
    ap.add_argument("--near-dup", action="store_true", help="同一正規化原文只留一筆（人工>品質>最新）")
    ap.add_argument("--pii-mode", choices=("mask", "drop"), default="mask")
    ap.add_argument("--names-file", help="要遮蔽的姓名清單（UTF-8，一行一個）；本工具不讀個資庫")
    ap.add_argument("--mask-all-urls", action="store_true", help="遮蔽所有 URL（預設只遮含 token/金鑰參數者）")
    ap.add_argument("--include-live", action="store_true", help="納入尚在進行中的場次")
    ap.add_argument("--hash-salt", default=os.environ.get("ZEN_CORPUS_HASH_SALT") or DEFAULT_SALT,
                    help="場次 id 雜湊用的鹽（預設取 ZEN_CORPUS_HASH_SALT）")
    ap.add_argument("--dry-run", action="store_true", help="只計算並印出統計，不寫檔")
    a = ap.parse_args(argv)
    pairs = _csv(a.pairs)
    bad = [p for p in pairs if p not in PAIRS]
    srcs = _csv(a.sources)
    if bad or not pairs or any(s not in SOURCES for s in srcs) or not srcs:
        ap.error(f"pairs 只能是 {PAIRS}，sources 只能是 {SOURCES}")
    if not 1 <= a.min_tm_quality <= 5 or not 0.0 <= a.min_quality <= 1.0:
        ap.error("--min-tm-quality 需 1..5，--min-quality 需 0..1")
    if not a.dry_run and not a.out:
        ap.error("--out is required unless --dry-run")
    names = Path(a.names_file).read_text(encoding="utf-8").splitlines() if a.names_file else []
    cfg = ExportConfig(pairs=pairs, sources=srcs, allowed_licenses=_csv(a.allow_license),
                       include_unknown=a.include_unknown_license, license_map=load_license_map(a.license_map),
                       min_quality=a.min_quality, min_tm_quality=a.min_tm_quality, near_dup=a.near_dup,
                       pii_mode=a.pii_mode, names=names, mask_all_urls=a.mask_all_urls, include_live=a.include_live,
                       hash_salt=a.hash_salt)
    try:
        stats = export(a.db, a.out, cfg, dry_run=a.dry_run)
    except (IdentityDbRefused, FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"corpus_export 失敗：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output_total": stats["output_total"], "dropped": stats["dropped"], "dedup": stats["dedup"],
                      "pii": stats["pii"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
