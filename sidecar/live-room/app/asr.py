from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
import json
import socket
import time
import urllib.error
import urllib.request
import threading

from app.native_paths import NativePaths


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("本機辨識不接受重新導向")


def _local_open(request, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


@dataclass
class AsrResult:
    ok: bool
    text: str = ""
    error: str = ""
    loaded_once: bool = False


class CliAsr:
    """每次呼叫都啟動一次 whisper-cli。這不是常駐模型。"""

    def __init__(self, whisper: Path, model: Path, runner: Callable | None = None, threads: int = 6, timeout_s: float = 120, audio_context: int = 0, beam_size: int = 0, best_of: int = 0):
        self.whisper = whisper
        self.model = model
        self.runner = runner
        self.threads = threads
        self.calls = 0
        self.timeout_s = timeout_s
        self.audio_context = audio_context
        self.beam_size = beam_size
        self.best_of = best_of

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        self.calls += 1
        if not self.whisper.exists():
            return AsrResult(ok=False, error="找不到 tools/whisper-cli.exe")
        if not self.model.exists():
            return AsrResult(ok=False, error="找不到 Breeze 模型。請先安裝，不要改走雲端辨識。")
        import subprocess

        paths = NativePaths(self.model)
        run = self.runner or subprocess.run
        try:
            args = ["-m", paths.argument(self.model), "-f", paths.argument(wav, allow_copy=True),
                    "-l", "zh", "-np", "-nt", "-t", str(self.threads), "--prompt", prompt]
            if self.audio_context:
                args += ["--audio-ctx", str(self.audio_context)]
            if self.beam_size:
                args += ["--beam-size", str(self.beam_size)]
            if self.best_of:
                args += ["--best-of", str(self.best_of)]
            cmd = paths.cli_command(self.whisper, args)
            proc = run(cmd, cwd=paths.cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return AsrResult(ok=False, error="whisper-cli 逾時", loaded_once=False)
        except OSError:
            return AsrResult(ok=False, error="whisper-cli 無法啟動。請重新安裝執行檔與 DLL。", loaded_once=False)
        finally:
            paths.close()
        text = " ".join(line.strip() for line in (proc.stdout or "").splitlines() if line.strip())
        if proc.returncode != 0:
            err = (proc.stderr or "")[-400:] or "Breeze 辨識失敗"
            return AsrResult(ok=False, text=text, error=err, loaded_once=False)
        return AsrResult(ok=True, text=text, loaded_once=False)


def require_loopback_url(base_url: str) -> str:
    """Accept only a fully parsed loopback URL. A string prefix is not enough."""
    try:
        parts = urlsplit((base_url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise ValueError("常駐辨識網址無法解析") from exc
    if parts.scheme not in {"http", "https"}:
        raise ValueError("常駐辨識網址必須是 http 或 https")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("常駐辨識只允許 loopback，拒絕 " + (host or "空位址"))
    if parts.username is not None or parts.password is not None:
        raise ValueError("常駐辨識網址不能帶帳號")
    if parts.query or parts.fragment:
        raise ValueError("常駐辨識網址不能帶查詢字串或片段")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("常駐辨識網址的埠不正確")
    return base_url.strip()


class ResidentAsr:
    """Local whisper-server adapter. A ready flag without a transport or process is not inference."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8178",
        transport: Callable | None = None,
        server_bin: Path | None = None,
        model: Path | None = None,
        popen: Callable | None = None,
        threads: int = 6,
        startup_timeout_s: float = 180,
        inference_timeout_s: float = 120,
        poll_interval_s: float = 0.25,
        health_probe: Callable | None = None,
        audio_context: int = 0,
        beam_size: int = 0,
        best_of: int = 0,
    ):
        self.base_url = require_loopback_url(base_url)
        self.transport = transport
        self.server_bin = Path(server_bin) if server_bin else None
        self.model = Path(model) if model else None
        self.popen = popen
        self.threads = threads
        self.calls = 0
        self.loads = 0
        self.restarts = 0
        self.max_restarts = 2
        self.ready = False
        self.proc = None
        self.last_error = ""
        self.startup_timeout_s = startup_timeout_s
        self.inference_timeout_s = inference_timeout_s
        self.poll_interval_s = poll_interval_s
        self.health_probe = health_probe
        self.audio_context = audio_context
        self.beam_size = beam_size
        self.best_of = best_of
        self.native_paths = None
        self.startup_output = b""
        self.output_thread = None
        self.capture_startup = False

    def _drain_stderr(self, stream) -> None:
        try:
            while block := stream.read(1024):
                if self.capture_startup:
                    self.startup_output = (self.startup_output + block)[-4096:]
        finally:
            stream.close()

    def _startup_failure(self, message: str) -> str:
        if self.output_thread:
            self.output_thread.join(timeout=1)
        detail = self.startup_output.decode("utf-8", errors="replace").strip()[-600:]
        return message + (" " + detail if detail else "")

    def health(self) -> bool:
        if self.transport is not None and self.ready and self.loads >= 1:
            return True
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.health_probe is not None:
                    return bool(self.health_probe())
                with _local_open(self.base_url.rstrip("/") + "/health", timeout=1) as response:
                    body = response.read(65537)
                    return len(body) <= 65536 and json.loads(body).get("status") == "ok"
            except (OSError, ValueError, AttributeError, urllib.error.URLError):
                return False
        return False

    def mark_ready_for_test(self) -> None:
        """Old shortcut. It does not load a model and must not produce caption text."""
        self.ready = True

    def start(self) -> AsrResult:
        if self.transport is not None and self.server_bin is None:
            self.loads = 1
            self.ready = True
            self.last_error = ""
            return AsrResult(ok=True, loaded_once=True)
        if self.server_bin is None or not self.server_bin.exists():
            self.ready = False
            self.last_error = "找不到 whisper-server，沒有改走雲端。"
            return AsrResult(ok=False, error=self.last_error)
        if self.model is None or not self.model.exists():
            self.ready = False
            self.last_error = "找不到 Breeze 模型。請先安裝，不要改走雲端辨識。"
            return AsrResult(ok=False, error=self.last_error)
        if self.proc is not None:
            self.close()
        parts = urlsplit(self.base_url)
        host = parts.hostname or "127.0.0.1"
        port = str(parts.port or (443 if parts.scheme == "https" else 80))
        try:
            with socket.create_connection((host, int(port)), timeout=0.2):
                self.last_error = "常駐辨識埠已被其他服務占用。請關閉該服務或修改 BREEZE_RESIDENT_URL。"
                self.ready = False
                return AsrResult(ok=False, error=self.last_error)
        except OSError:
            pass
        import subprocess
        opener = self.popen or subprocess.Popen
        try:
            self.native_paths = NativePaths(self.model)
            cmd = [str(self.server_bin.resolve()), "-m", self.native_paths.argument(self.model), "--host", host, "--port", port,
                   "-l", "zh", "-t", str(self.threads)]
            if self.audio_context:
                cmd += ["--audio-ctx", str(self.audio_context)]
            if self.beam_size:
                cmd += ["--beam-size", str(self.beam_size)]
            if self.best_of:
                cmd += ["--best-of", str(self.best_of)]
            self.startup_output = b""
            self.capture_startup = True
            self.proc = opener(cmd, cwd=self.native_paths.cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            stream = getattr(self.proc, "stderr", None)
            if stream is not None:
                # Always drain the pipe; keep only a bounded startup diagnostic.
                self.output_thread = threading.Thread(target=self._drain_stderr, args=(stream,), daemon=True)
                self.output_thread.start()
        except OSError as exc:
            self.close()
            self.ready = False
            self.last_error = f"whisper-server 無法啟動：{exc}"[:180]
            return AsrResult(ok=False, error=self.last_error)
        poll = getattr(self.proc, "poll", None)
        if self.proc is None or (callable(poll) and poll() is not None):
            error = self._startup_failure("whisper-server 啟動後立刻結束，沒有改走雲端。")
            self.close()
            self.last_error = error
            return AsrResult(ok=False, error=self.last_error)
        deadline = time.monotonic() + self.startup_timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.last_error = self._startup_failure("whisper-server 在模型就緒前結束，請檢查模型與 DLL。")
                break
            if self.health():
                self.loads += 1
                self.ready = True
                self.capture_startup = False
                self.startup_output = b""
                self.last_error = ""
                return AsrResult(ok=True, loaded_once=self.loads == 1)
            time.sleep(self.poll_interval_s)
        else:
            self.last_error = "Breeze 模型載入逾時；常駐服務尚未就緒。"
        error = self.last_error
        self.close()
        self.last_error = error
        return AsrResult(ok=False, error=error, loaded_once=False)

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        if self.ready and self.transport is None and self.proc is None:
            return AsrResult(ok=False, error="測試就緒標記不能代替推論。常駐服務沒有載入模型。", loaded_once=False)
        if not self.health():
            if self.restarts < self.max_restarts and (self.server_bin or self.transport):
                self.restarts += 1
                started = self.start()
                if not started.ok:
                    return AsrResult(ok=False, error=started.error or "常駐辨識還沒就緒，沒有改走雲端。")
            else:
                return AsrResult(ok=False, error=self.last_error or "常駐辨識還沒就緒，沒有改走雲端。")
        self.calls += 1
        try:
            if self.transport is not None:
                text = self.transport(wav, prompt)
            else:
                text = self._http_inference(wav, prompt)
        except Exception as exc:
            self.last_error = str(exc)[:180]
            return AsrResult(ok=False, error=self.last_error or "常駐辨識失敗", loaded_once=self.loads == 1)
        cleaned = str(text or "").strip()
        if not cleaned:
            return AsrResult(ok=False, error="常駐辨識沒有回傳文字", loaded_once=self.loads == 1)
        return AsrResult(ok=True, text=cleaned, loaded_once=self.loads == 1)

    def _http_inference(self, wav: Path, prompt: str) -> str:
        import secrets
        boundary = "breeze" + secrets.token_hex(16)
        audio = wav.read_bytes()
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n{prompt}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n"
            ).encode() + audio + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with _local_open(req, timeout=self.inference_timeout_s) as resp:
            body = resp.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("常駐辨識回應過大")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise ValueError("常駐辨識沒有回傳有效文字欄位")
        return data["text"]

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        self.ready = False
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        if self.output_thread:
            self.output_thread.join(timeout=1)
            self.output_thread = None
        self.capture_startup = False
        if self.native_paths:
            self.native_paths.close()
            self.native_paths = None
