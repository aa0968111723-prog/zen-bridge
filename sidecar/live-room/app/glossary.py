"""Per-room glossary: parse, normalize, and locked-term checks.

The default table is empty. No club glossary is built in. Locked-term misses
are reported only. Replacing leftover Chinese inside `en` is not implemented.
"""

from __future__ import annotations

import re
import unicodedata

from app.textutil import strict_legacy_rows

SCHEMA_VERSION = 1
MAX_TERMS = 200
# The host textarea still refuses more than this many lines. Raising it waits on a product decision.
LEGACY_BOX_LIMIT = 40
MAX_ALIASES = 8
MIN_ZH = 1
MAX_ZH = 20
MIN_ALIAS = 2
MAX_ALIAS = 20
MAX_EN = 80
MAX_CATEGORY = 20
MAX_NOTE = 80
PROMPT_LIMIT = 40
# Host PUT body. 200 terms of the field limits fit; anything larger is refused.
GLOSSARY_MAX_BODY = 256 * 1024
# Header only. Translations stay out until the user confirms them.
TEMPLATE_CSV = "zh,aliases,en,lock,category,note\n"

# Minimum traditional fold used only for matching. Output is always the canonical term.
FOLD = str.maketrans(
    "学会观经数开静禅语头话调务师处关习觉围产",
    "學會觀經數開靜禪語頭話調務師處關習覺圍產",
)
# Common words that must never be aliases. Matching ignores them even if stored.
ALIAS_STOP = frozenset({"開始", "法式", "師傅", "社科", "只觀", "工案", "開事", "一座", "產修"})
# A hit that overlaps one of these words is not rewritten. The host still sees a flag.
# Simplified and fullwidth forms are folded at match time; this list is the traditional spelling.
COMMON_GUARD = (
    "開始", "法式", "師傅", "社科", "只觀", "工案", "開事", "一座", "產修",
    "建設", "建設課程", "開設", "開設課程", "天空", "學會",
)


# Match keys are pure, and lectures repeat the same characters.
_FOLD_CACHE: dict[str, str] = {}


def _fold_char(ch: str) -> str:
    """One source character becomes one match character, so indexes stay aligned.

    NFKC and casefold are applied only when they stay one character. A compatibility
    form that expands (for example a ligature) keeps the traditional fold instead.
    """
    cached = _FOLD_CACHE.get(ch)
    if cached is not None:
        return cached
    mapped = _fold_one(ch)
    _FOLD_CACHE[ch] = mapped
    return mapped


def _fold_one(ch: str) -> str:
    nfkc = unicodedata.normalize("NFKC", ch)
    if len(nfkc) == 1:
        folded = nfkc.casefold()
        if len(folded) == 1:
            mapped = folded.translate(FOLD)
            if len(mapped) == 1:
                return mapped
    mapped = ch.translate(FOLD)
    return mapped if len(mapped) == 1 else ch


def _match_key(text: str) -> str:
    return "".join(_fold_char(ch) for ch in text)


_STOP_KEYS = frozenset(_match_key(word) for word in ALIAS_STOP)
_GUARD_KEYS = tuple(
    sorted({_match_key(word) for word in COMMON_GUARD if len(_match_key(word)) >= 2}, key=len, reverse=True)
)


def _is_stop_alias(alias: str) -> bool:
    return alias in ALIAS_STOP or _match_key(alias) in _STOP_KEYS


def _traditional(text: str) -> str:
    """Simplified-to-traditional only. Fullwidth and case stay for match time."""
    return text.translate(FOLD)


def is_locked(term: dict) -> bool:
    if "lock" in term and term["lock"] is not None:
        parsed = _parse_lock(term["lock"])
        return True if parsed is None else parsed
    if "locked" in term and term["locked"] is not None:
        parsed = _parse_lock(term["locked"])
        return True if parsed is None else parsed
    return True


