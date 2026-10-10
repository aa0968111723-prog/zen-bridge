#!/usr/bin/env python3
"""Plan offline whisper.cpp-style ASR parameter sweeps; never load or run a model.

When --max-runs is set, keep the first N rows from the deterministic parameter
grid (threads, audio_ctx, beam_size, best_of, then mt_quant).
"""

import argparse
import csv
import itertools
import json


CSV_COLUMNS = (
    "run_id",
    "threads",
    "audio_ctx",
    "beam_size",
    "best_of",
    "use_vulkan",
    "mt_quant",
    "notes",
)
DEFAULT_PHYSICAL_CORES = 6
DEFAULT_LOGICAL_CORES = 12
AUDIO_CONTEXTS = (0, 128, 256)
BEAM_SIZES = (1, 2, 5)
BEST_OF_VALUES = (1, 2)


def _core_count(value, fallback):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return fallback
    return value


def _thread_counts(hardware):
    physical = _core_count(hardware.get("physical_cores"), DEFAULT_PHYSICAL_CORES)
    logical = _core_count(hardware.get("logical_cores"), DEFAULT_LOGICAL_CORES)
    physical = min(physical, logical)

    # Reserve all logical siblings of one physical core for MT/UI work.
    reserved = max(1, (logical + physical - 1) // physical)
    available = max(1, logical - reserved)
    return tuple(count for count in (1, 2, 4, 8, available) if count <= available)


def build_plan(hardware, lang="en", max_runs=None):
    """Return deterministic run dictionaries based on a hw_probe JSON object."""
    if lang not in ("en", "ja"):
        raise ValueError("lang must be en or ja")
    if max_runs is not None and max_runs < 0:
        raise ValueError("max_runs must be non-negative")
    if max_runs == 0:
        return []

    devices = hardware.get("vulkan_devices") or []
    use_vulkan = isinstance(devices, (list, tuple)) and any(
        isinstance(device, str) and device.strip() for device in devices
    )
    quantizations = ("Q8", "Q4") if lang == "ja" else ("Q4", "Q8")
    threads = _thread_counts(hardware)
    rows = []
    combinations = itertools.product(
        threads,
        AUDIO_CONTEXTS,
        BEAM_SIZES,
        BEST_OF_VALUES,
        quantizations,
    )
    for index, (thread_count, audio_ctx, beam_size, best_of, mt_quant) in enumerate(
        combinations, start=1
    ):
        rows.append(
            {
                "run_id": f"run-{index:03d}",
                "threads": thread_count,
                "audio_ctx": audio_ctx,
                "beam_size": beam_size,
                "best_of": best_of,
                "use_vulkan": str(use_vulkan).lower(),
                "mt_quant": mt_quant,
                "notes": "Plan only; retain one physical core for MT/UI.",
            }
        )
        if max_runs is not None and len(rows) >= max_runs:
            break
    return rows


def _summary(rows):
    ranges = {}
    for column in ("threads", "audio_ctx", "beam_size", "best_of"):
        values = [row[column] for row in rows]
        ranges[column] = {
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return {"count": len(rows), "ranges": ranges}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hw", required=True, help="hw_probe JSON file")
    parser.add_argument("--out", required=True, help="output CSV file")
    parser.add_argument("--max-runs", type=int, help="keep the first N grid rows")
    parser.add_argument("--lang", choices=("en", "ja"), default="en")
    args = parser.parse_args(argv)
    if args.max_runs is not None and args.max_runs < 0:
        parser.error("--max-runs must be non-negative")

    with open(args.hw, encoding="utf-8") as hardware_file:
        hardware = json.load(hardware_file)
    rows = build_plan(hardware, lang=args.lang, max_runs=args.max_runs)
    with open(args.out, "w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(_summary(rows), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
