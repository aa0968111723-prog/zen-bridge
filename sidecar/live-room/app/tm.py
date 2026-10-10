"""Translation memory (TM) over zen.sqlite3 ``tm_units``.

- exact match (normalized source + target language) bypasses the model entirely;
- fuzzy matches (FTS5 trigram candidates, difflib ratio >= BREEZE_TM_FUZZY_MIN) become
  few-shot ``examples`` in the prompt;
- a TM translation that drops a locked glossary term is not trusted as a bypass: the model
  is asked instead, and the TM line is only a fallback (the pipeline still flags it).

Reads use short-lived read-only connections. Writes (use_count) go through the ledger,
which is the single writer. glossary.py is imported, never modified.
"""
from __future__ import annotations

import difflib
import hashlib
import inspect
import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.glossary import FOLD, missing_locked
from app.translate import TranslateResult

log = logging.getLogger("breeze.tm")

_PUNCT = re.compile(r"[\s\u3000-\u303f\uff00-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
                    r"\u2000-\u206f!-/:-@\[-`{-~]+")
# One-to-one simplified -> traditional characters common in Dharma lectures. Matching only;
# displayed text is never changed. Extends glossary.FOLD without editing glossary.py.
_EXTRA_S = "缘净无为进说经观佛门众戒乐现见实们这个时间问题应该还没头发对来为过"
_EXTRA_T = "緣淨無為進說經觀佛門眾戒樂現見實們這個時間問題應該還沒頭發對來為過"
_EXTRA_FOLD = str.maketrans(_EXTRA_S, _EXTRA_T)


def norm(text: str) -> str:
    """NFKC, drop punctuation and spaces, casefold, fold simplified to traditional."""
    s = unicodedata.normalize("NFKC", text or "")
    s = _PUNCT.sub("", s).casefold()
    return s.translate(FOLD).translate(_EXTRA_FOLD)