def legacy_terms(rows: list[dict]) -> list[dict]:
    """Old `zh=en` rows become locked terms. Aliases are kept when the line had them."""
    terms = []
    for row in rows:
        terms.append({
            "zh": row["zh"],
            "aliases": list(row.get("aliases") or []),
            "en": row["en"],
            "lock": True,
            "category": "",
            "note": "",
        })
    return terms


# The host page has no separate editor. Rich fields are changed with this request.
_LEGACY_EDIT_PLACE = "請用 PUT /api/rooms/{room_id}/glossary 修改。這是技術操作，請找負責詞表的人。格式見 README「修改房間術語表」。"
_LEGACY_RICH = "含備註、分類或未鎖定的詞，或文字框無法原樣表示的內容"


def _legacy_edit_place(room_id: str | None = None) -> str:
    if room_id is None:
        return _LEGACY_EDIT_PLACE
    room = str(room_id).strip() or "class"
    return _LEGACY_EDIT_PLACE.replace("{room_id}", room)


def _box_aliases(term: dict) -> list[str]:
    raw = term.get("aliases") or []
    if not isinstance(raw, list):
        return []
    return [alias for alias in raw if isinstance(alias, str) and alias]


def _term_unexpressable(term) -> bool:
    """True when a legacy line would drop or change this term.

    `zh|alias=en` round-trips aliases. Lock off, a note, a category, or text the
    line would rewrite (a `|` inside the canonical, a padded alias) does not.
    """
    if not isinstance(term, dict):
        return False
    if not is_locked(term):
        return True
    if str(term.get("note") or "").strip():
        return True
    if str(term.get("category") or "").strip():
        return True
    zh = term.get("zh")
    en = term.get("en")
    if not isinstance(zh, str) or not isinstance(en, str):
        return True
    aliases = _box_aliases(term)
    left = "|".join([zh, *aliases]) if aliases else zh
    rows, problems = strict_legacy_rows(left + "=" + en, limit=LEGACY_BOX_LIMIT)
    if problems or len(rows) != 1:
        return True
    row = rows[0]
    return row.get("zh") != zh or row.get("en") != en or list(row.get("aliases") or []) != aliases


def legacy_box_block(terms, room_id: str | None = None) -> str:
    """Why the textarea must not replace this glossary. Empty when the box may edit it.

    More than LEGACY_BOX_LIMIT rows cannot be shown. Lock off, a note, a category,
    or text that does not round-trip through `zh|alias=en` cannot be shown either.
    Aliases that survive that syntax do not lock the box. Refusing the post is what
    stops one visible line from deleting a field the box cannot write back.
    `room_id`, when known, replaces the `{room_id}` placeholder in the hint.
    """
    rows = list(terms or [])
    rich = any(_term_unexpressable(term) for term in rows)
    count = len(rows)
    limit = f"（主持頁最多 {LEGACY_BOX_LIMIT} 條）"
    place = _legacy_edit_place(room_id)
    if count > LEGACY_BOX_LIMIT and rich:
        return f"這個房間的術語表有 {count} 條{limit}，而且{_LEGACY_RICH}，這裡只能看、不能改。{place}"
    if count > LEGACY_BOX_LIMIT:
        return f"這個房間的術語表有 {count} 條{limit}，這裡只能看、不能改。{place}"
    if rich:
        return f"這個房間的術語表{_LEGACY_RICH}，這裡只能看、不能改。{place}"
    return ""


def legacy_omitted_count(existing, incoming) -> int:
    """How many stored canonical terms the new legacy text does not mention."""
    kept: set[str] = set()
    for term in incoming or []:
        if isinstance(term, dict) and isinstance(term.get("zh"), str) and term["zh"]:
            kept.add(term["zh"])
    omitted = 0
    seen: set[str] = set()
    for term in existing or []:
        if not isinstance(term, dict):
            continue
        zh = term.get("zh")
        if not isinstance(zh, str) or not zh or zh in seen:
            continue
        seen.add(zh)
        if zh not in kept:
            omitted += 1
    return omitted


