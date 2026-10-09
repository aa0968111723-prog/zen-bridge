from __future__ import annotations

import hashlib
import io
import json
import socket
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult, CliAsr, ResidentAsr
from app.doctor import inspect
from app.native_paths import NativePaths
from app.server import create_app
from app.settings import Settings
from scripts.install_runtime import download_verified, extract_binaries


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Process:
    def __init__(self, stop=lambda: None):
        self.stopped = False
        self.stop = stop

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True
        self.stop()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


def test_resident_waits_for_http_model_ready_and_reuses_process(tmp_path):
    seen = {"health": 0, "processes": 0, "bodies": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/health"
            seen["health"] += 1
            loading = seen["health"] < 3
            self.send_response(503 if loading else 200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "loading model" if loading else "ok"}).encode())

        def do_POST(self):
            assert self.path == "/inference"
            seen["bodies"].append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"text": "般若中文測試"}).encode())

    port = free_port()
    def popen(*args, **kwargs):
        seen["processes"] += 1
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        return Process(stop)
    binary, model, wav = (tmp_path / name for name in ("server", "model", "audio.wav"))
    for path in (binary, model, wav):
        path.write_bytes(b"fixture")
    asr = ResidentAsr(f"http://127.0.0.1:{port}", server_bin=binary, model=model, popen=popen, startup_timeout_s=2, poll_interval_s=0.01)
    try:
        assert asr.start().ok
        assert seen["health"] >= 3
        assert asr.loads == 1
        assert asr.transcribe(wav, "提示").text == "般若中文測試"
        assert asr.transcribe(wav, "提示").text == "般若中文測試"
        assert seen["processes"] == 1
        assert all(b'name="response_format"' in body and b'name="language"' in body for body in seen["bodies"])
    finally:
        asr.close()
    assert not asr.health()


def test_resident_loading_timeout_cleans_up_child(tmp_path):
    binary, model = tmp_path / "server", tmp_path / "model"
    binary.touch()
    model.touch()
    process = Process()
    asr = ResidentAsr(f"http://127.0.0.1:{free_port()}", server_bin=binary, model=model, popen=lambda *a, **kw: process, health_probe=lambda: False, startup_timeout_s=0.03, poll_interval_s=0.005)
    result = asr.start()
    assert not result.ok and not result.loaded_once
    assert process.stopped and asr.proc is None and not asr.ready
    assert asr.loads == 0


def test_resident_does_not_adopt_existing_service(tmp_path):
    binary, model = tmp_path / "server", tmp_path / "model"
    binary.touch()
    model.touch()
    called = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        asr = ResidentAsr(f"http://127.0.0.1:{listener.getsockname()[1]}", server_bin=binary, model=model, popen=lambda *a, **kw: called.append(True))
        assert not asr.start().ok
    assert not called and asr.proc is None


