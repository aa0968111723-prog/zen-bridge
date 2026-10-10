"""Opt-in GPU (Vulkan) ASR path: config, self-test, CPU fallback, status surfacing. Fake binaries only."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import asr_gpu
from app.asr import AsrResult

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_whisper_server.py"


class Cpu:
    made = 0

    def __init__(self):
        Cpu.made += 1
        self.started = False
        self.calls = []

    def start(self):
        self.started = True
        return AsrResult(ok=True, loaded_once=True)

    def transcribe(self, wav, prompt, language="zh"):
        self.calls.append(wav)
        return AsrResult(ok=True, text="cpu")

    def health(self):
        return self.started

    def close(self):
        self.started = False

    def restart_status(self):
        return {"asr_degraded": False}


@pytest.fixture(autouse=True)
def _reset():
    asr_gpu.reset_status()
    Cpu.made = 0
    yield
    asr_gpu.reset_status()


def _dir(tmp_path):
    d = tmp_path / "whisper-vulkan"
    d.mkdir()
    (d / "whisper-server.exe").write_bytes(b"fake")
    return d


def _popen(mode, monkeypatch, argv_log=None):
    def popen(cmd, **kw):
        env = dict(kw.get("env") or {})
        env["FAKE_WS_MODE"] = mode
        if argv_log:
            env["FAKE_WS_ARGV"] = str(argv_log)
        kw["env"] = env
        kw.pop("cwd", None)
        return subprocess.Popen([sys.executable, str(FAKE)] + list(cmd[1:]), **kw)
    return popen


def _gpu_factory(mode, monkeypatch, argv_log=None):
    def make(cfg, model):
        return asr_gpu.VulkanServerAsr(cfg, model, popen=_popen(mode, monkeypatch, argv_log),
                                       startup_timeout_s=20, inference_timeout_s=10)
    return make


def _env(d, **extra):
    return {"BREEZE_ASR_GPU": "vulkan", "BREEZE_WHISPER_DIR": str(d), **extra}


def _model(tmp_path):
    m = tmp_path / "ggml-breeze.bin"
    m.write_bytes(b"model")
    return m


def test_default_is_the_cpu_worker_unchanged(tmp_path):
    asr, started = asr_gpu.build_asr(Cpu, _model(tmp_path), env={})
    assert isinstance(asr, Cpu) and started.ok and Cpu.made == 1
    st = asr_gpu.gpu_status()
    assert st["requested"] is None and st["active"] == "cpu"
    assert asr_gpu.GpuAsrConfig.from_env({"BREEZE_ASR_GPU": "off"}) is None


def test_config_defaults_are_the_measured_params(tmp_path):
    cfg = asr_gpu.GpuAsrConfig.from_env(_env(tmp_path))
    assert (cfg.threads, cfg.audio_ctx, cfg.beam, cfg.no_fallback) == (4, 640, 1, True)
    assert asr_gpu.GpuAsrConfig.from_env({"BREEZE_ASR_GPU": "cuda", "BREEZE_WHISPER_DIR": "x"}).errors
    assert asr_gpu.GpuAsrConfig.from_env({"BREEZE_ASR_GPU": "vulkan"}).errors


def test_gpu_worker_passes_self_test_and_is_used(tmp_path, monkeypatch):
    log = tmp_path / "argv.json"
    asr, started = asr_gpu.build_asr(Cpu, _model(tmp_path), env=_env(_dir(tmp_path)),
                                     gpu_factory=_gpu_factory("ok", monkeypatch, log))
    try:
        assert started.ok and isinstance(asr, asr_gpu.GpuWithCpuFallback) and asr.on_gpu
        argv = json.loads(log.read_text(encoding="utf-8"))
        joined = " ".join(argv)
        assert "-t 4" in joined and "--audio-ctx 640" in joined and "--beam-size 1" in joined and "-nf" in argv
        assert "8645" not in joined
        assert "-nc" not in argv and "-mc 0" in joined      # whisper-server has no -nc
        wav = tmp_path / "a.wav"
        wav.write_bytes(asr_gpu.selftest_wav(0.2))
        res = asr.transcribe(wav, "")
        assert res.ok and res.text == "測試字幕" and Cpu.made == 0
        st = asr_gpu.gpu_status()
        assert st["active"] == "vulkan" and st["ok"] is True and st["selftest_ms"] is not None
        assert st["device"].startswith("Fake Radeon")
        assert asr.restart_status()["asr_gpu"] == "vulkan"
    finally:
        asr.close()


def test_missing_binary_falls_back_to_cpu(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    asr, started = asr_gpu.build_asr(Cpu, _model(tmp_path), env=_env(empty))
    assert isinstance(asr, Cpu) and started.ok
    st = asr_gpu.gpu_status()
    assert st["requested"] == "vulkan" and st["active"] == "cpu" and st["ok"] is False
    assert "找不到 whisper-server" in st["error"]


@pytest.mark.parametrize("mode, why", [("crash", "自我測試失敗"), ("vkerr", "GPU 驅動錯誤"),
                                       ("nohealth", "啟動失敗")])
def test_failed_self_test_or_start_falls_back_to_cpu(tmp_path, monkeypatch, mode, why):
    def make(cfg, model):
        return asr_gpu.VulkanServerAsr(cfg, model, popen=_popen(mode, monkeypatch),
                                       startup_timeout_s=3 if mode == "nohealth" else 20, inference_timeout_s=10)
    asr, started = asr_gpu.build_asr(Cpu, _model(tmp_path), env=_env(_dir(tmp_path)), gpu_factory=make)
    assert isinstance(asr, Cpu) and started.ok and Cpu.made == 1
    st = asr_gpu.gpu_status()
    assert st["active"] == "cpu" and st["ok"] is False and why in st["error"], st


def test_self_test_timeout_falls_back(tmp_path, monkeypatch):
    t0 = time.monotonic()
    asr, _ = asr_gpu.build_asr(Cpu, _model(tmp_path),
                               env=_env(_dir(tmp_path), BREEZE_ASR_GPU_SELFTEST_S="5"),
                               gpu_factory=_gpu_factory("hang", monkeypatch))
    assert isinstance(asr, Cpu) and time.monotonic() - t0 < 30
    assert "自我測試失敗" in asr_gpu.gpu_status()["error"]


class StubGpu:
    def __init__(self):
        self.gpu_error, self.restarts, self.max_restarts, self.closed = "", 0, 2, False

    def transcribe(self, wav, prompt, language="zh"):
        return AsrResult(ok=False, error="boom")

    def health(self):
        return False

    def close(self):
        self.closed = True


def test_runtime_device_error_switches_to_cpu_and_stays():
    gpu = StubGpu()
    w = asr_gpu.GpuWithCpuFallback(gpu, Cpu)
    assert w.transcribe("x.wav", "").ok is False and w.on_gpu        # one failure, restarts left
    gpu.gpu_error = "VK_ERROR_DEVICE_LOST"
    res = w.transcribe("y.wav", "")
    assert res.ok and res.text == "cpu" and not w.on_gpu and gpu.closed
    assert w.transcribe("z.wav", "").text == "cpu" and Cpu.made == 1
    st = asr_gpu.gpu_status()
    assert st["active"] == "cpu" and "VK_ERROR_DEVICE_LOST" in st["error"] and st["fell_back_at"]


def test_restarts_used_up_also_switch():
    gpu = StubGpu()
    gpu.restarts = 2
    w = asr_gpu.GpuWithCpuFallback(gpu, Cpu)
    assert w.transcribe("x.wav", "").text == "cpu" and not w.on_gpu


def test_status_is_in_metrics_and_hw_status(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    import asyncio
    from tests.test_round2 import app_for, auth, token_of
    asr_gpu._set_status(requested="vulkan", active="cpu", ok=False, error="GPU 自我測試失敗")
    monkeypatch.setattr("app.hw_tune.status", lambda **kw: {"topology": {}})

    async def go():
        app = app_for()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as c:
            tok = await token_of(app, c)
            m = (await c.get("/api/metrics", headers=auth(tok))).json()
            hw = (await c.get("/api/hw/status", headers=auth(tok))).json()
        return m, hw
    m, hw = asyncio.run(go())
    assert m["asr_gpu"]["error"] == "GPU 自我測試失敗" and hw["asr_gpu"]["requested"] == "vulkan"


def test_admin_overview_shows_the_gpu_state():
    js = (Path(__file__).resolve().parents[1] / "app/admin/static/app.js").read_text(encoding="utf-8")
    assert 'card("ASR 加速", asrGpuText(ov.asr_gpu)' in js and "已改用 CPU" in js
    obs = (Path(__file__).resolve().parents[1] / "app/admin/observability.py").read_text(encoding="utf-8")
    assert '"asr_gpu": (m or {}).get("asr_gpu")' in obs


def test_gpu_try_tool_runs_clips_through_the_app_worker(tmp_path, monkeypatch, capsys):
    from tools import gpu_try
    wav = tmp_path / "c.wav"
    wav.write_bytes(asr_gpu.selftest_wav(0.5))
    real = asr_gpu.build_asr
    monkeypatch.setattr(asr_gpu, "build_asr", lambda cpu, model, env=None: real(Cpu, model, env={}))
    assert gpu_try.main(["--model", str(_model(tmp_path)), str(wav)]) == 0
    out = capsys.readouterr().out
    assert '"active": "cpu"' in out and '"text": "cpu"' in out
