#!/usr/bin/env python3
"""Offline transcript pair check: live transcript vs. complete transcript (queue item 35).

  python tools/transcript_pair_check.py --dir recordings/harvest/mixed/rh_20261017_ab12cd34
  python tools/transcript_pair_check.py --live live.jsonl --complete transcript.json
  python tools/transcript_pair_check.py --live live.jsonl --complete transcript.srt --json OUT.json

zen-bridge keeps two transcripts for every recording:

(a) the **live** transcript, appended while recording is still running
    (``live.jsonl`` — one JSON event per line, ``{"at", "type": "final"|"draft"|...,
    "seq", "t0_ms", "t1_ms", "text", "asr_text", "clip_id"}``), and
(b) the **complete** transcript written next to the audio when recording ends
    (``transcript.json`` with ``segments[]`` of ``start_ms``/``end_ms``/
    ``live_t0_ms``/``live_t1_ms``/``text``/``clip_id``, plus ``transcript.srt``,
    ``transcript.vtt`` and ``transcript.txt`` lines like
    ``[00:00:01.000] Speaker 0: 【未校對】文字``).

This tool reads both (read-only, stdlib only, no network) and checks that they
agree:

* **missing segments** — live captions absent from the complete transcript and
  complete segments that never appeared live;
* **timestamps** — going backwards, overlapping, or ending before they start;
* **text divergence** — ``1 - difflib.SequenceMatcher.ratio()`` over a CJK-aware
  token sequence (each CJK ideograph / kana / hangul is one token, each Latin or
  digit run is one token; NFKC, case-folded, whitespace and punctuation ignored).

Accepted formats (auto-detected from the extension, then the content):

* ``jsonl`` — zen-bridge ``live.jsonl`` events (only ``type == "final"`` lines;
  the last revision of each ``seq``/``clip_id`` wins, drafts are ignored) or
  generic lines with ``start``/``end`` (seconds), ``start_ms``/``end_ms`` or
  ``t0_ms``/``t1_ms`` plus ``text``;
* ``json`` — zen-bridge ``transcript.json`` (``{"segments": [...]}``) or a plain
  list of such objects;
* ``srt`` / ``vtt`` — standard cues (``<v tag>`` and other cue tags stripped);
* ``txt`` — ``[hh:mm:ss] text``, ``[hh:mm:ss.mmm] text`` or ``[mm:ss] text``
  lines (untimed lines continue the previous segment).

Segments are paired by ``clip_id`` when both sides carry it, otherwise by time
overlap on the live timeline (``transcript.json`` ``live_t0_ms`` is used when
present; ``--offset-ms`` shifts the complete transcript). Overlapping segments
form groups, so merged / split captions still compare as one unit.

==========  ==================================================================  ====
status      condition                                                           exit
==========  ==================================================================  ====
``ok``      nothing missing, timestamps sane, divergence < ``--warn-divergence``  0
``warn``    some segment missing, overlap, live timestamp going backwards,       1
            corrupt live line, or divergence ≥ ``--warn-divergence`` (0.05)
``fail``    missing tokens ≥ ``--fail-missing`` (0.05) of either side,           2
            complete-transcript timestamps backwards / negative, nothing
            pairable, or divergence ≥ ``--fail-divergence`` (0.20)
(input)     missing / unreadable / undecodable / unknown-format input, no        3
            segments on both sides, or invalid options
(output)    ``--json`` path already exists (never overwritten) or unwritable     4
==========  ==================================================================  ====

Human output is Traditional Chinese. ``--json OUT`` writes one JSON object to a
*new* file only (exclusive create; an existing path is refused with exit 4).
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TOOL = "transcript_pair_check"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

DEFAULT_WARN_DIVERGENCE = 0.05
DEFAULT_FAIL_DIVERGENCE = 0.20
DEFAULT_FAIL_MISSING = 0.05
DEFAULT_OVERLAP_TOLERANCE_MS = 50
DEFAULT_MIN_OVERLAP_MS = 200
DEFAULT_POINT_TOLERANCE_MS = 1500
INFERRED_LAST_MS = 2000
MAX_LISTED = 20
SNIPPET = 40

FORMATS = ("jsonl", "json", "srt", "vtt", "txt")
LIVE_NAMES = ("live.jsonl",)
COMPLETE_NAMES = ("transcript.json", "transcript.srt", "transcript.vtt", "transcript.txt")

_TS = r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
_CUE_RE = re.compile(r"^\s*" + _TS + r"\s*-->\s*" + _TS)
_TXT_RE = re.compile(r"^\s*\[\s*" + _TS + r"\s*(?:(?:-->|-|–|~)\s*" + _TS + r"\s*)?\]\s?(.*)$")
_TAG_PREFIX_RE = re.compile(r"^([^\n\r<>:：\[\]]{1,24}): ")
_MARK_RE = re.compile(r"^【(?:未校對|排除|[A-Za-z_]{1,16})】")
_VOICE_RE = re.compile(r"^<v(?:\.[^ >]*)?(?:\s+([^>]*))?>")
_HTML_TAG_RE = re.compile(r"</?[^>]{1,40}>")
_LATIN_RUN_RE = re.compile(r"[0-9a-z]+")


class InputError(Exception):
    """Unreadable / unparsable input (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means "fail" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 參數錯誤：{message}\n")
        raise SystemExit(EXIT_INPUT)


@dataclass
class Segment:
    index: int                 # 0-based position after parsing (file order)
    start_ms: int | None
    end_ms: int | None
    text: str
    key: str | None = None     # clip_id when available
    line: int | None = None    # 1-based source line (best effort)
    align_start: int | None = None
    align_end: int | None = None
    end_inferred: bool = False
    tokens: list[str] = field(default_factory=list)


@dataclass
class Parsed:
    path: str
    fmt: str
    segments: list[Segment]
    timeline: str = "native"   # "live" when transcript.json live_t0_ms is used for pairing
    corrupt_lines: list[int] = field(default_factory=list)
    drafts_ignored: int = 0
    revisions: int = 0
    other_events: int = 0
    speaker_tags_stripped: int = 0


# --------------------------------------------------------------------------- text
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
        or 0x20000 <= o <= 0x3134F
        or 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF   # hiragana / katakana
        or 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF   # hangul
    )


