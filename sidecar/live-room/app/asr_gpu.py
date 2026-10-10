"""Opt-in GPU (Vulkan) path for the Breeze ASR worker.

Default is unchanged: the CPU pywhispercpp worker (app.native_asr). Set

  BREEZE_ASR_GPU=vulkan
  BREEZE_WHISPER_DIR=<folder with whisper-server.exe + ggml-vulkan.dll + ggml-*.dll>

and the server starts that folder's ``whisper-server`` on a free loopback port with the
laptop-measured parameters (TEST-REPORT §8: 4 threads, audio_ctx 640, beam 1, no fallback;
RTF 0.3-0.55 on the Radeon iGPU vs 2.24 at best on CPU). Before it is used, a startup self-test
transcribes a short generated clip under a timeout. If the binary is missing, the server does not
come up, the self-test fails/times out, or the GPU worker later fails for good (restarts used up),
the CPU worker takes over automatically and ``GPU_STATUS`` says why (shown in /api/metrics ->
admin overview, and /api/hw/status).

Optional overrides: BREEZE_ASR_GPU_THREADS (4), BREEZE_ASR_GPU_AUDIO_CTX (640),
BREEZE_ASR_GPU_BEAM (1), BREEZE_ASR_GPU_NO_FALLBACK (1), BREEZE_ASR_GPU_SELFTEST_S (60).
"""
from __future__ import annotations

import io
import math
import os
import re
import socket
import struct
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.asr import AsrResult, ResidentAsr
from app.gpu_env import shared_memory_from

GPU_KINDS = ("vulkan",)
SERVER_NAMES = ("whisper-server.exe", "whisper-server")
_DEVICE = re.compile(r"ggml_vulkan: 0 = ([^\r\n|]+)")
_GPU_FATAL = re.compile(r"VK_ERROR_\w+|DEVICE_LOST|ErrorDeviceLost")

# Last known state of the GPU path, for /api/metrics, admin overview and /api/hw/status.
GPU_STATUS: dict = {"requested": None, "active": "cpu", "ok": None, "error": "", "device": "",
                    "selftest_ms": None, "params": {}, "fell_back_at": None}
_status_lock = threading.Lock()


def gpu_status() -> dict:
    with _status_lock:
        return dict(GPU_STATUS, params=dict(GPU_STATUS.get("params") or {}))


def _set_status(**kw) -> None:
    with _status_lock:
        GPU_STATUS.update(kw)


def reset_status() -> None:
    _set_status(requested=None, active="cpu", ok=None, error="", device="", selftest_ms=None,
                params={}, fell_back_at=None)


def _int(env, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(str(env.get(key, "")).strip() or default)))
    except ValueError:
        return default


@dataclass
class GpuAsrConfig:
    kind: str
    whisper_dir: Path | None
    threads: int = 4
    audio_ctx: int = 640
    beam: int = 1
    no_fallback: bool = True
    selftest_s: float = 60.0
    errors: list = field(default_factory=list)

    @classmethod
    def from_env(cls, env=None) -> "GpuAsrConfig | None":
        env = os.environ if env is None else env
        kind = (env.get("BREEZE_ASR_GPU") or "").strip().lower()
        if kind in ("", "0", "off", "cpu", "none"):
            return None
        raw_dir = (env.get("BREEZE_WHISPER_DIR") or "").strip()
        cfg = cls(kind=kind, whisper_dir=Path(os.path.expandvars(raw_dir)) if raw_dir else None,
                  threads=_int(env, "BREEZE_ASR_GPU_THREADS", 4, 1, 16),
                  audio_ctx=_int(env, "BREEZE_ASR_GPU_AUDIO_CTX", 640, 0, 1500),
                  beam=_int(env, "BREEZE_ASR_GPU_BEAM", 1, 1, 8),
                  no_fallback=(env.get("BREEZE_ASR_GPU_NO_FALLBACK") or "1").strip() != "0",
                  selftest_s=float(_int(env, "BREEZE_ASR_GPU_SELFTEST_S", 60, 5, 600)))
        if kind not in GPU_KINDS:
            cfg.errors.append(f"BREEZE_ASR_GPU={kind} 不支援（只支援 vulkan）")
        if cfg.whisper_dir is None:
            cfg.errors.append("BREEZE_ASR_GPU 有設，但 BREEZE_WHISPER_DIR 沒設")
        return cfg

    def server_bin(self) -> Path | None:
        if self.whisper_dir is None:
            return None
        for name in SERVER_NAMES:
            p = self.whisper_dir / name
            if p.is_file():
                return p
        return None

    def params(self) -> dict:
        return {"threads": self.threads, "audio_ctx": self.audio_ctx, "beam": self.beam,
                "no_fallback": self.no_fallback, "whisper_dir": str(self.whisper_dir or "")}


