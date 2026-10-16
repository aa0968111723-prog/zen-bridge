#!/usr/bin/env python3
"""Offline RAM headroom guard before loading a local model (queue item 32).

  python tools/ram_headroom_guard.py [--budget-gb 4.8] [--margin-gb 1.0] [--json]
  python tools/ram_headroom_guard.py --meminfo /proc/meminfo --json
  python tools/ram_headroom_guard.py --available-gb 3.2 --total-gb 15.4

Reads *available* physical memory (stdlib only, read-only, no network):

* Windows: ``kernel32.GlobalMemoryStatusEx`` via ``ctypes``.
* Linux: ``/proc/meminfo`` (``MemAvailable``; older kernels fall back to
  ``MemFree + Buffers + Cached + SReclaimable - Shmem``).
* macOS / other: reported as ``unknown`` (no subprocess, no psutil).

The available amount is compared with a model RAM budget (default
``4.8`` GB, GiB = 1024**3 bytes, same unit as ``hw_probe.ram_available_gb``)
plus a safety margin for the app / OS (default ``1.0`` GB):

==========  ==========================================================  ====
status      condition                                                    exit
==========  ==========================================================  ====
``ok``      available >= budget + margin                                 0
``warn``    budget <= available < budget + margin                        1
``fail``    available < budget                                           2
``unknown`` memory could not be read (unsupported OS / unreadable file)  3
(usage)     invalid options (negative budget, unreadable forced input)   4
==========  ==========================================================  ====

Traditional Chinese advice (close background apps, switch to a Q4 quantized
model, …) is printed for warn / fail / unknown. ``--json`` prints one JSON
object to stdout instead of the human-readable text.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from pathlib import Path
from typing import Any

TOOL = "ram_headroom_guard"
SCHEMA_VERSION = 1

DEFAULT_BUDGET_GB = 4.8
DEFAULT_MARGIN_GB = 1.0
GIB = 1024**3
KIB = 1024

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 4

EXIT_CODES = {
    "ok": EXIT_OK,
    "warn": EXIT_WARN,
    "fail": EXIT_FAIL,
    "unknown": EXIT_UNKNOWN,
}

STATUS_ZH = {
    "ok": "足夠",
    "warn": "偏緊",
    "fail": "不足",
    "unknown": "無法判斷",
}

# Common background apps on a club laptop that eat RAM during a live session.
BACKGROUND_APPS_ZH = (
    "Chrome／Edge 多餘分頁（每個分頁約 100–500 MB）",
    "Teams／LINE／Discord 等通訊軟體",
    "OBS（若本場不錄影／不直播）",
    "Steam／Epic 等遊戲平台與其 Overlay",
    "OneDrive／Google Drive 同步、Windows Update 背景下載",
)


class GuardInputError(ValueError):
    """Raised for invalid options or an unreadable forced input."""


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse ``/proc/meminfo`` text into ``{key: bytes}``.

    Values with a ``kB`` suffix are KiB (kernel convention) and converted to
    bytes; values without a unit (e.g. ``HugePages_Total``) are kept as-is.
    Malformed lines are ignored.
    """
    result: dict[str, int] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        parts = rest.split()
        if not key or not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        if len(parts) > 1 and parts[1].lower() == "kb":
            value *= KIB
        result[key] = value
    return result


def memory_from_meminfo(info: dict[str, int]) -> dict[str, Any]:
    """Derive total / available bytes from parsed meminfo.

    Returns ``{"total_bytes", "available_bytes", "method"}``; ``available``
    is ``None`` when neither ``MemAvailable`` nor ``MemFree`` exists.
    """
    total = info.get("MemTotal")
    if "MemAvailable" in info:
        return {
            "total_bytes": total,
            "available_bytes": info["MemAvailable"],
            "method": "proc_meminfo:MemAvailable",
        }
    if "MemFree" in info:
        estimate = (
            info.get("MemFree", 0)
            + info.get("Buffers", 0)
            + info.get("Cached", 0)
            + info.get("SReclaimable", 0)
            - info.get("Shmem", 0)
        )
        if total is not None:
            estimate = min(estimate, total)
        return {
            "total_bytes": total,
            "available_bytes": max(0, estimate),
            "method": "proc_meminfo:estimate",
        }
    return {"total_bytes": total, "available_bytes": None, "method": "proc_meminfo:missing"}


