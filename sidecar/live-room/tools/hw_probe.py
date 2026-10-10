#!/usr/bin/env python3
"""Read-only local hardware facts: python tools/hw_probe.py [--json]."""
from __future__ import annotations

import argparse
import ctypes
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


CPU_FLAGS = ("avx", "avx2", "fma", "f16c", "avx512f", "avx512_vnni", "avx_vnni")
# Windows has no IsProcessorFeaturePresent identifiers for FMA, F16C or VNNI.
WINDOWS_FEATURES = {"avx": 39, "avx2": 40, "avx512f": 41}


def _windows_cpu_model() -> str:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
    ) as key:
        return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()


def _linux_cpuinfo() -> dict:
    text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    model = None
    flag_sets = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        if key.strip() == "model name" and model is None:
            model = value.strip()
        if key.strip() == "flags":
            flag_sets.append(set(value.split()))
    result = {}
    if model:
        result["cpu_model"] = model
    if flag_sets:
        flags = set.intersection(*flag_sets)
        result["cpu_flags"] = {name: name in flags for name in CPU_FLAGS}
    else:
        raise ValueError("/proc/cpuinfo has no CPU flags")
    return result


def _run(command: list[str]) -> str:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=3,
        check=True,
    ).stdout


def _vulkan_devices() -> list[str]:
    executable = shutil.which("vulkaninfo")
    if executable is None:
        raise FileNotFoundError("vulkaninfo is not on PATH")
    output = _run([executable, "--summary"])
    names = re.findall(r"^\s*deviceName\s*=\s*(.+?)\s*$", output, re.MULTILINE)
    if not names:
        raise ValueError("vulkaninfo summary contains no device names")
    return list(dict.fromkeys(names))


def _power_plan() -> str:
    output = _run(["powercfg", "/getactivescheme"])
    match = re.search(
        r"\b([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b"
        r"(?:\s+\((.+)\))?",
        output,
    )
    if match is None:
        raise ValueError("powercfg returned no active scheme")
    return match.group(2) or match.group(1)


def probe() -> dict:
    """Return best-effort facts; unavailable values remain null, with notes."""
    result = {
        "os": None,
        "python": None,
        "cpu_model": None,
        "physical_cores": None,
        "logical_cores": None,
        "cpu_flags": {name: None for name in CPU_FLAGS},
        "ram_total_gb": None,
        "ram_available_gb": None,
        "on_ac_power": None,
        "battery_percent": None,
        "power_plan": None,
        "vulkan_devices": [],
        "notes": [],
    }

    def step(label, action):
        try:
            return action()
        except Exception as exc:
            result["notes"].append(f"{label}: {type(exc).__name__}: {exc}")
            return None

    result["os"] = step("os", platform.system)
    result["python"] = step("python", platform.python_version)
    result["cpu_model"] = step("cpu model", platform.processor) or None
    result["physical_cores"] = step("physical cores", lambda: psutil.cpu_count(logical=False))
    result["logical_cores"] = step("logical cores", lambda: psutil.cpu_count(logical=True))

    def memory():
        mem = psutil.virtual_memory()
        result["ram_total_gb"] = mem.total / 1024**3
        result["ram_available_gb"] = mem.available / 1024**3

    step("RAM", memory)

    def battery():
        status = psutil.sensors_battery()
        if status is not None:
            result["on_ac_power"] = (
                None if status.power_plugged is None else bool(status.power_plugged)
            )
            result["battery_percent"] = (
                None if status.percent is None else int(status.percent)
            )

    step("battery", battery)
    if result["os"] == "Windows":
        model = step("Windows CPU model", _windows_cpu_model)
        if model:
            result["cpu_model"] = model

        def windows_flags():
            feature = ctypes.WinDLL("kernel32", use_last_error=True).IsProcessorFeaturePresent
            feature.argtypes = [ctypes.c_uint]
            feature.restype = ctypes.c_int
            for name, identifier in WINDOWS_FEATURES.items():
                result["cpu_flags"][name] = step(
                    f"CPU flag {name}", lambda: bool(feature(identifier))
                )

        step("Windows CPU flags", windows_flags)
        result["notes"].append("Windows feature API cannot determine fma, f16c or VNNI flags")
        result["power_plan"] = step("power plan", _power_plan)
    elif result["os"] == "Linux":
        info = step("Linux CPU info", _linux_cpuinfo)
        if info is not None:
            result.update(info)
    else:
        result["notes"].append("CPU flags are unavailable on this OS")

    devices = step("Vulkan", _vulkan_devices)
    if devices is not None:
        result["vulkan_devices"] = devices
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print pretty JSON")
    args = parser.parse_args(argv)
    facts = probe()
    print(json.dumps(facts, indent=2) if args.json else facts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
