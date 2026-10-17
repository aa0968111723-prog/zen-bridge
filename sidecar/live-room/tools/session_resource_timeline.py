#!/usr/bin/env python3
"""Offline resource-pressure vs caption-latency timeline (queue item 39).

  python tools/session_resource_timeline.py --csv perf.csv --captions live.jsonl
  python tools/session_resource_timeline.py --csv perf.csv --captions live.jsonl --json OUT.json
  python tools/session_resource_timeline.py --csv cpu.csv --csv ram.csv --captions a.jsonl \\
      --ram-low-mb 1024 --cpu-high 90 --process python --utc-offset +08:00

Answers one question about a recorded session on the club laptop: *when the
machine ran short of RAM (available < 1 GB) or the CPU was pegged (> 90 %),
did the caption latency go up at the same time?*  Read-only, stdlib only, no
network, no app imports; inputs are never modified.

Resource input (``--csv``, repeatable):

* Windows ``typeperf`` / Performance Monitor CSV, e.g.
  ``typeperf "\\Processor(_Total)\\% Processor Time" "\\Memory\\Available MBytes"
  "\\Process(python)\\Working Set" -si 1 -o perf.csv``.  The first header cell
  looks like ``(PDH-CSV 4.0) (Taipei Standard Time)(-480)``; the bias (minutes,
  UTC = local + bias) gives the UTC offset of the naive ``MM/DD/YYYY
  HH:MM:SS.mmm`` timestamps.  English, Traditional Chinese and Japanese counter
  names are recognised (``% Processor Time`` / ``% 處理器時間`` / ``% プロセッサ
  時間``; ``Available MBytes|KBytes|Bytes`` / ``可用 MB`` / ``可用位元組``;
  ``Working Set`` / ``工作集`` / ``ワーキング セット``).  Blank cells (``" "``,
  typical for the first sample) are skipped.  UTF-8, UTF-8 with BOM, UTF-16
  (with or without BOM) and cp950 files are accepted; typeperf status lines
  (``Exiting, please wait...``) are ignored.
* A simple custom CSV with a ``timestamp`` column (ISO 8601, ``MM/DD/YYYY
  HH:MM:SS``, or epoch seconds / milliseconds) and any of ``cpu_pct``,
  ``available_ram_mb`` (``_kb`` / ``_gb`` / ``_bytes`` suffixes also work) and
  ``working_set_mb`` (likewise; ``rss_mb`` is accepted too).

CPU: the ``_Total`` instance wins; otherwise per-core columns are averaged.
``Process(...)`` CPU columns are ignored.  Working set: all matching process
columns are summed (``--process NAME`` keeps only instances starting with NAME,
so ``python`` also matches ``python#1``).  Working set is reported but is not a
pressure signal.

Caption input (``--captions``, repeatable): UTF-8 JSON Lines.  Each object may
carry a wall clock (``at_ms``/``ts_ms``/``wall_ms``/``t_wall_ms``/``time_ms``
in ms, or ``at``/``ts``/``time``/``timestamp``/``updated_at``/``created_at`` as
epoch seconds or an ISO string) and a latency: ``draft_latency_ms`` /
``final_latency_ms`` (draft / final), or a generic ``latency_ms`` /
``end_to_end_ms`` / ``e2e_ms`` / ``delay_ms`` / ``lag_ms`` (``*_s`` in seconds)
whose kind comes from ``kind`` / ``stage`` / ``status`` / ``type``
(``draft``/``partial`` vs ``final``/``caption``/``update``).  Without an
explicit latency, ``wall − t1_wall_ms`` or ``first_token_ms −
speech_start_ms`` (draft) is used.  Lines missing a clock or a latency are
counted and skipped (not an error); undecodable JSON lines are a warning.

Naive times (typeperf, custom CSV, ISO without offset) use the CSV header bias,
else ``--utc-offset``, else +08:00 (Asia/Taipei, noted in the report).  Epoch
numbers and ISO strings with an offset are absolute.

Alignment: each caption (at its wall time minus ``--lag-s``) takes the nearest
CPU / RAM sample within ``--max-skew-s`` (default 1.5 × that series' median
sample interval, at least 1 s).  Captions with no sample nearby are
"uncovered" and left out.  A caption is *under pressure* when that RAM sample
is below ``--ram-low-mb`` (1024) or that CPU sample is above ``--cpu-high``
(90).  For each kind (draft / final / other) and reason (any / ram / cpu) the
median and p95 (nearest-rank) latency inside vs outside pressure are compared;
latency *rose* when both sides have ≥ ``--min-samples`` (5) captions, the
inside median ≥ ``--rise-ratio`` (1.25) × the outside median and is at least
``--rise-min-ms`` (300) higher.  Pressure windows (contiguous flagged samples)
are listed with their captions; Pearson r of latency vs CPU / RAM is shown for
information only.

==============  ==============================================================  ====
status          condition                                                       exit
==============  ==============================================================  ====
``ok``          no pressure window, or pressure without a latency rise and no   0
                warnings
``correlated``  latency rose inside pressure for some kind / reason (wins over  1
                warnings, which are still listed)
``warn``        insufficient data (too few captions inside or outside           2
                pressure), CPU or RAM series missing, < 50 % of captions
                covered by the resource timeline, bad CSV rows or JSONL lines
(input)         missing / unreadable / undecodable input, no timestamp or no    3
                CPU / RAM column, no usable resource samples or caption
                latencies, invalid options (argparse errors use 3, not 2)
(output)        ``--json`` path already exists (never overwritten) or           4
                unwritable
==============  ==============================================================  ====

Human output is Traditional Chinese.  ``--json OUT`` writes one JSON object to
a *new* file only (exclusive create).  No clock, locale or local time zone is
consulted, so the same inputs always give the same report.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TOOL = "session_resource_timeline"
SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_CORRELATED = 1
EXIT_WARN = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

EXIT_CODES = {"ok": EXIT_OK, "correlated": EXIT_CORRELATED, "warn": EXIT_WARN}
STATUS_ZH = {"ok": "正常", "correlated": "資源壓力時字幕延遲同步上升", "warn": "需留意／資料不足"}

DEFAULT_RAM_LOW_MB = 1024.0
DEFAULT_CPU_HIGH = 90.0
DEFAULT_MIN_SAMPLES = 5
DEFAULT_RISE_RATIO = 1.25
DEFAULT_RISE_MIN_MS = 300.0
DEFAULT_OFFSET_MIN = 480  # Asia/Taipei
MIN_COVERAGE = 0.5
MAX_LATENCY_MS = 600_000.0  # > 10 min is not a caption latency
MIN_EPOCH_S = 1e8  # anything smaller is a relative clock, not a wall clock

KINDS = ("draft", "final", "other")
REASONS = ("any", "ram", "cpu")
KIND_ZH = {"draft": "草稿", "final": "定稿", "other": "未分類", "all": "全部"}
REASON_ZH = {"any": "任一壓力", "ram": "RAM 不足", "cpu": "CPU 滿載"}

_PDH = re.compile(r"\(PDH-CSV\s*[\d.]+\)\s*(?:\(([^)]*)\))?\s*(?:\((-?\d+)\))?", re.I)
_COUNTER = re.compile(r"^\\\\[^\\]*\\([^\\]+?)(?:\(([^)]*)\))?\\(.+)$")
_OFFSET = re.compile(r"^(?:UTC)?([+-])(\d{1,2}):?(\d{2})?$", re.I)

CPU_HINTS = ("% processor time", "processor time", "% processor utility", "processor utility",
             "處理器時間", "處理器使用率", "プロセッサ時間", "プロセッサ ユーティリティ", "プロセッサ使用率")
CPU_CUSTOM = ("cpu",)
RAM_HINTS = ("available", "avail", "可用", "利用可能", "使用可能", "free_ram", "free_mem", "free ram")
WS_HINTS = ("working set", "working_set", "workingset", "工作集", "ワーキング セット", "ワーキングセット",
            "rss", "ws_mb", "ws_kb", "ws_gb", "ws_bytes")
PROCESS_OBJECTS = ("process", "處理程序", "程序", "プロセス", "进程")
TIMESTAMP_NAMES = ("timestamp", "time", "ts", "datetime", "date", "時間", "時間戳記", "時刻", "at")
TYPEPERF_MESSAGES = ("exiting", "the command completed", "正在結束", "命令已順利完成", "終了しています",
                     "コマンドは正常に完了")

MS_TS_KEYS = ("at_ms", "ts_ms", "wall_ms", "t_wall_ms", "time_ms", "timestamp_ms", "updated_at_ms",
              "shown_at_ms", "emitted_at_ms")
S_TS_KEYS = ("at", "ts", "time", "timestamp", "updated_at", "wall", "shown_at", "emitted_at", "created_at")
DRAFT_KEYS = ("draft_latency_ms", "draft_lag_ms", "draft_delay_ms", "first_token_latency_ms")
DRAFT_KEYS_S = ("draft_latency_s", "draft_lag_s", "draft_delay_s")
FINAL_KEYS = ("final_latency_ms", "final_lag_ms", "final_delay_ms")
FINAL_KEYS_S = ("final_latency_s", "final_lag_s", "final_delay_s")
GENERIC_KEYS = ("latency_ms", "end_to_end_ms", "e2e_ms", "caption_latency_ms", "delay_ms", "lag_ms")
GENERIC_KEYS_S = ("latency_s", "end_to_end_s", "e2e_s", "delay_s", "lag_s")
KIND_FIELDS = ("kind", "stage", "status", "type")
DRAFT_WORDS = {"draft", "partial", "interim", "streaming", "草稿"}
FINAL_WORDS = {"final", "caption", "update", "done", "finalized", "committed", "定稿"}

ADVICE_RAM_ZH = [
    "關閉瀏覽器多餘分頁、OBS、雲端同步等背景程式，讓可用 RAM 保持在 1 GB 以上。",
    "改用較小的量化模型（例如 Q4）或較小的 ASR 模型，降低工作集。",
    "上課前先用 tools/ram_headroom_guard.py 確認可用記憶體是否足夠。",
]
ADVICE_CPU_ZH = [
    "改用「高效能」電源計畫並插電；電池／省電模式下 CPU 容易滿載又降頻。",
    "降低 ASR 執行緒數或改用較快的模型，避免 CPU 長時間超過 90 %。",
    "可用 tools/cpu_throttle_report.py 檢查同一份 typeperf CSV 是否同時降頻。",
]


class InputError(Exception):
    """Unreadable / invalid input (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means "warn" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 參數錯誤：{message}\n")
        raise SystemExit(EXIT_INPUT)


# --------------------------------------------------------------------------- small helpers
def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..1); None for empty input."""
    ordered = sorted(values)
    if not ordered:
        return None
    k = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return float(ordered[k])


def _round(value: float | None, nd: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), nd)


def parse_offset(text: str) -> int:
    """'+08:00' / '-0530' / 'UTC+8' → minutes east of UTC."""
    m = _OFFSET.match((text or "").strip())
    if not m:
        raise ValueError(f"無法解析 UTC 位移：{text!r}（例：+08:00）")
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes >= 60:
        raise ValueError(f"UTC 位移超出範圍：{text!r}")
    total = hours * 60 + minutes
    return -total if m.group(1) == "-" else total


def _tz(offset_min: int) -> timezone:
    return timezone(timedelta(minutes=offset_min))


def fmt_offset(offset_min: int) -> str:
    sign = "-" if offset_min < 0 else "+"
    a = abs(offset_min)
    return f"{sign}{a // 60:02d}:{a % 60:02d}"


def fmt_time(t: float | None, offset_min: int) -> str | None:
    if t is None:
        return None
    dt = datetime.fromtimestamp(t, _tz(offset_min))
    spec = "milliseconds" if dt.microsecond else "seconds"
    return dt.isoformat(timespec=spec)


_TS_FORMATS = (
    "%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S",
)


def _epoch_from_number(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    if value > 1e11:  # milliseconds
        value /= 1000.0
    return value if value >= MIN_EPOCH_S else None


def parse_time(raw: Any, offset_min: int) -> float | None:
    """Timestamp cell / JSON value → epoch seconds (naive text uses offset_min)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return _epoch_from_number(float(raw))
    text = str(raw).strip().strip('"').strip()
    if not text:
        return None
    try:
        return _epoch_from_number(float(text))
    except ValueError:
        pass
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(offset_min))
    return dt.timestamp()


