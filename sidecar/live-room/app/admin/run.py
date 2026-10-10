"""python -m app.admin.run  — start the admin backend on 127.0.0.1:8791 (only)."""
from __future__ import annotations

import os
import sys

from app.admin.security import LOOPBACK_HOSTS, RESERVED_PORTS


def resolve_bind(env: dict | None = None) -> tuple[str, int]:
    env = os.environ if env is None else env
    host = (env.get("ZEN_ADMIN_HOST") or "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"ZEN_ADMIN_HOST 只能是 loopback（127.0.0.1 / ::1 / localhost），拒絕 {host}")
    try:
        port = int((env.get("ZEN_ADMIN_PORT") or "8791").strip())
    except ValueError:
        raise SystemExit("ZEN_ADMIN_PORT 必須是數字")
    if port in RESERVED_PORTS:
        raise SystemExit(f"埠 {port} 保留給 Hermes，請換埠")
    if not (1024 <= port <= 65535):
        raise SystemExit("ZEN_ADMIN_PORT 必須在 1024–65535")
    bind = "::1" if host == "::1" else "127.0.0.1"
    return bind, port


def bind_socket(bind: str, port: int):
    """QA 後端 B3: take the port BEFORE the app (and its job worker) starts, so a second
    instance exits without claiming jobs. Windows: SO_EXCLUSIVEADDRUSE; never SO_REUSEADDR there."""
    import socket
    fam = socket.AF_INET6 if ":" in bind else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # POSIX: only skips TIME_WAIT
        sock.bind((bind, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise SystemExit(f"埠 {port} 已被占用：可能已經有一個後台在執行（http://127.0.0.1:{port}/admin）。這個實例不會啟動，也不會碰任何工作。")
    sock.set_inheritable(False)
    return sock


def main(argv: list[str] | None = None) -> None:
    bind, port = resolve_bind()
    # Codex 2569a06 hold: embeddings stay off until the idle-gate fix passes on-device QA.
    # Refuse before binding the port, so a refused start never claims 8791 or a job.
    if (os.getenv("ZEN_EMBED") or "0").strip() == "1":
        raise SystemExit('Embedding 暫停：閒置判斷 QA 修正與實機驗收尚未完成。')
    sock = bind_socket(bind, port)
    from app.logfile import install_file_log
    install_file_log("admin")                      # QA CTO-07: persistent rotating log
    import uvicorn

    from app.admin.db import data_dir, default_db_path, identity_db_path
    from app.admin.live_client import LiveRoomClient, live_url_from_env
    from app.admin.security import load_or_create_token
    from app.admin.server import create_admin_app

    token_file = data_dir() / "admin.token"
    digest, plain = load_or_create_token(token_file)
    live = LiveRoomClient(live_url_from_env())
    # Codex 2569a06 hold: no embedding handler, no embed scan (ZEN_EMBED=1 is refused above).
    # CTO-03: automatic backup every ZEN_BACKUP_INTERVAL_H hours (default 24, 0 = off).
    hours = float(os.getenv("ZEN_BACKUP_INTERVAL_H") or 24)
    app = create_admin_app(default_db_path(), token_hash_hex=digest, port=port, identity_path=identity_db_path(),
                           live_client=live, embed_handler=None, embed_scan_s=0.0,
                           backup_scan_s=3600.0, backup_every_s=max(hours, 1.0) * 3600 if hours > 0 else float("inf"),
                           # DBA D4: retention runs daily unless ZEN_RETENTION=0
                           metrics_sample_s=float(os.getenv("ZEN_METRICS_SAMPLE_S") or 15),
                           retention_every_s=86400.0 if (os.getenv("ZEN_RETENTION") or "0").strip() == "1" else 0.0)
    code_url = f"http://127.0.0.1:{port}/admin/login#code={app.state.codes.issue()}"
    print(f"Zen 後台：http://127.0.0.1:{port}/admin", file=sys.stderr)
    from app.admin.db import backup_dir, same_disk
    if same_disk(default_db_path(), backup_dir()):
        print("警告：備份資料夾和資料庫在同一顆硬碟，硬碟故障會一起遺失。請設 ZEN_BACKUP_DIR 指到外接碟或雲端同步資料夾。",
              file=sys.stderr)
    print(f"一次性登入網址（5 分鐘內有效、只能用一次）：{code_url}", file=sys.stderr)
    if (os.getenv("ZEN_ADMIN_OPEN_BROWSER") or "").strip() == "1":
        import webbrowser
        webbrowser.open(code_url)
    if plain:
        print(f"CLI 權杖（只顯示這一次，請自行保存；檔案只存雜湊）：{plain}", file=sys.stderr)
    config = uvicorn.Config(app, host=bind, port=port, proxy_headers=False, forwarded_allow_ips="", access_log=False,
                            server_header=False, limit_concurrency=int(os.getenv("ZEN_ADMIN_MAX_CONN") or 200),
                            log_level=os.getenv("ZEN_ADMIN_LOG_LEVEL", "info"))
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
