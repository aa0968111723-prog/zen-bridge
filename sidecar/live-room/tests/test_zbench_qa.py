"""Regression tests for QA 效能長 P0-1..P0-5 (zbench measurement correctness and safety)."""
import importlib.util
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from tests.local_fakes import FakeOpener

ZB = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "zbench.py"
psutil = pytest.importorskip("psutil")


@pytest.fixture
def zb():
    spec = importlib.util.spec_from_file_location("zbench_qa", ZB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ABORT.clear()
    yield mod
    mod.ABORT.clear()
    mod.kill_children()


def _wav(path: Path, ms: int = 60):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16 * ms))


BURN = "import time\nt=time.time()\nwhile time.time()-t<3: pass\n"


# P0-1 -----------------------------------------------------------------------------
def test_sampler_counts_child_cpu(zb):
    p = subprocess.Popen([sys.executable, "-c", BURN])
    try:
        s = zb.Sampler(p.pid, include_self=False)
        s.start()
        time.sleep(2.0)
        rows = s.stop()
    finally:
        p.kill()
        p.wait()
    ncpu = psutil.cpu_count() or 1
    own = [r["own_cpu"] for r in rows[2:]]
    assert own and max(own) > 50.0 / ncpu, own          # one busy core is 100/ncpu %
    bg = sum(r["bg_cpu"] for r in rows[2:]) / len(rows[2:])
    sysm = sum(r["sys_cpu"] for r in rows[2:]) / len(rows[2:])
    assert bg < sysm


def test_sampler_reuses_process_objects(zb):
    # A childless process of our own: os.getpid() may have leftover children from other tests.
    # A Windows venv executable is a redirector that creates another Python
    # process. Use the base interpreter to keep this fixture truly childless.
    child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), "-c", "import time; time.sleep(30)"])
    try:
        s = zb.Sampler(child.pid, include_self=False)
        first = s.procs()
        second = s.procs()
    finally:
        child.kill()
        child.wait()
    assert [primed for _, primed in first] == [False]
    assert [primed for _, primed in second] == [True]
    assert first[0][0] is second[0][0]


# P0-2 -----------------------------------------------------------------------------
def test_classify_detects_throttle_at_1s_interval(zb):
    quiet = [{"bg_cpu": 1.0}] * 10
    perf = [100.0] * 10 + [70.0] * 12
    assert zb.classify(quiet, perf, False, 0, 0, perf_interval=1.0, baseline_n=10) == "THROTTLED"
    assert zb.classify(quiet, [100.0] * 30, False, 0, 0, perf_interval=1.0, baseline_n=10, perf_required=True) == "OK"
    assert zb.classify(quiet, [], False, 0, 0, perf_required=True) == "UNKNOWN_NO_PERF"
    assert zb.classify(quiet, [100.0] * 30, False, None, 0, perf_interval=1.0, perf_required=True) == "UNKNOWN_NO_PERF"


ROW = {"t": 0, "bg_cpu": 0.0, "sys_cpu": 0.0, "own_cpu": 0.0, "rss": 0, "peak_wset": 0, "private": 0, "avail": 0}


class _Perf:
    available, interval, error = True, 1.0, ""

    def __init__(self, values):
        self.values = values

    def window(self, t0, t1):
        return self.values


def test_record_passes_perf_and_ev37_to_classify(zb, tmp_path):
    b = zb.Bench({}, tmp_path, 1, 0, perf=_Perf([100.0] * 10 + [60.0] * 15), ev37=lambda s: 0)
    b._record({"run_id": "x", "rc": 0, "duration_s": 30}, [ROW])
    assert b.runs[-1]["status"] == "THROTTLED" and b.runs[-1]["perf_n"] == 25
    b2 = zb.Bench({}, tmp_path, 1, 0, perf=_Perf([100.0] * 30), ev37=lambda s: 2)
    b2._record({"run_id": "y", "rc": 0, "duration_s": 30}, [ROW])
    assert b2.runs[-1]["status"] == "THROTTLED" and b2.runs[-1]["ev37_delta"] == 2


def test_ev37_query_counts_events(zb):
    class R:
        returncode = 0
        stdout = "<Event xmlns='x'></Event><Event xmlns='x'></Event>"
    seen = {}

    def runner(args, **kw):
        seen["args"] = args
        return R()
    assert zb.ev37_since(60, runner=runner) == 2
    assert "EventID=37" in seen["args"][3] and "timediff" in seen["args"][3]