def parse_number(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        text = str(raw).strip().strip('"').strip().replace(",", "")
        if not text or text in {".", "-", "--", "N/A", "n/a"}:
            return None
        try:
            v = float(text)
        except ValueError:
            return None
    return v if math.isfinite(v) else None


def decode_bytes(data: bytes, path: Path, *, allow_ansi: bool) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise InputError(f"UTF-16 解碼失敗：{path}（{exc}）") from None
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise InputError(f"UTF-8 解碼失敗：{path}（{exc}）") from None
    sample = data[:4096]
    if len(sample) >= 4 and sample.count(b"\x00") >= len(sample) // 4:
        le = sample[1::2].count(b"\x00") >= sample[0::2].count(b"\x00")
        try:
            return data.decode("utf-16-le" if le else "utf-16-be")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if allow_ansi:
        try:
            return data.decode("cp950")
        except UnicodeDecodeError:
            pass
    raise InputError(f"無法解碼（請存成 UTF-8／UTF-16）：{path}")


def _read(path: Path, *, allow_ansi: bool) -> str:
    try:
        if not path.is_file():
            raise InputError(f"找不到檔案：{path}")
        data = path.read_bytes()
    except OSError as exc:
        raise InputError(f"無法讀取：{path}（{exc}）") from None
    if not data.strip():
        raise InputError(f"檔案是空的：{path}")
    return decode_bytes(data, path, allow_ansi=allow_ansi)


# --------------------------------------------------------------------------- resource CSV
@dataclass
class Column:
    index: int
    header: str
    metric: str            # cpu | ram | ws
    scale: float           # → percent (cpu) or MB (ram / ws)
    instance: str | None
    is_total: bool = False


@dataclass
class CsvResult:
    path: str
    fmt: str
    pdh_timezone: str | None
    offset_min: int
    offset_source: str
    columns: dict[str, list[str]]
    rows: int = 0
    bad_rows: int = 0
    blank_cells: int = 0
    message_lines: int = 0
    series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _split_counter(header: str) -> tuple[str | None, str | None, str]:
    m = _COUNTER.match(header.strip())
    if not m:
        return None, None, header.strip()
    return m.group(1).strip(), (m.group(2) or None), m.group(3).strip()


def _unit_scale(text: str, default: float) -> float:
    """Scale factor to MB from the unit words in a header."""
    n = _norm(text)
    tokens = set(re.split(r"[^a-z0-9]+", n))
    if "mbytes" in n or "mb" in tokens or "mib" in tokens or n.endswith("mb") or "mb)" in n:
        return 1.0
    if "kbytes" in n or "kb" in tokens or "kib" in tokens or n.endswith("kb"):
        return 1.0 / 1024
    if "gbytes" in n or "gb" in tokens or "gib" in tokens or n.endswith("gb"):
        return 1024.0
    if "bytes" in n or "byte" in tokens or "位元組" in n or "バイト" in n:
        return 1.0 / (1024 * 1024)
    return default


def classify_column(index: int, header: str, processes: tuple[str, ...]) -> Column | None:
    obj, inst, counter = _split_counter(header)
    pdh = obj is not None
    n_obj = _norm(obj or "")
    n_counter = _norm(counter)
    inst_n = _norm(inst or "")
    is_process = pdh and n_obj in PROCESS_OBJECTS  # exact: "Processor" is not "Process"

    if any(h in n_counter for h in WS_HINTS) and not any(h in n_counter for h in ("peak", "峰值", "ピーク")):
        if is_process and inst_n in {"_total", "idle"}:
            return None
        if processes and pdh and not any(inst_n.startswith(p) for p in processes):
            return None  # --process only filters typeperf instances; custom columns are kept
        scale = _unit_scale(counter, 1.0 / (1024 * 1024) if pdh else 1.0)
        return Column(index, header, "ws", scale, inst)
    if is_process:
        return None
    if any(h in n_counter for h in RAM_HINTS):
        scale = _unit_scale(counter, 1.0 / (1024 * 1024) if pdh else 1.0)
        return Column(index, header, "ram", scale, inst)
    if any(h in n_counter for h in CPU_HINTS) or (
            not pdh and any(n_counter.startswith(h) or f"_{h}" in n_counter or f" {h}" in n_counter
                            for h in CPU_CUSTOM)):
        return Column(index, header, "cpu", 1.0, inst, is_total=inst_n.endswith("_total"))
    return None


def _timestamp_col(header: list[str]) -> int | None:
    if header and (_PDH.search(header[0]) or _norm(header[0]).startswith("(")):
        return 0
    for i, h in enumerate(header):
        if _norm(h).strip('"') in TIMESTAMP_NAMES:
            return i
    for i, h in enumerate(header):
        n = _norm(h)
        if "timestamp" in n or "時間" in n or n.startswith("time"):
            return i
    return None


def read_resource_csv(path: Path, *, offset_override: int | None, processes: tuple[str, ...]) -> CsvResult:
    text = _read(path, allow_ansi=True)
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)
    if not rows:
        raise InputError(f"CSV 沒有標題列：{path}")
    header = [c.strip() for c in rows[0]]
    pdh = _PDH.search(header[0]) if header else None
    pdh_tz = None
    if offset_override is not None:
        offset, source = offset_override, "--utc-offset"
    elif pdh and pdh.group(2) is not None:
        offset, source = -int(pdh.group(2)), "pdh_header"
    else:
        offset, source = DEFAULT_OFFSET_MIN, "default"
    if pdh:
        pdh_tz = (pdh.group(1) or "").strip() or None
    ts_idx = _timestamp_col(header)
    if ts_idx is None:
        raise InputError(f"CSV 找不到時間欄（typeperf 的 (PDH-CSV 4.0) 或 timestamp）：{path}")

    cols: list[Column] = []
    for i, h in enumerate(header):
        if i == ts_idx:
            continue
        c = classify_column(i, h, processes)
        if c is not None:
            cols.append(c)
    cpu_cols = [c for c in cols if c.metric == "cpu"]
    totals = [c for c in cpu_cols if c.is_total]
    if totals:
        cpu_cols = totals[:1]
    elif any(c.instance for c in cpu_cols):
        cpu_cols = [c for c in cpu_cols if c.instance] or cpu_cols
    else:
        cpu_cols = cpu_cols[:1]
    ram_cols = [c for c in cols if c.metric == "ram"][:1]
    ws_cols = [c for c in cols if c.metric == "ws"]
    if not cpu_cols and not ram_cols:
        raise InputError(f"CSV 找不到 CPU％或可用 RAM 欄位：{path}")

    res = CsvResult(path=str(path), fmt="typeperf" if pdh else "custom", pdh_timezone=pdh_tz,
                    offset_min=offset, offset_source=source,
                    columns={"cpu": [c.header for c in cpu_cols], "ram": [c.header for c in ram_cols],
                             "working_set": [c.header for c in ws_cols]},
                    series={"cpu": [], "ram": [], "ws": []})
    for row in rows[1:]:
        cells = [c for c in row]
        if not any(c.strip() for c in cells):
            continue
        non_empty = [c for c in cells if c.strip()]
        t = parse_time(cells[ts_idx], offset) if ts_idx < len(cells) else None
        if t is None:
            joined = _norm(" ".join(non_empty))
            if len(non_empty) == 1 or any(joined.startswith(m) for m in TYPEPERF_MESSAGES):
                res.message_lines += 1
            else:
                res.bad_rows += 1
            continue
        res.rows += 1

        def values(group: list[Column]) -> list[float]:
            out = []
            for c in group:
                v = parse_number(cells[c.index]) if c.index < len(cells) else None
                if v is None:
                    res.blank_cells += 1
                    continue
                out.append(v * c.scale)
            return out

        cpu_v = values(cpu_cols)
        if cpu_v:
            res.series["cpu"].append((t, sum(cpu_v) / len(cpu_v)))
        ram_v = values(ram_cols)
        if ram_v:
            res.series["ram"].append((t, ram_v[0]))
        ws_v = values(ws_cols)
        if ws_v and len(ws_v) == len(ws_cols):
            res.series["ws"].append((t, sum(ws_v)))
    if not res.series["cpu"] and not res.series["ram"]:
        raise InputError(f"CSV 沒有任何含時間的 CPU／RAM 數值列：{path}")
    return res


