"""Hardware probing never needs real Windows APIs or Vulkan on CI."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("hw_probe", ROOT / "tools" / "hw_probe.py")
assert _spec is not None and _spec.loader is not None
hw_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hw_probe)


@pytest.fixture
def hardware(monkeypatch):
    monkeypatch.setattr(hw_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hw_probe.platform, "python_version", lambda: "3.12.0")
    monkeypatch.setattr(hw_probe.platform, "processor", lambda: "fallback CPU")
    psutil = SimpleNamespace(
        cpu_count=Mock(side_effect=lambda logical: 12 if logical else 6),
        virtual_memory=Mock(
            return_value=SimpleNamespace(total=15.4 * 1024**3, available=8 * 1024**3)
        ),
        sensors_battery=Mock(return_value=SimpleNamespace(power_plugged=True, percent=87.9)),
    )
    monkeypatch.setattr(hw_probe, "psutil", psutil)
    key = Mock()
    key.__enter__ = Mock(return_value=key)
    key.__exit__ = Mock(return_value=False)
    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=Mock(return_value=key),
        QueryValueEx=Mock(return_value=("AMD Ryzen 5 5600H", 1)),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    feature = Mock(side_effect=lambda identifier: identifier in (39, 40))
    monkeypatch.setattr(
        hw_probe.ctypes, "WinDLL",
        Mock(return_value=SimpleNamespace(IsProcessorFeaturePresent=feature)),
        raising=False,
    )
    monkeypatch.setattr(hw_probe.shutil, "which", Mock(return_value="/mock/vulkaninfo"))

    def run(command, **kwargs):
        assert kwargs["timeout"] <= 3
        assert kwargs["check"] is True
        assert kwargs["capture_output"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "shell" not in kwargs
        if command == ["powercfg", "/getactivescheme"]:
            return SimpleNamespace(stdout=(
                "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)\n"
            ))
        assert command == ["/mock/vulkaninfo", "--summary"]
        return SimpleNamespace(stdout=(
            "Devices:\nGPU0:\n\tdeviceName = AMD Radeon Graphics\n"
            "GPU1:\n\tdeviceName = Second GPU\n"
        ))

    runner = Mock(side_effect=run)
    monkeypatch.setattr(hw_probe.subprocess, "run", runner)
    return SimpleNamespace(psutil=psutil, feature=feature, runner=runner, registry=registry)


def test_windows_happy_path(hardware):
    facts = hw_probe.probe()
    assert facts["os"] == "Windows"
    assert facts["python"] == "3.12.0"
    assert facts["cpu_model"] == "AMD Ryzen 5 5600H"
    assert (facts["physical_cores"], facts["logical_cores"]) == (6, 12)
    assert facts["cpu_flags"] == {
        "avx": True, "avx2": True, "fma": None, "f16c": None,
        "avx512f": False, "avx512_vnni": None, "avx_vnni": None,
    }
    assert [call.args[0] for call in hardware.feature.call_args_list] == [39, 40, 41]
    assert facts["ram_total_gb"] == pytest.approx(15.4)
    assert facts["ram_available_gb"] == 8
    assert facts["on_ac_power"] is True
    assert facts["battery_percent"] == 87
    assert facts["power_plan"] == "Balanced"
    assert facts["vulkan_devices"] == ["AMD Radeon Graphics", "Second GPU"]


def test_linux_cpuinfo(hardware, monkeypatch):
    monkeypatch.setattr(hw_probe.platform, "system", lambda: "Linux")
    read = Mock(return_value=(
        "processor : 0\nmodel name : AMD Ryzen 5 5600H\n"
        "flags : fpu avx avx2 fma f16c avx_vnni\n\n"
        "processor : 1\nmodel name : AMD Ryzen 5 5600H\n"
        "flags : fpu avx avx2 fma f16c\n"
    ))
    monkeypatch.setattr(hw_probe.Path, "read_text", read)
    facts = hw_probe.probe()
    assert facts["cpu_model"] == "AMD Ryzen 5 5600H"
    assert facts["cpu_flags"] == {
        "avx": True, "avx2": True, "fma": True, "f16c": True,
        "avx512f": False, "avx512_vnni": False, "avx_vnni": False,
    }
    assert facts["power_plan"] is None
    hardware.feature.assert_not_called()
    hardware.registry.OpenKey.assert_not_called()
    assert hardware.runner.call_count == 1
    read.assert_called_once_with(encoding="utf-8", errors="replace")


def test_vulkaninfo_missing(hardware, monkeypatch):
    monkeypatch.setattr(hw_probe.shutil, "which", Mock(return_value=None))
    facts = hw_probe.probe()
    assert facts["vulkan_devices"] == []
    assert any("vulkaninfo is not on PATH" in note for note in facts["notes"])
    assert hardware.runner.call_count == 1


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("vulkaninfo", 3),
    subprocess.CalledProcessError(1, "vulkaninfo"),
    OSError("cannot execute vulkaninfo"),
])
def test_vulkaninfo_failure(hardware, failure):
    hardware.runner.side_effect = [SimpleNamespace(stdout=(
        "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e (Balanced)"
    )), failure]
    facts = hw_probe.probe()
    assert facts["vulkan_devices"] == []
    assert any("Vulkan:" in note and type(failure).__name__ in note for note in facts["notes"])
    assert facts["logical_cores"] == 12


def test_vulkaninfo_malformed(hardware):
    hardware.runner.return_value = SimpleNamespace(stdout="not a summary")
    hardware.runner.side_effect = None
    facts = hw_probe.probe()
    assert facts["vulkan_devices"] == []
    assert facts["power_plan"] is None
    assert any("no device names" in note for note in facts["notes"])
    assert any("no active scheme" in note for note in facts["notes"])


def test_desktop_battery_none(hardware):
    hardware.psutil.sensors_battery.return_value = None
    facts = hw_probe.probe()
    assert facts["on_ac_power"] is None
    assert facts["battery_percent"] is None
    assert not any(note.startswith("battery:") for note in facts["notes"])


def test_probe_steps_fail_independently(hardware, monkeypatch):
    hardware.psutil.cpu_count.side_effect = RuntimeError("count unavailable")
    hardware.psutil.virtual_memory.side_effect = OSError("RAM unavailable")
    hardware.psutil.sensors_battery.side_effect = OSError("battery unavailable")
    hardware.registry.OpenKey.side_effect = OSError("registry unavailable")
    monkeypatch.setattr(hw_probe.ctypes, "WinDLL", Mock(side_effect=OSError("API unavailable")))
    hardware.runner.side_effect = subprocess.TimeoutExpired("command", 3)
    facts = hw_probe.probe()
    assert facts["cpu_model"] == "fallback CPU"
    assert facts["physical_cores"] is None
    assert facts["logical_cores"] is None
    assert facts["ram_total_gb"] is None
    assert facts["ram_available_gb"] is None
    assert all(value is None for value in facts["cpu_flags"].values())
    for label in ("physical cores", "logical cores", "RAM", "battery",
                  "Windows CPU model", "Windows CPU flags", "power plan", "Vulkan"):
        assert any(note.startswith(label + ":") for note in facts["notes"])
    json.dumps(facts, allow_nan=False)


def test_linux_cpuinfo_failure(hardware, monkeypatch):
    monkeypatch.setattr(hw_probe.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hw_probe.Path, "read_text", Mock(side_effect=OSError("unreadable")))
    facts = hw_probe.probe()
    assert all(value is None for value in facts["cpu_flags"].values())
    assert any("Linux CPU info:" in note for note in facts["notes"])
    assert facts["vulkan_devices"]


def test_json_serialisable_and_cli(hardware, capsys):
    facts = hw_probe.probe()
    assert json.loads(json.dumps(facts, allow_nan=False)) == facts
    assert hw_probe.main(["--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == facts
    assert "\n  \"os\"" in output.out
    assert output.err == ""
    assert hw_probe.main([]) == 0
    assert "'cpu_flags':" in capsys.readouterr().out
