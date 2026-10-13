from __future__ import annotations

import os
import threading
import webbrowser
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from app.settings import Settings, fill_process_environ
from app.doctor import inspect

ROOT = Path(__file__).resolve().parents[1]


def open_when_serving(port: int, stopped: threading.Event, timeout_s: float = 200) -> None:
    deadline = time.monotonic() + timeout_s
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}"
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            try:
                response = opener.open(url + "/api/health", timeout=1)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                data = json.loads(response.read(65536))
            if data.get("service") == "breeze-live-room":
                webbrowser.open(url + "/")
                return
        except (OSError, ValueError, AttributeError):
            pass
        stopped.wait(0.25)


def main() -> None:
    from app.console import safe_console
    safe_console()
    os.chdir(ROOT)
    fill_process_environ()
    from app.vad import enable_verified_silero
    if enable_verified_silero() == "silero":
        print("語音活動偵測：已接上 Silero。沒有開麥克風。", flush=True)
    try:
        settings = Settings.from_env()
        report = inspect(settings)
    except (OSError, ValueError):
        print("設定或資料目錄無法讀取，請檢查 .env 或執行 doctor.bat。")
        raise SystemExit(1)
    if not report["ok"]:
        for check in report["checks"]:
            if not check["ok"]:
                print(f"待修正 {check['name']}：{check['detail']}")
        print("服務尚未啟動；請修正後重跑 start.bat。")
        raise SystemExit(1)
    from app.logfile import install_file_log
    log_path = install_file_log("live-room")      # QA CTO-07: persistent rotating log
    print(f"字幕服務啟動中，主持頁：http://127.0.0.1:{settings.port}", flush=True)
    if log_path:
        print(f"log：{log_path}", flush=True)
    stopped = threading.Event()
    if os.getenv("BREEZE_OPEN_BROWSER") == "1":
        threading.Thread(target=open_when_serving, args=(settings.port, stopped, settings.resident_startup_s + 20), daemon=True).start()
    sockets = None
    try:
        from app.listen_bind import audience_hosts, open_listen_sockets
        sockets = open_listen_sockets(settings.port, audience_hosts(settings.share_host))
        bound = ", ".join(f"{sock.getsockname()[0]}:{sock.getsockname()[1]}" for sock in sockets)
        print(f"字幕服務只聽 {bound}，不對所有網卡開放。", flush=True)
        config = uvicorn.Config("app.server:app", host="127.0.0.1", port=settings.port, proxy_headers=False)
        uvicorn.Server(config).run(sockets=sockets)
    finally:
        stopped.set()
        for sock in sockets or []:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