# --------------------------------------------------------------------------- caption JSONL
@dataclass
class CaptionSample:
    t: float
    kind: str
    latency_ms: float
    source: str
    line: int
    file: str


@dataclass
class CaptionFileResult:
    path: str
    lines: int = 0
    events: int = 0
    samples: int = 0
    bad_lines: int = 0
    no_timestamp: int = 0
    no_latency: int = 0
    invalid_latency: int = 0


def _kind_of(obj: dict) -> str:
    for key in KIND_FIELDS:
        v = obj.get(key)
        if isinstance(v, str):
            w = v.strip().lower()
            if w in DRAFT_WORDS:
                return "draft"
            if w in FINAL_WORDS:
                return "final"
    return "other"


def _event_time(obj: dict, offset_min: int) -> float | None:
    for key in MS_TS_KEYS:
        v = parse_number(obj.get(key))
        if v is not None:
            t = _epoch_from_number(v / 1000.0)
            if t is not None:
                return t
    for key in S_TS_KEYS:
        if key in obj:
            t = parse_time(obj.get(key), offset_min)
            if t is not None:
                return t
    return None


def _latencies(obj: dict, t: float) -> list[tuple[str, float | None, str]]:
    out: list[tuple[str, float | None, str]] = []

    def first(keys: tuple[str, ...], mult: float) -> tuple[float | None, str] | None:
        for k in keys:
            if k in obj and obj[k] is not None:
                v = parse_number(obj[k])
                return (None if v is None else v * mult), k
        return None

    for kind, ms_keys, s_keys in (("draft", DRAFT_KEYS, DRAFT_KEYS_S), ("final", FINAL_KEYS, FINAL_KEYS_S)):
        hit = first(ms_keys, 1.0) or first(s_keys, 1000.0)
        if hit:
            out.append((kind, hit[0], hit[1]))
    if out:
        return out
    hit = first(GENERIC_KEYS, 1.0) or first(GENERIC_KEYS_S, 1000.0)
    if hit:
        return [(_kind_of(obj), hit[0], hit[1])]
    t1 = parse_number(obj.get("t1_wall_ms"))
    if t1 is not None and t1 > MIN_EPOCH_S * 1000:
        return [(_kind_of(obj), t * 1000.0 - t1, "wall-t1_wall_ms")]
    s0, ft = parse_number(obj.get("speech_start_ms")), parse_number(obj.get("first_token_ms"))
    if s0 is not None and ft is not None:
        return [("draft", ft - s0, "first_token_ms-speech_start_ms")]
    return []


