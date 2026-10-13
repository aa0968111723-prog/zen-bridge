"""8780 must not listen on every NIC, and the audience address must stay bindable."""
import socket

import pytest

from app.listen_bind import audience_hosts, open_listen_sockets


def test_audience_hosts_keep_loopback_and_qr_address(monkeypatch):
    monkeypatch.setattr("app.listen_bind.lan_ip", lambda: "10.1.2.3")
    monkeypatch.setattr("app.listen_bind.machine_share_name", lambda: None)
    assert audience_hosts("203.0.113.10") == ["127.0.0.1", "203.0.113.10"]
    assert "0.0.0.0" not in audience_hosts("0.0.0.0")
    assert audience_hosts("") == ["127.0.0.1", "10.1.2.3"]


def test_name_qr_address_is_included(monkeypatch):
    monkeypatch.setattr("app.listen_bind.machine_share_name", lambda: "zenpc")
    monkeypatch.setattr("app.listen_bind.socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("192.168.1.20", 0))])
    hosts = audience_hosts("203.0.113.10")
    assert hosts[0] == "127.0.0.1"
    assert "192.168.1.20" in hosts
    assert "0.0.0.0" not in hosts


def test_open_sockets_skip_a_missing_phone_address():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sockets = open_listen_sockets(port, ["127.0.0.1", "203.0.113.10", "0.0.0.0"])
    try:
        assert [sock.getsockname()[0] for sock in sockets] == ["127.0.0.1"]
        assert sockets[0].getsockname()[1] == port
    finally:
        for sock in sockets:
            sock.close()


def test_reserved_gateway_port_is_refused():
    with pytest.raises(SystemExit, match="8645"):
        open_listen_sockets(8645, ["127.0.0.1"])
