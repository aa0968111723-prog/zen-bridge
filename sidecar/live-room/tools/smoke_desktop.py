#!/usr/bin/env python3
"""Desktop-flow smoke test for the live-room backend (zen-bridge).

Steps (each prints PASS / FAIL / SKIP and the first FAIL names the step):

  start_backend  spawn the backend on a free loopback port, wait for /api/health ready
  host_token     GET /api/host-token (loopback only; the token is never printed)
  open_room      POST /api/rooms/open + /api/session/active
  captions_zh    push audio slices, each must come back with Chinese text
  translation    every pushed line gets a translation that passes the en / ja caption gate
  pause_resume   POST pause -> an upload is refused (409) -> POST resume -> a fresh slice
                 after resume + grace is recognised again; the paused slice never surfaces
  export         GET /api/export srt / vtt / txt / json; SRT cues hold zh + translation,
                 the paused line is absent
  end_session    POST /api/session/end
  shutdown       stop the backend within the shutdown budget

Modes
  --fake (default)  the backend runs in a child process (sys.executable) with fake parts:
                    scripted ASR (the Chinese text rides in a 'zhtx' chunk of a real WAV),
                    a loopback fake Ollama (/api/chat, en) or fake llama-server
                    (/v1/chat/completions, ja) behind the REAL app translator clients
                    (app.translate.Translator / app.mt_backend.HyMtLlamaServer). No network,
                    no models, ledger / TM / VAD / embeddings off, data in a temp dir.
  --real            spawns ``python -m app.run`` (doctor + real ASR + configured MT) with
                    BREEZE_PORT set to the chosen port. Needs --audio <speech.wav>.

Usage
  python tools/smoke_desktop.py --fake --lang en
  python tools/smoke_desktop.py --fake --lang ja --json report.json
  python tools/smoke_desktop.py --real --lang en --audio C:\\path\\speech.wav --timeout 180

Exit code: 0 all steps passed, 1 a step failed, 2 bad arguments. Port 8645 is never used.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORBIDDEN_PORT = 8645
LANGS = ("en", "ja")
STEPS = ("start_backend", "host_token", "open_room", "captions_zh", "translation",
         "pause_resume", "export", "end_session", "shutdown")
FAKE_LINES = ("各位法師、各位居士，大家晚安。", "今天我們繼續講因緣具足的道理。",
              "這一句在私密暫停時錄下，不應該出現。", "恢復之後的第一句話。")
FAKE_EN = {FAKE_LINES[0]: "Good evening, venerable masters and lay friends.",
           FAKE_LINES[1]: "Today we continue with the teaching on conditions coming together.",
           FAKE_LINES[2]: "This line was recorded during private pause.",
           FAKE_LINES[3]: "The first sentence after resuming."}
FAKE_JA = {FAKE_LINES[0]: "法師の皆さん、居士の皆さん、こんばんは。",
           FAKE_LINES[1]: "今日は因縁が整うという教えの続きをお話しします。",
           FAKE_LINES[2]: "この文はプライベート一時停止中に録音されました。",
           FAKE_LINES[3]: "再開してから最初の一文です。"}
PAUSED_INDEX = 2
SHUTDOWN_BUDGET_S = 10.0
ROOM = "smoke"


# ---------------------------------------------------------------------------- report
@dataclass
class Step:
    name: str
    status: str = "SKIP"          # PASS | FAIL | SKIP
    detail: str = ""
    elapsed_ms: int = 0


@dataclass
class Report:
    mode: str
    lang: str
    port: int = 0
    ok: bool = False
    failed_step: str = ""
    total_ms: int = 0
    platform: str = field(default_factory=lambda: f"{platform.system()} {platform.release()} py{platform.python_version()}")
    steps: list = field(default_factory=list)

    def step(self, name: str) -> Step:
        for s in self.steps:
            if s.name == name:
                return s
        s = Step(name)
        self.steps.append(s)
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d


class StepFailed(Exception):
    pass


# ---------------------------------------------------------------------------- audio helpers
def wav_bytes(seconds: float = 0.5, rate: int = 16000, text: str | None = None, tone_hz: float = 220.0) -> bytes:
    """16 kHz mono s16 WAV with a quiet tone. ``text`` rides in a RIFF 'zhtx' chunk (fake ASR)."""
    import math
    frames = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * tone_hz * i / rate))) for i in range(frames))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    data = buf.getvalue()
    if text is None:
        return data
    payload = text.encode("utf-8")
    if len(payload) % 2:
        payload += b"\0"
    chunk = b"zhtx" + struct.pack("<I", len(payload)) + payload
    body = data[12:] + chunk
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def wav_text(data: bytes) -> str | None:
    """Read the 'zhtx' chunk back (None when absent or not a RIFF/WAVE file)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"zhtx":
            return body.rstrip(b"\0").decode("utf-8")
        pos += 8 + size + (size % 2)
    return None


