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


def main(argv: list[str] | None = None) -> None:
    bind, port = resolve_bind()
    import uvicorn

    from app.admin.db import data_dir, default_db_path, identity_db_path
    from app.admin.live_client import LiveDown, LiveRoomClient
    from app.admin.security import load_or_create_token
    from app.admin.server import create_admin_app

    token_file = data_dir() / "admin.token"
    digest, plain = load_or_create_token(token_file)
    live = LiveRoomClient()
    embed_handler = None
    if (os.getenv("ZEN_EMBED") or "1").strip() != "0":
        from app.embed import backfill_handler, embedder_from_env, gate_from_env
        from app.runtime_tuning import env_float, env_int
        from app.admin import db as zdb

        def probe():
            try:
                return live.metrics()
            except LiveDown:
                return None            # live room not running: nothing to compete with
        embedder = embedder_from_env()
        embed_handler = backfill_handler(lambda: zdb.connect(default_db_path()), embedder, gate_from_env(probe),
                                         batch=env_int("ZEN_EMBED_BATCH", 16, minimum=1),
                                         defer_s=env_float("ZEN_EMBED_DEFER_S", 30.0, minimum=1.0))
        embed_handler.model = embedder.model
    app = create_admin_app(default_db_path(), token_hash_hex=digest, port=port, identity_path=identity_db_path(),
                           live_client=live, embed_handler=embed_handler,
                           embed_scan_s=60.0 if embed_handler else 0.0)
    code_url = f"http://127.0.0.1:{port}/admin/login#code={app.state.codes.issue()}"
    print(f"Zen 後台：http://127.0.0.1:{port}/admin", file=sys.stderr)
    print(f"一次性登入網址（5 分鐘內有效、只能用一次）：{code_url}", file=sys.stderr)
    if (os.getenv("ZEN_ADMIN_OPEN_BROWSER") or "").strip() == "1":
        import webbrowser
        webbrowser.open(code_url)
    if plain:
        print(f"CLI 權杖（只顯示這一次，請自行保存；檔案只存雜湊）：{plain}", file=sys.stderr)
    uvicorn.run(app, host=bind, port=port, proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, log_level=os.getenv("ZEN_ADMIN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
