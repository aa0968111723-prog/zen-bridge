from __future__ import annotations

import json
from pathlib import Path


def annotate_question(zh: str) -> str:
    """Add a fullwidth question mark only when the line already asks one. Keep the raw ASR text elsewhere."""
    text = (zh or "").strip()
    if not text or text.endswith(("?", "？", "。", "！")):
        return text
    if text.endswith(("嗎", "呢")):
        return text + "？"
    return text


def should_join(prev: str, nxt: str, gap_ms: int) -> bool:
    """Conservative join. Live segments stay separate so retries keep the same segment id."""
    if gap_ms > 400 or not prev or not nxt:
        return False
    if prev[-1] in "。！？?!.":
        return False
    if len(nxt) > 8:
        return False
    return True


def utf8_text(value: str) -> str:
    """Drop characters that cannot be encoded as UTF-8. Valid text is unchanged."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        return value
    kept = []
    for char in value:
        try:
            char.encode("utf-8")
        except UnicodeEncodeError:
            continue
        kept.append(char)
    return "".join(kept)


def _utf8_ok(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _scrub_value(value):
    if isinstance(value, str):
        return utf8_text(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub_value(item) if isinstance(item, (str, list, dict)) else item
            for key, item in value.items()
        }
    return value


def scrub_caption(event: dict) -> dict:
    """Keep a caption publishable. Bad English is a failed translation; Chinese stays.

    A lone surrogate cannot be written to SQLite or sent on a WebSocket. Dropping it
    here stops one model reply from closing every new listener.
    """
    if not isinstance(event, dict):
        return {}
    en = event.get("en")
    en_bad = isinstance(en, str) and not _utf8_ok(en)
    out = _scrub_value(event)
    if not isinstance(out, dict) or not en_bad:
        return out if isinstance(out, dict) else {}
    out["en"] = ""
    if str(out.get("type") or "") in {"captions_cleared", "caption_deleted"}:
        return out
    if str(out.get("status") or "") in {"", "ready", "ok"}:
        out["status"] = "translate_failed"
    if str(out.get("translate_status") or "") in {"", "ok"}:
        out["translate_status"] = "bad_response"
    if not str(out.get("error") or "").strip():
        out["error"] = "英譯不是純英文，中文仍保留"
    return out


def _split_glossary(text: str) -> tuple[str, str] | None:
    # A line that already has "=" keeps the old split, including a later fullwidth equals.
    if "=" in text:
        left, right = text.split("=", 1)
        return left, right
    index = text.find("＝")
    if index < 0:
        return None
    return text[:index], text[index + 1 :]


def parse_glossary(raw: str, limit: int = 40) -> list[dict]:
    """Legacy host textarea. `zh=en` results stay the same, including the silent cap of 40.

    Also accepts a fullwidth equals, and `標準詞|別名1|別名2=English`.
    Strict rejection lives on the room glossary API, not here.
    """
    rows = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        split = _split_glossary(text)
        if split is None:
            continue
        left, right = split
        en = right.strip()[:80]
        if "|" in left:
            parts = [part.strip() for part in left.split("|")]
            zh = parts[0].strip()[:40]
            aliases = [part for part in parts[1:] if part]
        else:
            zh = left.strip()[:40]
            aliases = []
        if zh and en:
            row = {"zh": zh, "en": en}
            if aliases:
                row["aliases"] = aliases
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


# These separators are line breaks for str.splitlines but must not wipe a glossary.
_LEGACY_BREAKS = ("\x85", "\u2028", "\u2029")


def strict_legacy_rows(raw: str, limit: int = 40, max_en: int = 80) -> tuple[list[dict], list[dict]]:
    """Parse a host textarea without dropping rows or cutting English short.

    A non-blank line that is not `zh=en` is rejected. Any rejected entry means the
    caller must not save. Blank lines and `#` comments are ignored, but they still
    count toward the line number on each accepted row. An empty alias fragment is
    rejected, same as PUT. An empty box is rejected so it cannot clear the room.
    `parse_glossary` still truncates for old callers.
    """
    if not isinstance(raw, str):
        return [], [{"line": 0, "reason": "text 必須是文字"}]
    for mark in _LEGACY_BREAKS:
        if mark in raw:
            return [], [{"line": 0, "reason": "術語含有不可見的換行，沒有寫入"}]
    rows: list[dict] = []
    rejected: list[dict] = []
    for line_no, line in enumerate(raw.split("\n"), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        split = _split_glossary(text)
        if split is None:
            rejected.append({"line": line_no, "reason": "缺少 = 或 ＝"})
            continue
        left, right = split
        en = right.strip()
        line_bad = False
        if "|" in left:
            parts = [part.strip() for part in left.split("|")]
            zh = parts[0].strip()
            aliases = []
            for part in parts[1:]:
                # Whitespace-only, including a fullwidth space, is an empty alias.
                # A zero-width character is not stripped; validation rejects it later.
                if not part:
                    reason = "別名是空的，不能當別名"
                    if not rejected or rejected[-1].get("line") != line_no or rejected[-1].get("reason") != reason:
                        rejected.append({"line": line_no, "reason": reason})
                    line_bad = True
                    continue
                aliases.append(part)
        else:
            zh = left.strip()
            aliases = []
        if not zh or not en:
            rejected.append({"line": line_no, "reason": "中文或英文是空的"})
            line_bad = True
        if line_bad:
            continue
        if len(rows) >= limit:
            return [], [{"line": line_no, "reason": f"術語超過 {limit} 條"}]
        if len(en) > max_en:
            rejected.append({"line": line_no, "reason": f"英文超過 {max_en} 字"})
            continue
        row = {"zh": zh, "en": en, "line": line_no}
        if aliases:
            row["aliases"] = aliases
        rows.append(row)
    if rejected:
        return [], rejected
    if not rows:
        return [], [{"line": 0, "reason": "沒有有效的術語，不會清空這個房間的詞表"}]
    return rows, []


# A single session's t0/t1 stay relative to that session. Export places later sessions
# after the previous cue so a 100-minute class, and a second take, stay monotonic.
_MAX_TIMELINE_MS = 48 * 60 * 60 * 1000


def export_text(events: list[dict], kind: str) -> str:
    rows = [item for item in events if item.get("status") not in {"missing", "error", "timeout", "cancelled"} or item.get("zh") or item.get("en")]
    rows = _with_timeline(rows)
    if kind == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if kind == "txt":
        lines = []
        for item in rows:
            stamp = _range(item)
            body = " ".join(part for part in (item.get("zh") or "", item.get("en") or "") if part)
            lines.append(f"{stamp} {body}".strip())
        return "\n".join(lines) + ("\n" if lines else "")
    if kind in {"srt", "vtt"}:
        return _cues(rows, vtt=kind == "vtt")
    raise ValueError("不支援的匯出格式")


def _clamp_ms(value) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return 0
    if ms < 0:
        return 0
    if ms > _MAX_TIMELINE_MS:
        return _MAX_TIMELINE_MS
    return ms


def _cue_text(item: dict) -> str:
    text = item.get("zh") or ""
    if item.get("en"):
        text = (text + "\n" + item["en"]).strip()
    return text.strip()


def _session_rank(rows: list[dict]) -> dict[str, tuple]:
    first: dict[str, int] = {}
    ordinal: dict[str, int] = {}
    for index, item in enumerate(rows):
        sid = str(item.get("session_id") or "")
        if sid not in first:
            first[sid] = index
        raw = item.get("session_ord")
        try:
            parsed = int(raw) if raw is not None and raw != "" else 0
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            ordinal[sid] = parsed if sid not in ordinal else min(ordinal[sid], parsed)
    return {sid: (ordinal.get(sid, 10**12), first[sid], sid) for sid in first}


def _order_rows(rows: list[dict]) -> list[dict]:
    """One order for store and bus: session_ord, then first appearance, then seq."""
    rank = _session_rank(rows)
    def key(pair: tuple[int, dict]) -> tuple:
        index, item = pair
        sid = str(item.get("session_id") or "")
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        return (*rank.get(sid, (10**12, index, sid)), seq, index)
    return [item for _index, item in sorted(enumerate(rows), key=key)]


def _with_timeline(rows: list[dict]) -> list[dict]:
    """Copy rows onto one clock. Session k starts when session k-1's last cue ends."""
    ordered = _order_rows(rows)
    blocks: list[tuple[str, list[dict]]] = []
    for item in ordered:
        sid = str(item.get("session_id") or "")
        if not blocks or blocks[-1][0] != sid:
            blocks.append((sid, []))
        blocks[-1][1].append(item)
    placed: list[dict] = []
    offset = 0
    for _sid, group in blocks:
        copies: list[dict] = []
        for item in group:
            copied = dict(item)
            copied["timeline_offset_ms"] = offset
            if item.get("t0_ms") is None:
                copies.append(copied)
                continue
            start = _clamp_ms(item.get("t0_ms")) + offset
            if item.get("t1_ms") is None:
                end = start + 1000
            else:
                end = _clamp_ms(item.get("t1_ms")) + offset
            if end <= start:
                end = start + 400
            copied["t0_ms"] = start
            copied["t1_ms"] = end
            copies.append(copied)
        last_end = None
        for copied in copies:
            if copied.get("t0_ms") is not None and _cue_text(copied):
                last_end = int(copied["t1_ms"])
        placed.extend(copies)
        if last_end is not None:
            offset = last_end
    cue_at = [index for index, item in enumerate(placed) if item.get("t0_ms") is not None and _cue_text(item)]
    for left, right in zip(cue_at, cue_at[1:]):
        cur = placed[left]
        nxt = placed[right]
        if int(cur["t0_ms"]) > int(nxt["t0_ms"]):
            nxt["t0_ms"] = int(cur["t0_ms"])
            if int(nxt["t1_ms"]) <= int(nxt["t0_ms"]):
                nxt["t1_ms"] = int(nxt["t0_ms"]) + 400
        if int(cur["t1_ms"]) > int(nxt["t0_ms"]):
            cur["t1_ms"] = int(nxt["t0_ms"])
        if int(cur["t1_ms"]) <= int(cur["t0_ms"]):
            cur["t1_ms"] = int(cur["t0_ms"]) + 1
            if int(cur["t1_ms"]) > int(nxt["t0_ms"]):
                nxt["t0_ms"] = int(cur["t1_ms"])
                if int(nxt["t1_ms"]) <= int(nxt["t0_ms"]):
                    nxt["t1_ms"] = int(nxt["t0_ms"]) + 400
    for index in cue_at:
        if int(placed[index]["t1_ms"]) <= int(placed[index]["t0_ms"]):
            placed[index]["t1_ms"] = int(placed[index]["t0_ms"]) + 1
    return placed


def _range(item: dict) -> str:
    if item.get("t0_ms") is None:
        return ""
    start = int(item["t0_ms"])
    end = int(item["t1_ms"]) if item.get("t1_ms") is not None else start + 1000
    if end <= start:
        end = start + 400
    return f"{_ts(start)} --> {_ts(end)}"


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _cues(rows: list[dict], vtt: bool) -> str:
    blocks = ["WEBVTT\n"] if vtt else []
    index = 1
    for item in rows:
        if item.get("t0_ms") is None:
            continue
        start = int(item["t0_ms"])
        end = int(item["t1_ms"]) if item.get("t1_ms") is not None else start + 1000
        if end <= start:
            end = start + 400
        text = _cue_text(item)
        if not text:
            continue
        stamp = f"{_ts(start)} --> {_ts(end)}"
        if vtt:
            stamp = stamp.replace(",", ".")
            blocks.append(f"{stamp}\n{text}\n")
        else:
            blocks.append(f"{index}\n{stamp}\n{text}\n")
        index += 1
    return "\n".join(blocks)


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
