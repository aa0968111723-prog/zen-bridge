#!/usr/bin/env python3
"""Offline caption gap detector: speech without captions on screen (queue item 36).

  python tools/caption_gap_detector.py --db captions.sqlite3 [--room R] [--session S]
  python tools/caption_gap_detector.py --jsonl recordings/.../live.jsonl --json OUT.json
  python tools/caption_gap_detector.py --db zen.sqlite3 --max-gap-s 6 --max-draft-s 10

Finds two kinds of problems in a recorded live-caption session (read-only,
stdlib only, no network, no app imports):

* **caption gaps** — while somebody is speaking, the screen gets no new draft
  or final caption for more than ``--max-gap-s`` seconds;
* **stuck drafts** — a grey draft line stays on screen for more than
  ``--max-draft-s`` seconds before its final line replaces it (or never gets
  one).

It reports p50 / p95 / max of both, and lists the offending windows.

Inputs (at least one; both may be given; never modified):

* ``--db`` — SQLite opened with a URI ``mode=ro`` (plus ``immutable=1`` when
  there is no ``-wal`` file).  Either the live-room caption store
  (``captions.sqlite3``, table ``captions``: ``id, room_id, session_id, seq,
  version, zh, en, status, t0_ms, t1_ms, updated_at``) or the admin ledger
  (``zen.sqlite3``: ``segments`` ``t0_ms/t1_ms/status/speech_ratio/created_at``
  joined with the first ``transcripts.created_at``).
* ``--jsonl`` — UTF-8 JSON Lines (repeatable).  Recording ``live.jsonl`` events
  (``{"at", "type": "start"|"draft"|"final"|"pause"|"resume"|"end", "seq",
  "t0_ms", "t1_ms", "text", "asr_text"}``) or live-room bus events
  (``type`` ``caption``/``final``/``update``/``draft``/``paused``/``resumed``
  with ``id, room_id, session_id, seq, version, zh, en, status, t0_ms, t1_ms,
  speech_ratio, t1_wall_ms``) carrying a wall clock in ``at``/``ts``/``time``/
  ``updated_at`` (seconds) or ``at_ms``/``ts_ms``/``wall_ms`` (milliseconds).
  Optional ``{"type": "vad"|"speech", "t0_ms", "t1_ms"}`` lines add explicit
  speech activity.

Assumptions (the live room stores no separate VAD track):

* **Speech activity** is derived from the audio slices that exist: a slice
  ``[t0_ms, t1_ms]`` counts as speech unless its status is ``silent`` /
  ``cancelled`` / ``paused`` / ``deleted`` or Silero ``speech_ratio`` is 0, and
  it has recognised text, failed (``error``/``missing``/``timeout``) or is still
  in progress.  Draft lines with ``t0_ms/t1_ms`` and explicit ``vad`` lines also
  count.
* ``t0_ms/t1_ms`` are on the session audio timeline; the screen is on the wall
  clock.  The offset comes from ``t1_wall_ms`` when present (median), else from
  the fastest observed slice (``min(wall - t1_ms)``, minus ``--min-lag-ms``), so
  the first wait of an utterance is a lower bound.
* The ``captions`` table only keeps the *last* write time (``updated_at``), so a
  row is assumed on screen at that time (an upper bound); rows rewritten more
  than ``--late-rewrite-s`` after their audio (later edits) are left out.
* Drafts are never stored in either database, so stuck-draft checks need JSONL.
* Private pause (``paused``/``resumed``, recording ``pause``/``resume``) windows
  are not speech; the gap is closed when the pause starts.

==========  ==================================================================  ====
status      condition                                                           exit
==========  ==================================================================  ====
``ok``      no gap > ``--max-gap-s`` (5), no draft > ``--max-draft-s`` (8),      0
            nothing left in progress
``warn``    some gap or stuck draft over the limit, slices still in progress,   1
            corrupt JSONL lines, no speech timeline, or a high p95 from < 20 gaps
``fail``    caption-gap p95 ≥ ``--fail-p95-s`` (10) over ≥ 20 gap samples       2
(input)     missing / unreadable / unknown-schema input, no timed caption       3
            events, or invalid options (argparse errors use 3, not 2)
(output)    ``--json`` path already exists (never overwritten) or unwritable     4
==========  ==================================================================  ====

Human output is Traditional Chinese. ``--json OUT`` writes one JSON object to a
*new* file only (exclusive create; an existing path is refused with exit 4).
Percentiles use nearest-rank.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL = "caption_gap_detector"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

DEFAULT_MAX_GAP_S = 5.0
DEFAULT_MAX_DRAFT_S = 8.0
DEFAULT_FAIL_P95_S = 10.0
DEFAULT_MIN_LAG_MS = 0
DEFAULT_LATE_REWRITE_S = 120.0
OVERLAP_TOLERANCE_MS = 500
MIN_GAPS_FOR_FAIL = 20            # nearest-rank p95 of fewer samples is just the max: warn, do not fail
MAX_LISTED = 20
SNIPPET = 30

FAILURES = frozenset({"error", "missing", "timeout", "cancelled"})
NO_SPEECH = frozenset({"silent", "cancelled", "paused", "deleted"})
NO_DISPLAY = FAILURES | {"silent", "paused", "deleted"}
IN_PROGRESS = frozenset({"queued", "transcribing", "decoding", "received", ""})
TERMINAL = FAILURES | {"ready", "zh_ready", "translate_failed", "silent", "asr_done", "translated", "deleted"}
CAPTION_TYPES = frozenset({"caption", "final", "update"})
PAUSE_TYPES = frozenset({"paused", "pause"})
RESUME_TYPES = frozenset({"resumed", "resume"})
VAD_TYPES = frozenset({"vad", "speech", "speech_activity"})
WALL_S_KEYS = ("at", "ts", "time", "updated_at", "created_at")
WALL_MS_KEYS = ("at_ms", "ts_ms", "wall_ms")
TEXT_KEYS = ("text", "zh", "asr_text", "en")


class InputError(Exception):
    """Unreadable / unparsable input (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means "fail" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 參數錯誤：{message}\n")
        raise SystemExit(EXIT_INPUT)


# --------------------------------------------------------------------------- model
@dataclass
class Seg:
    key: Any
    source: str                       # "jsonl" | "captions" | "ledger"
    t0: int | None = None
    t1: int | None = None
    status: str = ""
    order: tuple = (-1, -1.0)         # (version, wall) of the event that set ``status``
    text: str = ""
    ratio: float | None = None
    first_seen: float | None = None   # wall ms of the first event that carried t1_ms
    display_at: float | None = None   # wall ms the caption first showed text
    terminal_at: float | None = None
    late: bool = False


@dataclass
class Draft:
    at: float
    key: Any
    t0: int | None
    t1: int | None
    text: str


@dataclass
class Session:
    room_id: str
    session_id: str
    segs: dict = field(default_factory=dict)
    drafts: list = field(default_factory=list)
    updates: list = field(default_factory=list)          # (wall ms, "draft" | "caption")
    terminals: list = field(default_factory=list)        # (wall ms, key, t0, t1)
    vad: list = field(default_factory=list)              # (t0, t1, wall ms)
    pauses: list = field(default_factory=list)           # (wall ms, "pause" | "resume")
    walls: list = field(default_factory=list)
    end_at: float | None = None
    t1_wall: list = field(default_factory=list)
    anon: int = 0

    @property
    def label(self) -> str:
        if self.room_id and self.session_id:
            return f"{self.room_id}/{self.session_id}"
        return self.session_id or self.room_id or "（未標示場次）"


@dataclass
class Loaded:
    sessions: dict = field(default_factory=dict)
    inputs: list = field(default_factory=list)
    corrupt_lines: int = 0
    untimed_events: int = 0
    other_events: int = 0
    room_pauses: dict = field(default_factory=dict)

    def session(self, room: str, sess: str) -> Session:
        key = (room, sess)
        if key not in self.sessions:
            self.sessions[key] = Session(room, sess)
        return self.sessions[key]


# --------------------------------------------------------------------------- helpers
def nearest_rank(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(1, math.ceil(p * len(ordered))) - 1])


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            f = float(value)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else int(round(f))


