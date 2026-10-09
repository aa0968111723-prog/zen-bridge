"""Install pinned CPU runtimes without changing user configuration or recordings."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(path: Path, asset: dict) -> bool:
    return path.is_file() and path.stat().st_size == asset["size"] and sha256(path) == asset["sha256"]


def download_verified(asset: dict, target: Path, opener=None) -> Path:
    """Resume a bounded download; publish only after exact size and SHA256 match."""
    if not asset["url"].startswith("https://"):
        raise ValueError("下載來源必須使用 HTTPS")
    target.parent.mkdir(parents=True, exist_ok=True)
    if verified(target, asset):
        return target
    part = target.with_name(target.name + ".part")
    if verified(part, asset):
        part.replace(target)
        return target
    if part.exists() and part.stat().st_size >= asset["size"]:
        part.unlink()
    open_url = opener or urllib.request.urlopen
    for attempt in range(2):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "breeze-live-room-installer"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(asset["url"], headers=headers)
        try:
            with open_url(req, timeout=60) as response:
                status = response.status
                if status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-") or not content_range.endswith(f"/{asset['size']}"):
                        raise RuntimeError("伺服器回傳錯誤的續傳範圍")
                    mode = "ab"
                elif status == 200:
                    offset, mode = 0, "wb"
                else:
                    raise RuntimeError(f"下載伺服器回應 {status}")
                total = offset
                last_progress = offset
                with part.open(mode) as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > asset["size"]:
                            raise RuntimeError("下載內容超過預期大小")
                        stream.write(block)
                        if total - last_progress >= 50 * 1024 * 1024:
                            print(f"下載 {target.name}：{total * 100 // asset['size']}%", flush=True)
                            last_progress = total
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset and attempt == 0:
                part.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"下載失敗：HTTP {exc.code}；可重新執行 install.bat") from exc
    if not verified(part, asset):
        if part.exists() and part.stat().st_size >= asset["size"]:
            part.unlink()
        raise RuntimeError("下載尚未完整或 SHA256 不符；既有檔案未被取代")
    part.replace(target)
    return target


def extract_binaries(archive: Path, destination: Path) -> None:
    """Keep executable/DLL siblings together; never extract arbitrary ZIP paths."""
    destination.mkdir(parents=True, exist_ok=True)
    names = set()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename or ":" in member.filename:
                raise RuntimeError("ZIP 包含不允許的路徑")
            if member.is_dir() or path.suffix.lower() not in {".exe", ".dll"}:
                continue
            name = path.name
            if name.casefold() in names:
                raise RuntimeError("ZIP 內有重複的程式或 DLL 名稱")
            names.add(name.casefold())
            with source.open(member) as incoming, (destination / name).open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def install_shortcut(root, runner=subprocess.run) -> int:
    """Create desktop shortcuts. Failure is non-fatal unless BREEZE_REQUIRE_SHORTCUT=1."""
    if os.getenv("BREEZE_SKIP_SHORTCUT") == "1":
        return 0
    try:
        shortcut = runner(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\\install-shortcut.ps1"],
            cwd=root,
        )
    except (FileNotFoundError, OSError):
        print("無法執行 powershell.exe。")
        print("桌面捷徑未建立，仍可雙擊 start.bat。")
        if os.getenv("BREEZE_REQUIRE_SHORTCUT") == "1":
            return 1
        return 0
    if shortcut.returncode:
        print("桌面捷徑未建立，仍可雙擊 start.bat。")
        if os.getenv("BREEZE_REQUIRE_SHORTCUT") == "1":
            return shortcut.returncode or 1
    return 0


def main() -> int:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        print("此安裝入口適用 Windows 64 位元 Intel／AMD 電腦。")
        return 1
    sys.path.insert(0, str(ROOT))
    from app.settings import Settings, fill_process_environ
    fill_process_environ(ROOT / ".env")
    settings = Settings.from_env()
    if port_in_use(settings.port):
        print(f"埠 {settings.port} 已被使用，請先關閉字幕程式或在 .env 更改 BREEZE_PORT。")
        return 1
    if shutil.disk_usage(ROOT).free < 5 * 1024 ** 3:
        print("安裝需要至少 5GB 可用空間。")
        return 1
    manifest = json.loads((ROOT / "runtime-manifest.json").read_text(encoding="utf-8"))
    cache = ROOT / ".downloads"
    assets = manifest["assets"]
    print("下載並核對 Breeze 模型、whisper.cpp CPU 套件及 ffmpeg；第一次約 1.2GB。", flush=True)
    model = download_verified(assets["model"], ROOT / "models" / assets["model"]["name"])
    model.with_suffix(model.suffix + ".sha256").write_text(assets["model"]["sha256"] + "\n", encoding="ascii")
    archives = {}
    for key in ("whisper", "ffmpeg"):
        print(f"準備 {key} {assets[key]['version']}…", flush=True)
        archives[key] = download_verified(assets[key], cache / assets[key]["name"])
    tools = ROOT / "tools"
    with tempfile.TemporaryDirectory(prefix="runtime-", dir=cache) as work:
        stage = Path(work) / "tools"
        stage.mkdir()
        for archive in archives.values():
            extract_binaries(archive, stage)
        for name, args in (("whisper-cli.exe", ["--help"]), ("whisper-server.exe", ["--help"]), ("ffmpeg.exe", ["-version"])):
            result = subprocess.run([str(stage / name), *args], capture_output=True, timeout=20)
            if result.returncode != 0:
                raise RuntimeError(f"{name} 無法執行（代碼 {result.returncode}）；既有 tools 未變更")
        backup = cache / ("previous-tools-" + uuid.uuid4().hex[:12])
        if tools.exists():
            tools.replace(backup)
        try:
            stage.replace(tools)
        except BaseException:
            if backup.exists():
                backup.replace(tools)
            raise
        if backup.exists():
            print("舊工具已備份到 " + str(backup), flush=True)
    if not (ROOT / ".env").exists():
        shutil.copy2(ROOT / ".env.example", ROOT / ".env")
    print("模型與工具已核對；既有 .env 和逐字稿保留。", flush=True)
    doctor = subprocess.run([sys.executable, "-m", "app.doctor", "--verify-model"], cwd=ROOT)
    if doctor.returncode:
        return doctor.returncode
    shortcut_code = install_shortcut(ROOT)
    if shortcut_code:
        return shortcut_code
    print("安裝及啟動前檢查完成。請雙擊 start.bat，再進行麥克風試錄。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"安裝未完成：{exc}", file=sys.stderr)
        raise SystemExit(1)
