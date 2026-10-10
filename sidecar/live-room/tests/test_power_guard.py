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


def test_unknown_values_are_notes_not_failures(monkeypatch):
    monkeypatch.setattr(power_guard, "_is_windows", lambda: False)

    class NoBattery:
        def sensors_battery(self):
            return None

        def cpu_percent(self, interval):
            assert interval == 0.5
            return 0

    monkeypatch.setattr(power_guard, "psutil", NoBattery())
    result = power_guard.check()
    assert result["trustworthy"] is True
    assert result["details"] == {
        "on_battery": None,
        "power_plan": None,
        "battery_saver": None,
        "cpu_percent": 0.0,
    }
    assert len(result["reasons"]) == 3
    assert all("unknown" in reason for reason in result["reasons"])


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

    monkeypatch.setattr(power_guard, "_is_windows", lambda: True)
    monkeypatch.setattr(power_guard, "psutil", FakePsutil())
    monkeypatch.setattr(power_guard.ctypes, "windll", SimpleNamespace(kernel32=Kernel()), raising=False)

    def run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return SimpleNamespace(stdout="Power Scheme GUID: balanced (Balanced)\n")

    monkeypatch.setattr(power_guard.subprocess, "run", run)
    result = power_guard.check()
    assert result["trustworthy"] is True
    assert calls["command"] == ["powercfg", "/getactivescheme"]
    assert calls["kwargs"]["timeout"] == 3
    assert calls["cpu_interval"] == 0.5
    assert result["details"]["battery_saver"] is False


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
        SimpleNamespace(kernel32=SimpleNamespace(GetSystemPowerStatus=lambda status: 0)),
        raising=False,
    )
    result = power_guard.check()
    assert result["trustworthy"] is True
    assert len(result["reasons"]) == 4
    assert all("unknown" in reason for reason in result["reasons"])


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
    monkeypatch.setattr(
        power_guard,
        "check",
        lambda: {"trustworthy": True, "reasons": [], "details": {}},
    )
    assert power_guard.main(["--strict"]) == 0
