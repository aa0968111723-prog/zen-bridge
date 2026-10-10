import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

POWER_GUARD = Path(__file__).resolve().parents[1] / "tools" / "power_guard.py"
_spec = importlib.util.spec_from_file_location("power_guard", POWER_GUARD)
power_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(power_guard)


def _good(**overrides):
    values = {
        "battery": False,
        "power_plan": "Balanced",
        "power_mode_overlay": "00000000-0000-0000-0000-000000000000",
        "battery_saver": False,
        "cpu_percent": 25,
    }
    values.update(overrides)
    return power_guard.check(**values)


@pytest.mark.parametrize(
    ("input_value", "expected_reason"),
    [
        ({"battery": True}, "running on battery"),
        ({"battery_saver": True}, "battery saver is on"),
        ({"power_plan": "Power Saver"}, "power plan is Power saver"),
        ({"power_plan": f"GUID {power_guard.POWER_SAVER_GUID} (localized name)"},
         "power plan is Power saver"),
        ({"power_plan": "電源プラン (省電力)"}, "power plan is Power saver"),
        ({"power_plan": "電源計劃 (省電)"},
         "power plan is Power saver"),
        ({"cpu_percent": 25.01}, "background CPU load is high"),
    ],
)
def test_each_untrustworthy_condition(input_value, expected_reason):
    result = _good(**input_value)
    assert result["trustworthy"] is False
    assert any(expected_reason in reason for reason in result["reasons"])


def test_balanced_plan_and_cpu_at_threshold_are_trustworthy():
    result = _good()
    assert result["trustworthy"] is True
    assert result["details"]["cpu_percent"] == 25


@pytest.mark.parametrize("windows", [False, True])
def test_injected_inputs_do_not_probe_host(monkeypatch, windows):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: windows)

    def unexpected_probe(*args):
        pytest.fail("injected inputs must not probe the host")

    for probe in (
        "_read_battery",
        "_read_power_plan",
        "_read_power_mode_overlay",
        "_read_battery_saver",
        "_read_cpu_percent",
    ):
        monkeypatch.setattr(power_guard, probe, unexpected_probe)
    result = _good()
    assert result["trustworthy"] is True
    assert result["reasons"] == []
    assert result["unknown"] == []


@pytest.mark.parametrize("overlay", ["", " \t", 123, False])
def test_invalid_injected_overlay_is_reported_without_probing(monkeypatch, overlay):
    def unexpected_probe():
        pytest.fail("invalid injected overlay must not probe the host")

    monkeypatch.setattr(power_guard, "_read_power_mode_overlay", unexpected_probe)
    result = _good(power_mode_overlay=overlay)
    assert result["trustworthy"] is True
    assert result["details"]["power_mode_overlay"] is None
    assert result["unknown"] == ["power mode overlay unknown (invalid value)"]
    assert result["reasons"] == result["unknown"]


def test_unknown_values_are_notes_not_failures(monkeypatch):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: False)

    class NoBattery:
        def sensors_battery(self):
            return None

        def cpu_percent(self, interval):
            assert interval == 0.125
            return 0

    monkeypatch.setattr(power_guard, "psutil", NoBattery())
    result = power_guard.check(cpu_interval=0.125)
    assert result["trustworthy"] is True
    assert result["details"] == {
        "on_battery": None,
        "power_plan": None,
        "power_mode_overlay": None,
        "battery_saver": None,
        "cpu_percent": 0.0,
    }
    assert len(result["reasons"]) == 4
    assert all("unknown" in reason for reason in result["reasons"])
    assert result["unknown"] == result["reasons"]


def test_unknown_battery_source_and_empty_power_plan_are_reported():
    battery = SimpleNamespace(power_plugged=None)
    result = power_guard.check(
        battery=battery,
        power_plan="",
        power_mode_overlay="",
        battery_saver=False,
        cpu_percent=0,
    )
    assert result["trustworthy"] is True
    assert result["details"]["on_battery"] is None
    assert result["details"]["power_plan"] is None
    assert len(result["reasons"]) == 3
    assert all("unknown" in reason for reason in result["reasons"])


def test_power_efficiency_overlay_is_untrustworthy(monkeypatch):
    monkeypatch.setattr(
        power_guard,
        "_read_power_mode_overlay",
        lambda: (power_guard.POWER_EFFICIENCY_OVERLAY_GUID, None),
    )
    result = _good(power_mode_overlay=None)
    assert result["trustworthy"] is False
    assert result["details"]["power_mode_overlay"] == power_guard.POWER_EFFICIENCY_OVERLAY_GUID
    assert "power mode is Best power efficiency" in result["reasons"]


def test_injected_power_efficiency_overlay_is_untrustworthy():
    overlay = power_guard.POWER_EFFICIENCY_OVERLAY_GUID.upper()
    result = _good(power_mode_overlay=overlay)
    assert result["trustworthy"] is False
    assert result["details"]["power_mode_overlay"] == overlay
    assert "power mode is Best power efficiency" in result["reasons"]
    assert result["unknown"] == []


