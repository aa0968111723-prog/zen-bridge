#!/usr/bin/env python3
"""Probe Windows Vulkan implicit layers (round4 D7); stdlib only, read-only.

  python tools/vk_layer_probe.py [--json OUT] [--layer-dir DIR ...]
  python tools/vk_layer_probe.py --manifest PATH [--json OUT]

Lists Vulkan *implicit* layer manifests and flags known overlay / HUD injectors
that commonly break or slow ggml-vulkan / whisper-server (OBS, Steam Overlay,
RTSS / MSI Afterburner, Discord, NVIDIA Overlay, Special K, ReShade, …).

Discovery order (best-effort; each source missing is noted, never fatal):

1. Explicit ``--manifest`` JSON files (tests / offline fixtures).
2. Explicit ``--layer-dir`` directories (scan ``*.json``).
3. On Windows: registry values under
   ``HKLM/HKCU\\SOFTWARE\\Khronos\\Vulkan\\ImplicitLayers``
   (and WOW6432Node), treated as absolute paths to layer JSON.
4. Common filesystem locations (Windows ProgramData / Linux
   ``/usr/share/vulkan/implicit_layer.d`` and XDG config).

No Vulkan SDK, ``vulkaninfo``, or network is required. When nothing is
readable the tool exits 0 with ``available=false`` and a Traditional Chinese
note (graceful skip). JSON goes to stdout; ``--json`` also writes a *new*
file (never overwrites). Exit: 0 always for a successful probe (including
empty / unavailable); 2 for bad options / unreadable forced inputs.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Callable, Iterable

TOOL = "vk_layer_probe"

# (tag, Traditional Chinese label, compiled patterns matched against
# layer name / description / library path / file path — case-insensitive).
KNOWN_OVERLAYS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "OBS",
        "OBS Studio 圖形攔截（常與 Vulkan 衝突）",
        re.compile(r"obs|graphics-hook|obs-vulkan", re.I),
    ),
    (
        "Steam",
        "Steam Overlay",
        re.compile(r"steam|steamoverlay|GameOverlayRenderer", re.I),
    ),
    (
        "RTSS",
        "RivaTuner Statistics Server / MSI Afterburner OSD",
        re.compile(r"rtss|rivatuner|afterburner|RTSSHooks", re.I),
    ),
    (
        "Discord",
        "Discord Overlay",
        re.compile(r"discord", re.I),
    ),
    (
        "NVIDIA",
        "NVIDIA GeForce Experience / ShadowPlay Overlay",
        re.compile(r"nvidia.*(overlay|share|shadow)|NvOH|GFExperience", re.I),
    ),
    (
        "SpecialK",
        "Special K 注入層",
        re.compile(r"special.?k|SpecialK", re.I),
    ),
    (
        "ReShade",
        "ReShade",
        re.compile(r"reshade", re.I),
    ),
    (
        "XboxGameBar",
        "Xbox Game Bar",
        re.compile(r"gamebar|xbox.*overlay", re.I),
    ),
)

WINDOWS_REGISTRY_KEYS = (
    r"SOFTWARE\Khronos\Vulkan\ImplicitLayers",
    r"SOFTWARE\WOW6432Node\Khronos\Vulkan\ImplicitLayers",
)

WINDOWS_DIR_CANDIDATES = (
    r"%ProgramData%\Vulkan\ImplicitLayer",
    r"%ProgramData%\vulkan\implicit_layer.d",
    r"%LOCALAPPDATA%\Vulkan\ImplicitLayer",
)

LINUX_DIR_CANDIDATES = (
    "/etc/vulkan/implicit_layer.d",
    "/usr/share/vulkan/implicit_layer.d",
    "/usr/local/share/vulkan/implicit_layer.d",
)


RegistryLister = Callable[[], list[str]]


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _xdg_config_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    dirs.append(Path(home) / "vulkan" / "implicit_layer.d")
    for part in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":"):
        part = part.strip()
        if part:
            dirs.append(Path(part) / "vulkan" / "implicit_layer.d")
    return dirs


def default_layer_dirs(system: str | None = None) -> list[Path]:
    system = system or platform.system()
    if system == "Windows":
        return [_expand(p) for p in WINDOWS_DIR_CANDIDATES]
    dirs = [_expand(p) for p in LINUX_DIR_CANDIDATES]
    dirs.extend(_xdg_config_dirs())
    return dirs


def list_windows_registry_layer_paths(
    *,
    open_key=None,
    enum_value=None,
    query_info=None,
    hives=None,
) -> list[str]:
    """Return absolute paths stored as ImplicitLayers registry values.

    Inject ``open_key`` / ``enum_value`` / ``query_info`` / ``hives`` in tests
    so the box (Linux) never needs ``winreg``.
    """
    if open_key is None:
        try:
            import winreg  # type: ignore
        except ImportError as exc:  # pragma: no cover - non-Windows
            raise RuntimeError(f"winreg unavailable: {exc}") from exc
        open_key = winreg.OpenKey
        enum_value = winreg.EnumValue
        query_info = winreg.QueryInfoKey
        hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)

    paths: list[str] = []
    for hive in hives or ():
        for subkey in WINDOWS_REGISTRY_KEYS:
            try:
                key = open_key(hive, subkey)
            except OSError:
                continue
            try:
                count = query_info(key)[1]  # number of values
                for index in range(count):
                    try:
                        name, _data, _typ = enum_value(key, index)
                    except OSError:
                        break
                    name = str(name).strip()
                    if name:
                        paths.append(name)
            finally:
                try:
                    key.Close()
                except Exception:
                    pass
    # Preserve order, drop duplicates
    return list(dict.fromkeys(paths))


def _load_manifest(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _layer_entries(data: dict) -> list[dict]:
    """Normalize Khronos layer JSON (single layer or layer.library_path list)."""
    if "layer" in data and isinstance(data["layer"], dict):
        return [data["layer"]]
    if "layers" in data and isinstance(data["layers"], list):
        return [item for item in data["layers"] if isinstance(item, dict)]
    # Some manifests put fields at the top level
    if any(k in data for k in ("name", "library_path", "api_version")):
        return [data]
    return []


def classify_overlay(
    *,
    name: str = "",
    description: str = "",
    library_path: str = "",
    file_path: str = "",
) -> list[dict]:
    blob = " ".join([name, description, library_path, file_path])
    hits: list[dict] = []
    for tag, label, pattern in KNOWN_OVERLAYS:
        if pattern.search(blob):
            hits.append({"tag": tag, "label_zh": label})
    return hits


def parse_manifest(path: Path, data: dict | None = None) -> list[dict]:
    data = data if data is not None else _load_manifest(path)
    if data is None:
        return [
            {
                "file": str(path),
                "ok": False,
                "error": "unreadable_or_invalid_json",
                "overlays": [],
            }
        ]
    entries = _layer_entries(data)
    if not entries:
        return [
            {
                "file": str(path),
                "ok": False,
                "error": "no_layer_object",
                "overlays": classify_overlay(file_path=str(path)),
            }
        ]
    out: list[dict] = []
    for layer in entries:
        name = str(layer.get("name") or "")
        description = str(layer.get("description") or "")
        library_path = str(layer.get("library_path") or "")
        api_version = str(layer.get("api_version") or layer.get("apiVersion") or "")
        implementation_version = str(
            layer.get("implementation_version")
            or layer.get("implementationVersion")
            or ""
        )
        overlays = classify_overlay(
            name=name,
            description=description,
            library_path=library_path,
            file_path=str(path),
        )
        out.append(
            {
                "file": str(path),
                "ok": True,
                "name": name or None,
                "description": description or None,
                "library_path": library_path or None,
                "api_version": api_version or None,
                "implementation_version": implementation_version or None,
                "disable_environment": layer.get("disable_environment"),
                "enable_environment": layer.get("enable_environment"),
                "overlays": overlays,
                "is_known_overlay": bool(overlays),
            }
        )
    return out


def collect_manifest_paths(
    *,
    manifests: Iterable[Path] = (),
    layer_dirs: Iterable[Path] = (),
    include_defaults: bool = True,
    registry_lister: RegistryLister | None = None,
    system: str | None = None,
) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    paths: list[Path] = []
    system = system or platform.system()

    for item in manifests:
        paths.append(Path(item))

    dirs = [Path(d) for d in layer_dirs]
    if include_defaults:
        dirs.extend(default_layer_dirs(system))

    for directory in dirs:
        if not directory.is_dir():
            notes.append(f"layer_dir_missing: {directory}")
            continue
        try:
            found = sorted(directory.glob("*.json"))
        except OSError as exc:
            notes.append(f"layer_dir_unreadable: {directory} ({type(exc).__name__})")
            continue
        if not found:
            notes.append(f"layer_dir_empty: {directory}")
        paths.extend(found)

    if registry_lister is None and system == "Windows":
        registry_lister = list_windows_registry_layer_paths

    if registry_lister is not None:
        try:
            for raw in registry_lister():
                paths.append(Path(raw))
        except Exception as exc:
            notes.append(f"registry: {type(exc).__name__}: {exc}")
    elif system == "Windows":
        notes.append("registry: skipped (no lister)")
    else:
        notes.append("registry: skipped (non-Windows)")

    # de-dupe while preserving order
    unique = list(dict.fromkeys(paths))
    return unique, notes


def probe(
    *,
    manifests: Iterable[Path] = (),
    layer_dirs: Iterable[Path] = (),
    include_defaults: bool = True,
    registry_lister: RegistryLister | None = None,
    system: str | None = None,
) -> dict:
    system = system or platform.system()
    paths, notes = collect_manifest_paths(
        manifests=manifests,
        layer_dirs=layer_dirs,
        include_defaults=include_defaults,
        registry_lister=registry_lister,
        system=system,
    )

    layers: list[dict] = []
    for path in paths:
        layers.extend(parse_manifest(path))

    known = [layer for layer in layers if layer.get("is_known_overlay")]
    available = bool(layers) or any(
        n.startswith("layer_dir_empty:") or n.startswith("layer_dir_missing:")
        for n in notes
    )
    # available=true means we could *attempt* discovery; still report layers=[]
    # when the machine simply has no implicit layers installed.
    if not paths and not any(n.startswith("registry:") for n in notes):
        # No sources at all — treat as Vulkan layer info unavailable.
        available = False

    # Refine: if we successfully scanned at least one existing dir or registry
    # returned (even empty via lister without error), available=True.
    scanned = any(
        not n.startswith("layer_dir_missing:")
        and not n.startswith("layer_dir_unreadable:")
        and not n.startswith("registry:")
        for n in notes
    ) or bool(paths)
    if registry_lister is not None or system == "Windows":
        # Windows probe always "available" once registry path attempted,
        # even when empty — unless winreg missing and no dirs.
        if any(n.startswith("registry: skipped") or n.startswith("registry: ") for n in notes):
            if not paths and not scanned:
                available = False
            else:
                available = True
        else:
            available = True
    elif scanned or paths:
        available = True
    else:
        available = False

    summary_zh: list[str] = []
    if not available and not layers:
        summary_zh.append(
            "無法讀取 Vulkan implicit layer（可能未安裝 Vulkan Runtime／無登錄機碼）。"
            "這是安全略過，不代表測試失敗。"
        )
    elif not layers:
        summary_zh.append("未發現任何 Vulkan implicit layer 清單。")
    else:
        summary_zh.append(f"共發現 {len(layers)} 個 implicit layer 項目。")
    if known:
        tags = sorted({hit["tag"] for layer in known for hit in layer["overlays"]})
        summary_zh.append(
            "偵測到可能干擾 ggml-vulkan／whisper 的覆蓋層："
            + "、".join(tags)
            + "。建議關閉對應 Overlay，或維持 VK_LOADER_LAYERS_DISABLE=~implicit~（見 round4 D7）。"
        )
    else:
        summary_zh.append("未標到 OBS／Steam／RTSS 等已知覆蓋層。")

    return {
        "tool": TOOL,
        "os": system,
        "available": available,
        "layer_count": len(layers),
        "known_overlay_count": len(known),
        "layers": layers,
        "known_overlays": known,
        "notes": notes,
        "summary_zh": summary_zh,
        "advice_zh": (
            "若要跑本機 Vulkan ASR／MT：先關掉 OBS／Steam／RTSS Overlay，"
            "或確認程序已設 VK_LOADER_LAYERS_DISABLE=~implicit~。"
            if known
            else "目前未標到常見 Overlay；正式量測前仍建議關閉遊戲內覆蓋層。"
        ),
    }


def _write_json(path: Path, payload: dict) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        metavar="OUT",
        help="also write JSON to this new file (never overwrites)",
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=[],
        metavar="PATH",
        help="explicit layer JSON (repeatable); skips default discovery when set alone with --no-defaults",
    )
    parser.add_argument(
        "--layer-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directory of *.json manifests (repeatable)",
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="do not scan default OS dirs / Windows registry",
    )
    args = parser.parse_args(argv)

    try:
        result = probe(
            manifests=[Path(p) for p in args.manifest],
            layer_dirs=[Path(p) for p in args.layer_dir],
            include_defaults=not args.no_defaults,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"{TOOL}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        try:
            _write_json(Path(args.json), result)
        except FileExistsError as exc:
            print(f"{TOOL}: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"{TOOL}: cannot write {args.json}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())