def read_captions(path: Path, offset_min: int) -> tuple[CaptionFileResult, list[CaptionSample]]:
    text = _read(path, allow_ansi=False)
    res = CaptionFileResult(path=str(path))
    samples: list[CaptionSample] = []
    for lineno, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        res.lines += 1
        try:
            obj = json.loads(line)
        except ValueError:
            res.bad_lines += 1
            continue
        if not isinstance(obj, dict):
            res.bad_lines += 1
            continue
        res.events += 1
        t = _event_time(obj, offset_min)
        if t is None:
            res.no_timestamp += 1
            continue
        lats = _latencies(obj, t)
        if not lats:
            res.no_latency += 1
            continue
        for kind, ms, src in lats:
            if ms is None or ms < 0 or ms > MAX_LATENCY_MS:
                res.invalid_latency += 1
                continue
            samples.append(CaptionSample(t, kind, float(ms), src, lineno, str(path)))
            res.samples += 1
    return res, samples


# --------------------------------------------------------------------------- analysis
@dataclass
class Series:
    name: str
    points: list[tuple[float, float]]
    interval_s: float
    skew_s: float
    times: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.points.sort()
        self.times = [p[0] for p in self.points]

    def nearest(self, t: float) -> float | None:
        if not self.points:
            return None
        i = bisect.bisect_left(self.times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.points):
                d = abs(self.times[j] - t)
                if best is None or d < best[0]:
                    best = (d, self.points[j][1])
        if best is None or best[0] > self.skew_s:
            return None
        return best[1]


def median_interval(times: list[float]) -> float | None:
    gaps = [b - a for a, b in zip(times, times[1:]) if b - a > 0]
    return float(statistics.median(gaps)) if gaps else None


