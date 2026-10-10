"""QA 效能長 P0-6 / P0-7: the embedding gate must fail closed and use a sustained window."""
import socket
import urllib.error

import pytest

from app import embed
from app.admin.live_client import LiveDown, LiveRefused, LiveRoomClient, live_url_from_env

IDLE = {"pending": 0, "inflight": 0, "translate_queued": 0, "translate_busy": 0, "backlog_s": 0.0}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _periodic_gate(clock, busy_s=3.0, period_s=6.0, idle_s=10.0):
    def probe():
        busy = (clock.t % period_s) < busy_s
        return {**IDLE, "pending": 1 if busy else 0}

    def sleep(s):
        clock.t += s
    return embed.IdleGate(probe=probe, idle_s=idle_s, clock=clock, poll_s=1.0, sleep=sleep)


def test_periodic_busy_never_passes_confirm():
    """Every 6 s the ASR is busy for 3 s: sparse checks at 4, 34, 64 ... s used to pass."""
    clock = Clock()
    gate = _periodic_gate(clock)
    for start in range(4, 400, 30):
        clock.t = float(start)
        assert gate.confirm()[0] is False, start


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), LiveDown("breaker open"), LiveDown("live returned 503"),
                                 ValueError("bad json"), RuntimeError("live returned 503")])
def test_probe_errors_are_busy(exc):
    def boom():
        raise exc
    gate = embed.IdleGate(probe=boom, idle_s=0)
    ok, why = gate.check()
    assert ok is False and why.startswith("probe_error")


@pytest.mark.parametrize("exc", [ConnectionRefusedError(), LiveRefused("refused")])
def test_only_refused_means_live_down(exc):
    def boom():
        raise exc
    assert embed.IdleGate(probe=boom).check() == (True, "live_down")


def test_asr_idle_s_inside_window_is_busy():
    gate = embed.IdleGate(probe=lambda: {**IDLE, "asr_idle_s": 4.0}, idle_s=10)
    assert gate.check()[0] is False


def test_listeners_count_as_busy():
    gate = embed.IdleGate(probe=lambda: {**IDLE, "listeners": 3}, idle_s=0)
    assert gate.check()[0] is False


def test_live_started_during_confirm_defers():
    clock = Clock()
    state = {"m": None}

    def sleep(s):
        clock.t += s
        state["m"] = dict(IDLE)          # App comes up mid-window
    gate = embed.IdleGate(probe=lambda: state["m"], idle_s=10, clock=clock, sleep=sleep)
    assert gate.confirm() == (False, "live_started")


def test_sustained_quiet_passes():
    clock = Clock()

    def sleep(s):
        clock.t += s
    gate = embed.IdleGate(probe=lambda: {**IDLE, "asr_idle_s": 999.0}, idle_s=10, clock=clock, sleep=sleep)
    clock.t = 0
    assert gate.confirm()[0] is False      # first look starts the window
    clock.t = 10
    assert gate.confirm() == (True, "idle")


def test_client_distinguishes_refused_from_timeout():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                   # nothing listening -> refused
    client = LiveRoomClient(f"http://127.0.0.1:{port}", sleeper=lambda _s: None)
    assert client.port_refused() is True
    with pytest.raises(LiveRefused):
        client.metrics()

    class TimeoutOpener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))
    c2 = LiveRoomClient("http://127.0.0.1:8780", sleeper=lambda _s: None)
    c2._opener = TimeoutOpener()
    with pytest.raises(LiveDown) as ei:
        c2.metrics()
    assert not isinstance(ei.value, LiveRefused)


def test_live_url_from_env():
    assert live_url_from_env({}) == "http://127.0.0.1:8780"
    assert live_url_from_env({"BREEZE_PORT": "18893"}) == "http://127.0.0.1:18893"
    assert live_url_from_env({"ZEN_LIVE_URL": "http://localhost:9000/"}) == "http://localhost:9000"
    with pytest.raises(Exception):
        live_url_from_env({"ZEN_LIVE_URL": "http://127.0.0.1:8645"})
    with pytest.raises(Exception):
        live_url_from_env({"ZEN_LIVE_URL": "http://example.com:80"})
