"""Check whether a local benchmark's power and CPU conditions are trustworthy."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess

POWER_SAVER_GUID = "a1841308-3541-4fab-bc81-f71556f20b4a"
POWER_EFFICIENCY_OVERLAY_GUID = "961cc777-2547-4f9d-8174-7d86181b8a7a"

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


def _read_power_mode_overlay():
    if not _is_windows():
        return None, "power mode overlay unknown (Windows only)"
    try:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        overlay = GUID()
        result = ctypes.windll.powrprof.PowerGetEffectiveOverlayScheme(ctypes.byref(overlay))
        if result != 0:
            return None, "power mode overlay unknown (Windows API failed)"
        data4 = bytes(overlay.Data4)
        value = (
            f"{overlay.Data1:08x}-{overlay.Data2:04x}-{overlay.Data3:04x}-"
            f"{data4[0]:02x}{data4[1]:02x}-{''.join(f'{byte:02x}' for byte in data4[2:])}"
        )
        return value, None
    except Exception as exc:
        return None, f"power mode overlay unknown ({type(exc).__name__})"


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


def _read_cpu_percent(interval):
    if psutil is None or not hasattr(psutil, "cpu_percent"):
        return None, "background CPU load unknown (psutil unavailable)"
    try:
        return float(psutil.cpu_percent(interval=interval)), None
    except Exception as exc:
        return None, f"background CPU load unknown ({type(exc).__name__})"


def check(
    *,
    battery=None,
    power_plan=None,
    battery_saver=None,
    cpu_percent=None,
    max_cpu_percent=25,
    cpu_interval=0.5,
) -> dict:
    """Return whether power conditions and background load support a benchmark."""
    reasons = []
    unknown = []
    details = {}

    def add_unknown(reason):
        reasons.append(reason)
        unknown.append(reason)

    if battery is None:
        battery, note = _read_battery()
        if note:
            add_unknown(note)
    elif isinstance(battery, bool):
        pass
    else:
        try:
            plugged = battery.power_plugged
            if isinstance(plugged, bool):
                battery = not plugged
            else:
                battery = None
                add_unknown("battery state unknown (invalid battery value)")
        except Exception:
            battery = None
            add_unknown("battery state unknown (invalid battery value)")
    details["on_battery"] = battery
    if battery is True:
        reasons.append("running on battery")

    if power_plan is None:
        power_plan, note = _read_power_plan()
        if note:
            add_unknown(note)
    elif not isinstance(power_plan, str) or not power_plan.strip():
        power_plan = None
        add_unknown("power plan unknown (invalid value)")
    details["power_plan"] = power_plan
    if isinstance(power_plan, str) and any(
        phrase in power_plan.casefold()
        for phrase in ("power saver", "省電", "省电", POWER_SAVER_GUID)
    ):
        reasons.append("power plan is Power saver")

    power_mode_overlay, note = _read_power_mode_overlay()
    if note:
        add_unknown(note)
    details["power_mode_overlay"] = power_mode_overlay
    if (
        isinstance(power_mode_overlay, str)
        and POWER_EFFICIENCY_OVERLAY_GUID in power_mode_overlay.casefold()
    ):
        reasons.append("power mode is Best power efficiency")

    if battery_saver is None:
        battery_saver, note = _read_battery_saver()
        if note:
            add_unknown(note)
    elif not isinstance(battery_saver, bool):
        battery_saver = None
        add_unknown("battery saver state unknown (invalid value)")
    details["battery_saver"] = battery_saver
    if battery_saver is True:
        reasons.append("battery saver is on")

    if cpu_percent is None:
        cpu_percent, note = _read_cpu_percent(cpu_interval)
        if note:
            add_unknown(note)
    else:
        try:
            cpu_percent = float(cpu_percent)
        except (TypeError, ValueError):
            cpu_percent = None
            add_unknown("background CPU load unknown (invalid value)")
    details["cpu_percent"] = cpu_percent
    if cpu_percent is not None and cpu_percent > max_cpu_percent:
        reasons.append(f"background CPU load is high ({cpu_percent:g}%)")

    trustworthy = not any(
        reason in reasons
        for reason in (
            "running on battery",
            "power plan is Power saver",
            "power mode is Best power efficiency",
            "battery saver is on",
        )
    ) and not (cpu_percent is not None and cpu_percent > max_cpu_percent)
    return {"trustworthy": trustworthy, "reasons": reasons, "unknown": unknown, "details": details}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check benchmark power conditions.")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="exit with status 2 if untrustworthy")
    args = parser.parse_args(argv)

    result = check()
    unknown_windows_power = _is_windows() and (
        result["details"].get("on_battery") is None
        or result["details"].get("power_plan") is None
    )
    strict_failure = not result["trustworthy"] or unknown_windows_power
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result["trustworthy"]:
            status = "NOT TRUSTWORTHY"
        elif args.strict and unknown_windows_power:
            status = "UNKNOWN POWER STATE"
        else:
            status = "TRUSTWORTHY"
        print(status)
        for reason in result["reasons"]:
            print(f"- {reason}")
    return 2 if args.strict and strict_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