def test_cli_windows_utf8_and_missing_dll(tmp_path):
    binary, model = tmp_path / "cli.exe", tmp_path / "model"
    binary.touch()
    model.touch()
    def runner(cmd, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["timeout"] == 7
        return subprocess.CompletedProcess(cmd, 0, stdout="繁體中文", stderr="")
    assert CliAsr(binary, model, runner=runner, timeout_s=7).transcribe(tmp_path / "wav", "").text == "繁體中文"
    def missing_dll(*args, **kwargs):
        raise OSError("DLL unavailable")
    result = CliAsr(binary, model, runner=missing_dll).transcribe(tmp_path / "wav", "")
    assert not result.ok and "DLL" in result.error


def test_native_windows_paths_preserve_unicode_files_and_prompt(tmp_path):
    folder = tmp_path / "host 中文" / "models"
    folder.mkdir(parents=True)
    model = folder / "模型.bin"
    audio = tmp_path / "語音.wav"
    model.write_bytes(b"model")
    audio.write_bytes(b"audio")
    paths = NativePaths(model)
    # Exercise the Windows path strategy on either test platform.
    paths.cwd = folder
    try:
        model_arg = paths.argument(model)
        audio_arg = paths.argument(audio, allow_copy=True)
        assert model_arg.isascii() and audio_arg.isascii()
        assert (folder / model_arg).read_bytes() == b"model"
        assert (folder / audio_arg).read_bytes() == b"audio"
        command = paths.cli_command(folder / "whisper-cli.exe", ["-m", model_arg, "-f", audio_arg, "--prompt", "般若\n中文"])
        response = folder / command[1][1:]
        assert response.read_text(encoding="utf-8").splitlines()[-1] == "般若 中文"
        workspace = response.parent
    finally:
        paths.close()
    assert not workspace.exists()
    assert model.read_bytes() == b"model" and audio.read_bytes() == b"audio"


def test_resident_reports_bounded_startup_failure_and_closes_pipe(tmp_path):
    binary, model = tmp_path / "server", tmp_path / "model"
    binary.touch()
    model.touch()
    process = Process()
    process.stopped = True
    process.stderr = io.BytesIO(b"x" * 8000 + b"\nmodel file not found")
    asr = ResidentAsr(f"http://127.0.0.1:{free_port()}", server_bin=binary, model=model, popen=lambda *a, **kw: process)
    result = asr.start()
    assert not result.ok and "model file not found" in result.error
    assert len(asr.startup_output) <= 4096
    assert process.stderr.closed and asr.proc is None and asr.native_paths is None


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


def asset(data):
    return {"url": "https://example.invalid/pinned", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@pytest.mark.parametrize("resume", [True, False])
def test_download_resumes_or_restarts_when_range_ignored(tmp_path, resume):
    data = b"complete-model"
    target = tmp_path / "model"
    target.with_name("model.part").write_bytes(data[:3])
    def opener(req, timeout):
        assert req.headers["Range"] == "bytes=3-"
        return Response(data[3:], 206, {"Content-Range": f"bytes 3-{len(data)-1}/{len(data)}"}) if resume else Response(data)
    download_verified(asset(data), target, opener=opener)
    assert target.read_bytes() == data and not target.with_name("model.part").exists()


def test_bad_download_preserves_previous_model(tmp_path):
    target = tmp_path / "model"
    target.write_bytes(b"previous")
    with pytest.raises(RuntimeError, match="SHA256"):
        download_verified(asset(b"good"), target, opener=lambda *a, **kw: Response(b"evil"))
    assert target.read_bytes() == b"previous"


def test_zip_dll_siblings_and_traversal(tmp_path):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("bin/whisper-cli.exe", b"binary")
        zipped.writestr("bin/ggml.dll", b"dependency")
    extract_binaries(archive, tmp_path / "tools")
    assert (tmp_path / "tools" / "ggml.dll").read_bytes() == b"dependency"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape.exe", b"bad")
    with pytest.raises(RuntimeError):
        extract_binaries(archive, tmp_path / "bad-tools")
    assert not (tmp_path / "escape.exe").exists()


def test_doctor_port_conflict_and_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-never-export-this")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("whisper-cli.exe", "whisper-server.exe", "ffmpeg.exe"):
        (tools / name).touch()
    model = tmp_path / "model"
    model.write_bytes(b"fixture")
    original_run = subprocess.run
    def native_probe(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0].endswith(".exe"):
            return subprocess.CompletedProcess(cmd, 0)
        return original_run(cmd, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", native_probe)
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        settings = Settings(port=listener.getsockname()[1], model_path=str(model), asr_mode="resident")
        report = inspect(settings, root=tmp_path)
    assert not report["ok"]
    assert not next(row for row in report["checks"] if row["name"] == "port")["ok"]
    assert "secret-never-export-this" not in json.dumps(report)
    assert report["microphone_tested"] is False and report["model_inference_tested"] is False


@pytest.mark.anyio
async def test_setup_and_health_do_not_claim_unloaded_resident_ready():
    asr = ResidentAsr()
    asr.mark_ready_for_test()
    app = create_app(Settings(allow_testclient=True), asr=asr)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            setup = (await client.get("/api/setup")).json()
            assert setup["asr_mode"] == "resident" and setup["asr_ready"] is False
            response = await client.get("/api/health")
            assert response.status_code == 503 and response.json()["ready"] is False
            assert app.state.token not in response.text
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_saved_export_survives_live_history_limit_and_restart(tmp_path):
    class LocalAsr:
        def transcribe(self, path, prompt):
            return AsrResult(ok=True, text=path.read_bytes().decode("utf-8"))
    def decode(source, work):
        wav = work / "audio.wav"
        wav.write_bytes(source.read_bytes())
        return wav
    settings = Settings(history_limit=2, data_path=str(tmp_path / "captions.sqlite3"), translate=False)
    first = create_app(settings, asr=LocalAsr(), decoder=decode)
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = (await client.get("/api/host-token")).json()["token"]
            headers = {"Authorization": "Bearer " + token, "Origin": "http://127.0.0.1:8780"}
            for seq in range(1, 6):
                response = await client.post("/api/push", data={"room_id": "long-room", "session_id": "z-old", "seq": str(seq), "t0_ms": str((seq-1)*6000), "t1_ms": str(seq*6000)}, files={"audio": ("test.wav", f"中文 {seq}".encode(), "audio/wav")}, headers=headers)
                assert response.status_code == 200
            newer = await client.post("/api/push", data={"room_id": "long-room", "session_id": "a-new", "seq": "1", "t0_ms": "0", "t1_ms": "6000"}, files={"audio": ("test.wav", "新場次".encode(), "audio/wav")}, headers=headers)
            assert newer.status_code == 200
            assert len(first.state.bus.history("long-room")) == 2
            exported = await client.get("/api/export", params={"room_id": "long-room", "kind": "json"}, headers=headers)
            assert [(r["session_id"], r["seq"]) for r in exported.json()] == [("z-old", s) for s in range(1, 6)] + [("a-new", 1)]
            assert (await client.get("/api/export", params={"room_id": "other-room", "kind": "json"}, headers=headers)).json() == []
    finally:
        await first.state.shutdown()
    resumed = create_app(settings, asr=LocalAsr(), decoder=decode)
    try:
        assert resumed.state.bus.history("long-room") == []
        async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
            token = (await client.get("/api/host-token")).json()["token"]
            srt = await client.get("/api/export", params={"room_id": "long-room", "kind": "srt"}, headers={"Authorization": "Bearer " + token})
            assert srt.status_code == 200 and srt.text.count("-->") == 6
            assert "中文 1" in srt.text and "中文 5" in srt.text and "新場次" in srt.text
    finally:
        await resumed.state.shutdown()