def read_linux(path: str | Path = "/proc/meminfo") -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "total_bytes": None,
            "available_bytes": None,
            "method": "proc_meminfo",
            "error": f"cannot read {path}: {exc}",
        }
    return memory_from_meminfo(parse_meminfo(text))


def _memorystatusex_type():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    return MEMORYSTATUSEX


def read_windows(kernel32: Any = None) -> dict[str, Any]:
    """Read physical memory via ``GlobalMemoryStatusEx``.

    Inject ``kernel32`` in tests (any object with a ``GlobalMemoryStatusEx``
    callable receiving ``ctypes.byref(struct)``) so Linux CI never touches
    ``ctypes.windll``.
    """
    import ctypes

    struct_type = _memorystatusex_type()
    status = struct_type()
    status.dwLength = ctypes.sizeof(struct_type)
    try:
        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ok = kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception as exc:  # AttributeError off Windows, OSError, …
        return {
            "total_bytes": None,
            "available_bytes": None,
            "method": "GlobalMemoryStatusEx",
            "error": f"GlobalMemoryStatusEx unavailable: {exc}",
        }
    if not ok:
        return {
            "total_bytes": None,
            "available_bytes": None,
            "method": "GlobalMemoryStatusEx",
            "error": "GlobalMemoryStatusEx returned 0",
        }
    return {
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
        "memory_load_pct": int(status.dwMemoryLoad),
        "method": "GlobalMemoryStatusEx",
    }


def read_memory(
    system: str | None = None,
    *,
    meminfo_path: str | Path = "/proc/meminfo",
    kernel32: Any = None,
) -> dict[str, Any]:
    """Dispatch to the OS reader; unsupported systems report unknown."""
    system = system or platform.system()
    if system == "Windows":
        reading = read_windows(kernel32)
    elif system == "Linux":
        reading = read_linux(meminfo_path)
    else:
        reading = {
            "total_bytes": None,
            "available_bytes": None,
            "method": "unsupported",
            "error": f"memory probe not implemented for {system or 'unknown OS'}",
        }
    reading["os"] = system
    return reading


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _check_gb(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GuardInputError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise GuardInputError(f"{label} must be finite and non-negative")
    return value


def classify(
    available_gb: float | None,
    budget_gb: float = DEFAULT_BUDGET_GB,
    margin_gb: float = DEFAULT_MARGIN_GB,
) -> str:
    """Return ``ok`` / ``warn`` / ``fail`` / ``unknown``."""
    budget_gb = _check_gb(budget_gb, "budget_gb")
    margin_gb = _check_gb(margin_gb, "margin_gb")
    if available_gb is None:
        return "unknown"
    available_gb = _check_gb(available_gb, "available_gb")
    if available_gb < budget_gb:
        return "fail"
    if available_gb < budget_gb + margin_gb:
        return "warn"
    return "ok"


def advice_zh(status: str, *, shortfall_gb: float, budget_gb: float) -> list[str]:
    if status == "ok":
        return ["可用記憶體足夠，可以載入模型。正式場次中仍建議不要開啟大型程式。"]
    if status == "unknown":
        return [
            "無法讀取本機可用記憶體（此作業系統未支援或讀取失敗）。",
            "請開啟「工作管理員 → 效能 → 記憶體」手動確認「可用」至少有 "
            f"{budget_gb:.1f} GB 以上再載入模型。",
        ]
    tips: list[str] = []
    if status == "fail":
        tips.append(
            f"可用記憶體不足，還差約 {shortfall_gb:.1f} GB；直接載入模型可能卡頓、"
            "大量換頁或被系統強制結束。"
        )
    else:
        tips.append(
            "可用記憶體剛好夠模型本身，但留給系統與本程式的餘裕不足，"
            "長時間場次可能變慢。"
        )
    tips.append("建議先關閉這些背景程式：" + "；".join(BACKGROUND_APPS_ZH) + "。")
    tips.append(
        "改用 Q4 量化模型（例如 Q4_K_M GGUF），通常比 Q8／FP16 省下一半以上記憶體。"
    )
    if status == "fail":
        tips.append(
            "仍不夠時：改用較小的語音模型（例如 small 取代 medium）、縮短 MT 的 context，"
            "或重新開機後只開本程式。"
        )
    return tips


def evaluate(
    reading: dict[str, Any],
    *,
    budget_gb: float = DEFAULT_BUDGET_GB,
    margin_gb: float = DEFAULT_MARGIN_GB,
) -> dict[str, Any]:
    """Build the JSON-able report from a memory reading."""
    budget_gb = _check_gb(budget_gb, "budget_gb")
    margin_gb = _check_gb(margin_gb, "margin_gb")
    avail_b = reading.get("available_bytes")
    total_b = reading.get("total_bytes")
    available_gb = None if avail_b is None else round(avail_b / GIB, 3)
    total_gb = None if total_b is None else round(total_b / GIB, 3)
    status = classify(available_gb, budget_gb, margin_gb)
    required_gb = round(budget_gb + margin_gb, 3)
    if available_gb is None:
        shortfall = None
        headroom = None
    else:
        shortfall = round(max(0.0, budget_gb - available_gb), 3)
        headroom = round(available_gb - budget_gb, 3)
    notes: list[str] = []
    if reading.get("error"):
        notes.append(str(reading["error"]))
    summary = f"RAM 檢查：{STATUS_ZH[status]}（{status}）"
    if available_gb is not None:
        summary += f"；可用 {available_gb:.2f} GB"
        if total_gb is not None:
            summary += f"／總共 {total_gb:.2f} GB"
    summary += f"；模型預算 {budget_gb:.2f} GB＋餘裕 {margin_gb:.2f} GB"
    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "exit_code": EXIT_CODES[status],
        "os": reading.get("os"),
        "method": reading.get("method"),
        "available_gb": available_gb,
        "total_gb": total_gb,
        "memory_load_pct": reading.get("memory_load_pct"),
        "budget_gb": round(budget_gb, 3),
        "margin_gb": round(margin_gb, 3),
        "required_gb": required_gb,
        "headroom_gb": headroom,
        "shortfall_gb": shortfall,
        "summary_zh": summary,
        "advice_zh": advice_zh(
            status, shortfall_gb=shortfall or 0.0, budget_gb=budget_gb
        ),
        "notes": notes,
    }