def make_series(name: str, points: list[tuple[float, float]], max_skew: float | None) -> Series:
    pts = sorted(points)
    iv = median_interval([p[0] for p in pts]) or 1.0
    skew = max_skew if max_skew is not None else max(1.0, 1.5 * iv)
    return Series(name, pts, iv, skew)


def series_stats(s: Series | None, offset_min: int) -> dict | None:
    if s is None or not s.points:
        return None
    vals = [v for _, v in s.points]
    out = {"n": len(vals), "interval_s": _round(s.interval_s, 3), "max_skew_s": _round(s.skew_s, 3),
           "start": fmt_time(s.times[0], offset_min), "end": fmt_time(s.times[-1], offset_min),
           "min": _round(min(vals)), "median": _round(statistics.median(vals)), "max": _round(max(vals))}
    if s.name == "ws":
        out["first"] = _round(vals[0])
        out["last"] = _round(vals[-1])
        out["growth"] = _round(vals[-1] - vals[0])
        peak_i = max(range(len(vals)), key=lambda i: (vals[i], -i))
        out["peak_at"] = fmt_time(s.times[peak_i], offset_min)
    return out


def find_windows(s: Series, flag, reason: str) -> list[dict]:
    windows: list[dict] = []
    run: list[tuple[float, float]] = []
    half = s.interval_s / 2.0
    gap_limit = max(2.0 * s.interval_s, s.skew_s)

    def flush() -> None:
        if run:
            vals = [v for _, v in run]
            windows.append({"reason": reason, "start_t": run[0][0] - half, "end_t": run[-1][0] + half,
                            "samples": len(run),
                            "peak": max(vals) if reason == "cpu" else min(vals)})
            run.clear()

    prev_t = None
    for t, v in s.points:
        if flag(v):
            if run and prev_t is not None and t - prev_t > gap_limit:
                flush()
            run.append((t, v))
        else:
            flush()
        prev_t = t
    flush()
    return windows


