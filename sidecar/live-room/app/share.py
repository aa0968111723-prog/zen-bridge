from __future__ import annotations

import os
import socket
from urllib.parse import quote


def shareable(ip: str) -> bool:
    if not ip or ip.startswith("127.") or ip.startswith("169.254.") or ip in {"0.0.0.0", "::1"}:
        return False
    return True


def lan_ip() -> str | None:
    override = os.getenv("BREEZE_SHARE_HOST", "").strip()
    if shareable(override):
        return override
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not shareable(ip):
        return None
    return ip


def list_share_hosts() -> list[str]:
    """Candidates a person can pick. VPN, hotspot, and extra NICs all show up; nothing is guessed away except loopback."""
    found: list[str] = []

    def add(ip: str) -> None:
        if shareable(ip) and ip not in found:
            found.append(ip)

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        add(info[4][0])
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(address)
    except OSError:
        pass
    add(lan_ip() or "")
    return found


def listen_url(
    room_id: str,
    port: int,
    scheme: str = "http",
    host: str | None = None,
    listen_key: str | None = None,
) -> str | None:
    ip = host if host else lan_ip()
    if not ip or not shareable(ip):
        return None
    url = f"{scheme}://{ip}:{port}/r/{quote(room_id)}"
    if listen_key:
        url += "?k=" + quote(listen_key, safe="")
    return url


def machine_share_name() -> str | None:
    """round4 #6: this PC's name for the 「用名稱連線」 QR (BREEZE_SHARE_NAME overrides; "off" disables).
    Only plain DNS-label names are used; anything odd -> None (no QR rather than a broken one)."""
    import re
    raw = os.getenv("BREEZE_SHARE_NAME")
    if raw is not None and raw.strip().lower() in {"off", "0", "none"}:
        return None
    name = (raw or socket.gethostname() or "").strip().rstrip(".").lower()
    if not name or len(name) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*", name):
        return None
    if name in {"localhost"}:
        return None
    return name