def format_text(report: dict[str, Any]) -> str:
    lines = [report["summary_zh"]]
    for tip in report["advice_zh"]:
        lines.append(f"- {tip}")
    for note in report["notes"]:
        lines.append(f"（備註）{note}")
    lines.append(f"exit code: {report['exit_code']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # usage errors → EXIT_USAGE
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=TOOL,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--budget-gb",
        type=float,
        default=DEFAULT_BUDGET_GB,
        help=f"model RAM budget in GB (default {DEFAULT_BUDGET_GB})",
    )
    parser.add_argument(
        "--margin-gb",
        type=float,
        default=DEFAULT_MARGIN_GB,
        help=f"extra headroom for app/OS before 'ok' (default {DEFAULT_MARGIN_GB})",
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of text"
    )
    parser.add_argument(
        "--meminfo",
        metavar="PATH",
        help="read this /proc/meminfo-format file instead of probing the OS",
    )
    parser.add_argument(
        "--available-gb",
        type=float,
        help="use this available-RAM value (GB) instead of probing (offline checks)",
    )
    parser.add_argument(
        "--total-gb", type=float, help="optional total RAM (GB) with --available-gb"
    )
    return parser


def main(argv: list[str] | None = None, *, reader=None) -> int:
    """CLI entry. ``reader`` (no-arg callable → reading dict) is for tests."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        budget = _check_gb(args.budget_gb, "--budget-gb")
        margin = _check_gb(args.margin_gb, "--margin-gb")
        if args.available_gb is not None and args.meminfo:
            raise GuardInputError("use either --available-gb or --meminfo, not both")
        if args.available_gb is not None:
            avail = _check_gb(args.available_gb, "--available-gb")
            total = None
            if args.total_gb is not None:
                total = _check_gb(args.total_gb, "--total-gb")
            reading = {
                "os": platform.system(),
                "method": "manual",
                "available_bytes": int(avail * GIB),
                "total_bytes": None if total is None else int(total * GIB),
            }
        elif args.meminfo:
            path = Path(args.meminfo)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise GuardInputError(f"cannot read --meminfo {path}: {exc}") from exc
            reading = memory_from_meminfo(parse_meminfo(text))
            reading["os"] = "meminfo-file"
        elif reader is not None:
            reading = reader()
        else:
            reading = read_memory()
        report = evaluate(reading, budget_gb=budget, margin_gb=margin)
    except GuardInputError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