def merge_windows(windows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for w in sorted(windows, key=lambda w: (w["start_t"], w["end_t"])):
        if merged and w["start_t"] <= merged[-1]["end_t"]:
            m = merged[-1]
            m["end_t"] = max(m["end_t"], w["end_t"])
            m["reasons"] = sorted(set(m["reasons"]) | {w["reason"]})
            if w["reason"] == "cpu":
                m["cpu_max"] = w["peak"] if m.get("cpu_max") is None else max(m["cpu_max"], w["peak"])
            else:
                m["ram_min_mb"] = w["peak"] if m.get("ram_min_mb") is None else min(m["ram_min_mb"], w["peak"])
        else:
            merged.append({"start_t": w["start_t"], "end_t": w["end_t"], "reasons": [w["reason"]],
                           "cpu_max": w["peak"] if w["reason"] == "cpu" else None,
                           "ram_min_mb": w["peak"] if w["reason"] == "ram" else None})
    return merged


def _group_stats(values: list[float]) -> dict:
    return {"n": len(values),
            "median_ms": _round(statistics.median(values)) if values else None,
            "p95_ms": _round(percentile(values, 0.95)),
            "max_ms": _round(max(values)) if values else None}


def compare(inside: list[float], outside: list[float], *, min_samples: int, ratio: float,
            rise_min_ms: float) -> dict:
    a, b = _group_stats(inside), _group_stats(outside)
    sufficient = len(inside) >= min_samples and len(outside) >= min_samples
    med_in = statistics.median(inside) if inside else None
    med_out = statistics.median(outside) if outside else None
    p95_in, p95_out = percentile(inside, 0.95), percentile(outside, 0.95)
    med_ratio = (med_in / med_out) if (med_in is not None and med_out) else None
    diff = (med_in - med_out) if (med_in is not None and med_out is not None) else None
    rises = bool(sufficient and med_in is not None and med_out is not None
                 and med_in >= ratio * med_out and diff is not None and diff >= rise_min_ms)
    return {"inside": a, "outside": b, "sufficient": sufficient,
            "median_ratio": _round(med_ratio, 2), "median_diff_ms": _round(diff),
            "p95_ratio": _round(p95_in / p95_out, 2) if (p95_in is not None and p95_out) else None,
            "rises": rises}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return round(statistics.correlation(xs, ys), 3)
    except (statistics.StatisticsError, ZeroDivisionError, ValueError):
        return None


@dataclass
class Settings:
    ram_low_mb: float = DEFAULT_RAM_LOW_MB
    cpu_high: float = DEFAULT_CPU_HIGH
    lag_s: float = 0.0
    max_skew_s: float | None = None
    min_samples: int = DEFAULT_MIN_SAMPLES
    rise_ratio: float = DEFAULT_RISE_RATIO
    rise_min_ms: float = DEFAULT_RISE_MIN_MS


def analyze(csvs: list[CsvResult], cap_files: list[CaptionFileResult], samples: list[CaptionSample],
            settings: Settings, *, display_offset: int, offset_source: str) -> dict:
    pts: dict[str, list[tuple[float, float]]] = {"cpu": [], "ram": [], "ws": []}
    for c in csvs:
        for k in pts:
            pts[k].extend(c.series.get(k, []))
    series = {k: make_series(k, v, settings.max_skew_s) for k, v in pts.items() if v}
    cpu, ram, ws = series.get("cpu"), series.get("ram"), series.get("ws")

    warnings: list[dict] = []
    notes: list[str] = []

    def warn(code: str, msg: str) -> None:
        warnings.append({"code": code, "message_zh": msg})

    if offset_source == "default":
        notes.append(f"CSV 標題沒有時區偏移，無時區的時間一律視為 UTC{fmt_offset(display_offset)}（台北）；"
                     "如不對請加 --utc-offset。")
    bad_rows = sum(c.bad_rows for c in csvs)
    if bad_rows:
        warn("bad_csv_rows", f"CSV 有 {bad_rows} 列時間無法解析，已略過。")
    bad_lines = sum(f.bad_lines for f in cap_files)
    if bad_lines:
        warn("bad_jsonl_lines", f"字幕 JSONL 有 {bad_lines} 行不是 JSON 物件，已略過。")
    if cpu is None:
        warn("missing_cpu", "資源 CSV 沒有 CPU％欄位，只能判斷 RAM 壓力。")
    if ram is None:
        warn("missing_ram", "資源 CSV 沒有可用 RAM 欄位，只能判斷 CPU 壓力。")
    blanks = sum(c.blank_cells for c in csvs)
    if blanks:
        notes.append(f"略過 {blanks} 個空白／非數值的資源儲存格（typeperf 第一筆常是空白）。")
    msgs = sum(c.message_lines for c in csvs)
    if msgs:
        notes.append(f"略過 {msgs} 行 typeperf 狀態訊息。")
    skipped = {"no_timestamp": sum(f.no_timestamp for f in cap_files),
               "no_latency": sum(f.no_latency for f in cap_files),
               "invalid_latency": sum(f.invalid_latency for f in cap_files)}
    if any(skipped.values()):
        notes.append("字幕事件略過：無時間 {no_timestamp}、無延遲欄位 {no_latency}、延遲無效 {invalid_latency}。"
                     .format(**skipped))

    # ---- align captions
    rows: list[dict] = []
    for s in sorted(samples, key=lambda s: (s.t, s.kind, s.file, s.line)):
        at = s.t - settings.lag_s
        cv = cpu.nearest(at) if cpu else None
        rv = ram.nearest(at) if ram else None
        rows.append({"s": s, "cpu": cv, "ram": rv, "covered": cv is not None or rv is not None,
                     "cpu_high": cv is not None and cv > settings.cpu_high,
                     "ram_low": rv is not None and rv < settings.ram_low_mb})
    covered = [r for r in rows if r["covered"]]
    coverage = len(covered) / len(rows) if rows else 0.0
    if rows and coverage < MIN_COVERAGE:
        warn("low_overlap", f"只有 {len(covered)}/{len(rows)} 筆字幕落在資源時間軸內（< 50%）；"
                            "請確認兩邊是同一場次、時區（--utc-offset）是否正確。")

    # ---- pressure windows
    raw_windows: list[dict] = []
    if ram:
        raw_windows += find_windows(ram, lambda v: v < settings.ram_low_mb, "ram")
    if cpu:
        raw_windows += find_windows(cpu, lambda v: v > settings.cpu_high, "cpu")
    merged = merge_windows(raw_windows)
    for w in merged:
        inside = [r for r in covered if w["start_t"] <= r["s"].t - settings.lag_s <= w["end_t"]
                  and (r["ram_low"] or r["cpu_high"])]
        lats = [r["s"].latency_ms for r in inside]
        w["captions"] = len(inside)
        w["median_latency_ms"] = _round(statistics.median(lats)) if lats else None
        w["max_latency_ms"] = _round(max(lats)) if lats else None
        by_kind = {}
        for k in KINDS:
            kl = [r["s"].latency_ms for r in inside if r["s"].kind == k]
            if kl:
                by_kind[k] = {"n": len(kl), "median_ms": _round(statistics.median(kl))}
        w["by_kind"] = by_kind
    pressure_s = sum(w["end_t"] - w["start_t"] for w in merged)

    # ---- comparisons
    def flagged(r: dict, reason: str) -> bool | None:
        if reason == "ram":
            return None if r["ram"] is None else r["ram_low"]
        if reason == "cpu":
            return None if r["cpu"] is None else r["cpu_high"]
        return r["ram_low"] or r["cpu_high"]

    comparison: dict[str, dict] = {}
    kinds_present = [k for k in KINDS if any(r["s"].kind == k for r in covered)]
    for reason in REASONS:
        if (reason == "ram" and ram is None) or (reason == "cpu" and cpu is None):
            continue
        comparison[reason] = {}
        for kind in kinds_present + ["all"]:
            sel = [r for r in covered if kind == "all" or r["s"].kind == kind]
            inside = [r["s"].latency_ms for r in sel if flagged(r, reason) is True]
            outside = [r["s"].latency_ms for r in sel if flagged(r, reason) is False]
            comparison[reason][kind] = compare(inside, outside, min_samples=settings.min_samples,
                                               ratio=settings.rise_ratio, rise_min_ms=settings.rise_min_ms)

    rising = [(reason, kind) for reason, by_kind in comparison.items() for kind, c in by_kind.items()
              if kind != "all" and c["rises"]]
    correlated = bool(rising)
    pressured_captions = sum(1 for r in covered if r["ram_low"] or r["cpu_high"])
    any_sufficient = any(c["sufficient"] for k, c in comparison.get("any", {}).items() if k != "all")
    if merged and not correlated and not any_sufficient:
        warn("insufficient_samples",
             f"有資源壓力，但壓力內／外的字幕都至少要 {settings.min_samples} 筆才能比較"
             f"（壓力內 {pressured_captions} 筆、壓力外 {len(covered) - pressured_captions} 筆）。")

    correlation: dict[str, dict] = {}
    for kind in kinds_present:
        sel = [r for r in covered if r["s"].kind == kind]
        cs = [r for r in sel if r["cpu"] is not None]
        rs = [r for r in sel if r["ram"] is not None]
        correlation[kind] = {
            "latency_vs_cpu": pearson([r["cpu"] for r in cs], [r["s"].latency_ms for r in cs]),
            "latency_vs_available_ram": pearson([r["ram"] for r in rs], [r["s"].latency_ms for r in rs]),
        }

    if correlated:
        status = "correlated"
    elif warnings:
        status = "warn"
    else:
        status = "ok"

    suggestions: list[str] = []
    if any(reason in ("ram", "any") for reason, _ in rising) and any("ram" in w["reasons"] for w in merged):
        suggestions += ADVICE_RAM_ZH
    if any(reason in ("cpu", "any") for reason, _ in rising) and any("cpu" in w["reasons"] for w in merged):
        suggestions += ADVICE_CPU_ZH
    if any(w["code"] == "low_overlap" for w in warnings):
        suggestions.append("確認 typeperf 與字幕紀錄是同一場次；typeperf 用本機時間，字幕用 epoch 時間，"
                           "時區不同時用 --utc-offset 指定 CSV 的時區。")

    by_kind = {k: sum(1 for r in rows if r["s"].kind == k) for k in KINDS}
    all_t = [t for s in series.values() for t in s.times]
    windows_out = [{
        "start": fmt_time(w["start_t"], display_offset), "end": fmt_time(w["end_t"], display_offset),
        "duration_s": _round(w["end_t"] - w["start_t"], 3), "reasons": w["reasons"],
        "cpu_max_pct": _round(w["cpu_max"]), "ram_min_mb": _round(w["ram_min_mb"]),
        "captions": w["captions"], "median_latency_ms": w["median_latency_ms"],
        "max_latency_ms": w["max_latency_ms"], "by_kind": w["by_kind"],
    } for w in merged]

    report: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": EXIT_CODES[status],
        "correlated": correlated,
        "rising": [{"reason": r, "kind": k} for r, k in rising],
        "settings": {"ram_low_mb": settings.ram_low_mb, "cpu_high_pct": settings.cpu_high,
                     "lag_s": settings.lag_s, "max_skew_s": settings.max_skew_s,
                     "min_samples": settings.min_samples, "rise_ratio": settings.rise_ratio,
                     "rise_min_ms": settings.rise_min_ms, "percentile": "nearest-rank"},
        "display_utc_offset": fmt_offset(display_offset),
        "offset_source": offset_source,
        "inputs": {
            "csv": [{"path": c.path, "format": c.fmt, "pdh_timezone": c.pdh_timezone,
                     "utc_offset": fmt_offset(c.offset_min), "offset_source": c.offset_source,
                     "columns": c.columns, "rows": c.rows, "bad_rows": c.bad_rows,
                     "blank_cells": c.blank_cells, "message_lines": c.message_lines} for c in csvs],
            "captions": [vars(f).copy() for f in cap_files],
        },
        "timeline": {"start": fmt_time(min(all_t), display_offset) if all_t else None,
                     "end": fmt_time(max(all_t), display_offset) if all_t else None,
                     "duration_s": _round(max(all_t) - min(all_t), 3) if all_t else None},
        "resources": {"cpu_pct": series_stats(cpu, display_offset),
                      "available_ram_mb": series_stats(ram, display_offset),
                      "working_set_mb": series_stats(ws, display_offset)},
        "captions": {"samples": len(rows), "by_kind": by_kind, "covered": len(covered),
                     "uncovered": len(rows) - len(covered), "coverage": _round(coverage, 3),
                     "under_pressure": pressured_captions, "skipped": skipped},
        "pressure": {"windows": windows_out, "window_count": len(windows_out),
                     "total_s": _round(pressure_s, 3),
                     "ram_windows": sum(1 for w in raw_windows if w["reason"] == "ram"),
                     "cpu_windows": sum(1 for w in raw_windows if w["reason"] == "cpu")},
        "comparison": comparison,
        "correlation": correlation,
        "warnings": warnings,
        "notes": notes,
        "suggestions": suggestions,
    }
    report["summary_zh"] = render_summary(report, settings)
    return report