def slice_wav(path: Path, count: int, seconds: float) -> list[tuple[bytes, int, int]]:
    """Cut ``count`` slices of ``seconds`` from a PCM WAV (wrapping around a short file)."""
    with wave.open(str(path), "rb") as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    step = max(1, int(seconds * rate)) * width * ch
    if not frames:
        raise ValueError("音檔是空的")
    out = []
    for i in range(count):
        start = (i * step) % len(frames)
        chunk = (frames[start:] + frames)[:step]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(chunk)
        out.append((buf.getvalue(), int(i * seconds * 1000), int((i + 1) * seconds * 1000)))
    return out


# ---------------------------------------------------------------------------- ports
def free_port(host: str = "127.0.0.1") -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]
        if port != FORBIDDEN_PORT:
            return port
    raise RuntimeError("找不到可用的本機埠")


# ---------------------------------------------------------------------------- caption gates
def accept_translation(text: str, lang: str, zh: str = "") -> bool:
    if not text:
        return False
    from app.mt_backend import validate_caption
    return validate_caption(text, lang, zh=zh) is not None


# ---------------------------------------------------------------------------- fake child server
class _FakeMtHandler:
    """Loopback fake for Ollama /api/chat (en) and llama-server /v1/chat/completions (ja)."""

    def __init__(self, mode: str, lang: str, stop: threading.Event):
        self.mode, self.lang, self.stop = mode, lang, stop

    def reply(self, path: str, body: dict) -> tuple[int, bytes]:
        if self.mode == "down":
            return 503, b"fake MT down"
        if self.mode == "slow":
            self.stop.wait(60)
            return 503, b"fake MT slow"
        messages = body.get("messages") or []
        content = str(messages[-1].get("content") if messages else "")
        table = FAKE_JA if self.lang == "ja" else FAKE_EN
        zh = ""
        try:                                   # app.translate sends a JSON payload {"current": zh}
            zh = str(json.loads(content).get("current") or "")
        except (ValueError, AttributeError):
            # Hy-MT prompt: context lines come first, the line to translate is last.
            found = [(content.rfind(k), k) for k in table if k in content]
            zh = max(found)[1] if found else ""
        text = "" if self.mode == "empty" else table.get(zh, "Smoke translation." if self.lang == "en" else "スモーク翻訳です。")
        if path.endswith("/api/chat"):
            out = {"model": "fake", "message": {"role": "assistant", "content": text}, "done": True,
                   "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}
        else:
            out = {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return 200, json.dumps(out, ensure_ascii=False).encode("utf-8")


def _start_fake_mt(mode: str, lang: str, stop: threading.Event) -> tuple[object, int]:
    import http.server
    logic = _FakeMtHandler(mode, lang, stop)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            code, payload = logic.reply(self.path, body)
            try:
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="smoke-fake-mt", daemon=True).start()
    return srv, srv.server_address[1]


class FakeAsr:
    """Returns the Chinese text stored in the WAV's 'zhtx' chunk (no model)."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str = ""):
        from app.asr import AsrResult
        del prompt
        self.calls += 1
        text = wav_text(Path(wav).read_bytes())
        if text is None:
            return AsrResult(ok=False, error="fake ASR: no zhtx chunk")
        return AsrResult(ok=True, text=text)


def _copy_decoder(src: Path, work: Path) -> Path:
    out = Path(work) / "audio.wav"
    out.write_bytes(Path(src).read_bytes())
    return out


class JaPipelineAdapter:
    """Pipeline-shaped translator over an MtBackend with a fixed tgt_lang.

    The pipeline calls ``translate(zh, glossary=, context=, deadline=, cancel=)`` and has no
    tgt_lang yet; this adapter is the mount point until per-session tgt_lang is wired
    (see NOTES.md). Used only by the fake child for --lang ja.
    """

    enabled = True
    engine = "hymt"
    tokens_used = 0

    def __init__(self, backend, tgt_lang: str = "ja"):
        self.backend = backend
        self.tgt_lang = tgt_lang
        self.model = getattr(backend, "model", "")
        self.key = ""

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        return self.backend.translate(zh, tgt_lang=self.tgt_lang, glossary=glossary, context=context,
                                      deadline=deadline, cancel=cancel)

    def status_label(self) -> str:
        return f"smoke fake {self.tgt_lang}"

    def price_note(self) -> dict:
        return {}


def serve_fake(port: int, lang: str, work: Path, mt_mode: str, fail: str) -> int:
    """Child process entry: real create_app with fake ASR / decoder / MT on 127.0.0.1:port."""
    if fail == "start":
        print("smoke fake backend: simulated start failure", file=sys.stderr, flush=True)
        return 3
    for key in [k for k in os.environ if k.endswith("_API_KEY")]:
        os.environ.pop(key, None)
    os.environ.update({"ZEN_LEDGER": "0", "BREEZE_TM": "0", "BREEZE_VAD": "off", "ZEN_EMBED": "0",
                       "ZEN_DESKTOP_MAINT": "0", "ZEN_LOG": "off", "BREEZE_ASR_LOG": "off",
                       "ZEN_DATA_DIR": str(work / "zen-data")})
    import uvicorn
    from app.server import create_app
    from app.settings import Settings
    stop = threading.Event()
    mt_srv, mt_port = _start_fake_mt(mt_mode, lang, stop)
    settings = Settings(port=port, data_path=str(work / "captions.sqlite3"), translate=True,
                        translate_timeout_s=20.0, silence_rms=0.0)
    if lang == "ja":
        from app.mt_backend import HyMtLlamaServer
        translator = JaPipelineAdapter(HyMtLlamaServer(base_url=f"http://127.0.0.1:{mt_port}/v1", model="fake-hymt"))
    else:
        from app.translate_config import build_translator
        translator = build_translator(settings, env={
            "BREEZE_TRANSLATE_ENGINE": "local", "BREEZE_TRANSLATE_MODEL": "fake",
            "BREEZE_TRANSLATE_BASE_URL": f"http://127.0.0.1:{mt_port}/v1", "BREEZE_TRANSLATE_PROTOCOL": "ollama"})
    app = create_app(settings, asr=FakeAsr(), translator=translator, decoder=_copy_decoder)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           proxy_headers=False, lifespan="on"))

    def watch_stdin():                       # parent closes stdin (or writes "quit") -> graceful exit
        try:
            for line in sys.stdin:
                if line.strip() == "quit":
                    break
        except (OSError, ValueError):
            pass
        stop.set()
        server.should_exit = True
    threading.Thread(target=watch_stdin, name="smoke-stdin", daemon=True).start()
    try:
        server.run()
    finally:
        stop.set()
        mt_srv.shutdown()
    return 0 if server.started else 4


# ---------------------------------------------------------------------------- http client
class Http:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.origin = self.base
        self.token = ""
        self.room = ROOM
        self.session = "smoke-s1"
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def call(self, method: str, path: str, *, body: bytes | None = None, ctype: str | None = None,
             timeout: float = 10.0, auth: bool = True, headers: dict | None = None) -> tuple[int, bytes]:
        h = {"Origin": self.origin}
        if auth and self.token:
            h["Authorization"] = "Bearer " + self.token
        if ctype:
            h["Content-Type"] = ctype
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with self._open(req, timeout=max(0.05, timeout)) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, path: str, payload: dict | None = None, **kw) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        code, raw = self.call(method, path, body=body, ctype="application/json" if body is not None else None, **kw)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            data = {"_raw": raw[:200].decode("utf-8", "replace")}
        return code, data if isinstance(data, dict) else {"_list": data}

    def push(self, seq: int, audio: bytes, t0: int, t1: int, timeout: float) -> tuple[int, dict]:
        boundary = "smoke" + os.urandom(8).hex()
        fields = {"room_id": self.room, "session_id": self.session, "seq": str(seq), "t0_ms": str(t0), "t1_ms": str(t1),
                  "wait_translation": "0"}
        parts = []
        for k, v in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
                     "Content-Type: audio/wav\r\n\r\n".encode() + audio + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        code, raw = self.call("POST", "/api/push", body=b"".join(parts), ctype=f"multipart/form-data; boundary={boundary}",
                              timeout=timeout, headers={"x-breeze-async-translation": "1"})
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = {"_raw": raw[:200].decode("utf-8", "replace")}
        return code, data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------- runner
class Smoke:
    def __init__(self, args):
        self.args = args
        self.mode = "real" if args.real else "fake"
        self.report = Report(self.mode, args.lang)
        for name in STEPS:
            self.report.step(name)
        self.t_start = time.monotonic()
        self.deadline = self.t_start + float(args.timeout)
        self.proc: subprocess.Popen | None = None
        self.http: Http | None = None
        self.work: Path | None = None
        self._own_work = False
        self.logs: tuple | None = None
        self.pushed: dict[int, str] = {}      # seq -> zh (published lines)
        self.paused_zh = ""
        self.room = args.room
        # Unique per run: a persistent real store never sees the same (room, session, seq) twice.
        self.session = f"smoke-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}"

    # -------------------------------------------------- helpers
    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepFailed(f"超過 --timeout {self.args.timeout:g} 秒")
        return left

    def say(self, s: Step) -> None:
        if not self.args.quiet:
            print(f"[{s.status}] {s.name} ({s.elapsed_ms} ms){': ' + s.detail if s.detail else ''}", flush=True)

    def child_log_tail(self, n: int = 1200) -> str:
        if not self.logs:
            return ""
        text = ""
        for f in self.logs:
            try:
                f.flush()
                text += Path(f.name).read_text(encoding="utf-8", errors="replace")[-n:]
            except OSError:
                pass
        return " ".join(text.split())[-n:]

    # -------------------------------------------------- steps
    def start_backend(self) -> str:
        port = self.args.port or free_port()
        self.report.port = port
        if self.args.work_dir:
            self.work = Path(self.args.work_dir)
            self.work.mkdir(parents=True, exist_ok=True)
        else:
            self.work = Path(tempfile.mkdtemp(prefix="zen-smoke-"))
            self._own_work = True
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if self.mode == "fake":
            cmd = [sys.executable, str(Path(__file__).resolve()), "_serve-fake", "--port", str(port),
                   "--lang", self.args.lang, "--work-dir", str(self.work),
                   "--fake-mt", self.args.fake_mt, "--fake-fail", self.args.fake_fail]
        else:
            env["BREEZE_PORT"] = str(port)
            env.setdefault("ZEN_EMBED", "0")
            cmd = [sys.executable, "-m", "app.run"]
        out = open(self.work / "backend.out.log", "w+", encoding="utf-8")
        err = open(self.work / "backend.err.log", "w+", encoding="utf-8")
        self.logs = (out, err)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                     creationflags=flags)
        self.http = Http(port)
        self.http.room, self.http.session = self.room, self.session
        budget = min(self.remaining(), float(self.args.start_timeout))
        end = time.monotonic() + budget
        last = ""
        while time.monotonic() < end:
            rc = self.proc.poll()
            if rc is not None:
                raise StepFailed(f"後端程序提早結束（exit {rc}）：{self.child_log_tail()}")
            try:
                code, data = self.http.json("GET", "/api/health", auth=False, timeout=1.0)
                if data.get("service") == "breeze-live-room" and code == 200 and data.get("ready"):
                    return f"port {port}，{'fake' if self.mode == 'fake' else 'app.run'} 已就緒"
                last = f"HTTP {code} {data.get('error') or ''}".strip()
            except OSError as exc:
                last = type(exc).__name__
            time.sleep(0.1)
        raise StepFailed(f"{budget:.1f} 秒內 /api/health 沒有就緒（最後：{last}）")

    def host_token(self) -> str:
        code, data = self.http.json("GET", "/api/host-token", auth=False, timeout=min(5, self.remaining()))
        tok = data.get("token")
        if code != 200 or not isinstance(tok, str) or not tok:
            raise StepFailed(f"拿不到主持權杖（HTTP {code}）")
        self.http.token = tok
        return "已取得（不印出）"

    def open_room(self) -> str:
        code, data = self.http.json("POST", "/api/rooms/open", {"room_id": self.room}, timeout=min(5, self.remaining()))
        if code != 200 or not data.get("ok"):
            raise StepFailed(f"/api/rooms/open HTTP {code} {data.get('detail', '')}")
        if data.get("paused"):
            raise StepFailed("新開的房間居然是暫停狀態")
        code, d2 = self.http.json("POST", "/api/session/active", {"room_id": self.room, "active": True},
                                  timeout=min(5, self.remaining()))
        if code != 200:
            raise StepFailed(f"/api/session/active HTTP {code}")
        return f"room={self.room} session={self.session}"

    def _slices(self) -> list[tuple[bytes, int, int, str | None]]:
        if self.mode == "fake":
            out = []
            for i, zh in enumerate(FAKE_LINES):
                out.append((wav_bytes(0.5, text=zh), i * 500, (i + 1) * 500, zh))
            return out
        raw = slice_wav(Path(self.args.audio), 4, float(self.args.slice_s))
        return [(a, t0, t1, None) for a, t0, t1 in raw]

    def captions_zh(self) -> str:
        self.slices = self._slices()
        for seq in (1, 2):
            audio, t0, t1, expect = self.slices[seq - 1]
            code, data = self.http.push(seq, audio, t0, t1, timeout=min(self.remaining(), float(self.args.asr_timeout)))
            zh = str(data.get("zh") or "")
            if code != 200 or not zh:
                raise StepFailed(f"seq {seq} 沒有中文字幕（HTTP {code}，status={data.get('status')}，{data.get('detail') or data.get('error') or ''}）")
            if expect is not None and zh != expect:
                raise StepFailed(f"seq {seq} 中文不符：{zh!r} ≠ {expect!r}")
            self.pushed[seq] = zh
        return f"{len(self.pushed)} 段中文已回來"

    def _export_rows(self) -> list[dict]:
        code, raw = self.http.call("GET", f"/api/export?room_id={self.room}&kind=json", timeout=min(5, self.remaining()))
        if code != 200:
            raise StepFailed(f"/api/export json HTTP {code}")
        rows = json.loads(raw.decode("utf-8"))
        return rows if isinstance(rows, list) else []

    def _wait_translations(self, seqs) -> dict[int, str]:
        budget_end = time.monotonic() + min(self.remaining(), float(self.args.mt_timeout))
        got: dict[int, str] = {}
        status: dict[int, str] = {}
        while True:
            for row in self._export_rows():
                if row.get("session_id") != self.session:
                    continue
                seq = int(row.get("seq") or 0)
                if seq in seqs:
                    status[seq] = str(row.get("translate_status") or row.get("status") or "")
                    if row.get("en"):
                        got[seq] = str(row["en"])
            if all(s in got for s in seqs):
                return got
            final_fail = [s for s in seqs if s not in got and status.get(s) not in ("", "queued", "zh_ready", None)
                          and status.get(s) != "ok"]
            if final_fail and all(status.get(s) not in ("zh_ready",) for s in final_fail):
                bad = ", ".join(f"seq {s}={status.get(s)}" for s in final_fail)
                raise StepFailed(f"翻譯缺少：{bad}")
            if time.monotonic() >= budget_end:
                missing = ", ".join(f"seq {s}({status.get(s) or '未出現'})" for s in seqs if s not in got)
                if time.monotonic() >= self.deadline:
                    raise StepFailed(f"超過 --timeout {self.args.timeout:g} 秒，仍缺翻譯：{missing}")
                raise StepFailed(f"{self.args.mt_timeout:g} 秒內沒有翻譯：{missing}")
            time.sleep(0.1)

    def translation(self) -> str:
        got = self._wait_translations(set(self.pushed))
        for seq, text in got.items():
            if not accept_translation(text, self.args.lang, zh=self.pushed[seq]):
                raise StepFailed(f"seq {seq} 的譯文不是合格的 {self.args.lang} 字幕：{text[:60]!r}")
            if self.mode == "fake":
                table = FAKE_JA if self.args.lang == "ja" else FAKE_EN
                if text != table[self.pushed[seq]]:
                    raise StepFailed(f"seq {seq} 譯文不符：{text!r}")
        return f"{len(got)} 段 {self.args.lang} 譯文通過字幕檢查"

    def pause_resume(self) -> str:
        code, data = self.http.json("POST", f"/api/rooms/{self.room}/pause", {}, timeout=min(5, self.remaining()))
        if code != 200 or data.get("paused") is not True:
            raise StepFailed(f"pause HTTP {code} {data}")
        audio, t0, t1, expect = self.slices[PAUSED_INDEX]
        self.paused_zh = expect or ""
        code, data = self.http.push(3, audio, t0, t1, timeout=min(self.remaining(), float(self.args.asr_timeout)))
        if code != 409:
            raise StepFailed(f"暫停中上傳應該被拒（409），實際 HTTP {code} status={data.get('status')}")
        code, data = self.http.json("POST", "/api/rooms/open", {"room_id": self.room}, timeout=min(5, self.remaining()))
        if data.get("paused") is not True:
            raise StepFailed("暫停中 /api/rooms/open 沒有回報 paused")
        code, data = self.http.json("POST", f"/api/rooms/{self.room}/resume", {}, timeout=min(5, self.remaining()))
        if code != 200 or data.get("paused") is not False:
            raise StepFailed(f"resume HTTP {code} {data}")
        audio, t0, t1, expect = self.slices[3]
        # A slice that began capturing before resume + grace is dropped by design; wait it out.
        try:
            from app.pipeline import Pipeline
            grace = float(getattr(Pipeline, "PAUSE_RESUME_GRACE_S", 1.0))
        except Exception:
            grace = 1.0
        wait = (t1 - t0) / 1000.0 + grace + 0.3
        if wait > self.remaining():
            raise StepFailed(f"剩餘時間不夠等恢復緩衝 {wait:.1f} 秒")
        time.sleep(wait)
        code, data = self.http.push(4, audio, t0 + 60000, t1 + 60000,
                                    timeout=min(self.remaining(), float(self.args.asr_timeout)))
        zh = str(data.get("zh") or "")
        if code != 200 or not zh:
            raise StepFailed(f"恢復後的第一段沒有中文（HTTP {code}，{data.get('detail') or data.get('status')}）")
        if expect is not None and zh != expect:
            raise StepFailed(f"恢復後中文不符：{zh!r}")
        self.pushed[4] = zh
        got = self._wait_translations({4})
        if not accept_translation(got[4], self.args.lang, zh=zh):
            raise StepFailed(f"恢復後譯文不合格：{got[4][:60]!r}")
        return f"暫停中上傳被拒 409；恢復等 {wait:.1f} 秒後 seq 4 有中文與譯文"

    def export(self) -> str:
        code, raw = self.http.call("GET", f"/api/export?room_id={self.room}&kind=srt", timeout=min(5, self.remaining()))
        if code != 200:
            raise StepFailed(f"SRT 匯出 HTTP {code}")
        srt = raw.decode("utf-8")
        cues = [c for c in srt.replace("\r\n", "\n").split("\n\n") if c.strip()]
        if len(cues) < len(self.pushed):
            raise StepFailed(f"SRT 只有 {len(cues)} 個 cue，預期至少 {len(self.pushed)}")
        if "-->" not in srt:
            raise StepFailed("SRT 沒有時間軸")
        for zh in self.pushed.values():
            if zh not in srt:
                raise StepFailed(f"SRT 少了中文：{zh[:20]!r}")
        if self.paused_zh and self.paused_zh in srt:
            raise StepFailed("暫停中錄下的那一段出現在 SRT（私密暫停外洩）")
        rows = {int(r.get("seq") or 0): r for r in self._export_rows() if r.get("session_id") == self.session}
        for seq in self.pushed:
            en = str(rows.get(seq, {}).get("en") or "")
            if not en or en not in srt:
                raise StepFailed(f"SRT 少了 seq {seq} 的譯文")
        if 3 in rows and (rows[3].get("zh") or rows[3].get("en")):
            raise StepFailed("暫停中錄下的 seq 3 出現在 JSON 匯出")
        for kind in ("vtt", "txt"):
            code, raw = self.http.call("GET", f"/api/export?room_id={self.room}&kind={kind}", timeout=min(5, self.remaining()))
            if code != 200 or not raw.strip():
                raise StepFailed(f"{kind} 匯出 HTTP {code}")
        if self.args.save_srt:
            Path(self.args.save_srt).write_text(srt, encoding="utf-8")
        return f"SRT {len(cues)} cues；vtt/txt/json 200；暫停段未出現"

    def end_session(self) -> str:
        code, data = self.http.json("POST", "/api/session/end", {"room_id": self.room, "session_id": self.session, "flush_s": 0},
                                    timeout=min(10, self.remaining()))
        if code != 200 or not data.get("ok"):
            raise StepFailed(f"/api/session/end HTTP {code}")
        return "ok"

    def shutdown(self) -> str:
        """Graceful first (stdin close / Ctrl+Break / SIGINT), then terminate, then kill."""
        proc = self.proc
        if proc is None:
            return "沒有啟動"
        if proc.poll() is not None:
            return f"已結束（exit {proc.returncode}）"
        t0 = time.monotonic()
        how = "graceful"
        try:
            if self.mode == "fake":
                try:
                    proc.stdin.write(b"quit\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                except OSError:
                    pass
            elif os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=SHUTDOWN_BUDGET_S * 0.7)
        except subprocess.TimeoutExpired:
            how = "terminate"
            proc.terminate()
            try:
                proc.wait(timeout=SHUTDOWN_BUDGET_S * 0.3)
            except subprocess.TimeoutExpired:
                how = "kill"
                proc.kill()
                proc.wait(timeout=5)
        took = time.monotonic() - t0
        if how != "graceful":
            raise StepFailed(f"後端沒有在 {SHUTDOWN_BUDGET_S * 0.7:.0f} 秒內正常結束，已 {how}")
        return f"{how} 結束（exit {proc.returncode}，{took:.1f} 秒）"

    # -------------------------------------------------- driver
    def run(self) -> Report:
        failed = None
        try:
            for name in STEPS:
                s = self.report.step(name)
                if name == "shutdown":
                    break
                if failed:
                    s.status, s.detail = "SKIP", f"前一步 {failed} 失敗"
                    self.say(s)
                    continue
                t0 = time.monotonic()
                try:
                    s.detail = getattr(self, name)()
                    s.status = "PASS"
                except StepFailed as exc:
                    s.status, s.detail, failed = "FAIL", str(exc), name
                except Exception as exc:  # noqa: BLE001 - a smoke reports, it does not crash
                    s.status, s.detail, failed = "FAIL", f"{type(exc).__name__}: {exc}", name
                s.elapsed_ms = int((time.monotonic() - t0) * 1000)
                self.say(s)
        finally:
            s = self.report.step("shutdown")
            t0 = time.monotonic()
            try:
                s.detail = self.shutdown()
                s.status = "PASS"
            except StepFailed as exc:
                s.status, s.detail = "FAIL", str(exc)
                failed = failed or "shutdown"
            except Exception as exc:  # noqa: BLE001
                s.status, s.detail = "FAIL", f"{type(exc).__name__}: {exc}"
                failed = failed or "shutdown"
            s.elapsed_ms = int((time.monotonic() - t0) * 1000)
            self.say(s)
            for f in self.logs or ():
                try:
                    f.close()
                except OSError:
                    pass
            if self._own_work and not self.args.keep_work and self.work is not None:
                shutil.rmtree(self.work, ignore_errors=True)
        self.report.ok = failed is None
        self.report.failed_step = failed or ""
        self.report.total_ms = int((time.monotonic() - self.t_start) * 1000)
        return self.report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="zen-bridge 桌面流程煙霧測試")
    m = p.add_mutually_exclusive_group()
    m.add_argument("--fake", action="store_true", help="假元件（預設）")
    m.add_argument("--real", action="store_true", help="真元件：python -m app.run，需要 --audio")
    p.add_argument("--lang", choices=LANGS, default="en", help="本場翻譯目標語（每場一種）")
    p.add_argument("--port", type=int, default=0, help="後端埠；0 = 自動找空埠（永不使用 8645）")
    p.add_argument("--timeout", type=float, default=60.0, help="整體時限（秒，不含 shutdown 的 10 秒）")
    p.add_argument("--start-timeout", type=float, default=30.0, help="等 /api/health 就緒的上限")
    p.add_argument("--asr-timeout", type=float, default=30.0, help="單段 /api/push 的上限")
    p.add_argument("--mt-timeout", type=float, default=30.0, help="等翻譯的上限")
    p.add_argument("--room", default=ROOM, help="煙霧測試用的房間代號（預設 smoke）")
    p.add_argument("--audio", help="--real 用的中文語音 WAV（PCM）")
    p.add_argument("--slice-s", type=float, default=3.0, help="--real 每段秒數")
    p.add_argument("--json", help="把報告寫成 JSON（路徑，或 - 表示 stdout）")
    p.add_argument("--save-srt", help="把匯出的 SRT 另存一份")
    p.add_argument("--work-dir", help="後端暫存資料夾（預設建立暫存資料夾，結束後移除）")
    p.add_argument("--keep-work", action="store_true", help="保留自動建立的暫存資料夾")
    p.add_argument("--quiet", action="store_true")
    # test hooks (fake mode only)
    p.add_argument("--fake-mt", choices=("ok", "down", "slow", "empty"), default="ok", help=argparse.SUPPRESS)
    p.add_argument("--fake-fail", choices=("none", "start"), default="none", help=argparse.SUPPRESS)
    return p


def _utf8_streams() -> None:
    """Windows: a piped / redirected stdout defaults to the ANSI code page (cp1252 / cp950), which
    cannot print every caption character. A real console uses the wide-char API and is left alone."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif stream is not None:
                stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_serve-fake"]:
        sp = argparse.ArgumentParser()
        sp.add_argument("--port", type=int, required=True)
        sp.add_argument("--lang", choices=LANGS, default="en")
        sp.add_argument("--work-dir", required=True)
        sp.add_argument("--fake-mt", default="ok")
        sp.add_argument("--fake-fail", default="none")
        a = sp.parse_args(argv[1:])
        if a.port == FORBIDDEN_PORT:
            return 2
        return serve_fake(a.port, a.lang, Path(a.work_dir), a.fake_mt, a.fake_fail)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.port == FORBIDDEN_PORT:
        print("FAIL: 8645 是保留埠，煙霧測試不使用", file=sys.stderr)
        return 2
    try:
        from app.rooms import validate_room_id
        validate_room_id(args.room)
    except Exception:
        print("FAIL: --room 不合法", file=sys.stderr)
        return 2
    if not 0 <= args.port <= 65535 or args.timeout <= 0:
        print("FAIL: --port 或 --timeout 不合法", file=sys.stderr)
        return 2
    if args.real and (not args.audio or not Path(args.audio).is_file()):
        print("FAIL: --real 需要 --audio 指向存在的中文語音 WAV", file=sys.stderr)
        return 2
    report = Smoke(args).run()
    if args.json:
        text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
    if not args.quiet:
        if report.ok:
            print(f"RESULT: PASS（{report.mode}, {report.lang}, {report.total_ms} ms）", flush=True)
        else:
            bad = next((s for s in report.steps if s.name == report.failed_step), None)
            print(f"RESULT: FAIL at step '{report.failed_step}': {bad.detail if bad else ''}", flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    _utf8_streams()
    raise SystemExit(main())