def test_cpu_threshold_and_interval_are_injectable(monkeypatch):
    calls = {}

    class FakePsutil:
        def cpu_percent(self, interval):
            calls["interval"] = interval
            return 30

    monkeypatch.setattr(power_guard, "psutil", FakePsutil())
    result = power_guard.check(
        battery=False,
        power_plan="Balanced",
        power_mode_overlay="00000000-0000-0000-0000-000000000000",
        battery_saver=False,
        cpu_interval=0,
        max_cpu_percent=30,
    )
    assert calls["interval"] == 0
    assert result["trustworthy"] is True


def test_windows_probes_use_powercfg_timeout_and_system_status(monkeypatch):
    calls = {}

    class FakePsutil:
        def sensors_battery(self):
            return SimpleNamespace(power_plugged=True)

        def cpu_percent(self, interval):
            calls["cpu_interval"] = interval
            return 4.5

    class Kernel:
        def GetSystemPowerStatus(self, status_ref):
            calls["status_ref"] = status_ref
            status_ref._obj.SystemStatusFlag = 0
            return 1

    class PowerProfile:
        def PowerGetEffectiveOverlayScheme(self, overlay_ref):
            overlay = overlay_ref._obj
            overlay.Data1 = 0x961CC777
            overlay.Data2 = 0x2547
            overlay.Data3 = 0x4F9D
            overlay.Data4[:] = (0x81, 0x74, 0x7D, 0x86, 0x18, 0x1B, 0x8A, 0x7A)
            return 0

    monkeypatch.setattr(power_guard, "_is_windows", lambda: True)
    monkeypatch.setattr(power_guard, "psutil", FakePsutil())
    monkeypatch.setattr(
        power_guard.ctypes,
        "windll",
        SimpleNamespace(kernel32=Kernel(), powrprof=PowerProfile()),
        raising=False,
    )

    def run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return SimpleNamespace(stdout="Power Scheme GUID: balanced (Balanced)\n")

    monkeypatch.setattr(power_guard.subprocess, "run", run)
    result = power_guard.check()
    assert result["trustworthy"] is False
    assert calls["command"] == ["powercfg", "/getactivescheme"]
    assert calls["kwargs"]["timeout"] == 3
    assert calls["cpu_interval"] == 0.5
    assert result["details"]["battery_saver"] is False
    assert result["details"]["power_mode_overlay"] == power_guard.POWER_EFFICIENCY_OVERLAY_GUID
    assert result["trustworthy"] is False


def test_localized_unknown_power_plan_is_detected_by_guid():
    result = _good(power_plan=f"当前电源方案 GUID: {power_guard.POWER_SAVER_GUID} (未知)")
    assert result["trustworthy"] is False
    assert "power plan is Power saver" in result["reasons"]


def test_probe_errors_are_reported_without_raising(monkeypatch):
    class BrokenPsutil:
        def sensors_battery(self):
            raise OSError("unavailable")

        def cpu_percent(self, interval):
            raise OSError("unavailable")

    monkeypatch.setattr(power_guard, "psutil", BrokenPsutil())
    monkeypatch.setattr(power_guard, "_is_windows", lambda: True)
    monkeypatch.setattr(power_guard.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(
        power_guard.ctypes,
        "windll",
        SimpleNamespace(
            kernel32=SimpleNamespace(GetSystemPowerStatus=lambda status: 0),
            powrprof=SimpleNamespace(PowerGetEffectiveOverlayScheme=lambda overlay: 1),
        ),
        raising=False,
    )
    result = power_guard.check()
    assert result["trustworthy"] is True
    assert len(result["reasons"]) == 5
    assert all("unknown" in reason for reason in result["reasons"])
    assert len(result["unknown"]) == 5


def test_cli_json_and_strict_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(
        power_guard,
        "check",
        lambda: {"trustworthy": False, "reasons": ["running on battery"], "details": {}},
    )
    assert power_guard.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["trustworthy"] is False
    assert power_guard.main(["--strict"]) == 2
    assert "NOT TRUSTWORTHY" in capsys.readouterr().out


def test_cli_strict_succeeds_when_trustworthy(monkeypatch):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: False)
    monkeypatch.setattr(
        power_guard,
        "check",
        lambda: {"trustworthy": True, "reasons": [], "details": {}},
    )
    assert power_guard.main(["--strict"]) == 0


def test_cli_strict_fails_for_unknown_windows_power_state(monkeypatch, capsys):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: True)
    monkeypatch.setattr(
        power_guard,
        "check",
        lambda: {
            "trustworthy": True,
            "reasons": ["battery state unknown", "power plan unknown"],
            "unknown": ["battery state unknown", "power plan unknown"],
            "details": {"on_battery": None, "power_plan": None},
        },
    )
    assert power_guard.main(["--strict", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["unknown"]


def test_cli_strict_allows_unknown_power_state_off_windows(monkeypatch):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: False)
    monkeypatch.setattr(
        power_guard,
        "check",
        lambda: {
            "trustworthy": True,
            "reasons": ["power plan unknown"],
            "unknown": ["power plan unknown"],
            "details": {"on_battery": None, "power_plan": None},
        },
    )
    assert power_guard.main(["--strict"]) == 0