# --------------------------------------------------------------------------- output
def _ms(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f} ms"


def render_summary(r: dict, st: Settings) -> str:
    off = r["display_utc_offset"]
    lines = [f"資源壓力 × 字幕延遲時間軸：{STATUS_ZH[r['status']]}（exit {r['exit_code']}）"]
    tl = r["timeline"]
    res = r["resources"]
    parts = []
    if res["cpu_pct"]:
        parts.append(f"CPU {res['cpu_pct']['n']} 筆（最高 {res['cpu_pct']['max']}%）")
    if res["available_ram_mb"]:
        parts.append(f"可用 RAM {res['available_ram_mb']['n']} 筆（最低 {res['available_ram_mb']['min']} MB）")
    if res["working_set_mb"]:
        w = res["working_set_mb"]
        parts.append(f"工作集 {w['n']} 筆（峰值 {w['max']} MB，增加 {w['growth']} MB）")
    lines.append(f"資源時間軸：{tl['start']} ～ {tl['end']}（UTC{off}）；" + "、".join(parts))
    c = r["captions"]
    bk = c["by_kind"]
    lines.append(f"字幕延遲樣本：{c['samples']} 筆（草稿 {bk['draft']}、定稿 {bk['final']}、未分類 {bk['other']}），"
                 f"落在資源時間軸內 {c['covered']} 筆，其中壓力下 {c['under_pressure']} 筆。")
    p = r["pressure"]
    if p["window_count"]:
        lines.append(f"壓力區間：{p['window_count']} 段，共 {p['total_s']:.1f} 秒"
                     f"（RAM < {st.ram_low_mb:g} MB：{p['ram_windows']} 段；CPU > {st.cpu_high:g}%：{p['cpu_windows']} 段）")
        for w in p["windows"][:20]:
            why = "＋".join(REASON_ZH[x] for x in w["reasons"])
            extra = []
            if w["ram_min_mb"] is not None:
                extra.append(f"RAM 最低 {w['ram_min_mb']} MB")
            if w["cpu_max_pct"] is not None:
                extra.append(f"CPU 最高 {w['cpu_max_pct']}%")
            kinds = "、".join(f"{KIND_ZH[k]} {v['n']} 筆中位數 {_ms(v['median_ms'])}" for k, v in w["by_kind"].items())
            lines.append(f"  - {w['start'][11:19]}–{w['end'][11:19]}（{w['duration_s']:.1f} 秒，{why}；"
                         f"{'、'.join(extra)}）字幕 {w['captions']} 筆" + (f"：{kinds}" if kinds else ""))
        if p["window_count"] > 20:
            lines.append(f"  … 另有 {p['window_count'] - 20} 段（見 JSON）")
    else:
        lines.append(f"壓力區間：無（可用 RAM 未低於 {st.ram_low_mb:g} MB、CPU 未超過 {st.cpu_high:g}%）。")
    for reason, by_kind in r["comparison"].items():
        for kind, cmpd in by_kind.items():
            if kind == "all" or not cmpd["inside"]["n"]:
                continue
            a, b = cmpd["inside"], cmpd["outside"]
            verdict = "→ 延遲同步上升" if cmpd["rises"] else ("→ 未見明顯上升" if cmpd["sufficient"] else "→ 樣本不足")
            ratio = f"（×{cmpd['median_ratio']}）" if cmpd["median_ratio"] is not None else ""
            lines.append(f"{REASON_ZH[reason]}／{KIND_ZH[kind]}：壓力內 {a['n']} 筆 中位數 {_ms(a['median_ms'])}／"
                         f"p95 {_ms(a['p95_ms'])}；壓力外 {b['n']} 筆 {_ms(b['median_ms'])}／{_ms(b['p95_ms'])}"
                         f"{ratio} {verdict}")
    if r["correlated"]:
        lines.append("判定：資源壓力期間字幕延遲同步上升（"
                     + "、".join(f"{REASON_ZH[x['reason']]}／{KIND_ZH[x['kind']]}" for x in r["rising"]) + "）。")
    elif p["window_count"]:
        lines.append("判定：有資源壓力，但字幕延遲沒有明顯同步上升。" if r["status"] == "ok"
                     else "判定：有資源壓力，但資料不足或有警告，無法下結論。")
    else:
        lines.append("判定：期間沒有資源壓力。" if r["status"] == "ok" else "判定：沒有資源壓力，但有警告需留意。")
    for w in r["warnings"]:
        lines.append(f"警告：{w['message_zh']}")
    for n in r["notes"]:
        lines.append(f"附註：{n}")
    if r["suggestions"]:
        lines.append("建議：")
        lines.extend(f"  - {s}" for s in r["suggestions"])
    return "\n".join(lines)


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
    ap = _Parser(prog="session_resource_timeline.py",
                 description="唯讀對齊 typeperf／自訂資源 CSV 與字幕 JSONL 時間軸，判斷 RAM 不足或 CPU 滿載時字幕延遲是否同步上升。")
    ap.add_argument("--csv", type=Path, action="append", required=True, metavar="PATH",
                    help="typeperf 或自訂資源 CSV（可重複）")
    ap.add_argument("--captions", type=Path, action="append", required=True, metavar="PATH",
                    help="字幕事件 JSONL（可重複）")
    ap.add_argument("--json", type=Path, metavar="OUT", help="另寫 JSON 報告到新檔（已存在就拒絕，不覆蓋）")
    ap.add_argument("--ram-low-mb", type=float, default=DEFAULT_RAM_LOW_MB, help="可用 RAM 低於此值算壓力（預設 1024）")
    ap.add_argument("--cpu-high", type=float, default=DEFAULT_CPU_HIGH, help="CPU％高於此值算壓力（預設 90）")
    ap.add_argument("--utc-offset", metavar="±HH:MM", help="無時區時間的 UTC 位移（預設：CSV 標題，否則 +08:00）")
    ap.add_argument("--process", action="append", default=[], metavar="NAME",
                    help="只加總這些程序的工作集（typeperf 執行個體名稱開頭，可重複）")
    ap.add_argument("--lag-s", type=float, default=0.0, help="字幕時間往前推幾秒再對齊資源（預設 0）")
    ap.add_argument("--max-skew-s", type=float, help="字幕與最近資源樣本的最大時間差（預設 1.5×取樣間隔，至少 1 秒）")
    ap.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES, help="壓力內／外各至少幾筆字幕（預設 5）")
    ap.add_argument("--rise-ratio", type=float, default=DEFAULT_RISE_RATIO, help="壓力內中位數至少為壓力外幾倍（預設 1.25）")
    ap.add_argument("--rise-min-ms", type=float, default=DEFAULT_RISE_MIN_MS, help="且至少高出幾 ms（預設 300）")
    return ap


