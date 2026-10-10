"""Persistent rotating log files for the live-room and admin processes (QA CTO-07).

%LOCALAPPDATA%\\ZenBridge\\logs\\<name>.log (ZEN_DATA_DIR or ZEN_LOG_DIR override), 5 MB x 5,
INFO and above, with the same secret-redaction filter as the admin console handlers.
ZEN_LOG=off disables it. Never raises: a log that cannot be opened only prints a warning.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MARK = "_zen_file_log"


def log_dir(env=None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get("ZEN_LOG_DIR") or "").strip()
    if raw:
        return Path(raw)
    base = (env.get("ZEN_DATA_DIR") or "").strip()
    if base:
        return Path(base) / "logs"
    if (env.get("LOCALAPPDATA") or "").strip():
        return Path(env["LOCALAPPDATA"]) / "ZenBridge" / "logs"
    return Path.home() / ".local" / "share" / "ZenBridge" / "logs"


def install_file_log(name: str, env=None, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> Path | None:
    env = os.environ if env is None else env
    if (env.get("ZEN_LOG") or "").strip().lower() in ("off", "0", "none"):
        return None
    path = log_dir(env) / f"{name}.log"
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, _MARK, None) == str(path):
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    except OSError as exc:
        print(f"警告：無法開啟 log 檔 {path}（{exc}），只輸出到視窗")
        return None
    setattr(handler, _MARK, str(path))
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    try:
        from app.admin.security import _HANDLER_FILTER
        handler.addFilter(_HANDLER_FILTER)
    except Exception:          # pragma: no cover - security module always ships
        pass
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path
