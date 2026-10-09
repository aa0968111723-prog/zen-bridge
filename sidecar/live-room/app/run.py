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
    os.chdir(ROOT)
    fill_process_environ()
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
    print(f"字幕服務啟動中，主持頁：http://127.0.0.1:{settings.port}", flush=True)
    stopped = threading.Event()
    if os.getenv("BREEZE_OPEN_BROWSER") == "1":
        threading.Thread(target=open_when_serving, args=(settings.port, stopped, settings.resident_startup_s + 20), daemon=True).start()
    try:
        # Replay buckets use the TCP peer. The default proxy_headers=True would
        # trust X-Forwarded-For from loopback and split one local client into many.
        uvicorn.run("app.server:app", host="0.0.0.0", port=settings.port, proxy_headers=False)
    finally:
        stopped.set()


if __name__ == "__main__":
    main()