def _check_args(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int | None:
    for name in ("ram_low_mb", "cpu_high", "lag_s", "rise_ratio", "rise_min_ms"):
        v = getattr(args, name)
        if not math.isfinite(v):
            ap.error(f"--{name.replace('_', '-')} 必須是有限數字")
    if args.ram_low_mb <= 0:
        ap.error("--ram-low-mb 必須大於 0")
    if not 0 < args.cpu_high <= 100:
        ap.error("--cpu-high 必須介於 0 與 100 之間")
    if args.lag_s < 0:
        ap.error("--lag-s 不能是負數")
    if args.max_skew_s is not None and (not math.isfinite(args.max_skew_s) or args.max_skew_s <= 0):
        ap.error("--max-skew-s 必須大於 0")
    if args.min_samples < 1:
        ap.error("--min-samples 至少為 1")
    if args.rise_ratio < 1:
        ap.error("--rise-ratio 不能小於 1")
    if args.rise_min_ms < 0:
        ap.error("--rise-min-ms 不能是負數")
    offset = None
    if args.utc_offset is not None:
        try:
            offset = parse_offset(args.utc_offset)
        except ValueError as exc:
            ap.error(str(exc))
    return offset


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    offset = _check_args(ap, args)
    if args.json is not None:
        target = os.path.abspath(str(args.json))
        if any(os.path.abspath(str(p)) == target for p in [*args.csv, *args.captions]):
            ap.error("--json 不能指向輸入檔")
        if args.json.exists() or args.json.is_symlink():
            sys.stderr.write(f"拒絕覆蓋既有檔案：{args.json}（只寫新檔）\n")
            return EXIT_OUTPUT
    processes = tuple(p.strip().lower() for p in args.process if p.strip())
    try:
        csvs = [read_resource_csv(p, offset_override=offset, processes=processes) for p in args.csv]
        if offset is not None:
            display, source = offset, "--utc-offset"
        else:
            hdr = next((c for c in csvs if c.offset_source == "pdh_header"), None)
            display, source = (hdr.offset_min, "pdh_header") if hdr else (DEFAULT_OFFSET_MIN, "default")
        cap_files: list[CaptionFileResult] = []
        samples: list[CaptionSample] = []
        for p in args.captions:
            res, s = read_captions(p, display)
            cap_files.append(res)
            samples.extend(s)
        if not samples:
            raise InputError("字幕 JSONL 裡沒有任何同時有時間與延遲的事件")
    except InputError as exc:
        sys.stderr.write(f"輸入錯誤：{exc}\n")
        return EXIT_INPUT
    settings = Settings(ram_low_mb=args.ram_low_mb, cpu_high=args.cpu_high, lag_s=args.lag_s,
                        max_skew_s=args.max_skew_s, min_samples=args.min_samples,
                        rise_ratio=args.rise_ratio, rise_min_ms=args.rise_min_ms)
    report = analyze(csvs, cap_files, samples, settings, display_offset=display, offset_source=source)
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