def wall_ms(ev: dict) -> float | None:
    """Wall clock of an event in ms (seconds keys > 1e11 are taken as ms already)."""
    for k in WALL_MS_KEYS:
        v = _num(ev.get(k))
        if v is not None:
            return v
    for k in WALL_S_KEYS:
        v = _num(ev.get(k))
        if v is not None:
            return v if v > 1e11 else v * 1000.0
    return None


def _text(ev: dict) -> str:
    for k in TEXT_KEYS:
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _key(ev: dict) -> Any:
    seq = _int(ev.get("seq"))
    if seq is not None:
        return seq
    ident = ev.get("id")
    if isinstance(ident, str) and ident:
        tail = ident.rsplit(":", 1)[-1]
        return int(tail) if tail.isdigit() else ident
    return None


def _snip(text: str, n: int = SNIPPET) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def fmt_ms(ms: float | None) -> str:
    if ms is None:
        return "--:--:--"
    neg = ms < 0
    ms = abs(int(round(ms)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{'-' if neg else ''}{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _sec(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.1f} 秒"


def _r(v: float | None) -> float | int | None:
    if v is None:
        return None
    v = round(float(v), 3)
    return int(v) if v == int(v) else v


# --------------------------------------------------------------------------- caption events
def _add_caption(sess: Session, ev: dict, at: float, typ: str, source: str = "jsonl") -> None:
    key = _key(ev)
    if key is None:
        sess.anon += 1
        key = ("anon", sess.anon)
    seg = sess.segs.get(key)
    if seg is None:
        seg = sess.segs[key] = Seg(key=key, source=source)
    t0, t1 = _int(ev.get("t0_ms")), _int(ev.get("t1_ms"))
    if t0 is not None:
        seg.t0 = t0
    if t1 is not None:
        seg.t1 = t1
        seg.first_seen = at if seg.first_seen is None else min(seg.first_seen, at)
    raw_status = ev.get("status")
    status = str(raw_status).strip().lower() if isinstance(raw_status, str) else ""
    if not status and typ == "final":
        status = "ready"
    text = _text(ev)
    order = (_int(ev.get("version")) or 0, at)
    if order >= seg.order:
        seg.order, seg.status = order, status
        if text or status in NO_DISPLAY:
            seg.text = text
    ratio = _num(ev.get("speech_ratio"))
    if ratio is not None:
        seg.ratio = ratio
    tw = _num(ev.get("t1_wall_ms"))
    if tw is not None and t1 is not None:
        sess.t1_wall.append(tw - t1)
    if text and status not in NO_DISPLAY:
        seg.display_at = at if seg.display_at is None else min(seg.display_at, at)
        sess.updates.append((at, "caption"))
    if typ == "final" or status in TERMINAL or (not status and text):
        seg.terminal_at = at if seg.terminal_at is None else min(seg.terminal_at, at)
        sess.terminals.append((at, key, seg.t0 if t0 is None else t0, seg.t1 if t1 is None else t1))


def parse_jsonl(path: Path, out: Loaded, room: str | None, session: str | None) -> None:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise InputError(f"找不到檔案：{path}") from exc
    except IsADirectoryError as exc:
        raise InputError(f"這是資料夾不是檔案：{path}") from exc
    except OSError as exc:
        raise InputError(f"無法讀取：{path}（{exc.__class__.__name__}）") from exc
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"不是 UTF-8 文字檔：{path}（位元組 {exc.start}）") from exc
    cur_room, cur_sess = "", ""
    lines = corrupt = used = 0
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        lines += 1
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if not isinstance(ev, dict):
            corrupt += 1
            continue
        typ = str(ev.get("type") or "").strip().lower()
        ev_room = ev.get("room_id") if isinstance(ev.get("room_id"), str) else None
        ev_sess = ev.get("session_id") if isinstance(ev.get("session_id"), str) else None
        if typ == "start":
            cur_room, cur_sess = ev_room or "", ev_sess or ""
        r, s = (ev_room if ev_room is not None else cur_room), (ev_sess if ev_sess is not None else cur_sess)
        room_note = (typ in PAUSE_TYPES or typ in RESUME_TYPES) and ev_sess is None and bool(ev_room)
        if (room is not None and r != room) or (session is not None and s != session and not room_note):
            continue
        at = wall_ms(ev)
        if at is None:
            out.untimed_events += 1
            continue
        if room_note:                     # live-room bus notes carry only room_id: apply to its sessions
            out.room_pauses.setdefault(ev_room, []).append((at, "pause" if typ in PAUSE_TYPES else "resume"))
            used += 1
            continue
        sess = out.session(r, s)
        sess.walls.append(at)
        used += 1
        if typ == "draft":
            dt = _text(ev)
            if not dt:
                continue
            d = Draft(at, _key(ev), _int(ev.get("t0_ms")), _int(ev.get("t1_ms")), dt)
            sess.drafts.append(d)
            sess.updates.append((at, "draft"))
        elif typ in CAPTION_TYPES or (not typ and ("t0_ms" in ev or "seq" in ev)):
            _add_caption(sess, ev, at, typ or "caption")
        elif typ in VAD_TYPES:
            t0, t1 = _int(ev.get("t0_ms")), _int(ev.get("t1_ms"))
            if t0 is None or t1 is None or t1 <= t0 or ev.get("speech") is False:
                continue
            ratio = _num(ev.get("speech_ratio"))
            if ratio is not None and ratio <= 0:
                continue
            sess.vad.append((t0, t1, at))
        elif typ in PAUSE_TYPES or typ in RESUME_TYPES:
            sess.pauses.append((at, "pause" if typ in PAUSE_TYPES else "resume"))
        elif typ == "end":
            sess.end_at = at if sess.end_at is None else max(sess.end_at, at)
        else:
            out.other_events += 1
    out.corrupt_lines += corrupt
    out.inputs.append({"path": str(path), "kind": "jsonl", "schema": "events", "lines": lines,
                       "events_used": used, "corrupt_lines": corrupt})


# --------------------------------------------------------------------------- sqlite
def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = path.as_uri() + "?mode=ro"
    if not Path(str(path) + "-wal").exists():
        uri += "&immutable=1"
    elif not Path(str(path) + "-shm").exists():
        raise InputError("有 -wal 但沒有 -shm：請先 checkpoint 資料庫再離線分析")
    return sqlite3.connect(uri, uri=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def read_db(db: Path, out: Loaded, room: str | None, session: str | None) -> None:
    path = Path(db).resolve()
    if not path.is_file():
        raise InputError(f"找不到資料庫：{path}")
    try:
        with closing(_connect_ro(path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            where, params = [], []
            if "captions" in tables:
                schema = "captions"
                cols = _columns(conn, "captions")
                need = {"room_id", "session_id", "seq", "status", "t0_ms", "t1_ms", "updated_at"}
                if not need <= cols:
                    raise InputError(f"captions 表缺欄位：{', '.join(sorted(need - cols))}")
                ver = "version" if "version" in cols else "1"
                zh = "zh" if "zh" in cols else "''"
                en = "en" if "en" in cols else "''"
                if room is not None:
                    where.append("room_id=?")
                    params.append(room)
                if session is not None:
                    where.append("session_id=?")
                    params.append(session)
                sql = (f"SELECT room_id, session_id, seq, {ver} AS version, {zh} AS zh, {en} AS en, status, "
                       "t0_ms, t1_ms, updated_at FROM captions"
                       + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY room_id, session_id, seq")
                rows = [dict(r) for r in conn.execute(sql, params)]
            elif {"segments", "transcripts"} <= tables:
                schema = "ledger"
                ratio = "s.speech_ratio" if "speech_ratio" in _columns(conn, "segments") else "NULL"
                if room is not None:
                    where.append("s.room_id=?")
                    params.append(room)
                if session is not None:
                    where.append("s.session_id=?")
                    params.append(session)
                sql = ("SELECT s.room_id, s.session_id, s.seq, 1 AS version, s.status, s.t0_ms, s.t1_ms, "
                       f"{ratio} AS speech_ratio, s.created_at, t.first_at, t.text AS zh "
                       "FROM segments s LEFT JOIN ("
                       "  SELECT segment_id, MIN(created_at) AS first_at, MAX(text) AS text FROM transcripts"
                       "  WHERE text IS NOT NULL AND text != '' GROUP BY segment_id"
                       ") t ON t.segment_id = s.id"
                       + (" WHERE " + " AND ".join(where) if where else "")
                       + " ORDER BY s.room_id, s.session_id, s.seq")
                rows = [dict(r) for r in conn.execute(sql, params)]
            else:
                raise InputError("不支援的資料庫結構（需要 captions 表，或 segments＋transcripts 帳本表）")
    except sqlite3.Error as exc:
        raise InputError(f"無法唯讀開啟資料庫：{path}（{exc.__class__.__name__}: {exc}）") from exc
    for row in rows:
        sess = out.session(str(row.get("room_id") or ""), str(row.get("session_id") or ""))
        status = str(row.get("status") or "").strip().lower()
        key = _int(row.get("seq"))
        if key is None:
            sess.anon += 1
            key = ("anon", sess.anon)
        seg = Seg(key=key, source=schema, t0=_int(row.get("t0_ms")), t1=_int(row.get("t1_ms")), status=status)
        text = ""
        for k in ("zh", "en"):
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                text = v.strip()
                break
        seg.text = text
        if schema == "captions":
            at = _num(row.get("updated_at"))
            at = None if at is None else at * 1000.0
            seg.first_seen = at if seg.t1 is not None else None
            shown = at if (text and status not in NO_DISPLAY) else None
        else:
            seg.ratio = _num(row.get("speech_ratio"))
            created = _num(row.get("created_at"))
            seg.first_seen = None if created is None or seg.t1 is None else created * 1000.0
            first = _num(row.get("first_at"))
            shown = first * 1000.0 if (first is not None and text and status not in NO_DISPLAY) else None
            at = created * 1000.0 if created is not None else None
        seg.display_at = shown
        if shown is not None:
            sess.updates.append((shown, "caption"))
        if status in TERMINAL or shown is not None:
            seg.terminal_at = shown if shown is not None else at
            if seg.terminal_at is not None:
                sess.terminals.append((seg.terminal_at, key, seg.t0, seg.t1))
        for w in (at, shown):
            if w is not None:
                sess.walls.append(w)
        sess.segs[key] = seg
    out.inputs.append({"path": str(path), "kind": "db", "schema": schema, "rows": len(rows), "corrupt_lines": 0})


# --------------------------------------------------------------------------- analysis
def is_speech(seg: Seg) -> bool:
    if seg.late or seg.t0 is None or seg.t1 is None or seg.t1 <= seg.t0:
        return False
    if seg.status in NO_SPEECH or (seg.ratio is not None and seg.ratio <= 0):
        return False
    return bool(seg.text) or seg.status in FAILURES or seg.status in IN_PROGRESS


def merge(intervals: list[tuple[float, float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def subtract(intervals: list[list[float]], holes: list[tuple[float, float]]) -> list[list[float]]:
    out = intervals
    for hs, he in holes:
        nxt = []
        for s, e in out:
            if e <= hs or s >= he:
                nxt.append([s, e])
                continue
            if s < hs:
                nxt.append([s, hs])
            if e > he:
                nxt.append([he, e])
        out = nxt
    return out


def pause_windows(pauses: list[tuple[float, str]], end: float) -> list[tuple[float, float]]:
    out, start = [], None
    for at, kind in sorted(pauses):
        if kind == "pause" and start is None:
            start = at
        elif kind == "resume" and start is not None:
            out.append((start, at))
            start = None
    if start is not None:
        out.append((start, end))
    return out


def find_gaps(speech: list[list[float]], boundaries: list[tuple[float, str]]) -> list[dict]:
    """For each stretch between screen events, the time from the first speech in it to its end."""
    out, prev, j = [], -math.inf, 0
    for t, kind in sorted(boundaries):
        if t > prev:
            while j < len(speech) and speech[j][1] <= prev:
                j += 1
            if j < len(speech) and speech[j][0] < t:
                s = max(prev, speech[j][0])
                out.append({"start": s, "end": t, "gap_ms": t - s, "closed_by": kind})
            prev = t
    return out


def origin_of(sess: Session, min_lag_ms: int) -> tuple[float | None, str]:
    if sess.t1_wall:
        xs = sorted(sess.t1_wall)
        return xs[len(xs) // 2], "t1_wall_ms"
    cands = [seg.first_seen - seg.t1 for seg in sess.segs.values()
             if seg.first_seen is not None and seg.t1 is not None]
    cands += [d.at - d.t1 for d in sess.drafts if d.t1 is not None]
    cands += [at - t1 for _t0, t1, at in sess.vad]
    if not cands:
        return None, "none"
    return min(cands) - min_lag_ms, "min_lag"


def _covers(t0: int, t1: int, d0: int, d1: int) -> bool:
    """A final covers a draft when their audio overlaps (a point-like draft gets a small tolerance)."""
    if d1 <= d0:
        return t0 - OVERLAP_TOLERANCE_MS <= d0 <= t1 + OVERLAP_TOLERANCE_MS
    return min(t1, d1) - max(t0, d0) > 0


def draft_windows(sess: Session, end: float) -> tuple[list[dict], int]:
    terms = sorted(sess.terminals, key=lambda x: x[0])
    term_at = [t[0] for t in terms]
    keyed = {}
    for at, key, _t0, _t1 in terms:
        keyed.setdefault(key, []).append(at)
    groups: dict[Any, dict] = {}
    stale = 0
    for d in sorted(sess.drafts, key=lambda x: x.at):
        resolved, gid = None, None
        if d.key is not None and d.key in keyed:
            later = [a for a in keyed[d.key] if a >= d.at]
            if not later:
                stale += 1
                continue
            resolved, gid = min(later), ("k", d.key)
        else:
            i = bisect.bisect_left(term_at, d.at)
            for at, key, t0, t1 in terms[i:]:
                if d.t0 is None or d.t1 is None or t0 is None or t1 is None:
                    resolved, gid = at, ("t", at, str(key))
                    break
                if _covers(t0, t1, d.t0, d.t1):
                    resolved, gid = at, ("t", at, str(key))
                    break
            if gid is None:
                gid = ("open", d.key) if d.key is not None else ("open", bisect.bisect_right(term_at, d.at))
        g = groups.get(gid)
        if g is None:
            g = groups[gid] = {"first": d.at, "last": d.at, "resolved": resolved, "count": 0,
                               "text": d.text, "t0": d.t0, "key": d.key}
        g["count"] += 1
        g["last"] = max(g["last"], d.at)
        g["text"] = d.text
        if d.t0 is not None and (g["t0"] is None or d.t0 < g["t0"]):
            g["t0"] = d.t0
    out = []
    for g in groups.values():
        stop = g["resolved"] if g["resolved"] is not None else max(end, g["last"])
        out.append({**g, "duration_ms": stop - g["first"], "never_finalized": g["resolved"] is None})
    return sorted(out, key=lambda x: x["first"]), stale


def analyze_session(sess: Session, *, min_lag_ms: int, late_rewrite_ms: float) -> dict[str, Any]:
    origin, method = origin_of(sess, min_lag_ms)
    late = 0
    if origin is not None:
        for seg in sess.segs.values():
            if (seg.source == "captions" and seg.display_at is not None and seg.t1 is not None
                    and seg.display_at - (origin + seg.t1) > late_rewrite_ms):
                seg.late = True
                late += 1
    updates = [(t, k) for t, k in sess.updates]
    walls = list(sess.walls)
    if late:
        late_at = {seg.display_at for seg in sess.segs.values() if seg.late}
        updates = [(t, k) for t, k in updates if not (k == "caption" and t in late_at)]
        walls = [w for w in walls if w not in late_at]       # a later edit must not stretch the session
    raw: list[tuple[float, float]] = []
    if origin is not None:
        raw += [(origin + s.t0, origin + s.t1) for s in sess.segs.values() if is_speech(s)]
        raw += [(origin + d.t0, origin + d.t1) for d in sess.drafts
                if d.t0 is not None and d.t1 is not None and d.t1 > d.t0]
        raw += [(origin + a, origin + b) for a, b, _at in sess.vad]
    speech = merge(raw)
    walls += [t for t, _ in updates]
    start = min(walls) if walls else (speech[0][0] if speech else 0.0)
    end = max(walls) if walls else start
    if sess.end_at is not None:
        end = max(end, sess.end_at)
    if speech:
        end = max(end, speech[-1][1])
    pauses = pause_windows(sess.pauses, end)
    speech = subtract(speech, pauses)
    boundaries = list(updates)
    for ps, pe in pauses:
        boundaries.append((ps, "pause"))
        if pe < end:
            boundaries.append((pe, "resume"))
    boundaries.append((end, "end"))
    gaps = find_gaps(speech, boundaries)
    for g in gaps:
        g["audio_ms"] = None if origin is None else g["start"] - origin
        g["rel_ms"] = g["start"] - start
    drafts, stale = draft_windows(sess, end)
    pending = [s for s in sess.segs.values()
               if s.status in IN_PROGRESS and s.display_at is None and not s.text and not s.late
               and (s.t0 is not None or s.t1 is not None)]
    return {
        "session": sess, "origin": origin, "origin_method": method, "late": late, "start": start, "end": end,
        "speech_ms": sum(e - s for s, e in speech), "updates": updates, "gaps": gaps,
        "drafts": drafts, "stale_drafts": stale, "pending": pending,
        "has_timeline": origin is not None and bool(raw),
    }


def analyze(loaded: Loaded, *, max_gap_s: float = DEFAULT_MAX_GAP_S, max_draft_s: float = DEFAULT_MAX_DRAFT_S,
            fail_p95_s: float = DEFAULT_FAIL_P95_S, min_lag_ms: int = DEFAULT_MIN_LAG_MS,
            late_rewrite_s: float = DEFAULT_LATE_REWRITE_S) -> dict[str, Any]:
    max_gap_ms, max_draft_ms, fail_ms = max_gap_s * 1000.0, max_draft_s * 1000.0, fail_p95_s * 1000.0
    results = []
    for _, s in sorted(loaded.sessions.items()):
        if not (s.segs or s.drafts or s.vad or s.updates):
            continue                      # only start/end/other notes: nothing to judge
        s.pauses = list(s.pauses) + list(loaded.room_pauses.get(s.room_id, ()))
        results.append(analyze_session(s, min_lag_ms=min_lag_ms, late_rewrite_ms=late_rewrite_s * 1000.0))
    all_gaps, all_drafts, pending, sessions_out = [], [], [], []
    late_total = stale_total = 0
    for r in results:
        sess = r["session"]
        late_total += r["late"]
        stale_total += r["stale_drafts"]
        gvals = [g["gap_ms"] for g in r["gaps"]]
        dvals = [d["duration_ms"] for d in r["drafts"]]
        for g in r["gaps"]:
            all_gaps.append({
                "session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
                "start_at": _r(g["start"] / 1000.0), "end_at": _r(g["end"] / 1000.0),
                "rel_ms": _r(g["rel_ms"]), "audio_ms": _r(g["audio_ms"]), "gap_ms": _r(g["gap_ms"]),
                "closed_by": g["closed_by"], "open": g["closed_by"] == "end",
            })
        for d in r["drafts"]:
            all_drafts.append({
                "session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
                "key": d["key"] if isinstance(d["key"], (int, str)) or d["key"] is None else str(d["key"]),
                "first_at": _r(d["first"] / 1000.0), "rel_ms": _r(d["first"] - r["start"]),
                "audio_ms": d["t0"], "resolved_at": None if d["resolved"] is None else _r(d["resolved"] / 1000.0),
                "duration_ms": _r(d["duration_ms"]), "drafts": d["count"],
                "never_finalized": d["never_finalized"], "text": _snip(d["text"]),
            })
        for s in r["pending"]:
            pending.append({"session": sess.label, "key": s.key if isinstance(s.key, int) else str(s.key),
                            "status": s.status or "(空)", "t0_ms": s.t0, "t1_ms": s.t1})
        sessions_out.append({
            "session": sess.label, "room_id": sess.room_id, "session_id": sess.session_id,
            "origin_method": r["origin_method"], "speech_ms": _r(r["speech_ms"]),
            "duration_ms": _r(r["end"] - r["start"]),
            "updates": len(r["updates"]), "draft_updates": sum(1 for _, k in r["updates"] if k == "draft"),
            "caption_updates": sum(1 for _, k in r["updates"] if k == "caption"),
            "segments": len(sess.segs), "speech_segments": sum(1 for s in sess.segs.values() if is_speech(s)),
            "gaps": len(gvals), "gap_p50_ms": _r(nearest_rank(gvals, 0.5)),
            "gap_p95_ms": _r(nearest_rank(gvals, 0.95)), "gap_max_ms": _r(max(gvals) if gvals else None),
            "offending_gaps": sum(1 for v in gvals if v > max_gap_ms),
            "draft_windows": len(dvals), "draft_p95_ms": _r(nearest_rank(dvals, 0.95)),
            "stuck_drafts": sum(1 for v in dvals if v > max_draft_ms),
            "late_rewrites": r["late"], "stale_drafts": r["stale_drafts"], "pending": len(r["pending"]),
            "has_speech_timeline": r["has_timeline"],
        })

    gvals = [g["gap_ms"] for g in all_gaps]
    dvals = [d["duration_ms"] for d in all_drafts]
    offending = sorted((g for g in all_gaps if g["gap_ms"] > max_gap_ms), key=lambda g: -g["gap_ms"])
    stuck = sorted((d for d in all_drafts if d["duration_ms"] > max_draft_ms), key=lambda d: -d["duration_ms"])
    p95 = nearest_rank(gvals, 0.95)
    any_timeline = any(r["has_timeline"] for r in results)

    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    if p95 is not None and p95 >= fail_ms:
        if len(gvals) >= MIN_GAPS_FOR_FAIL:
            add("fail", "gap_p95", f"語音中字幕空窗 p95 {_sec(p95)} ≥ {fail_p95_s:g} 秒。")
        else:
            add("warn", "gap_p95_few", f"空窗 p95 {_sec(p95)} ≥ {fail_p95_s:g} 秒，但只有 {len(gvals)} 段樣本"
                                       f"（少於 {MIN_GAPS_FOR_FAIL}），只列警告。")
    if offending:
        add("warn", "gaps", f"有 {len(offending)} 段語音期間畫面超過 {max_gap_s:g} 秒沒有新字幕。")
    if stuck:
        never = sum(1 for d in stuck if d["never_finalized"])
        add("warn", "stuck_drafts", f"有 {len(stuck)} 組草稿超過 {max_draft_s:g} 秒未轉定稿"
                                    + (f"（其中 {never} 組到紀錄結束都沒有定稿）。" if never else "。"))
    if pending:
        add("warn", "pending", f"有 {len(pending)} 段音訊停在處理中、沒有字幕（queued／transcribing／received）。")
    if loaded.corrupt_lines:
        add("warn", "corrupt_lines", f"JSONL 有 {loaded.corrupt_lines} 行無法解析（常見於當機時最後一行被截斷）。")
    if not any_timeline:
        add("warn", "no_speech_timeline", "找不到帶 t0_ms／t1_ms 的語音活動（或無法對上牆鐘），無法判斷空窗。")

    if any(i["level"] == "fail" for i in issues):
        status, code = "fail", EXIT_FAIL
    elif issues:
        status, code = "warn", EXIT_WARN
    else:
        status, code = "ok", EXIT_OK

    assumptions = [
        "語音活動由音訊切片推得（live room 沒有獨立 VAD 紀錄）：非 silent／cancelled／paused、speech_ratio 不為 0，"
        "且有辨識文字、辨識失敗或仍在處理中的 [t0_ms, t1_ms]；草稿與 vad 行的時間也算。",
        "音訊時間軸對牆鐘：有 t1_wall_ms 用其中位數；否則取最快那段的 (寫入時間 − t1_ms)"
        + (f" 再減 {min_lag_ms} ms" if min_lag_ms else "") + "，因此每句話開頭的等待是下限估計。",
    ]
    if any(i["kind"] == "db" and i["schema"] == "captions" for i in loaded.inputs):
        assumptions.append("captions 表只存最後寫入時間（updated_at），以此當上螢幕時間（上限估計）；"
                           f"音訊結束後超過 {late_rewrite_s:g} 秒才改寫的列（多半是課後修正）不計，共 {late_total} 列。")
    if loaded.inputs and all(i["kind"] == "db" for i in loaded.inputs):
        assumptions.append("資料庫不存草稿，草稿是否卡住需改用 JSONL 事件紀錄檢查。")

    report: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": code,
        "inputs": loaded.inputs,
        "thresholds": {"max_gap_s": max_gap_s, "max_draft_s": max_draft_s, "fail_p95_s": fail_p95_s,
                       "min_lag_ms": min_lag_ms, "late_rewrite_s": late_rewrite_s},
        "sessions": sessions_out,
        "gaps": {
            "count": len(gvals), "p50_ms": _r(nearest_rank(gvals, 0.5)), "p95_ms": _r(p95),
            "max_ms": _r(max(gvals) if gvals else None), "offending_count": len(offending),
            "offending": offending[:MAX_LISTED],
        },
        "drafts": {
            "windows": len(dvals), "p50_ms": _r(nearest_rank(dvals, 0.5)), "p95_ms": _r(nearest_rank(dvals, 0.95)),
            "max_ms": _r(max(dvals) if dvals else None), "stuck_count": len(stuck),
            "never_finalized_count": sum(1 for d in all_drafts if d["never_finalized"]),
            "stale_count": stale_total, "stuck": stuck[:MAX_LISTED],
        },
        "pending": {"count": len(pending), "items": pending[:MAX_LISTED]},
        "counts": {"corrupt_lines": loaded.corrupt_lines, "untimed_events": loaded.untimed_events,
                   "other_events": loaded.other_events, "late_rewrites": late_total},
        "assumptions": assumptions,
        "issues": issues,
    }
    report["summary_zh"] = summary_zh(report)
    return report


# --------------------------------------------------------------------------- output
_STATUS_ZH = {"ok": "通過", "warn": "警告", "fail": "不通過"}
_CLOSED_ZH = {"caption": "定稿／字幕更新", "draft": "草稿", "pause": "暫停", "end": "紀錄結束", "resume": "恢復"}


def _where(item: dict) -> str:
    if item.get("audio_ms") is not None:
        return f"錄音 {fmt_ms(item['audio_ms'])}"
    return f"開場後 {fmt_ms(item.get('rel_ms'))}"


def summary_zh(r: dict[str, Any]) -> str:
    g, d = r["gaps"], r["drafts"]
    srcs = []
    for i in r["inputs"]:
        if i["kind"] == "db":
            srcs.append(f"{i['path']}（{'captions 表' if i['schema'] == 'captions' else '帳本 segments'}，{i['rows']} 列）")
        else:
            srcs.append(f"{i['path']}（JSONL {i['lines']} 行"
                        + (f"、壞行 {i['corrupt_lines']}" if i["corrupt_lines"] else "") + "）")
    speech = sum(s["speech_ms"] or 0 for s in r["sessions"])
    drafts_n = sum(s["draft_updates"] for s in r["sessions"])
    caps_n = sum(s["caption_updates"] for s in r["sessions"])
    th = r["thresholds"]
    lines = [
        f"字幕空窗檢查：{_STATUS_ZH[r['status']]}（exit {r['exit_code']}）",
        "- 輸入：" + "；".join(srcs),
        f"- 場次 {len(r['sessions'])} 個；語音活動合計 {_sec(speech)}；畫面更新 {drafts_n + caps_n} 次"
        f"（草稿 {drafts_n}、字幕 {caps_n}）",
        f"- 語音中無新字幕的空窗：{g['count']} 段；p50 {_sec(g['p50_ms'])}、p95 {_sec(g['p95_ms'])}、"
        f"最長 {_sec(g['max_ms'])}；超過 {th['max_gap_s']:g} 秒 {g['offending_count']} 段",
        f"- 草稿→定稿：{d['windows']} 組；p50 {_sec(d['p50_ms'])}、p95 {_sec(d['p95_ms'])}、最長 {_sec(d['max_ms'])}；"
        f"超過 {th['max_draft_s']:g} 秒 {d['stuck_count']} 組（從未定稿 {d['never_finalized_count']} 組）",
    ]
    if r["pending"]["count"]:
        lines.append(f"- 處理中未出字幕：{r['pending']['count']} 段")
    if r["issues"]:
        lines.append("問題：")
        for i in r["issues"]:
            lines.append(f"  [{'失敗' if i['level'] == 'fail' else '警告'}] {i['message']}")
    if g["offending"]:
        lines.append("空窗清單（最長在前）：")
        for w in g["offending"][:10]:
            lines.append(f"  {w['session']} {_where(w)} 起 {_sec(w['gap_ms'])} 沒有新字幕"
                         f"（結束於：{_CLOSED_ZH.get(w['closed_by'], w['closed_by'])}）")
    if d["stuck"]:
        lines.append("卡住的草稿：")
        for w in d["stuck"][:10]:
            tail = "到紀錄結束都沒有定稿" if w["never_finalized"] else "才轉定稿"
            lines.append(f"  {w['session']} {_where(w)} 草稿「{w['text']}」{_sec(w['duration_ms'])}{tail}")
    lines.append("假設：")
    lines.extend(f"  - {a}" for a in r["assumptions"])
    if r["status"] == "ok":
        lines.append("語音期間字幕持續更新，沒有超過門檻的空窗或卡住的草稿。")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="caption_gap_detector.py",
                 description="唯讀找出「有人在講話但畫面超過 N 秒沒有新字幕」的空窗，以及長時間未轉定稿的草稿。")
    ap.add_argument("--db", type=Path, help="字幕資料庫（captions.sqlite3 或 zen.sqlite3；以 mode=ro 開啟）")
    ap.add_argument("--jsonl", type=Path, action="append", default=[],
                    help="事件紀錄 JSONL（錄音 live.jsonl 或 live room 事件；可重複）")
    ap.add_argument("--room", help="只看這個 room_id")
    ap.add_argument("--session", help="只看這個 session_id")
    ap.add_argument("--max-gap-s", type=float, default=DEFAULT_MAX_GAP_S,
                    help=f"語音中畫面多久沒有新字幕算空窗（秒，預設 {DEFAULT_MAX_GAP_S:g}）")
    ap.add_argument("--max-draft-s", type=float, default=DEFAULT_MAX_DRAFT_S,
                    help=f"草稿停留多久未轉定稿算卡住（秒，預設 {DEFAULT_MAX_DRAFT_S:g}）")
    ap.add_argument("--fail-p95-s", type=float, default=DEFAULT_FAIL_P95_S,
                    help=f"空窗 p95 達此值即不通過（秒，預設 {DEFAULT_FAIL_P95_S:g}）")
    ap.add_argument("--min-lag-ms", type=int, default=DEFAULT_MIN_LAG_MS,
                    help="沒有 t1_wall_ms 時，假設最快那段字幕至少落後音訊多少毫秒（預設 0）")
    ap.add_argument("--late-rewrite-s", type=float, default=DEFAULT_LATE_REWRITE_S,
                    help=f"captions 表中音訊結束後多久才改寫的列視為課後修正、不計（秒，預設 {DEFAULT_LATE_REWRITE_S:g}）")
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
    if args.db is None and not args.jsonl:
        ap.error("請給 --db 或 --jsonl（至少一個）")
    for name in ("max_gap_s", "max_draft_s", "fail_p95_s", "late_rewrite_s"):
        v = getattr(args, name)
        if not (math.isfinite(v) and v > 0):
            ap.error(f"--{name.replace('_', '-')} 必須是正數")
    if args.max_gap_s > args.fail_p95_s:
        ap.error("--max-gap-s 不能大於 --fail-p95-s")
    if args.min_lag_ms < 0:
        ap.error("--min-lag-ms 不能是負數")

    if args.json is not None and (args.json.exists() or args.json.is_symlink()):
        sys.stderr.write(f"拒絕覆蓋既有檔案：{args.json}（--json 只寫新檔）\n")
        return EXIT_OUTPUT

    loaded = Loaded()
    try:
        if args.db is not None:
            read_db(args.db, loaded, args.room, args.session)
        for p in args.jsonl:
            parse_jsonl(p, loaded, args.room, args.session)
        if not any(s.updates or s.segs or s.drafts or s.vad for s in loaded.sessions.values()):
            raise InputError("沒有任何帶牆鐘時間的字幕／草稿事件可分析"
                             + (f"（{loaded.untimed_events} 筆事件缺 at／ts 時間）" if loaded.untimed_events else "")
                             + "。")
    except InputError as exc:
        sys.stderr.write(f"輸入錯誤：{exc}\n")
        return EXIT_INPUT

    report = analyze(loaded, max_gap_s=args.max_gap_s, max_draft_s=args.max_draft_s,
                     fail_p95_s=args.fail_p95_s, min_lag_ms=args.min_lag_ms, late_rewrite_s=args.late_rewrite_s)

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