def validate_terms(raw) -> tuple[list[dict], list[dict]]:
    """Return (accepted, rejected). Accepted terms are not saved by this function.

    Over the term cap, nothing else is accepted. A term with any problem is
    left out of `accepted` and listed in `rejected` with a reason.
    """
    if not isinstance(raw, list):
        return [], [{"line": 0, "reason": "terms 必須是陣列"}]
    if len(raw) > MAX_TERMS:
        return [], [{"line": MAX_TERMS + 1, "reason": f"術語超過 {MAX_TERMS} 條"}]

    parsed: list[tuple[int, dict | None]] = []
    rejected: list[dict] = []
    canons: list[str] = []
    for index, item in enumerate(raw, start=1):
        term, problems = _parse_term(item)
        for reason in problems:
            rejected.append({"line": index, "reason": reason})
        parsed.append((index, term))
        if term is not None and not term.get("_bad"):
            canons.append(term["zh"])
    canon_set = set(canons)
    canon_keys = {_match_key(zh) for zh in canons}
    canon_folded = [(zh, _match_key(zh)) for zh in canons]
    seen_zh: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    # Owners are keyed by the match fold, so 学社 and 學社 are the same alias.
    alias_owners: dict[str, list[tuple[int, str, str]]] = {}
    bad_lines: set[int] = set()
    for index, term in parsed:
        if term is None or term.get("_bad"):
            bad_lines.add(index)
            continue
        zh = term["zh"]
        zh_key = _match_key(zh)
        if zh in seen_zh or zh_key in seen_keys:
            rejected.append({"line": index, "reason": f"標準詞「{_clip(zh)}」重複"})
            bad_lines.add(index)
            continue
        seen_zh[zh] = index
        seen_keys[zh_key] = index
        for alias in term["aliases"]:
            alias_key = _match_key(alias)
            if len(alias) < MIN_ALIAS:
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」少於 2 字"})
                bad_lines.add(index)
            if _is_stop_alias(alias):
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」是常用詞，不能當別名"})
                bad_lines.add(index)
            if alias in canon_set or alias_key in canon_keys:
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」與標準詞相同"})
                bad_lines.add(index)
            if len(alias_key) >= MIN_ALIAS:
                for other, folded in canon_folded:
                    if other == zh or alias_key == folded or alias_key not in folded:
                        continue
                    rejected.append({
                        "line": index,
                        "reason": f"別名「{_clip(alias)}」是標準詞「{_clip(other)}」的子字串",
                    })
                    bad_lines.add(index)
                    break
            alias_owners.setdefault(alias_key, []).append((index, zh, alias))
    for _key, owners in alias_owners.items():
        targets = {zh for _, zh, _alias in owners}
        if len(targets) < 2:
            continue
        named = "、".join(f"「{_clip(zh)}」" for zh in sorted(targets))
        for index, _zh, alias in owners:
            rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」同時指向{named}"})
            bad_lines.add(index)
    accepted = []
    for index, term in parsed:
        if term is None or index in bad_lines or term.get("_bad"):
            continue
        accepted.append(_public_term(term))
    return accepted, rejected


def normalize(text: str, glossary) -> str:
    """Longest match, repeated until another pass would not change the line.

    One pass can turn an alias into text that matches a different alias. Repeat so
    normalize(normalize(x)) stays equal to normalize(x). A canonical written by an
    earlier pass stays in the sentence: a later alias may overlap it only when the
    canonical is still a substring afterwards. A cycle, or a chain that does not
    settle, is left unrewritten. The match tables are built once for every pass.
    """
    current = text or ""
    if not current:
        return current
    tables = _tables(glossary)
    seen = {current}
    for _ in range(8):
        rewritten, _flags = _apply(current, glossary, tables)
        if rewritten == current:
            return current
        if rewritten in seen:
            return text or ""
        seen.add(rewritten)
        current = rewritten
    rewritten, _flags = _apply(current, glossary, tables)
    if rewritten != current:
        return text or ""
    return current