def free_loopback_port(avoid=(8645,)) -> int:
    for _ in range(20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port
    raise OSError("no free loopback port")


def selftest_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A short, quiet 440 Hz tone: enough to run encoder + decoder once, no speech."""
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(800 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


class VulkanServerAsr(ResidentAsr):
    """whisper-server from BREEZE_WHISPER_DIR (Vulkan build), recommended params, device sniffing."""

    gpu = "vulkan"

    def __init__(self, cfg: GpuAsrConfig, model: Path, *, port: int | None = None, popen=None,
                 startup_timeout_s: float = 180, inference_timeout_s: float = 120, health_probe=None):
        super().__init__(f"http://127.0.0.1:{port or free_loopback_port()}", server_bin=cfg.server_bin(),
                         model=model, popen=popen, threads=cfg.threads, startup_timeout_s=startup_timeout_s,
                         inference_timeout_s=inference_timeout_s, audio_context=cfg.audio_ctx,
                         beam_size=cfg.beam, health_probe=health_probe)
        self.cfg = cfg
        # whisper-server (b5454) has no -nc; "-mc 0" = keep no text context between requests (same intent).
        self.no_context = False
        self.extra_args = ["-mc", "0"] + (["-nf"] if cfg.no_fallback else [])
        self.device = ""
        self.gpu_error = ""
        self.last_stats: dict = {}

    def _drain_stderr(self, stream) -> None:
        carry = b""
        read = getattr(stream, "read1", None) or stream.read     # read1: do not wait for a full 1 KB
        try:
            while block := read(1024):
                text = (carry + block).decode("utf-8", errors="replace")
                shared = shared_memory_from(text)
                if shared is not None:
                    self.ggml_shared_memory = shared
                m = _DEVICE.search(text)
                if m and not self.device:
                    self.device = m.group(1).strip()
                f = _GPU_FATAL.search(text)
                if f:
                    self.gpu_error = f.group(0)
                carry = block[-256:]
                if self.capture_startup:
                    self.startup_output = (self.startup_output + block)[-4096:]
        finally:
            stream.close()

    def transcribe(self, wav, prompt, language="zh"):       # language: CPU-worker signature
        t0 = time.monotonic()
        res = super().transcribe(Path(wav), prompt)
        self.last_stats = {"asr_s": round(time.monotonic() - t0, 3), "gpu": self.gpu}
        return res

    def self_test(self, timeout_s: float) -> AsrResult:
        saved = self.inference_timeout_s
        self.inference_timeout_s = timeout_s
        fd, name = tempfile.mkstemp(suffix=".wav", prefix="breeze-gpu-selftest-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(selftest_wav())
            res = super().transcribe(Path(name), "")
            settle = time.monotonic() + 0.5          # let the stderr drain catch a driver error line
            while res.ok and not self.gpu_error and time.monotonic() < settle:
                time.sleep(0.05)
            if res.ok and self.gpu_error:
                return AsrResult(ok=False, error=f"GPU 驅動錯誤：{self.gpu_error}")
            return res
        finally:
            self.inference_timeout_s = saved
            try:
                os.unlink(name)
            except OSError:
                pass

    def restart_status(self) -> dict:
        return {"asr_degraded": False, "asr_restarts": self.restarts, "asr_recent_failures": 0,
                "ggml_shared_memory": self.ggml_shared_memory, "asr_gpu": self.gpu}


class GpuWithCpuFallback:
    """Uses the GPU worker; when it fails for good, starts the CPU worker and stays on it."""

    def __init__(self, gpu: VulkanServerAsr, cpu_factory: Callable[[], object], clock=time.time):
        self.gpu = gpu
        self.cpu_factory = cpu_factory
        self.current = gpu
        self.clock = clock
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.__dict__["current"], name)

    @property
    def on_gpu(self) -> bool:
        return self.current is self.gpu

    def _fall_back(self, why: str):
        try:
            self.gpu.close()
        except Exception:
            pass
        cpu = self.cpu_factory()
        started = cpu.start()
        self.current = cpu
        _set_status(active="cpu", ok=False, error=f"GPU 執行中失敗，已改用 CPU：{why}"[:300],
                    fell_back_at=self.clock())
        return started

    def start(self):
        return self.current.start()

    def transcribe(self, wav, prompt, language="zh"):
        with self._lock:
            cur = self.current
        if cur is not self.gpu:
            return cur.transcribe(wav, prompt, language)
        res = cur.transcribe(wav, prompt, language)
        if res.ok:
            return res
        # ResidentAsr restarts itself up to max_restarts; once those are spent (or the driver
        # reported a device error) the GPU path is done for this run.
        if self.gpu.gpu_error or (self.gpu.restarts >= self.gpu.max_restarts and not self.gpu.health()):
            with self._lock:
                if self.current is self.gpu:
                    self._fall_back(self.gpu.gpu_error or res.error or "GPU worker 失敗")
            return self.current.transcribe(wav, prompt, language)
        return res

    def health(self):
        return self.current.health()

    def close(self):
        self.current.close()

    def restart_status(self) -> dict:
        fn = getattr(self.current, "restart_status", None)
        out = dict(fn()) if callable(fn) else {}
        out["asr_gpu"] = "vulkan" if self.on_gpu else "cpu"
        return out


def build_asr(cpu_factory: Callable[[], object], model: Path, env=None, *, gpu_factory=None):
    """Return (asr, start_result). Without BREEZE_ASR_GPU this is exactly the CPU worker."""
    cfg = GpuAsrConfig.from_env(env)
    if cfg is None:
        reset_status()
        cpu = cpu_factory()
        return cpu, cpu.start()
    _set_status(requested=cfg.kind, active="cpu", ok=None, error="", device="", selftest_ms=None,
                params=cfg.params(), fell_back_at=None)

    def to_cpu(why: str):
        _set_status(active="cpu", ok=False, error=why[:300])
        cpu = cpu_factory()
        return cpu, cpu.start()

    if cfg.errors:
        return to_cpu("；".join(cfg.errors) + "，已改用 CPU")
    if cfg.server_bin() is None:
        return to_cpu(f"在 {cfg.whisper_dir} 找不到 whisper-server，已改用 CPU")
    try:
        gpu = (gpu_factory or VulkanServerAsr)(cfg, model)
    except Exception as exc:
        return to_cpu(f"GPU worker 無法建立：{type(exc).__name__}，已改用 CPU")
    started = gpu.start()
    if not started.ok:
        gpu.close()
        return to_cpu(f"GPU worker 啟動失敗：{started.error}，已改用 CPU")
    t0 = time.monotonic()
    test = gpu.self_test(cfg.selftest_s)
    ms = int((time.monotonic() - t0) * 1000)
    if not test.ok:
        gpu.close()
        _set_status(selftest_ms=ms)
        return to_cpu(f"GPU 自我測試失敗（{ms} ms）：{test.error or '沒有回應'}，已改用 CPU")
    _set_status(active=cfg.kind, ok=True, error="", device=getattr(gpu, "device", ""), selftest_ms=ms)
    return GpuWithCpuFallback(gpu, cpu_factory), started