def tokenize(text: str, keep_punct: bool = False) -> list[str]:
    """CJK-aware tokens: one per CJK char, one per [0-9a-z] run; whitespace dropped."""
    norm = unicodedata.normalize("NFKC", text or "").casefold()
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in norm:
        if ch.isspace():
            flush()
            continue
        if _is_cjk(ch):
            flush()
            out.append(ch)
            continue
        if ch.isascii() and ch.isalnum():
            buf.append(ch)
            continue
        cat = unicodedata.category(ch)
        flush()
        if cat[0] in "PSC" or cat in ("Mn", "Me"):
            if keep_punct and cat[0] in "PS":
                out.append(ch)
            continue
        out.append(ch)  # other letters / digits (accented Latin, etc.): one token per char
    flush()
    return out


def _snip(text: str, n: int = SNIPPET) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "--:--:--"
    neg = ms < 0
    ms = abs(int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{'-' if neg else ''}{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _ts_to_ms(h: str | None, m: str, s: str, frac: str | None) -> int:
    ms = int((frac or "0").ljust(3, "0")[:3])
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + ms


# --------------------------------------------------------------------------- reading
def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise InputError(f"找不到檔案：{path}") from exc
    except IsADirectoryError as exc:
        raise InputError(f"這是資料夾不是檔案：{path}") from exc
    except OSError as exc:
        raise InputError(f"無法讀取：{path}（{exc.__class__.__name__}）") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise InputError(f"UTF-16 解碼失敗：{path}") from exc
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError(f"不是 UTF-8 文字檔：{path}（位元組 {exc.start}）") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_format(path: Path, text: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in FORMATS:
        return ext
    if ext in ("ndjson",):
        return "jsonl"
    head = text.lstrip()
    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith(("{", "[")):
        first = head.split("\n", 1)[0].strip()
        if head.startswith("["):
            if _TXT_RE.match(first):
                return "txt"
            return "json"
        try:
            json.loads(first)
            rest = head.split("\n", 1)[1].strip() if "\n" in head else ""
            if rest.startswith("{"):
                return "jsonl"
        except ValueError:
            pass
        return "json"
    lines = [ln for ln in head.split("\n")[:6] if ln.strip()]
    if any(_CUE_RE.match(ln) for ln in lines):
        return "srt"
    if any(_TXT_RE.match(ln) for ln in lines):
        return "txt"
    raise InputError(f"無法判斷格式：{path}（請用 --live-format／--complete-format 指定）")


def _num_ms(obj: dict, ms_keys: Iterable[str], sec_keys: Iterable[str]) -> int | None:
    for k in ms_keys:
        v = obj.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(round(v))
    for k in sec_keys:
        v = obj.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(round(v * 1000))
        if isinstance(v, str):
            m = re.fullmatch(r"\s*" + _TS + r"\s*", v)
            if m:
                return _ts_to_ms(*m.groups())
            try:
                return int(round(float(v) * 1000))
            except ValueError:
                pass
    return None


_START_MS = ("start_ms", "t0_ms", "begin_ms")
_END_MS = ("end_ms", "t1_ms", "stop_ms")
_START_S = ("start", "begin", "from", "t0")
_END_S = ("end", "stop", "to", "t1")


def _seg_text(obj: dict) -> str:
    for k in ("text", "zh", "asr_text", "content", "transcript"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _seg_key(obj: dict) -> str | None:
    v = obj.get("clip_id")
    return str(v) if v not in (None, "") else None


def parse_jsonl(text: str, path: str) -> Parsed:
    p = Parsed(path=path, fmt="jsonl", segments=[])
    by_key: dict[Any, dict] = {}
    order: list[Any] = []
    lines = text.split("\n")
    for no, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            p.corrupt_lines.append(no)
            continue
        if not isinstance(ev, dict):
            p.corrupt_lines.append(no)
            continue
        typ = ev.get("type")
        if typ == "draft":
            p.drafts_ignored += 1
            continue
        if typ not in (None, "final", "segment", "caption"):
            p.other_events += 1
            continue
        txt = _seg_text(ev)
        if not txt.strip():
            if typ == "final":
                p.other_events += 1
            continue
        start = _num_ms(ev, _START_MS, _START_S)
        if start is None and typ is None:
            p.other_events += 1
            continue
        if "seq" in ev and ev.get("seq") is not None:
            k: Any = ("seq", ev.get("seq"))
        elif _seg_key(ev):
            k = ("clip", _seg_key(ev))
        else:
            k = ("line", no)
        ev = dict(ev)
        ev["_line"] = no
        if k in by_key:
            p.revisions += 1
            by_key[k] = ev
        else:
            by_key[k] = ev
            order.append(k)
    for i, k in enumerate(order):
        ev = by_key[k]
        p.segments.append(Segment(
            index=i,
            start_ms=_num_ms(ev, _START_MS, _START_S),
            end_ms=_num_ms(ev, _END_MS, _END_S),
            text=_seg_text(ev),
            key=_seg_key(ev),
            line=ev["_line"],
        ))
    return p


def parse_json(text: str, path: str) -> Parsed:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise InputError(f"JSON 解析失敗：{path}（第 {getattr(exc, 'lineno', '?')} 行）") from exc
    if isinstance(doc, dict):
        items = doc.get("segments")
        if items is None:
            items = doc.get("items", doc.get("captions"))
    else:
        items = doc
    if not isinstance(items, list):
        raise InputError(f"JSON 裡找不到 segments 陣列：{path}")
    p = Parsed(path=path, fmt="json", segments=[])
    raw_live = []
    for obj in items:
        if not isinstance(obj, dict):
            continue
        txt = _seg_text(obj)
        start = _num_ms(obj, _START_MS, _START_S)
        if start is None and not txt.strip():
            continue
        seg = Segment(
            index=len(p.segments),
            start_ms=start,
            end_ms=_num_ms(obj, _END_MS, _END_S),
            text=txt,
            key=_seg_key(obj),
        )
        lt0, lt1 = obj.get("live_t0_ms"), obj.get("live_t1_ms")
        raw_live.append((seg, lt0 if isinstance(lt0, (int, float)) and not isinstance(lt0, bool) else None,
                         lt1 if isinstance(lt1, (int, float)) and not isinstance(lt1, bool) else None))
        p.segments.append(seg)
    with_live = [x for x in raw_live if x[1] is not None]
    if raw_live and len(with_live) * 2 >= len(raw_live):
        p.timeline = "live"
        for seg, lt0, lt1 in raw_live:
            if lt0 is not None:
                seg.align_start = int(round(lt0))
                seg.align_end = int(round(lt1)) if lt1 is not None else None
    return p


def _strip_tags_majority(p: Parsed, enabled: bool) -> None:
    if not enabled or not p.segments:
        return
    hits = [bool(_TAG_PREFIX_RE.match(s.text)) for s in p.segments]
    if sum(hits) * 10 < len(hits) * 6:   # strip "Speaker 0: " only when ≥60% of cues carry it
        return
    for s, hit in zip(p.segments, hits):
        if hit:
            s.text = _TAG_PREFIX_RE.sub("", s.text, count=1)
            p.speaker_tags_stripped += 1


def _clean_cue_text(text: str) -> tuple[str, bool]:
    voice = False
    m = _VOICE_RE.match(text)
    if m:
        text = text[m.end():]
        voice = True
    text = _HTML_TAG_RE.sub("", text)
    return text, voice


def parse_cues(text: str, path: str, fmt: str) -> Parsed:
    p = Parsed(path=path, fmt=fmt, segments=[])
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        m = _CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _ts_to_ms(*g[0:4]), _ts_to_ms(*g[4:8])
        line_no = i + 1
        i += 1
        body: list[str] = []
        while i < n and lines[i].strip() and not _CUE_RE.match(lines[i]):
            body.append(lines[i].strip())
            i += 1
        # SRT: a trailing numeric line before the next cue belongs to the next cue
        if body and fmt == "srt" and i < n and _CUE_RE.match(lines[i]) and body[-1].isdigit():
            body.pop()
        texts = []
        for b in body:
            t, voice = _clean_cue_text(b)
            if voice:
                p.speaker_tags_stripped += 1
            texts.append(t)
        p.segments.append(Segment(index=len(p.segments), start_ms=start, end_ms=end,
                                  text=" ".join(texts).strip(), line=line_no))
    return p


def parse_txt(text: str, path: str) -> Parsed:
    p = Parsed(path=path, fmt="txt", segments=[])
    for no, raw in enumerate(text.split("\n"), 1):
        if not raw.strip():
            continue
        m = _TXT_RE.match(raw)
        if m:
            g = m.groups()
            start = _ts_to_ms(*g[0:4])
            end = _ts_to_ms(*g[4:8]) if g[5] is not None else None
            p.segments.append(Segment(index=len(p.segments), start_ms=start, end_ms=end,
                                      text=g[8].strip(), line=no))
        elif p.segments:
            p.segments[-1].text = (p.segments[-1].text + " " + raw.strip()).strip()
    return p


def _strip_marks(p: Parsed) -> None:
    for s in p.segments:
        s.text = _MARK_RE.sub("", s.text.lstrip(), count=1)


def load(path: Path, fmt: str = "auto", strip_speaker_tags: bool = True) -> Parsed:
    text = read_text(path)
    if fmt == "auto":
        fmt = detect_format(path, text)
    if fmt == "jsonl":
        p = parse_jsonl(text, str(path))
    elif fmt == "json":
        p = parse_json(text, str(path))
    elif fmt in ("srt", "vtt"):
        p = parse_cues(text, str(path), fmt)
    elif fmt == "txt":
        p = parse_txt(text, str(path))
    else:  # pragma: no cover - argparse restricts choices
        raise InputError(f"不支援的格式：{fmt}")
    if fmt in ("srt", "vtt", "txt"):
        _strip_tags_majority(p, strip_speaker_tags)
        _strip_marks(p)
    return p


# --------------------------------------------------------------------------- checks
def _finalize_times(p: Parsed, offset_ms: int = 0) -> None:
    """Fill align_start/align_end; infer missing ends from the next start."""
    segs = p.segments
    for s in segs:
        if s.align_start is None:
            s.align_start = s.start_ms
            s.align_end = s.end_ms
    timed = sorted((s for s in segs if s.align_start is not None), key=lambda s: (s.align_start, s.index))
    for j, s in enumerate(timed):
        if s.align_end is None or s.align_end < s.align_start:
            nxt = next((t.align_start for t in timed[j + 1:] if t.align_start > s.align_start), None)
            s.align_end = nxt if nxt is not None else s.align_start + INFERRED_LAST_MS
            s.end_inferred = True
    if offset_ms:
        for s in segs:
            if s.align_start is not None:
                s.align_start += offset_ms
                s.align_end = (s.align_end or s.align_start) + offset_ms


def timing_issues(p: Parsed, overlap_tolerance_ms: int) -> dict[str, list[dict]]:
    backwards, overlaps, negative, untimed = [], [], [], []
    prev: Segment | None = None
    max_end: int | None = None
    for s in p.segments:
        if s.start_ms is None:
            untimed.append({"index": s.index, "line": s.line, "text": _snip(s.text)})
            continue
        if s.end_ms is not None and s.end_ms < s.start_ms:
            negative.append({"index": s.index, "line": s.line, "start_ms": s.start_ms, "end_ms": s.end_ms,
                             "text": _snip(s.text)})
        if prev is not None:
            if s.start_ms < prev.start_ms:
                backwards.append({"index": s.index, "line": s.line, "start_ms": s.start_ms,
                                  "prev_start_ms": prev.start_ms, "text": _snip(s.text)})
            elif max_end is not None and s.start_ms < max_end - overlap_tolerance_ms:
                overlaps.append({"index": s.index, "line": s.line, "start_ms": s.start_ms,
                                 "prev_end_ms": max_end, "overlap_ms": max_end - s.start_ms,
                                 "text": _snip(s.text)})
        prev = s
        if s.end_ms is not None and s.end_ms >= s.start_ms:
            max_end = s.end_ms if max_end is None else max(max_end, s.end_ms)
    return {"backwards": backwards, "overlaps": overlaps, "negative": negative, "untimed": untimed}


def _connected(a: Segment, b: Segment, min_overlap_ms: int, point_tol_ms: int) -> bool:
    if a.align_start is None or b.align_start is None:
        return False
    a0, a1 = a.align_start, max(a.align_end or a.align_start, a.align_start)
    b0, b1 = b.align_start, max(b.align_end or b.align_start, b.align_start)
    if a1 - a0 <= 0 or b1 - b0 <= 0 or a.end_inferred or b.end_inferred:
        if abs(a0 - b0) <= point_tol_ms:
            return True
    ov = min(a1, b1) - max(a0, b0)
    if ov <= 0:
        return False
    shorter = min(a1 - a0, b1 - b0)
    return ov >= min(min_overlap_ms, max(1, shorter)) and ov * 10 >= shorter * 3


def _groups_by_time(live: list[Segment], comp: list[Segment], min_overlap_ms: int,
                    point_tol_ms: int) -> list[tuple[list[Segment], list[Segment]]]:
    nodes = [("L", s) for s in live] + [("C", s) for s in comp]
    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    li = sorted((i for i, (side, s) in enumerate(nodes) if side == "L" and s.align_start is not None),
                key=lambda i: nodes[i][1].align_start)
    ci = sorted((i for i, (side, s) in enumerate(nodes) if side == "C" and s.align_start is not None),
                key=lambda i: nodes[i][1].align_start)
    lo = 0
    for i in li:
        a = nodes[i][1]
        reach = max(a.align_end or a.align_start, a.align_start) + point_tol_ms
        floor = a.align_start - point_tol_ms
        while lo < len(ci) and max(nodes[ci[lo]][1].align_end or 0, nodes[ci[lo]][1].align_start) < floor:
            lo += 1
        k = lo
        while k < len(ci) and nodes[ci[k]][1].align_start <= reach:
            if _connected(a, nodes[ci[k]][1], min_overlap_ms, point_tol_ms):
                union(i, ci[k])
            k += 1
    groups: dict[int, tuple[list[Segment], list[Segment]]] = {}
    for i, (side, s) in enumerate(nodes):
        g = groups.setdefault(find(i), ([], []))
        (g[0] if side == "L" else g[1]).append(s)
    return list(groups.values())


def _groups_by_key(live: list[Segment], comp: list[Segment], min_overlap_ms: int,
                   point_tol_ms: int) -> list[tuple[list[Segment], list[Segment]]]:
    """Pair by clip_id where both sides have it; everything else falls back to time overlap."""
    shared = {s.key for s in live if s.key} & {s.key for s in comp if s.key}
    groups: dict[Any, tuple[list[Segment], list[Segment]]] = {}
    for s in live:
        if s.key in shared:
            groups.setdefault(s.key, ([], []))[0].append(s)
    for s in comp:
        if s.key in shared:
            groups.setdefault(s.key, ([], []))[1].append(s)
    rest_l = [s for s in live if s.key not in shared]
    rest_c = [s for s in comp if s.key not in shared]
    return list(groups.values()) + _groups_by_time(rest_l, rest_c, min_overlap_ms, point_tol_ms)


def _choose_mode(live: list[Segment], comp: list[Segment], mode: str) -> str:
    if mode != "auto":
        return mode
    lk = {s.key for s in live if s.key}
    ck = [s.key for s in comp if s.key]
    if ck and lk and len(ck) * 2 >= len(comp) and sum(1 for k in ck if k in lk) * 2 >= len(ck):
        return "key"
    return "time"


def _sort_key(seg_list: list[Segment]) -> tuple:
    starts = [s.align_start for s in seg_list if s.align_start is not None]
    return (min(starts) if starts else 10 ** 15, min(s.index for s in seg_list))


def analyze(live: Parsed, comp: Parsed, *, mode: str = "auto", offset_ms: int = 0,
            warn_divergence: float = DEFAULT_WARN_DIVERGENCE, fail_divergence: float = DEFAULT_FAIL_DIVERGENCE,
            fail_missing: float = DEFAULT_FAIL_MISSING, overlap_tolerance_ms: int = DEFAULT_OVERLAP_TOLERANCE_MS,
            min_overlap_ms: int = DEFAULT_MIN_OVERLAP_MS, point_tolerance_ms: int = DEFAULT_POINT_TOLERANCE_MS,
            keep_punct: bool = False) -> dict[str, Any]:
    _finalize_times(live)
    _finalize_times(comp, offset_ms)
    for s in live.segments + comp.segments:
        s.tokens = tokenize(s.text, keep_punct)
    live_segs = [s for s in live.segments if s.tokens]
    comp_segs = [s for s in comp.segments if s.tokens]

    used_mode = _choose_mode(live_segs, comp_segs, mode)
    if used_mode == "key":
        groups = _groups_by_key(live_segs, comp_segs, min_overlap_ms, point_tolerance_ms)
    else:
        groups = _groups_by_time(live_segs, comp_segs, min_overlap_ms, point_tolerance_ms)
    groups.sort(key=lambda g: _sort_key(sorted(g[0] + g[1], key=lambda s: s.index)))

    live_tok_total = sum(len(s.tokens) for s in live_segs)
    comp_tok_total = sum(len(s.tokens) for s in comp_segs)
    missing_in_complete, missing_in_live, pairs = [], [], []
    m_total = 0
    pair_live_tok = pair_comp_tok = 0
    miss_c_tok = miss_l_tok = 0
    for lg, cg in groups:
        lg = sorted(lg, key=lambda s: (s.align_start if s.align_start is not None else 0, s.index))
        cg = sorted(cg, key=lambda s: (s.align_start if s.align_start is not None else 0, s.index))
        if lg and not cg:
            for s in lg:
                miss_c_tok += len(s.tokens)
                missing_in_complete.append({"start_ms": s.align_start, "end_ms": s.align_end, "line": s.line,
                                            "tokens": len(s.tokens), "text": _snip(s.text)})
            continue
        if cg and not lg:
            for s in cg:
                miss_l_tok += len(s.tokens)
                missing_in_live.append({"start_ms": s.align_start, "end_ms": s.align_end, "line": s.line,
                                        "tokens": len(s.tokens), "text": _snip(s.text)})
            continue
        lt = [t for s in lg for t in s.tokens]
        ct = [t for s in cg for t in s.tokens]
        sm = difflib.SequenceMatcher(None, lt, ct, autojunk=False)
        matches = sum(b.size for b in sm.get_matching_blocks())
        m_total += matches
        pair_live_tok += len(lt)
        pair_comp_tok += len(ct)
        ratio = 2.0 * matches / (len(lt) + len(ct)) if (lt or ct) else 1.0
        pairs.append({
            "start_ms": lg[0].align_start,
            "live_segments": len(lg),
            "complete_segments": len(cg),
            "divergence": round(1.0 - ratio, 4),
            "live_text": _snip(" ".join(s.text for s in lg), 60),
            "complete_text": _snip(" ".join(s.text for s in cg), 60),
        })

    def rate(m: int, a: int, b: int) -> float | None:
        return round(1.0 - 2.0 * m / (a + b), 4) if (a + b) else None

    matched_div = rate(m_total, pair_live_tok, pair_comp_tok)
    overall_div = rate(m_total, live_tok_total, comp_tok_total)
    miss_c_ratio = round(miss_c_tok / live_tok_total, 4) if live_tok_total else 0.0
    miss_l_ratio = round(miss_l_tok / comp_tok_total, 4) if comp_tok_total else 0.0

    t_live = timing_issues(live, overlap_tolerance_ms)
    t_comp = timing_issues(comp, overlap_tolerance_ms)

    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    if live_segs and comp_segs and not pairs:
        add("fail", "nothing_paired",
            "兩份逐字稿沒有任何一段能對上：可能時間軸不同（錄音室的 .srt/.txt 用音檔時間；請改用 transcript.json "
            "或 --offset-ms），或其中一份是別堂課。")
    elif not live_segs:
        add("fail", "live_empty", "即時逐字稿沒有任何定稿文字（錄音時沒寫進 live.jsonl？）。")
    elif not comp_segs:
        add("fail", "complete_empty", "完整逐字稿沒有任何文字（錄音結束後沒寫出 transcript？）。")
    if missing_in_complete:
        lvl = "fail" if miss_c_ratio >= fail_missing else "warn"
        add(lvl, "missing_in_complete",
            f"完整逐字稿少了 {len(missing_in_complete)} 段即時字幕（{miss_c_ratio:.1%} 字）。")
    if missing_in_live:
        lvl = "fail" if miss_l_ratio >= fail_missing else "warn"
        add(lvl, "missing_in_live",
            f"有 {len(missing_in_live)} 段只在完整逐字稿出現、即時逐字稿沒有（{miss_l_ratio:.1%} 字；"
            "即時檔可能中途中斷或之後人工補段）。")
    if t_comp["backwards"]:
        add("fail", "complete_backwards", f"完整逐字稿有 {len(t_comp['backwards'])} 處時間戳倒退（應依時間排序）。")
    if t_comp["negative"]:
        add("fail", "complete_negative", f"完整逐字稿有 {len(t_comp['negative'])} 段結束早於開始。")
    if t_comp["overlaps"]:
        add("warn", "complete_overlap", f"完整逐字稿有 {len(t_comp['overlaps'])} 處時間重疊。")
    if t_live["backwards"]:
        add("warn", "live_backwards", f"即時逐字稿有 {len(t_live['backwards'])} 處時間戳倒退（定稿晚到或時鐘跳動）。")
    if t_live["negative"]:
        add("warn", "live_negative", f"即時逐字稿有 {len(t_live['negative'])} 段結束早於開始。")
    if t_live["overlaps"]:
        add("warn", "live_overlap", f"即時逐字稿有 {len(t_live['overlaps'])} 處時間重疊。")
    if t_live["untimed"] or t_comp["untimed"]:
        add("warn", "untimed", f"有 {len(t_live['untimed']) + len(t_comp['untimed'])} 段沒有時間戳，無法依時間比對。")
    if live.corrupt_lines:
        add("warn", "corrupt_lines",
            f"即時逐字稿有 {len(live.corrupt_lines)} 行無法解析（常見於當機時最後一行被截斷）。")
    if comp.corrupt_lines:
        add("warn", "complete_corrupt_lines", f"完整逐字稿有 {len(comp.corrupt_lines)} 行無法解析。")
    if matched_div is not None:
        if matched_div >= fail_divergence:
            add("fail", "divergence", f"對上段落的文字落差率 {matched_div:.1%} ≥ {fail_divergence:.0%}。")
        elif matched_div >= warn_divergence:
            add("warn", "divergence", f"對上段落的文字落差率 {matched_div:.1%} ≥ {warn_divergence:.0%}"
                                      "（課後校對修改也會造成落差）。")

    if any(i["level"] == "fail" for i in issues):
        status, code = "fail", EXIT_FAIL
    elif issues:
        status, code = "warn", EXIT_WARN
    else:
        status, code = "ok", EXIT_OK

    worst = sorted((p for p in pairs if p["divergence"] > 0), key=lambda p: -p["divergence"])[:MAX_LISTED]

    def side(p: Parsed, segs: list[Segment], tok: int) -> dict[str, Any]:
        starts = [s.align_start for s in segs if s.align_start is not None]
        ends = [s.align_end for s in segs if s.align_end is not None]
        return {
            "path": p.path, "format": p.fmt, "timeline": p.timeline,
            "segments": len(p.segments), "text_segments": len(segs), "tokens": tok,
            "first_ms": min(starts) if starts else None, "last_ms": max(ends) if ends else None,
            "corrupt_lines": p.corrupt_lines[:MAX_LISTED], "corrupt_line_count": len(p.corrupt_lines),
            "drafts_ignored": p.drafts_ignored, "revisions": p.revisions, "other_events": p.other_events,
            "speaker_tags_stripped": p.speaker_tags_stripped,
        }

    def trim(t: dict[str, list[dict]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in t.items():
            out[k] = v[:MAX_LISTED]
            out[f"{k}_count"] = len(v)
        return out

    report: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": code,
        "match_mode": used_mode,
        "offset_ms": offset_ms,
        "thresholds": {
            "warn_divergence": warn_divergence, "fail_divergence": fail_divergence,
            "fail_missing": fail_missing, "overlap_tolerance_ms": overlap_tolerance_ms,
            "min_overlap_ms": min_overlap_ms, "point_tolerance_ms": point_tolerance_ms,
            "keep_punct": keep_punct,
        },
        "live": side(live, live_segs, live_tok_total),
        "complete": side(comp, comp_segs, comp_tok_total),
        "missing": {
            "in_complete_count": len(missing_in_complete), "in_complete_token_ratio": miss_c_ratio,
            "in_complete": missing_in_complete[:MAX_LISTED],
            "in_live_count": len(missing_in_live), "in_live_token_ratio": miss_l_ratio,
            "in_live": missing_in_live[:MAX_LISTED],
        },
        "timing": {"live": trim(t_live), "complete": trim(t_comp)},
        "divergence": {
            "paired_groups": len(pairs),
            "matched_divergence": matched_div,
            "overall_divergence": overall_div,
            "identical_groups": sum(1 for p in pairs if p["divergence"] == 0),
            "worst": worst,
        },
        "issues": issues,
    }
    report["summary_zh"] = summary_zh(report)
    return report


# --------------------------------------------------------------------------- output
_STATUS_ZH = {"ok": "通過", "warn": "警告", "fail": "不通過"}


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def summary_zh(r: dict[str, Any]) -> str:
    lv, cp, d, ms = r["live"], r["complete"], r["divergence"], r["missing"]
    lines = [
        f"逐字稿比對：{_STATUS_ZH[r['status']]}（exit {r['exit_code']}）",
        f"- 即時逐字稿：{lv['path']}（{lv['format']}，{lv['text_segments']} 段、{lv['tokens']} 字詞"
        + (f"、忽略草稿 {lv['drafts_ignored']} 筆" if lv["drafts_ignored"] else "")
        + (f"、修訂 {lv['revisions']} 次" if lv["revisions"] else "") + "）",
        f"- 完整逐字稿：{cp['path']}（{cp['format']}，{cp['text_segments']} 段、{cp['tokens']} 字詞；"
        f"時間軸：{'即時（live_t0_ms）' if cp['timeline'] == 'live' else '檔案本身'}）",
        f"- 配對方式：{'clip_id' if r['match_mode'] == 'key' else '時間重疊'}"
        + (f"（完整稿位移 {r['offset_ms']} ms）" if r["offset_ms"] else "")
        + f"；對上 {d['paired_groups']} 組，其中完全一致 {d['identical_groups']} 組",
        f"- 文字落差率：對上段落 {_pct(d['matched_divergence'])}；含缺段整體 {_pct(d['overall_divergence'])}",
        f"- 缺段：完整稿缺 {ms['in_complete_count']} 段（{_pct(ms['in_complete_token_ratio'])}）；"
        f"即時稿缺 {ms['in_live_count']} 段（{_pct(ms['in_live_token_ratio'])}）",
    ]
    tl, tc = r["timing"]["live"], r["timing"]["complete"]
    lines.append(f"- 時間戳：即時稿 倒退 {tl['backwards_count']}／重疊 {tl['overlaps_count']}／負長度 "
                 f"{tl['negative_count']}；完整稿 倒退 {tc['backwards_count']}／重疊 {tc['overlaps_count']}／"
                 f"負長度 {tc['negative_count']}")
    if r["issues"]:
        lines.append("問題：")
        for i in r["issues"]:
            lines.append(f"  [{'失敗' if i['level'] == 'fail' else '警告'}] {i['message']}")
    for s in ms["in_complete"][:5]:
        lines.append(f"  完整稿缺：{fmt_ms(s['start_ms'])} {s['text']}")
    for s in ms["in_live"][:5]:
        lines.append(f"  即時稿缺：{fmt_ms(s['start_ms'])} {s['text']}")
    for p in d["worst"][:5]:
        lines.append(f"  落差 {p['divergence']:.0%} @ {fmt_ms(p['start_ms'])}：即時「{p['live_text']}」→ 完整「{p['complete_text']}」")
    if r["status"] == "ok":
        lines.append("兩份逐字稿一致，可放心以完整逐字稿為準。")
    return "\n".join(lines)


def resolve_dir(d: Path) -> tuple[Path, Path]:
    if not d.is_dir():
        raise InputError(f"找不到錄音資料夾：{d}")
    live = next((d / n for n in LIVE_NAMES if (d / n).is_file()), None)
    comp = next((d / n for n in COMPLETE_NAMES if (d / n).is_file()), None)
    if live is None:
        raise InputError(f"資料夾裡沒有 live.jsonl：{d}")
    if comp is None:
        raise InputError(f"資料夾裡沒有 transcript.json／.srt／.vtt／.txt：{d}")
    return live, comp


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="transcript_pair_check.py",
                 description="唯讀比對即時逐字稿與錄音後完整逐字稿（缺段、時間戳、文字落差率）。")
    ap.add_argument("--dir", type=Path, help="錄音資料夾（自動找 live.jsonl 與 transcript.json/.srt/.vtt/.txt）")
    ap.add_argument("--live", type=Path, help="錄音時即時寫入的逐字稿（live.jsonl／.srt／.vtt／.txt／.json）")
    ap.add_argument("--complete", type=Path, help="錄音結束後與音檔並存的完整逐字稿")
    ap.add_argument("--live-format", choices=("auto",) + FORMATS, default="auto")
    ap.add_argument("--complete-format", choices=("auto",) + FORMATS, default="auto")
    ap.add_argument("--match", choices=("auto", "key", "time"), default="auto",
                    help="配對方式：clip_id（key）或時間重疊（time）；預設自動")
    ap.add_argument("--offset-ms", type=int, default=0, help="完整稿時間位移（毫秒，加到完整稿時間上）")
    ap.add_argument("--warn-divergence", type=float, default=DEFAULT_WARN_DIVERGENCE)
    ap.add_argument("--fail-divergence", type=float, default=DEFAULT_FAIL_DIVERGENCE)
    ap.add_argument("--fail-missing", type=float, default=DEFAULT_FAIL_MISSING,
                    help="缺段字數占比達此值即不通過（預設 0.05）")
    ap.add_argument("--overlap-tolerance-ms", type=int, default=DEFAULT_OVERLAP_TOLERANCE_MS)
    ap.add_argument("--min-overlap-ms", type=int, default=DEFAULT_MIN_OVERLAP_MS)
    ap.add_argument("--point-tolerance-ms", type=int, default=DEFAULT_POINT_TOLERANCE_MS)
    ap.add_argument("--keep-punct", action="store_true", help="標點也算進落差率")
    ap.add_argument("--keep-speaker-tags", action="store_true", help="不要去掉 SRT/TXT 的「Speaker 0: 」前綴")
    ap.add_argument("--json", type=Path, metavar="OUT", help="另寫 JSON 報告到新檔（已存在就拒絕，不覆蓋）")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.dir is not None and (args.live is not None or args.complete is not None):
        ap.error("--dir 不能和 --live/--complete 同時使用")
    if args.dir is None and (args.live is None or args.complete is None):
        ap.error("請給 --dir，或同時給 --live 與 --complete")
    for name in ("warn_divergence", "fail_divergence", "fail_missing"):
        v = getattr(args, name)
        if not (0.0 <= v <= 1.0):
            ap.error(f"--{name.replace('_', '-')} 必須在 0～1 之間")
    if args.warn_divergence > args.fail_divergence:
        ap.error("--warn-divergence 不能大於 --fail-divergence")
    for name in ("overlap_tolerance_ms", "min_overlap_ms", "point_tolerance_ms"):
        if getattr(args, name) < 0:
            ap.error(f"--{name.replace('_', '-')} 不能是負數")

    if args.json is not None and (args.json.exists() or args.json.is_symlink()):
        sys.stderr.write(f"拒絕覆蓋既有檔案：{args.json}（--json 只寫新檔）\n")
        return EXIT_OUTPUT

    try:
        if args.dir is not None:
            live_path, comp_path = resolve_dir(args.dir)
        else:
            live_path, comp_path = args.live, args.complete
        strip = not args.keep_speaker_tags
        live = load(live_path, args.live_format, strip)
        comp = load(comp_path, args.complete_format, strip)
        if not live.segments and not comp.segments:
            raise InputError("兩份逐字稿都沒有可比對的段落。")
    except InputError as exc:
        sys.stderr.write(f"輸入錯誤：{exc}\n")
        return EXIT_INPUT

    report = analyze(
        live, comp, mode=args.match, offset_ms=args.offset_ms,
        warn_divergence=args.warn_divergence, fail_divergence=args.fail_divergence,
        fail_missing=args.fail_missing, overlap_tolerance_ms=args.overlap_tolerance_ms,
        min_overlap_ms=args.min_overlap_ms, point_tolerance_ms=args.point_tolerance_ms,
        keep_punct=args.keep_punct,
    )

    if args.json is not None:
        try:
            with open(args.json, "x", encoding="utf-8", newline="\n") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except FileExistsError:
            sys.stderr.write(f"拒絕覆蓋既有檔案：{args.json}（--json 只寫新檔）\n")
            return EXIT_OUTPUT
        except OSError as exc:
            sys.stderr.write(f"無法寫入 JSON：{args.json}（{exc.__class__.__name__}）\n")
            return EXIT_OUTPUT

    print(report["summary_zh"])
    if args.json is not None:
        print(f"JSON 報告：{args.json}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