def guarded_flags(text: str, glossary) -> list[dict]:
    """Alias hits left in place because they sit inside a common word. Host-only."""
    _rewritten, flags = _apply(text, glossary)
    return flags


def matched_terms(zh: str, glossary) -> list[dict]:
    """Canonical hits in an already normalized line. One-character terms are not targets."""
    by_zh: dict[str, dict] = {}
    for term in _term_rows(glossary):
        text = term["zh"]
        if len(text) < 2 or _traditional(text) != text:
            continue
        by_zh[text] = term
    keys = sorted(by_zh, key=len, reverse=True)
    found: list[dict] = []
    index = 0
    text = zh or ""
    while index < len(text):
        for key in keys:
            if len(key) < 2:
                continue
            if text.startswith(key, index):
                found.append(by_zh[key])
                index += len(key)
                break
        else:
            index += 1
    return found


def prompt_terms(zh: str, glossary, context=None, limit: int = PROMPT_LIMIT) -> list[dict]:
    """Terms matched in this line, then in the previous lines. Cap is 40."""
    picked: list[dict] = []
    seen: set[str] = set()
    texts = [zh or ""]
    for line in list(context or [])[-4:]:
        texts.append(str(line or ""))
    for text in texts:
        for term in matched_terms(text, glossary):
            key = term["zh"]
            if key in seen:
                continue
            en = str(term.get("en") or "")
            if not en:
                continue
            seen.add(key)
            picked.append({"zh": key, "en": en, "locked": is_locked(term)})
            if len(picked) >= limit:
                return picked
    return picked


def missing_locked(zh: str, glossary, en: str) -> list[dict]:
    """Host-only flags. Does not change `en`."""
    flags = []
    for term in matched_terms(zh, glossary):
        if not is_locked(term):
            continue
        if term_hit(en, term):
            continue
        flags.append({"zh": term["zh"], "en": term.get("en") or "", "reason": "missing"})
    return flags


def term_hit(en: str, term: dict) -> bool:
    """ASCII words match on boundaries, so Zen does not hit zenith."""
    needle = _plain(str(term.get("en") or ""))
    if not needle:
        return False
    hay = _plain(str(en or ""))
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
    return re.search(pattern, hay) is not None


def _apply(text: str, glossary, tables=None) -> tuple[str, list[dict]]:
    if not text:
        return text or "", []
    canon, alias, en_of = tables if tables is not None else _tables(glossary)
    if not canon and not alias:
        return text, []
    folded = _match_key(text)
    # A fold that changed the length would shift every later index. Leave the line alone.
    if len(folded) != len(text):
        return text, []
    guards = _guard_spans(folded)
    # Canonicals already in this sentence stay put. An alias may overlap one only
    # when the output still contains that canonical, so 戊甲乙 becomes 戊丙丁 and
    # the next pass cannot turn the new 丙丁 into 庚辛.
    protected = _exact_canon_spans(text, folded, canon)
    longest = 1
    for key in canon:
        longest = max(longest, len(key))
    for key in alias:
        longest = max(longest, len(key))
    out: list[str] = []
    flags: list[dict] = []
    seen: set[str] = set()
    index = 0
    while index < len(text):
        chosen: tuple[int, str] | None = None
        suppressed: str | None = None
        limit = min(longest, len(text) - index)
        for size in range(limit, 1, -1):
            key = folded[index:index + size]
            if key in canon:
                replacement = canon[key]
            elif key in alias:
                if _overlaps(index, index + size, guards):
                    if suppressed is None:
                        suppressed = alias[key]
                    continue
                replacement = alias[key]
            else:
                continue
            if not _keeps_canons(text, index, index + size, replacement, protected):
                continue
            chosen = (size, replacement)
            break
        if chosen is not None:
            out.append(chosen[1])
            index += chosen[0]
            continue
        if suppressed and suppressed not in seen:
            seen.add(suppressed)
            flags.append({"zh": suppressed, "en": en_of.get(suppressed, ""), "reason": "guarded"})
        out.append(text[index])
        index += 1
    return "".join(out), flags


