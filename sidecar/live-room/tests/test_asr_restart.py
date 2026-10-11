"""round3 C2: native ASR worker restarts itself with backoff; 4 failures in 5 min -> degraded."""
import subprocess
import sys

from app import native_asr
from app.native_asr import NativeResidentAsr

# worker: answers, but exits on a clip whose name contains "crash" and hangs on "hang"
WORKER = """import json,os,sys,time
print('BREEZE_RESULT '+json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
    req=json.loads(line); name=os.path.basename(req['path'])
    if 'crash' in name: sys.exit(3)
    if 'hang' in name: time.sleep(30)
    print('BREEZE_RESULT '+json.dumps({'ok':True,'text':'好'}),flush=True)
"""


def engine(tmp_path, monkeypatch):
    helper = tmp_path / "worker.py"
    helper.write_text(WORKER, encoding="utf-8")
    real = subprocess.Popen
    launches = []

    def launch(command, **kw):
        launches.append(1)
        return real([sys.executable, "-u", str(helper)], **kw)
    monkeypatch.setattr("app.native_asr.subprocess.Popen", launch)
    asr = NativeResidentAsr(tmp_path / "model.bin", startup_timeout_s=5, inference_timeout_s=0.5, log_path=tmp_path / "w.log")
    t = {"now": 1000.0}
    asr.clock = lambda: t["now"]
    return asr, t, launches


def test_crash_then_next_segment_restarts(tmp_path, monkeypatch):
    asr, t, launches = engine(tmp_path, monkeypatch)
    try:
        assert asr.start().ok
        assert asr.transcribe(tmp_path / "a1.wav", "").text == "好"
        r = asr.transcribe(tmp_path / "a2-crash.wav", "")
        assert not r.ok
        t["now"] += 0.5
        early = asr.transcribe(tmp_path / "a3.wav", "")          # inside the 1 s backoff: dropped
        assert not early.ok and "重新啟動中" in early.error and len(launches) == 1
        t["now"] += 1.0
        assert asr.transcribe(tmp_path / "a4.wav", "").text == "好"
        assert len(launches) == 2 and asr.restarts == 1 and not asr.degraded
        assert asr.restart_status()["asr_restarts"] == 1
    finally:
        asr.close()


def test_timeout_drops_segment_logs_it_and_recovers(tmp_path, monkeypatch):
    asr, t, launches = engine(tmp_path, monkeypatch)
    try:
        assert asr.start().ok
        r = asr.transcribe(tmp_path / "b1-hang.wav", "")
        assert not r.ok and "逾時" in r.error
        t["now"] += 2
        assert asr.transcribe(tmp_path / "b2.wav", "").text == "好"
        for h in asr.worker_log.handlers:
            h.flush()
        assert "b1-hang.wav" in (tmp_path / "w.log").read_text(encoding="utf-8")
    finally:
        asr.close()


def test_fourth_failure_in_five_minutes_is_degraded(tmp_path, monkeypatch):
    asr, t, launches = engine(tmp_path, monkeypatch)
    try:
        assert asr.start().ok
        for i in range(3):
            assert not asr.transcribe(tmp_path / f"c{i}-crash.wav", "").ok
            t["now"] += 31
            assert asr.transcribe(tmp_path / f"ok{i}.wav", "").ok      # restarted
        assert asr.restarts == 3 and not asr.degraded
        assert not asr.transcribe(tmp_path / "c4-crash.wav", "").ok
        assert asr.degraded
        t["now"] += 31
        r = asr.transcribe(tmp_path / "late.wav", "")
        assert not r.ok and "失敗太多次" in r.error and len(launches) == 4
        asr.reset_degraded()
        assert asr.transcribe(tmp_path / "after-reset.wav", "").ok
    finally:
        asr.close()


def test_failures_older_than_window_forgotten(tmp_path, monkeypatch):
    asr, t, _ = engine(tmp_path, monkeypatch)
    try:
        assert asr.start().ok
        for i in range(5):
            assert not asr.transcribe(tmp_path / f"d{i}-crash.wav", "").ok
            t["now"] += native_asr.RESTART_WINDOW_S + 1
            assert asr.transcribe(tmp_path / f"ok{i}.wav", "").ok
        assert not asr.degraded and asr.restarts == 5
    finally:
        asr.close()