# P0-3 -----------------------------------------------------------------------------
FAKE_SERVER = r'''
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
sys.stderr.write("load " * 60000); sys.stderr.flush()          # ~300 KB at start-up
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        sys.stderr.write("system_info processing " * 8000); sys.stderr.flush()   # ~180 KB per request
        body = json.dumps({"text": "因緣"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def test_resident_server_stderr_never_blocks(zb, tmp_path):
    if zb.port_open(zb.RESIDENT_PORT):
        pytest.skip("resident port busy")
    srv_py = tmp_path / "fake_server.py"
    srv_py.write_text(FAKE_SERVER, encoding="utf-8")
    wav = tmp_path / "a.wav"
    _wav(wav, 200)
    rw = zb.ResidentWhisper(sys.executable, "m.bin", 2, 640, 1, log_path=tmp_path / "logs" / "r.server.log")
    rw.args = [sys.executable, str(srv_py), "--port", str(zb.RESIDENT_PORT)]
    t0 = time.monotonic()
    with rw as srv:
        for _ in range(6):
            assert srv.transcribe(wav)["text"] == "因緣"
    assert time.monotonic() - t0 < 60
    assert (tmp_path / "logs" / "r.server.log").stat().st_size > 1_000_000


# P0-4 -----------------------------------------------------------------------------
class _SlowServer:
    """RTF 1.5: every 60 ms slice takes 90 ms."""
    def __init__(self, *a, **kw):
        self.proc = type("P", (), {"pid": os.getpid()})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transcribe(self, wav):
        time.sleep(0.09)
        return {"asr_ms": 90.0, "audio_ms": 60.0, "rtf": 1.5, "text": "因緣", "rc": 0}


def test_layer3_latency_includes_backlog(zb, tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    audio.mkdir()
    _wav(audio / "0.wav", 60)
    monkeypatch.setattr(zb, "ResidentWhisper", _SlowServer)
    opener = FakeOpener({"message": {"content": "Hi"}, "eval_count": 1, "eval_duration": 1_000_000})
    m = {"whisper_server": "x", "segments_dir": str(audio), "asr_models": [{"name": "f", "path": "m"}],
         "concurrent": {"enabled": True, "minutes": 1.5 / 60, "stale_s": 999}}
    b = zb.Bench(m, tmp_path / "out", 1, 0, opener=opener, ev37=None)
    b.layer_concurrent()
    segs = [r for r in b.segments if r["run_id"].startswith("l3-")]
    assert len(segs) >= 8
    lat = [r["zh_latency_ms"] for r in segs]
    assert lat[-1] > lat[0] + 150, lat                  # backlog grows, not a flat 90 ms
    assert segs[-1]["queue_wait_ms"] > 100
    assert b.runs[-1]["config_id"] == "B" and b.runs[-1]["llm_threads"] == 2


# P0-5 -----------------------------------------------------------------------------
def test_bench_children_never_above_normal(zb):
    assert zb.BENCH_PRIO in ("normal", "below", "idle")
    import inspect
    assert 'prio="above"' not in inspect.getsource(zb)


def test_watchdog_aborts_and_kills_children(zb):
    child = zb.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    w = zb.Watchdog(interval=0.05, busy=lambda: (True, "live room is streaming (listeners=1)"), battery=lambda: False)
    w.start()
    assert zb.ABORT.wait(3)
    child.wait(5)
    assert child.returncode is not None
    assert zb.ABORT_REASON[0].startswith("ABORTED_LIVE")
    w.stop()


def test_watchdog_aborts_on_battery(zb):
    w = zb.Watchdog(interval=0.05, busy=lambda: (False, "idle"), battery=lambda: True)
    w.tick()
    assert zb.ABORT.is_set() and zb.ABORT_REASON[0].startswith("ABORTED_BATTERY")


def test_aborted_bench_stops_and_marks_run(zb, tmp_path):
    zb.request_abort("ABORTED_LIVE: test")
    opener = FakeOpener({"models": [{"name": "qwen3:4b"}]})
    b = zb.Bench({"llm": {"models": ["qwen3:4b"]}, "sentences": ["你好"]}, tmp_path, 3, 0, opener=opener, ev37=None)
    b.layer_llm()
    assert not [r for r in b.runs if r["run_id"].startswith("llm-qwen3:4b-")]
    b._record({"run_id": "z", "rc": 0}, [])
    assert b.runs[-1]["status"] == "ABORTED_LIVE"