def _exact_canon_spans(text: str, folded: str, canon: dict[str, str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if not canon:
        return spans
    longest = max(len(key) for key in canon)
    index = 0
    while index < len(text):
        found = 0
        limit = min(longest, len(text) - index)
        for size in range(limit, 1, -1):
            surface = canon.get(folded[index:index + size])
            if surface is not None and len(surface) == size and text.startswith(surface, index):
                found = size
                break
        if found:
            spans.append((index, index + found))
            index += found
        else:
            index += 1
    return spans


def _keeps_canons(text: str, start: int, end: int, replacement: str, spans: list[tuple[int, int]]) -> bool:
    """True when every protected canonical this edit touches still occurs afterwards.

    A piece that does not overlap the edit stays in the prefix or the suffix, so it
    is not rebuilt. An overlapping piece is kept when it still occurs outside the
    edit or across the short junction around the replacement. This matches searching
    the whole rewritten line without copying that line on every candidate.
    """
    for left, right in spans:
        if start >= right or end <= left:
            continue
        piece = text[left:right]
        if not piece or not _piece_in_replacement(text, start, end, replacement, piece):
            return False
    return True


def _piece_in_replacement(text: str, start: int, end: int, replacement: str, piece: str) -> bool:
    """Whether `piece` occurs in `text[:start] + replacement + text[end:]`."""
    if text.find(piece, 0, start) != -1:
        return True
    if text.find(piece, end) != -1:
        return True
    span = len(piece) - 1
    head = text[max(0, start - span):start] if span else ""
    tail = text[end:end + span] if span else ""
    return piece in head + replacement + tail


def _tables(glossary) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Canonical output, alias fold to canonical, and English for a guarded flag.

    A one-character canonical is never a replacement target. A simplified canonical
    is not either: matching must not rewrite traditional text into simplified.
    """
    canon: dict[str, str] = {}
    alias: dict[str, str] = {}
    en_of: dict[str, str] = {}
    rows = _term_rows(glossary)
    for term in rows:
        zh = term["zh"]
        if len(zh) < 2 or _traditional(zh) != zh:
            continue
        en_of.setdefault(zh, str(term.get("en") or ""))
        canon.setdefault(_match_key(zh), zh)
    for term in rows:
        zh = term["zh"]
        if zh not in en_of:
            continue
        for surface in term["aliases"]:
            if len(surface) < MIN_ALIAS or _is_stop_alias(surface):
                continue
            alias.setdefault(_match_key(surface), zh)
    return canon, alias, en_of


def _guard_spans(folded: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    length = len(folded)
    for start in range(length):
        for key in _GUARD_KEYS:
            size = len(key)
            if start + size > length:
                continue
            if folded.startswith(key, start):
                spans.append((start, start + size))
                break
    return spans


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for left, right in spans:
        if start < right and end > left:
            return True
    return False


def _term_rows(glossary) -> list[dict]:
    rows = []
    for item in glossary or []:
        if not isinstance(item, dict):
            continue
        zh = item.get("zh")
        if not isinstance(zh, str) or not zh:
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [part.strip() for part in aliases.split("|") if part.strip()]
        cleaned = []
        for alias in aliases:
            if isinstance(alias, str) and alias:
                cleaned.append(alias)
        rows.append({"zh": zh, "aliases": cleaned, "en": item.get("en") or "", "lock": item.get("lock", item.get("locked", True)), "category": item.get("category") or "", "note": item.get("note") or ""})
    return rows


def _parse_term(item) -> tuple[dict | None, list[str]]:
    problems: list[str] = []
    if not isinstance(item, dict):
        return None, ["術語必須是物件"]
    zh = item.get("zh")
    en = item.get("en")
    if not isinstance(zh, str):
        problems.append("中文必須是 1 到 20 字")
        zh_text = ""
    else:
        zh_text = zh.strip()
        if _has_control(zh_text):
            problems.append("中文含有控制字元")
        elif _traditional(zh_text) != zh_text:
            problems.append(f"標準詞「{_clip(zh_text)}」必須是繁體")
        elif not MIN_ZH <= len(zh_text) <= MAX_ZH:
            problems.append("中文必須是 1 到 20 字")
    if not isinstance(en, str):
        problems.append("英文必須是 1 到 80 字")
        en_text = ""
    else:
        en_text = en.strip()
        if _has_control(en) or _has_control(en_text):
            problems.append("英文含有控制字元或換行")
        elif not 1 <= len(en_text) <= MAX_EN:
            problems.append("英文必須是 1 到 80 字")
    aliases_raw = item.get("aliases", [])
    aliases: list[str] = []
    if aliases_raw is None:
        aliases_raw = []
    if not isinstance(aliases_raw, list):
        problems.append("別名必須是陣列")
    elif len(aliases_raw) > MAX_ALIASES:
        problems.append(f"別名最多 {MAX_ALIASES} 個")
    else:
        for alias in aliases_raw:
            if not isinstance(alias, str):
                problems.append("別名必須是文字")
                continue
            text = alias.strip()
            if not text:
                problems.append("別名是空的，不能當別名")
                continue
            if _has_control(text):
                visible = "".join(ch if not _has_control(ch) else f"U+{ord(ch):04X}" for ch in text)
                problems.append(f"別名「{_clip(visible)}」含有控制字元")
                continue
            if len(text) > MAX_ALIAS:
                problems.append(f"別名「{_clip(text)}」超過 {MAX_ALIAS} 字")
                continue
            if text not in aliases:
                aliases.append(text)
    category = item.get("category", "")
    note = item.get("note", "")
    if category is None:
        category = ""
    if note is None:
        note = ""
    if not isinstance(category, str):
        problems.append("分類必須是文字")
        category = ""
    else:
        category = category.strip()
        if _has_control(category):
            problems.append("分類含有控制字元或換行")
        elif len(category) > MAX_CATEGORY:
            problems.append(f"分類超過 {MAX_CATEGORY} 字")
    if not isinstance(note, str):
        problems.append("備註必須是文字")
        note = ""
    else:
        note = note.strip()
        if _has_control(note):
            problems.append("備註含有控制字元或換行")
        elif len(note) > MAX_NOTE:
            problems.append(f"備註超過 {MAX_NOTE} 字")
    lock_value = True
    if "lock" in item and item["lock"] is not None:
        parsed = _parse_lock(item["lock"])
        if parsed is None:
            problems.append("鎖定必須是布林值")
        else:
            lock_value = parsed
    elif "locked" in item and item["locked"] is not None:
        parsed = _parse_lock(item["locked"])
        if parsed is None:
            problems.append("鎖定必須是布林值")
        else:
            lock_value = parsed
    if problems or not zh_text or not en_text:
        term = {"_bad": True, "zh": zh_text, "aliases": aliases, "en": en_text, "lock": lock_value, "category": category, "note": note}
        return term, problems
    return {
        "zh": zh_text,
        "aliases": aliases,
        "en": en_text,
        "lock": lock_value,
        "category": category,
        "note": note,
    }, []


def _public_term(term: dict) -> dict:
    return {
        "zh": term["zh"],
        "aliases": list(term.get("aliases") or []),
        "en": term["en"],
        "lock": bool(term.get("lock", True)),
        "category": term.get("category") or "",
        "note": term.get("note") or "",
    }


def _parse_lock(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
    return None


def _has_control(text: str) -> bool:
    for char in text:
        if char in "\r\n\t":
            return True
        category = unicodedata.category(char)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            return True
    return False


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.replace("-", " ").casefold().split())


def _clip(text: str, limit: int = 20) -> str:
    flat = "".join(" " if _has_control(char) or char.isspace() and char != " " else char for char in text)
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"
