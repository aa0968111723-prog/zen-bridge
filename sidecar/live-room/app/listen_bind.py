"""Bind 8780 to loopback plus the audience address, never every NIC."""
from __future__ import annotations

import socket

from app.net import RESERVED_PORTS
from app.share import lan_ip, machine_share_name, shareable


def audience_hosts(share_host: str = "") -> list[str]:
    """127.0.0.1, the QR address, and the name QR's IPv4. Never 0.0.0.0 or ::."""
    hosts = ["127.0.0.1"]

    def add(ip: str | None) -> None:
        text = (ip or "").strip()
        if shareable(text) and text not in hosts:
            hosts.append(text)

    add(share_host)
    if len(hosts) == 1:
        add(lan_ip())
    name = machine_share_name()
    if name:
        try:
            infos = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            infos = []
        for info in infos:
            add(str(info[4][0]))
    return hosts


def open_listen_sockets(port: int, hosts: list[str]) -> list[socket.socket]:
    if port in RESERVED_PORTS:
        raise SystemExit("不能佔用 8645。")
    sockets: list[socket.socket] = []
    loopback = False
    for host in hosts:
        if host in {"0.0.0.0", "::", ""}:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            sock.close()
            if host == "127.0.0.1":
                raise SystemExit(f"本機 {port} 綁不上，字幕服務無法啟動。")
            print(f"手機觀眾位址 {host} 目前綁不上，這張網卡不開放。", flush=True)
            continue
        sock.listen(2048)
        sock.set_inheritable(True)
        sockets.append(sock)
        if host == "127.0.0.1":
            loopback = True
    if not loopback:
        for sock in sockets:
            sock.close()
        raise SystemExit(f"本機 {port} 綁不上，字幕服務無法啟動。")
    return sockets
