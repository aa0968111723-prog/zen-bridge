#!/usr/bin/env python3
"""Check an offline worker configuration against a hw_probe JSON report."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys


APP_OS_HEADROOM_GB = 1.0


class BudgetInputError(ValueError):
    """Raised when a hardware report or worker configuration is invalid."""


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetInputError(f"{label} must be a number")
    if not math.isfinite(value) or value < 0:
        raise BudgetInputError(f"{label} must be finite and non-negative")
    return float(value)


def _positive_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BudgetInputError(f"{label} must be a positive integer")
    return value


def _read_inputs(hardware, config):
    if not isinstance(hardware, dict):
        raise BudgetInputError("hardware JSON must be an object")
    if not isinstance(config, dict):
        raise BudgetInputError("config JSON must be an object")

    if hardware.get("ram_available_gb") is None:
        raise BudgetInputError(
            "hardware.ram_available_gb is required and must be a number"
        )
    if hardware.get("logical_cores") is None:
        raise BudgetInputError(
            "hardware.logical_cores is required and must be a positive integer"
        )
    available_ram = _number(hardware["ram_available_gb"], "hardware.ram_available_gb")
    logical_cores = _positive_integer(
        hardware["logical_cores"], "hardware.logical_cores"
    )

    devices = hardware.get("vulkan_devices", [])
    if not isinstance(devices, list) or any(not isinstance(item, str) for item in devices):
        raise BudgetInputError("hardware.vulkan_devices must be a list of strings")
    on_ac_power = hardware.get("on_ac_power")
    if on_ac_power is not None and not isinstance(on_ac_power, bool):
        raise BudgetInputError("hardware.on_ac_power must be a boolean or null")
    power_plan = hardware.get("power_plan")
    if power_plan is not None and not isinstance(power_plan, str):
        raise BudgetInputError("hardware.power_plan must be a string or null")

    workers = config.get("workers")
    if not isinstance(workers, list) or not workers:
        raise BudgetInputError("config.workers must be a non-empty list")

    checked_workers = []
    names = set()
    for index, worker in enumerate(workers):
        label = f"config.workers[{index}]"
        if not isinstance(worker, dict):
            raise BudgetInputError(f"{label} must be an object")
        name = worker.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BudgetInputError(f"{label}.name must be a non-empty string")
        if name.strip().casefold() in names:
            raise BudgetInputError(f"worker names must be unique: {name}")
        names.add(name.strip().casefold())
        backend = worker.get("backend")
        if backend not in ("cpu", "vulkan"):
            raise BudgetInputError(f"{label}.backend must be 'cpu' or 'vulkan'")
        checked_workers.append(
            {
                "name": name.strip(),
                "backend": backend,
                "threads": _positive_integer(worker.get("threads"), f"{label}.threads"),
                "model_ram_gb": _number(
                    worker.get("model_ram_gb"), f"{label}.model_ram_gb"
                ),
                "extra_ram_gb": _number(
                    worker.get("extra_ram_gb"), f"{label}.extra_ram_gb"
                ),
            }
        )

    return available_ram, logical_cores, devices, on_ac_power, power_plan, checked_workers


def check_budget(hardware, config):
    """Return a report describing whether the proposed workers fit."""
    available_ram, logical_cores, devices, on_ac_power, power_plan, workers = (
        _read_inputs(hardware, config)
    )

    required_ram = APP_OS_HEADROOM_GB + sum(
        worker["model_ram_gb"] + worker["extra_ram_gb"] for worker in workers
    )
    cpu_threads = sum(
        worker["threads"] for worker in workers if worker["backend"] == "cpu"
    )
    available_cpu_threads = max(0, logical_cores - 1)
    errors = []
    warnings = []
    if required_ram > available_ram:
        errors.append("RAM requirement exceeds available RAM")
    if cpu_threads > available_cpu_threads:
        errors.append("CPU worker threads exceed the logical-core budget")
    if any(worker["backend"] == "vulkan" for worker in workers) and not any(
        device.strip() for device in devices
    ):
        warnings.append("Vulkan is requested, but no Vulkan device was reported")

    named_workers = [
        (worker, re.sub(r"[^a-z0-9]", "", worker["name"].casefold()))
        for worker in workers
    ]
    final_asr = next(
        (
            worker
            for worker, name in named_workers
            if name.startswith("finalasr")
            or ("final" in name and "asr" in name)
            or ("breeze" in name and "asr" in name)
        ),
        None,
    )
    mt = next(
        (
            worker
            for worker, name in named_workers
            if name in ("mt", "translation", "machinetranslation")
            or name.startswith(("mt", "hymt"))
            or "translation" in name
        ),
        None,
    )
    if (
        final_asr is not None
        and mt is not None
        and final_asr["backend"] == "cpu"
        and mt["backend"] == "cpu"
        and final_asr["threads"] >= 4
        and mt["threads"] >= 4
    ):
        warnings.append(
            "Final ASR and MT both use at least 4 CPU threads; concurrent latency may suffer"
        )
    if on_ac_power is False:
        warnings.append("Hardware probe reports the system is running on battery")
    if isinstance(power_plan, str) and any(
        phrase in power_plan.casefold()
        for phrase in (
            "power saver",
            "省電",
            "省电",
            "a1841308-3541-4fab-bc81-f71556f20b4a",
        )
    ):
        warnings.append("Hardware probe reports a power saver plan")

    return {
        "fits": not errors,
        "ram": {
            "required_gb": required_ram,
            "available_gb": available_ram,
            "headroom_gb": APP_OS_HEADROOM_GB,
        },
        "cpu": {
            "requested_threads": cpu_threads,
            "available_threads": available_cpu_threads,
        },
        "errors": errors,
        "warnings": warnings,
    }


def format_summary(report):
    status = "FITS" if report["fits"] else "DOES NOT FIT"
    lines = [
        f"Model budget: {status}",
        (
            f"RAM: {report['ram']['required_gb']:.2f} GB required "
            f"(including {report['ram']['headroom_gb']:.1f} GB headroom), "
            f"{report['ram']['available_gb']:.2f} GB available"
        ),
        (
            f"CPU: {report['cpu']['requested_threads']} worker threads requested, "
            f"{report['cpu']['available_threads']} available"
        ),
    ]
    lines.extend(f"Error: {error}" for error in report["errors"])
    lines.extend(f"Warning: {warning}" for warning in report["warnings"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hw", required=True, help="hw_probe JSON file")
    parser.add_argument("--config", required=True, help="worker config JSON file")
    parser.add_argument("--json", help="write the report JSON to a new file")
    args = parser.parse_args(argv)

    try:
        with open(args.hw, encoding="utf-8") as hardware_file:
            hardware = json.load(hardware_file)
        with open(args.config, encoding="utf-8") as config_file:
            config = json.load(config_file)
        report = check_budget(hardware, config)
        if args.json:
            with open(args.json, "x", encoding="utf-8") as output_file:
                json.dump(report, output_file, indent=2)
                output_file.write("\n")
    except json.JSONDecodeError as exc:
        print(f"model_budget: invalid JSON input: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, BudgetInputError) as exc:
        print(f"model_budget: {exc}", file=sys.stderr)
        return 2

    print(format_summary(report))
    return 0 if report["fits"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
