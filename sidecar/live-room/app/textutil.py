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


def parse_glossary(raw: str, limit: int = 40) -> list[dict]:
    rows = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        zh, en = text.split("=", 1)
        zh = zh.strip()[:40]
        en = en.strip()[:80]
        if zh and en:
            rows.append({"zh": zh, "en": en})
        if len(rows) >= limit:
            break
    return rows


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
