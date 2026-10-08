"""Local preflight. No API calls, microphone capture or transcript collection."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from app.settings import Settings, fill_process_environ

ROOT = Path(__file__).resolve().parents[1]


def inspect(settings: Settings, root: Path = ROOT, verify_model: bool = False, probe_port: bool = True) -> dict:
    checks = []
    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    add("python", sys.version_info >= (3, 11), platform.python_version())
    for package in ("fastapi", "uvicorn", "python-multipart", "qrcode", "pillow"):
        try:
            add(package, True, importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            add(package, False, "請執行 install.bat 安裝套件")
    add("asr_mode", settings.asr_mode in {"cli", "resident", "native"}, settings.asr_mode)
    if settings.asr_mode == 'native':
        try:
            add('native_binding', True, importlib.metadata.version('pywhispercpp'))
        except importlib.metadata.PackageNotFoundError:
            add('native_binding', False, '請重新安裝 App 或執行 install.bat')
    model = Path(settings.model_path) if settings.model_path else root / "models" / "ggml-breeze-asr-25-q5_0.bin"
    add("model", model.is_file() and model.stat().st_size > 0, "模型存在" if model.is_file() else "缺少模型，請執行 install.bat")
    if verify_model and model.is_file():
        from scripts.install_runtime import verified
        asset = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))["assets"]["model"]
        add("model_sha256", verified(model, asset), "比對固定 Breeze q5 模型大小與 SHA256")
    whisper = Path(settings.whisper_path) if settings.whisper_path else root / "tools" / "whisper-cli.exe"
    server = Path(settings.server_path) if settings.server_path else root / "tools" / "whisper-server.exe"
    ffmpeg = root / "tools" / "ffmpeg.exe"
    ffmpeg_name = str(ffmpeg) if ffmpeg.is_file() else shutil.which("ffmpeg")
    binaries = [("whisper-cli", str(whisper), ["--help"]), ("ffmpeg", ffmpeg_name, ["-version"])]
    if settings.asr_mode == "resident":
        binaries.append(("whisper-server", str(server), ["--help"]))
    for name, binary, args in binaries:
        if not binary or not Path(binary).is_file():
            add(name, False, "缺少執行檔，請執行 install.bat")
            continue
        try:
            result = subprocess.run([binary, *args], capture_output=True, timeout=15)
            detail = "執行檔可啟動" if result.returncode == 0 else f"無法啟動，代碼 {result.returncode}；請重新安裝工具或檢查 DLL"
            if result.returncode in {-1073741515, 3221225781}:
                detail += "；缺少執行階段時，請安裝 Microsoft Visual C++ x64：https://aka.ms/vc14/vc_redist.x64.exe"
            add(name, result.returncode == 0, detail)
        except (OSError, subprocess.TimeoutExpired):
            add(name, False, "無法啟動或逾時；請重新安裝工具或檢查 DLL")
    paths = [root / "tmp"]
    if settings.data_path:
        database = Path(settings.data_path)
        paths.append((database if database.is_absolute() else root / database).parent)
    for index, path in enumerate(paths):
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b"ok")
            add(f"writable_{index}", True, "暫存／資料目錄可寫入")
        except OSError:
            add(f"writable_{index}", False, "暫存／資料目錄無法寫入")
    if probe_port:
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", settings.port))
                add("port", True, f"埠 {settings.port} 可用")
            except OSError:
                add("port", False, f"埠 {settings.port} 被占用；關閉其他服務或修改 BREEZE_PORT")
    return {"service": "breeze-live-room", "ok": all(row["ok"] for row in checks), "python": platform.python_version(), "platform": platform.system(), "port": settings.port, "checks": checks, "translation_configured": bool(os.getenv("OPENAI_API_KEY")) and settings.translate, "translation_tested": False, "microphone_tested": False, "model_inference_tested": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="字幕服務啟動前檢查；不會傳送逐字稿或呼叫付費 API")
    parser.add_argument("--verify-model", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    fill_process_environ(ROOT / ".env")
    try:
        report = inspect(Settings.from_env(), verify_model=args.verify_model)
    except (OSError, ValueError):
        print("設定或檔案無法讀取，請檢查 .env；不顯示設定值。", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for row in report["checks"]:
            print(f"{'通過' if row['ok'] else '待修正'} {row['name']}：{row['detail']}")
        print("檢查完成。麥克風、模型推論及長時間實測仍需在主持機執行。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
