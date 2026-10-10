"""Check whether a local benchmark's power and CPU conditions are trustworthy."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess

try:
    import psutil
except Exception:
    psutil = None


def _is_windows() -> bool:
    return os.name == "nt"


def _read_battery():
    if psutil is None or not hasattr(psutil, "sensors_battery"):
        return None, "psutil battery probe unavailable"
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return None, "battery state unknown (no battery detected)"
        if isinstance(battery.power_plugged, bool):
            return not battery.power_plugged, None
        return None, "battery state unknown (power source unavailable)"
    except Exception as exc:
        return None, f"battery state unknown ({type(exc).__name__})"


def _read_power_plan():
    if not _is_windows():
        return None, "power plan unknown (Windows only)"
    try:
        result = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        plan = result.stdout.strip()
        if not plan:
            return None, "power plan unknown (empty response)"
        return plan, None
    except Exception as exc:
        return None, f"power plan unknown ({type(exc).__name__})"


def _read_battery_saver():
    if not _is_windows():
        return None, "battery saver state unknown (Windows only)"
    try:
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32),
            ]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None, "battery saver state unknown (Windows API failed)"
        flag = status.SystemStatusFlag
        if flag in (0, 1):
            return bool(flag), None
        return None, "battery saver state unknown (Windows API returned an unknown flag)"
    except Exception as exc:
        return None, f"battery saver state unknown ({type(exc).__name__})"


def _read_cpu_percent():
    if psutil is None or not hasattr(psutil, "cpu_percent"):
        return None, "background CPU load unknown (psutil unavailable)"
    try:
        return float(psutil.cpu_percent(interval=0.5)), None
    except Exception as exc:
        return None, f"background CPU load unknown ({type(exc).__name__})"


def check(*, battery=None, power_plan=None, battery_saver=None, cpu_percent=None) -> dict:
    """Return whether power conditions and background load support a benchmark."""
    reasons = []
    details = {}

    if battery is None:
        battery, note = _read_battery()
        if note:
            reasons.append(note)
    elif isinstance(battery, bool):
        pass
    else:
        try:
            plugged = battery.power_plugged
            if isinstance(plugged, bool):
                battery = not plugged
            else:
                battery = None
                reasons.append("battery state unknown (invalid battery value)")
        except Exception:
            battery = None
            reasons.append("battery state unknown (invalid battery value)")
    details["on_battery"] = battery
    if battery is True:
        reasons.append("running on battery")

    if power_plan is None:
        power_plan, note = _read_power_plan()
        if note:
            reasons.append(note)
    elif not isinstance(power_plan, str) or not power_plan.strip():
        power_plan = None
        reasons.append("power plan unknown (invalid value)")
    details["power_plan"] = power_plan
    if isinstance(power_plan, str) and any(
        phrase in power_plan.casefold() for phrase in ("power saver", "省電", "省电")
    ):
        reasons.append("power plan is Power saver")

    if battery_saver is None:
        battery_saver, note = _read_battery_saver()
        if note:
            reasons.append(note)
    elif not isinstance(battery_saver, bool):
        battery_saver = None
        reasons.append("battery saver state unknown (invalid value)")
    details["battery_saver"] = battery_saver
    if battery_saver is True:
        reasons.append("battery saver is on")

    if cpu_percent is None:
        cpu_percent, note = _read_cpu_percent()
        if note:
            reasons.append(note)
    else:
        try:
            cpu_percent = float(cpu_percent)
        except (TypeError, ValueError):
            cpu_percent = None
            reasons.append("background CPU load unknown (invalid value)")
    details["cpu_percent"] = cpu_percent
    if cpu_percent is not None and cpu_percent > 25:
        reasons.append(f"background CPU load is high ({cpu_percent:g}%)")

    trustworthy = not any(
        reason in reasons
        for reason in ("running on battery", "power plan is Power saver", "battery saver is on")
    ) and not (cpu_percent is not None and cpu_percent > 25)
    return {"trustworthy": trustworthy, "reasons": reasons, "details": details}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check benchmark power conditions.")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="exit with status 2 if untrustworthy")
    args = parser.parse_args(argv)

    result = check()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "TRUSTWORTHY" if result["trustworthy"] else "NOT TRUSTWORTHY"
        print(status)
        for reason in result["reasons"]:
            print(f"- {reason}")
    return 2 if args.strict and not result["trustworthy"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
