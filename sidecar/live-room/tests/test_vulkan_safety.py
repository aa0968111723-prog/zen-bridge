"""round4 #9 (D7): Vulkan implicit layers off, ggml shared memory logged, whisper-server -nc."""
import io
import logging
import sys
from pathlib import Path

from app import gpu_env
from app.asr import ResidentAsr


def test_worker_env_disables_implicit_layers_by_default():
    env = gpu_env.worker_env({"PATH": "x"})
    assert env["VK_LOADER_LAYERS_DISABLE"] == "~implicit~"
    assert env["PATH"] == "x"


def test_worker_env_respects_user_value_and_opt_out():
    assert gpu_env.worker_env({"VK_LOADER_LAYERS_DISABLE": "foo"})["VK_LOADER_LAYERS_DISABLE"] == "foo"
    assert "VK_LOADER_LAYERS_DISABLE" not in gpu_env.worker_env({"BREEZE_VK_IMPLICIT_LAYERS": "1"})


def test_shared_memory_parse():
    line = "ggml_vulkan: 0 = AMD Radeon(TM) Graphics (AMD proprietary driver) | uma: 1 | fp16: 1 | warp size: 64 | shared memory: 32768 | int dot: 1"
    assert gpu_env.shared_memory_from(line) == 32768
    assert gpu_env.shared_memory_from("whisper_init_from_file: loading model") is None


class _Proc:
    def __init__(self, stderr):
        self.stderr = stderr

    def poll(self):
        return None


def test_whisper_server_gets_nc_and_vk_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BREEZE_RESIDENT_NO_CONTEXT", raising=False)
    monkeypatch.delenv("VK_LOADER_LAYERS_DISABLE", raising=False)
    server = tmp_path / "whisper-server"
    server.write_text("")
    model = tmp_path / "m.bin"
    model.write_text("")
    seen = {}

    def popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        return _Proc(io.BytesIO(b"ggml_vulkan: shared memory: 65536 |\n"))

    asr = ResidentAsr("http://127.0.0.1:18999", server_bin=server, model=model, popen=popen,
                      startup_timeout_s=0.3, poll_interval_s=0.05, health_probe=lambda: True)
    assert asr.start().ok
    assert "-nc" in seen["cmd"]
    assert seen["env"]["VK_LOADER_LAYERS_DISABLE"] == "~implicit~"
    asr.output_thread.join(timeout=2)
    assert asr.ggml_shared_memory == 65536


def test_whisper_server_nc_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BREEZE_RESIDENT_NO_CONTEXT", "0")
    server = tmp_path / "whisper-server"
    server.write_text("")
    model = tmp_path / "m.bin"
    model.write_text("")
    seen = {}

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(io.BytesIO(b""))

    asr = ResidentAsr("http://127.0.0.1:18998", server_bin=server, model=model, popen=popen,
                      startup_timeout_s=0.3, poll_interval_s=0.05, health_probe=lambda: True)
    assert asr.start().ok
    assert "-nc" not in seen["cmd"]


def test_native_worker_env_and_shared_memory_log(tmp_path, monkeypatch):
    import subprocess
    from app import native_asr
    seen = {}
    real = subprocess.Popen
    fake = tmp_path / "fake_worker.py"
    fake.write_text(
        "import sys\n"
        "sys.stderr.write('ggml_vulkan: 0 = Radeon | shared memory: 32768 | int dot: 1\\n'); sys.stderr.flush()\n"
        "print('BREEZE_RESULT {\"ready\": true}', flush=True)\n"
        "for line in sys.stdin: pass\n", encoding="utf-8")

    def launch(cmd, **kw):
        seen["env"] = kw.get("env")
        return real([sys.executable, str(fake)], **kw)

    monkeypatch.setattr("app.native_asr.subprocess.Popen", launch)
    log = tmp_path / "w.log"
    asr = native_asr.NativeResidentAsr(tmp_path / "m.bin", log_path=log)
    assert asr.start().ok
    asr.close()
    assert seen["env"]["VK_LOADER_LAYERS_DISABLE"] == "~implicit~"
    assert asr.ggml_shared_memory == 32768
    assert asr.restart_status()["ggml_shared_memory"] == 32768
    for h in logging.getLogger().handlers:
        h.flush()
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("w.log*"))
    assert "BREEZE_GGML shared_memory=32768" in text
