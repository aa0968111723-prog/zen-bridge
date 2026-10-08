"""Build the offline Windows App installer. User data never enters the package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.install_runtime import sha256
from scripts.prepare_desktop import prepare

SETUP_NAME = "Breeze-Live-Room-Setup.exe"


def find_iscc() -> Path:
    candidates = [
        Path(os.environ["INNO_SETUP"]) if os.environ.get("INNO_SETUP") else None,
        ROOT.parent / "inno" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 Inno Setup 編譯器 ISCC.exe")


def stage_python_cache() -> None:
    asset = json.loads((ROOT / "bootstrap-manifest.json").read_text(encoding="utf-8"))["python"]
    cache = ROOT / ".downloads"
    cache.mkdir(exist_ok=True)
    target = cache / ("python-" + asset["version"] + ".zip")
    if target.is_file() and sha256(target) == asset["sha256"]:
        return
    sibling = ROOT.parent / ("python." + asset["version"] + ".zip")
    if sibling.is_file() and sha256(sibling) == asset["sha256"]:
        shutil.copy2(sibling, target)
        return
    if not target.is_file():
        print("Python cache missing; prepare_desktop will download it.", flush=True)


def stage_bootstrapper(payload: Path) -> None:
    spec = json.loads((ROOT / "desktop" / "desktop-manifest.json").read_text(encoding="utf-8"))["bootstrapper"]
    cache = ROOT / ".downloads" / "WebView2Bootstrapper.exe"
    sibling = ROOT.parent / "cache" / "WebView2Bootstrapper.exe"
    source = cache if cache.is_file() else sibling
    if not source.is_file():
        cache.parent.mkdir(exist_ok=True)
        with urllib.request.urlopen(spec['url'], timeout=60) as incoming, cache.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        source = cache
    if source.stat().st_size != spec["size"] or sha256(source) != spec["sha256"]:
        raise RuntimeError("WebView2 bootstrapper is missing or failed checksum")
    destination = payload / "desktop" / "WebView2Bootstrapper.exe"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage_vc_runtime(payload: Path) -> None:
    spec = json.loads((ROOT / 'desktop/desktop-manifest.json').read_text())['vc_runtime']
    cached = ROOT / '.downloads/vc_redist.x64.exe'
    if not cached.is_file():
        with urllib.request.urlopen(spec['url'], timeout=60) as incoming, cached.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    if sha256(cached) != spec['sha256']:
        raise RuntimeError('Microsoft runtime checksum mismatch')
    shutil.copy2(cached, payload / 'desktop/vc_redist.x64.exe')


def clean_user_paths(payload: Path) -> None:
    for name in (".env",):
        path = payload / name
        if path.exists():
            path.unlink()
    for name in ("data", "logs", "tmp", ".updates", ".venv"):
        path = payload / name
        if path.exists():
            shutil.rmtree(path)


def build(payload: Path, output: Path, assets: Path, sdk: Path | None) -> Path:
    os.environ.pop("SSLKEYLOGFILE", None)
    stage_python_cache()
    if payload == ROOT or payload == assets or ROOT.is_relative_to(payload) or assets.is_relative_to(payload):
        raise ValueError('Payload must be a dedicated build directory, separate from source and user installation')
    prepare(payload, assets, sdk)
    stage_bootstrapper(payload)
    stage_vc_runtime(payload)
    clean_user_paths(payload)
    python = next((payload / ".python").glob("*/tools/python.exe"))
    probe = subprocess.run([str(python), "-c", "import fastapi, uvicorn, pywhispercpp, numpy"], cwd=payload)
    if probe.returncode != 0:
        raise RuntimeError("Bundled Python cannot import the App dependencies")
    compiler = find_iscc()
    output.mkdir(parents=True, exist_ok=True)
    version = (payload / "VERSION").read_text(encoding="utf-8").strip()
    command = [
        str(compiler),
        "/Qp",
        "/DPayloadDir=" + str(payload),
        "/DAppVersion=" + version,
        "/DPythonVersion=" + json.loads((payload / 'bootstrap-manifest.json').read_text())['python']['version'],
        "/DOutputDir=" + str(output),
        str(ROOT / "desktop" / "setup.iss"),
    ]
    subprocess.run(command, check=True)
    installer = output / SETUP_NAME
    if not installer.is_file():
        raise RuntimeError("Installer was not produced")
    digest = sha256(installer)
    (output / (SETUP_NAME + ".sha256")).write_text(digest + "  " + SETUP_NAME + "\n", encoding="ascii")
    print(f"{installer}: SHA256 {digest}", flush=True)
    return installer


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, default=ROOT / "dist" / "payload")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--assets", type=Path, default=ROOT)
    parser.add_argument("--sdk", type=Path)
    args = parser.parse_args()
    build(args.payload.resolve(), args.output.resolve(), args.assets.resolve(), args.sdk.resolve() if args.sdk else None)
