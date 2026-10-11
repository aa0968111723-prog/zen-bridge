#!/usr/bin/env python3
"""Write an offline local GGUF MT benchmark plan; never load or run a model.

An optional --models JSON file can override per-quantization RAM estimates with
an object such as {"quant_ram_gb": {"Q4_K_M": 2.6}}. These estimates are
approximate and represent the full model runtime footprint.
"""
from __future__ import annotations

import argparse
import csv
import json
import math


CSV_COLUMNS = (
    "target_lang",
    "backend",
    "quant",
    "threads",
    "gpu_layers",
    "est_ram_gb",
    "skip_reason",
    "notes",
)
QUANTIZATIONS = ("Q4_K_M", "Q5_K_M", "Q8_0")
THREAD_COUNTS = (1, 2, 4)
BACKENDS = ("llama.cpp", "Ollama")
DEFAULT_RAM_BUDGET_GB = 4.8
DEFAULT_QUANT_RAM_GB = {
    "Q4_K_M": 2.6,
    "Q5_K_M": 2.9,
    "Q8_0": 3.6,
}


def _valid_gb(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _quant_ram_estimates(models):
    estimates = dict(DEFAULT_QUANT_RAM_GB)
    if models is None:
        return estimates
    if not isinstance(models, dict) or not isinstance(models.get("quant_ram_gb"), dict):
        raise ValueError("models JSON must contain an object named 'quant_ram_gb'")
    for quant, value in models["quant_ram_gb"].items():
        if quant not in QUANTIZATIONS:
            raise ValueError(f"unsupported quantization in models JSON: {quant}")
        if not _valid_gb(value):
            raise ValueError(f"RAM estimate for {quant} must be a finite non-negative number")
        estimates[quant] = float(value)
    return estimates


def build_plan(hardware, ram_budget_gb=DEFAULT_RAM_BUDGET_GB, models=None):
    """Return a deterministic plan from hw_probe facts and optional estimates."""
    if not isinstance(hardware, dict):
        raise ValueError("hardware JSON must be an object")
    if not _valid_gb(ram_budget_gb):
        raise ValueError("RAM budget must be a finite non-negative number")
    estimates = _quant_ram_estimates(models)
    devices = hardware.get("vulkan_devices")
    has_vulkan = isinstance(devices, (list, tuple)) and any(
        isinstance(device, str) and device.strip() for device in devices
    )
    offload_modes = (0, -1) if has_vulkan else (0,)

    rows = []
    for target_lang in ("en", "ja"):
        for backend in BACKENDS:
            for quant in QUANTIZATIONS:
                estimate = estimates[quant]
                for threads in THREAD_COUNTS:
                    for gpu_layers in offload_modes:
                        rows.append(
                            {
                                "target_lang": target_lang,
                                "backend": backend,
                                "quant": quant,
                                "threads": threads,
                                "gpu_layers": gpu_layers,
                                "est_ram_gb": f"{estimate:.2f}",
                                "skip_reason": (
                                    f"estimated RAM exceeds {ram_budget_gb:g} GB budget"
                                    if estimate > ram_budget_gb
                                    else ""
                                ),
                                "notes": (
                                    "Try Q8_0 first for ja."
                                    if target_lang == "ja" and quant == "Q8_0"
                                    else ""
                                ),
                            }
                        )
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hw", required=True, help="hw_probe JSON file")
    parser.add_argument("--out", required=True, help="output CSV file (must not already exist)")
    parser.add_argument(
        "--ram-budget-gb",
        type=float,
        default=DEFAULT_RAM_BUDGET_GB,
        help=f"maximum estimated RAM before marking a row to skip (default: {DEFAULT_RAM_BUDGET_GB})",
    )
    parser.add_argument(
        "--models",
        help="optional JSON with quant_ram_gb estimates for Q4_K_M, Q5_K_M, and Q8_0",
    )
    args = parser.parse_args(argv)
    if not _valid_gb(args.ram_budget_gb):
        parser.error("--ram-budget-gb must be a finite non-negative number")

    with open(args.hw, encoding="utf-8") as hardware_file:
        hardware = json.load(hardware_file)
    models = None
    if args.models:
        with open(args.models, encoding="utf-8") as models_file:
            models = json.load(models_file)
    rows = build_plan(hardware, args.ram_budget_gb, models)

    try:
        output_file = open(args.out, "x", encoding="utf-8", newline="")
    except FileExistsError:
        parser.error(f"refusing to overwrite existing output: {args.out}")
    with output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