def src_hash(zh: str, tgt: str = "en", src_lang: str = "zh-TW") -> str:
    """sha1(src_lang|tgt_lang|src_norm) — schema v2 definition (tm_units.src_hash)."""
    return hashlib.sha1(f"{src_lang}|{tgt}|{norm(zh)}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TmHit:
    tm_id: int
    src: str
    tgt: str
    score: float  # 1.0 = exact


def locked_term_issues(zh: str, glossary, en: str) -> list[dict]:
    """Locked glossary terms present in ``zh`` but missing from ``en`` (glossary.missing_locked)."""
    if not glossary or not en:
        return []
    try:
        return missing_locked(zh, glossary, en)
    except Exception:
        log.exception("locked term check failed")
        return []


class TranslationMemory:
    def __init__(self, conn_factory, fuzzy_min: float = 0.86):
        self._conn_factory = conn_factory
        self.fuzzy_min = fuzzy_min

    def _rows(self, sql: str, args: tuple) -> list[tuple]:
        conn = self._conn_factory()
        try:
            return [tuple(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def exact(self, zh: str, tgt: str = "en") -> TmHit | None:
        if not norm(zh):
            return None
        rows = self._rows(
            "SELECT id, src_text, tgt_text FROM tm_units WHERE src_hash=? AND tgt_lang=? AND quality>=3 "
            "ORDER BY quality DESC, use_count DESC LIMIT 1",
            (src_hash(zh, tgt), tgt),
        )
        return TmHit(rows[0][0], rows[0][1], rows[0][2], 1.0) if rows else None

    def fuzzy(self, zh: str, tgt: str = "en", k: int = 3) -> list[TmHit]:
        n = norm(zh)
        if len(n) < 3:
            return []
        grams: list[str] = []
        for i in range(0, len(n) - 2):
            g = n[i:i + 3].replace('"', "")
            if len(g) == 3 and g not in grams:
                grams.append(g)
            if len(grams) >= 40:
                break
        if not grams:
            return []
        query = "src_text : (" + " OR ".join(f'"{g}"' for g in grams) + ")"
        rows = self._rows(
            "SELECT u.id, u.src_text, u.tgt_text FROM tm_fts f JOIN tm_units u ON u.id = f.rowid "
            "WHERE tm_fts MATCH ? AND u.tgt_lang = ? ORDER BY bm25(tm_fts) LIMIT 30",
            (query, tgt),
        )
        hits = [TmHit(r[0], r[1], r[2], difflib.SequenceMatcher(None, n, norm(r[1])).ratio()) for r in rows]
        hits = [h for h in hits if h.score >= self.fuzzy_min and h.score < 1.0]
        hits.sort(key=lambda h: -h.score)
        return hits[:k]


def add_unit(conn: sqlite3.Connection, zh: str, en: str, *, tgt: str = "en", origin: str = "correction",
             quality: int = 4, room_id: str | None = None, segment_id: str | None = None) -> int:
    """Insert or reinforce one TM unit. Caller owns the transaction. Returns tm_units.id."""
    h = src_hash(zh, tgt)
    conn.execute(
        """INSERT INTO tm_units(src_lang, tgt_lang, src_text, src_norm, tgt_text, src_hash, origin, quality, room_id, segment_id)
           VALUES ('zh-TW', ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(src_hash, tgt_text) DO UPDATE SET quality=MIN(5, quality+1), updated_at=unixepoch('subsec')""",
        (tgt, zh, norm(zh), en, h, origin, max(1, min(5, int(quality))), room_id, segment_id),
    )
    return int(conn.execute("SELECT id FROM tm_units WHERE src_hash=? AND tgt_text=?", (h, en)).fetchone()[0])


class MemoryTranslator:
    """Wraps a Translator. Same translate() signature, so pipeline.inspect works unchanged."""

    def __init__(self, inner, tm: TranslationMemory, *, fuzzy: bool = True, on_hit=None):
        self.inner = inner
        self.tm = tm
        self.fuzzy_enabled = fuzzy
        self.on_hit = on_hit
        self.tm_exact_hits = 0
        self.tm_fuzzy_used = 0
        self.tm_locked_rejects = 0
        self.tm_errors = 0
        try:
            self._inner_params = set(inspect.signature(inner.translate).parameters)
        except (TypeError, ValueError):
            self._inner_params = set()

    def __getattr__(self, name):
        # Only called for attributes not found on the wrapper: key, model, tokens_used, ...
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def stats(self) -> dict:
        return {"tm_exact_hits": self.tm_exact_hits, "tm_fuzzy_used": self.tm_fuzzy_used,
                "tm_locked_rejects": self.tm_locked_rejects, "tm_errors": self.tm_errors}

    def translate(self, zh: str, glossary=None, context=None, deadline: float | None = None, cancel=None) -> TranslateResult:
        if not zh or not getattr(self.inner, "enabled", True):
            return self._call_inner(zh, glossary, context, deadline, cancel, None)
        hit = None
        try:
            hit = self.tm.exact(zh)
        except Exception:
            self.tm_errors += 1
            log.debug("tm exact lookup failed", exc_info=True)
        if hit is not None:
            if not locked_term_issues(zh, glossary, hit.tgt):
                self.tm_exact_hits += 1
                self._notify(hit)
                return TranslateResult(hit.tgt, "ok", origin="tm_exact")
            self.tm_locked_rejects += 1
        examples = []
        if self.fuzzy_enabled:
            try:
                examples = [{"zh": h.src, "en": h.tgt} for h in self.tm.fuzzy(zh)
                            if not locked_term_issues(h.src, glossary, h.tgt)]
            except Exception:
                self.tm_errors += 1
                log.debug("tm fuzzy lookup failed", exc_info=True)
            if examples:
                self.tm_fuzzy_used += 1
        result = self._call_inner(zh, glossary, context, deadline, cancel, examples)
        if hit is not None and result.status != "ok":
            # Model unavailable: the TM line is better than nothing; pipeline flags the missing term.
            self._notify(hit)
            return TranslateResult(hit.tgt, "ok", origin="tm_exact")
        return result

    def _notify(self, hit: TmHit) -> None:
        if self.on_hit is None:
            return
        try:
            self.on_hit(hit)
        except Exception:
            log.debug("tm on_hit failed", exc_info=True)

    def _call_inner(self, zh, glossary, context, deadline, cancel, examples) -> TranslateResult:
        kwargs = {}
        params = self._inner_params
        if "glossary" in params:
            kwargs["glossary"] = glossary
        if "context" in params:
            kwargs["context"] = context
        if "deadline" in params and deadline is not None:
            kwargs["deadline"] = deadline
        if "cancel" in params and cancel is not None:
            kwargs["cancel"] = cancel
        if examples and "examples" in params:
            kwargs["examples"] = examples
        return self.inner.translate(zh, **kwargs)


def tm_from_env(inner, db_path: str | Path | None, env: dict | None = None, ledger=None):
    """Wrap ``inner`` when BREEZE_TM=1 (opt-in, default off). Returns ``inner`` unchanged otherwise."""
    env = os.environ if env is None else env
    if (env.get("BREEZE_TM") or "0").strip() != "1" or not db_path:
        return inner
    try:
        fuzzy_min = float(env.get("BREEZE_TM_FUZZY_MIN") or 0.86)
    except ValueError:
        fuzzy_min = 0.86
    path = Path(db_path)

    def factory():
        from app.admin.db import connect
        if not path.exists():
            raise FileNotFoundError(str(path))
        return connect(path, readonly=True, timeout_ms=1000)

    on_hit = None
    if ledger is not None and getattr(ledger, "enabled", False):
        on_hit = lambda hit: ledger.submit({"type": "tm_hit", "tm_id": hit.tm_id})  # noqa: E731
    fuzzy = (env.get("BREEZE_TM_FUZZY") or "1").strip() != "0"
    return MemoryTranslator(inner, TranslationMemory(factory, fuzzy_min=fuzzy_min), fuzzy=fuzzy, on_hit=on_hit)
