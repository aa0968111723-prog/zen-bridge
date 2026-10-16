"""Tests for tools/vk_layer_probe.py (queue item 30 / round4 D7)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "vk_layer_probe.py"

# Import as a plain module from tools/ (same pattern as other offline tools).
sys.path.insert(0, str(ROOT / "tools"))
import vk_layer_probe as probe  # noqa: E402


def _manifest(
    *,
    name: str,
    library_path: str = "layer.dll",
    description: str = "",
    api_version: str = "1.3.0",
) -> dict:
    return {
        "file_format_version": "1.0.0",
        "layer": {
            "name": name,
            "type": "GLOBAL",
            "library_path": library_path,
            "api_version": api_version,
            "implementation_version": "1",
            "description": description,
        },
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_classify_obs_steam_rtss():
    assert any(
        h["tag"] == "OBS"
        for h in probe.classify_overlay(
            name="VK_LAYER_OBS_HOOK", library_path=r"C:\Program Files\obs-studio\graphics-hook64.dll"
        )
    )
    assert any(
        h["tag"] == "Steam"
        for h in probe.classify_overlay(description="Steam Overlay Vulkan Layer")
    )
    assert any(
        h["tag"] == "RTSS"
        for h in probe.classify_overlay(file_path=r"C:\Program Files\RivaTuner Statistics Server\RTSSVkLayer64.json")
    )


def test_classify_discord_nvidia_reshade_specialk():
    tags = {
        hit["tag"]
        for text, _ in (
            ("Discord Overlay", "Discord"),
            ("NVIDIA GeForce Experience Overlay", "NVIDIA"),
            ("ReShade", "ReShade"),
            ("Special K", "SpecialK"),
        )
        for hit in probe.classify_overlay(description=text)
    }
    assert tags == {"Discord", "NVIDIA", "ReShade", "SpecialK"}


def test_parse_manifest_ok_and_overlay(tmp_path: Path):
    path = _write(
        tmp_path / "obs.json",
        _manifest(name="VK_LAYER_OBS_hook", library_path="graphics-hook64.dll", description="OBS"),
    )
    rows = probe.parse_manifest(path)
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert rows[0]["is_known_overlay"] is True
    assert rows[0]["overlays"][0]["tag"] == "OBS"


def test_parse_manifest_layers_array(tmp_path: Path):
    path = _write(
        tmp_path / "multi.json",
        {
            "file_format_version": "1.0.1",
            "layers": [
                _manifest(name="VK_LAYER_plain")["layer"],
                _manifest(name="VK_LAYER_MESA_device_select")["layer"],
            ],
        },
    )
    rows = probe.parse_manifest(path)
    assert len(rows) == 2
    assert all(r["ok"] for r in rows)
    assert all(not r["is_known_overlay"] for r in rows)


def test_parse_invalid_json(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    rows = probe.parse_manifest(path)
    assert rows[0]["ok"] is False
    assert rows[0]["error"] == "unreadable_or_invalid_json"


def test_probe_from_layer_dir_flags_steam(tmp_path: Path):
    layer_dir = tmp_path / "implicit_layer.d"
    layer_dir.mkdir()
    _write(
        layer_dir / "steam.json",
        _manifest(
            name="VK_LAYER_VALVE_steam_overlay_64",
            library_path="SteamOverlayVulkanLayer64.dll",
            description="Steam Overlay",
        ),
    )
    _write(
        layer_dir / "harmless.json",
        _manifest(name="VK_LAYER_LUNARG_api_dump", description="API dump"),
    )
    result = probe.probe(
        layer_dirs=[layer_dir],
        include_defaults=False,
        registry_lister=lambda: [],
        system="Linux",
    )
    assert result["tool"] == "vk_layer_probe"
    assert result["available"] is True
    assert result["layer_count"] == 2
    assert result["known_overlay_count"] == 1
    assert result["known_overlays"][0]["overlays"][0]["tag"] == "Steam"
    assert any("Steam" in line or "覆蓋層" in line for line in result["summary_zh"])


def test_probe_explicit_manifest_no_defaults(tmp_path: Path):
    path = _write(
        tmp_path / "rtss.json",
        _manifest(name="VK_LAYER_RTSS", library_path="RTSSVkLayer64.dll"),
    )
    result = probe.probe(
        manifests=[path],
        include_defaults=False,
        registry_lister=None,
        system="Linux",
    )
    assert result["layer_count"] == 1
    assert result["known_overlays"][0]["overlays"][0]["tag"] == "RTSS"


def test_probe_graceful_when_nothing_found(tmp_path: Path):
    missing = tmp_path / "nope"
    result = probe.probe(
        layer_dirs=[missing],
        include_defaults=False,
        registry_lister=None,
        system="Linux",
    )
    assert result["layer_count"] == 0
    assert result["available"] is False
    assert any("無法讀取" in line or "未發現" in line for line in result["summary_zh"])


def test_probe_empty_existing_dir_is_available(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = probe.probe(
        layer_dirs=[empty],
        include_defaults=False,
        registry_lister=None,
        system="Linux",
    )
    assert result["available"] is True
    assert result["layer_count"] == 0
    assert any(n.startswith("layer_dir_empty:") for n in result["notes"])


def test_registry_lister_injection(tmp_path: Path):
    path = _write(
        tmp_path / "discord.json",
        _manifest(name="VK_LAYER_Discord_Overlay", description="Discord Overlay"),
    )

    def fake_registry():
        return [str(path)]

    result = probe.probe(
        include_defaults=False,
        registry_lister=fake_registry,
        system="Windows",
    )
    assert result["os"] == "Windows"
    assert result["layer_count"] == 1
    assert result["known_overlays"][0]["overlays"][0]["tag"] == "Discord"


def test_list_windows_registry_layer_paths_mocked():
    class FakeKey:
        def Close(self):
            return None

    values = {
        ("HKLM", r"SOFTWARE\Khronos\Vulkan\ImplicitLayers"): [
            (r"C:\layers\obs.json", 0, 4),
            (r"C:\layers\steam.json", 0, 4),
        ],
        ("HKLM", r"SOFTWARE\WOW6432Node\Khronos\Vulkan\ImplicitLayers"): [],
        ("HKCU", r"SOFTWARE\Khronos\Vulkan\ImplicitLayers"): [
            (r"C:\layers\obs.json", 0, 4),  # duplicate
        ],
        ("HKCU", r"SOFTWARE\WOW6432Node\Khronos\Vulkan\ImplicitLayers"): [],
    }

    def open_key(hive, subkey):
        return FakeKey()

    def query_info(key):
        # We cannot see hive/subkey on FakeKey; use a side channel via enum order.
        return (0, 0)

    # Simpler approach: custom open_key that tags the key
    class TaggedKey:
        def __init__(self, hive, subkey):
            self.hive = hive
            self.subkey = subkey

        def Close(self):
            return None

    def open_key2(hive, subkey):
        return TaggedKey(hive, subkey)

    def query_info2(key):
        return (0, len(values[(key.hive, key.subkey)]))

    def enum_value2(key, index):
        return values[(key.hive, key.subkey)][index]

    paths = probe.list_windows_registry_layer_paths(
        open_key=open_key2,
        enum_value=enum_value2,
        query_info=query_info2,
        hives=("HKLM", "HKCU"),
    )
    assert paths == [r"C:\layers\obs.json", r"C:\layers\steam.json"]


def test_cli_json_stdout_and_file(tmp_path: Path):
    layer_dir = tmp_path / "d"
    layer_dir.mkdir()
    _write(layer_dir / "x.json", _manifest(name="VK_LAYER_noop"))
    out = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--no-defaults",
            "--layer-dir",
            str(layer_dir),
            "--json",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["layer_count"] == 1
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["layer_count"] == 1


def test_cli_refuses_overwrite(tmp_path: Path):
    existing = tmp_path / "out.json"
    existing.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--no-defaults",
            "--manifest",
            str(existing),  # invalid as layer but path exists for --json target
            "--json",
            str(existing),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "refusing to overwrite" in completed.stderr


def test_cli_graceful_skip_on_linux_defaults():
    """No Vulkan SDK on the box → exit 0 with available false or empty layers."""
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--no-defaults"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["tool"] == "vk_layer_probe"
    assert payload["layer_count"] == 0


def test_main_helper_returns_zero(tmp_path: Path, capsys):
    layer_dir = tmp_path / "d"
    layer_dir.mkdir()
    _write(layer_dir / "y.json", _manifest(name="VK_LAYER_y"))
    code = probe.main(["--no-defaults", "--layer-dir", str(layer_dir)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["layer_count"] == 1


def test_default_layer_dirs_windows_and_linux():
    win = probe.default_layer_dirs("Windows")
    assert any("Vulkan" in str(p) or "vulkan" in str(p) for p in win)
    linux = probe.default_layer_dirs("Linux")
    assert any(str(p).endswith("implicit_layer.d") for p in linux)


def test_advice_mentions_disable_env_when_overlay_present(tmp_path: Path):
    path = _write(
        tmp_path / "obs.json",
        _manifest(name="VK_LAYER_OBS", library_path="graphics-hook64.dll"),
    )
    result = probe.probe(manifests=[path], include_defaults=False, system="Windows")
    assert "VK_LOADER_LAYERS_DISABLE" in result["advice_zh"]
    assert "OBS" in result["advice_zh"] or any("OBS" in s for s in result["summary_zh"])