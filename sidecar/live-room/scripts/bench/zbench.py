"""zbench — local benchmark for zen-bridge on the user's own laptop (hardware.md §6).

Measures, per run, RTF / latency / memory for
  * ASR: Breeze-ASR-25 through whisper.cpp (whisper-cli = cold, whisper-server = resident),
    sweeping threads x audio_ctx x beam;
  * translation: Ollama /api/chat (options.num_thread, num_ctx; prompt_eval/eval timings);
  * embeddings: Ollama /api/embed in batches;
  * layer 3: ASR + translation (+ embeddings) together at real time (one slice every 6 s).
Writes runs.csv, segments.csv, samples.csv and results.json under %USERPROFILE%\\zen-bench\\results\\<stamp>.

Safety rules (§6.1), enforced here:
  read-only (no registry / power plan / BIOS / driver / Defender changes); priority and
  affinity only on child processes this script starts; Ollama is only sent HTTP requests
  (never restarted, its env never changed); nothing is downloaded (missing files are
  listed and the run stops); the live room must not be streaming; battery aborts unless
  --allow-battery; results stay on this machine.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

if os.name == 'nt':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')

try:
    import psutil
except ImportError:  # pragma: no cover - bench_local.ps1 installs it into the bench venv
    psutil = None

ROOT = Path(os.environ.get("ZBENCH_ROOT") or (Path.home() / "zen-bench"))
OLLAMA = os.environ.get("ZBENCH_OLLAMA", "http://127.0.0.1:11434")
LIVE_PORT, WHISPER_APP_PORT, OLLAMA_PORT, RESERVED = 8780, 8178, 11434, 8645
RESIDENT_PORT = 18178                      # our own whisper-server, never the app's 8178
TH = {"bg_mean": 15.0, "bg_spike": 30.0, "bg_spike_frac": 0.25, "throttle_drop": 0.15, "throttle_s": 10.0}
TIMING = re.compile(r"whisper_print_timings:\s+(\w+) time =\s+([\d.]+) ms")
PRIO_NAMES = ("idle", "below", "normal", "above")

SAMPLE_ZH = [
    "今天我們來談因緣具足的道理。", "禪修的時候，先把呼吸放慢。", "請大家把手機調成靜音。",
    "這個問題我們下課後再討論。", "佛法不離世間覺。", "我們每個人都有自己的功課。",
    "如果聽不清楚，請舉手讓我知道。", "這一段經文的意思是放下執著。",
]


# ------------------------------------------------------------------ pure helpers (tested)
def pct(xs) -> dict:
    a = sorted(float(x) for x in xs)
    if not a:
        return {"p50": None, "p95": None, "p99": None, "max": None, "n": 0}

    def q(p):
        k = (len(a) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(a) - 1)
        return a[lo] + (a[hi] - a[lo]) * (k - lo)
    return {"p50": q(0.50), "p95": q(0.95), "p99": q(0.99), "max": a[-1], "n": len(a)}


def cer(ref: str, hyp: str) -> float | None:
    """Character error rate for Chinese; whitespace and punctuation ignored."""
    strip = re.compile(r"[\s\u3000-\u303f\uff00-\uff0f\uff1a-\uff20,.!?;:'\"()\[\]-]")
    r, h = strip.sub("", ref or ""), strip.sub("", hyp or "")
    if not r:
        return None
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(r)


def audio_ctx_for(seconds: float, rule: str = "safe") -> int:
    """§4.1: 'linear' = s/30*1500+128 (whisper #1855); 'safe' = 640 floor for <=6 s slices."""
    if rule == "linear":
        return int(round(seconds / 30 * 1500 + 128))
    return max(640, int(-(-int(seconds * 100 + 64) // 64) * 64))


def parse_timings(stderr: str) -> dict:
    return {k: float(v) for k, v in TIMING.findall(stderr or "")}


def classify(samples: list[dict], perf: list[float], on_battery: bool, ev37_delta: int, rc: int,
             devicelost: bool = False, th: dict = TH) -> str:
    if rc != 0 or devicelost:
        return "FAILED"
    if on_battery:
        return "BATTERY"
    bg = [s.get("bg_cpu", 0.0) for s in samples] or [0.0]
    if sum(bg) / len(bg) > th["bg_mean"] or sum(1 for x in bg if x > th["bg_spike"]) / len(bg) > th["bg_spike_frac"]:
        return "UNKNOWN_BG_LOAD"
    if ev37_delta > 0:
        return "THROTTLED"
    if len(perf) > 40:
        base = sum(perf[:40]) / 40
        run = best = 0
        for x in perf:
            run = run + 1 if x < base * (1 - th["throttle_drop"]) else 0
            best = max(best, run)
        if best * 0.25 >= th["throttle_s"]:
            return "THROTTLED"
    return "OK"


def wav_ms(p: Path) -> float:
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate() * 1000


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    if port == RESERVED:
        return False                      # never touch Hermes
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url: str, body: dict | None = None, timeout: float = 120.0, headers: dict | None = None, opener=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ------------------------------------------------------------------ preflight (§6.1)
def live_room_busy(get=http_json) -> tuple[bool, str]:
    """Abort if the live room is streaming. Unknown state counts as busy (conservative)."""
    if not port_open(LIVE_PORT):
        return False, "live room not running"
    try:
        tok = get(f"http://127.0.0.1:{LIVE_PORT}/api/host-token", timeout=2)["token"]
        m = get(f"http://127.0.0.1:{LIVE_PORT}/api/metrics", timeout=3, headers={"Authorization": f"Bearer {tok}"})
    except Exception as exc:
        return True, f"live room is running and its state is unknown ({type(exc).__name__}); close it first"
    busy = [k for k in ("listeners", "pending", "inflight", "translate_queued") if int(m.get(k) or 0) > 0]
    if busy:
        return True, "live room is streaming (" + ", ".join(f"{k}={m.get(k)}" for k in busy) + ")"
    return False, "live room idle"


def on_battery() -> bool:
    if psutil is None or not hasattr(psutil, "sensors_battery"):
        return False
    b = psutil.sensors_battery()
    return bool(b is not None and not b.power_plugged)


def missing_files(matrix: dict) -> list[dict]:
    out = []
    for key in ("whisper_cli", "whisper_server", "llama_bench"):
        p = matrix.get(key)
        if p and not Path(p).exists():
            out.append({"name": key, "path": p, "url": "https://github.com/ggml-org/whisper.cpp/releases (or llama.cpp)"})
    for m in matrix.get("asr_models", []):
        if not Path(m["path"]).exists():
            out.append({"name": m["name"], "path": m["path"], "url": m.get("url", "?"), "license": m.get("license", "?")})
    seg_dir = Path(matrix.get("segments_dir", ROOT / "audio"))
    if matrix.get("layers", {}).get("asr", True) and not list(seg_dir.glob("*.wav")):
        out.append({"name": "audio segments", "path": str(seg_dir),
                    "url": "your own lecture recording cut into 6 s 16 kHz mono WAV files (+ .txt reference)"})
    return out


# ------------------------------------------------------------------ sampling
class Sampler(threading.Thread):
    """Every 250 ms: child-process tree (or named processes) + whole system."""

    def __init__(self, root_pid: int | None = None, names: tuple[str, ...] = (), interval: float = 0.25):
        super().__init__(daemon=True)
        self.root_pid, self.names, self.interval = root_pid, names, interval
        self.rows: list[dict] = []
        self.halt = threading.Event()

    def procs(self):
        if psutil is None:
            return []
        out = []
        if self.root_pid:
            try:
                root = psutil.Process(self.root_pid)
                out = [root] + root.children(recursive=True)
            except psutil.Error:
                pass
        if self.names:
            for p in psutil.process_iter(["name"]):
                if any(n in (p.info.get("name") or "").lower() for n in self.names):
                    out.append(p)
        return out

    def run(self):
        if psutil is None:
            return
        psutil.cpu_percent(None)
        ncpu = psutil.cpu_count() or 1
        while not self.halt.is_set():
            own = rss = peak = priv = 0.0
            for p in self.procs():
                try:
                    mi = p.memory_info()
                    own += p.cpu_percent(None) / ncpu
                    rss += mi.rss
                    peak += getattr(mi, "peak_wset", mi.rss)
                    priv += getattr(mi, "private", getattr(mi, "vms", 0))
                except psutil.Error:
                    pass
            sys_cpu = psutil.cpu_percent(None)
            self.rows.append({"t": time.time(), "sys_cpu": sys_cpu, "own_cpu": own, "bg_cpu": max(0.0, sys_cpu - own),
                              "rss": rss, "peak_wset": peak, "private": priv,
                              "avail": psutil.virtual_memory().available})
            self.halt.wait(self.interval)

    def stop(self) -> list[dict]:
        self.halt.set()
        if self.is_alive():
            self.join(2)
        return self.rows


def mem_summary(rows: list[dict]) -> dict:
    mb = 1024 * 1024
    return {"peak_wset_mb": max((r["peak_wset"] for r in rows), default=0) / mb,
            "peak_private_mb": max((r["private"] for r in rows), default=0) / mb,
            "min_avail_mb": min((r["avail"] for r in rows), default=0) / mb,
            "bg_cpu_mean": sum(r["bg_cpu"] for r in rows) / len(rows) if rows else None}


def set_child_priority(pid: int, prio: str, mask: int | None = None) -> None:
    """Only ever applied to a process this script started."""
    if psutil is None:
        return
    try:
        p = psutil.Process(pid)
        if os.name == "nt":
            p.nice({"idle": psutil.IDLE_PRIORITY_CLASS, "below": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                    "normal": psutil.NORMAL_PRIORITY_CLASS, "above": psutil.ABOVE_NORMAL_PRIORITY_CLASS}[prio])
        elif prio in ("idle", "below"):
            p.nice(19 if prio == "idle" else 10)       # POSIX can only lower without privileges
        if mask:
            p.cpu_affinity([i for i in range(psutil.cpu_count()) if mask >> i & 1])
    except Exception:
        pass


# ------------------------------------------------------------------ ASR
def whisper_args(cli: str, model: str, wav: Path, threads: int, ac: int, beam: int, nfa: bool, no_gpu: bool) -> list[str]:
    args = [cli, "-m", model, "-f", str(wav), "-l", "zh", "-t", str(threads), "-ac", str(ac),
            "-bs", str(beam), "-bo", str(max(beam, 1)), "-nt", "-np"]
    if nfa:
        args.append("-nfa")
    if no_gpu:
        args.append("-ng")
    return args


def run_whisper_cli(cli, model, wav, threads, ac, beam, *, nfa=True, no_gpu=True, prio="above", timeout=300) -> dict:
    args = whisper_args(cli, model, wav, threads, ac, beam, nfa, no_gpu)
    t0 = time.perf_counter()
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    set_child_priority(p.pid, prio)
    s = Sampler(p.pid)
    s.start()
    try:
        out, err = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        rc = -9
    samples = s.stop()
    ms = (time.perf_counter() - t0) * 1000
    audio = wav_ms(wav)
    return {"asr_ms": ms, "audio_ms": audio, "rtf": ms / audio if audio else None, "text": out.strip(), "rc": rc,
            "timings": parse_timings(err), "devicelost": "DeviceLost" in err or "VK_ERROR_DEVICE_LOST" in err,
            "system_info": next((ln for ln in err.splitlines() if ln.startswith("system_info")), ""), "samples": samples}


class ResidentWhisper:
    """whisper-server started by us on RESIDENT_PORT (warm model, closest to the App)."""

    def __init__(self, server: str, model: str, threads: int, ac: int, beam: int, prio: str = "above"):
        self.args = [server, "-m", model, "-t", str(threads), "-l", "zh", "--host", "127.0.0.1",
                     "--port", str(RESIDENT_PORT), "-ac", str(ac), "-bs", str(beam), "-nt", "-ng", "-nfa"]
        self.prio = prio
        self.proc = None

    def __enter__(self):
        if port_open(RESIDENT_PORT):
            raise RuntimeError(f"port {RESIDENT_PORT} is busy")
        self.proc = subprocess.Popen(self.args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        set_child_priority(self.proc.pid, self.prio)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server exited")
            if port_open(RESIDENT_PORT):
                return self
            time.sleep(0.5)
        raise RuntimeError("whisper-server did not start")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def transcribe(self, wav: Path) -> dict:
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + \
               f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"http://127.0.0.1:{RESIDENT_PORT}/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode())
        ms = (time.perf_counter() - t0) * 1000
        audio = wav_ms(wav)
        return {"asr_ms": ms, "audio_ms": audio, "rtf": ms / audio, "text": str(data.get("text", "")).strip(), "rc": 0}


# ------------------------------------------------------------------ Ollama (HTTP only)
def translate_messages(zh: str) -> list[dict]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from app.translate import Translator
        return Translator(enabled=True, key="x", model="bench", system_suffix=" /no_think").build_messages(zh, [], [])
    except Exception:
        return [{"role": "system", "content": "Translate the Traditional Chinese lecture line into natural English. "
                 "Output only the English. /no_think"}, {"role": "user", "content": zh}]


def ollama_translate(model: str, zh: str, threads: int, ctx: int, opener=None) -> dict:
    body = {"model": model, "messages": translate_messages(zh), "stream": False, "think": False, "keep_alive": -1,
            "options": {"num_thread": threads, "num_ctx": ctx, "num_predict": 160, "temperature": 0}}
    t0 = time.perf_counter()
    try:
        d = http_json(f"{OLLAMA}/api/chat", body, opener=opener)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "total_ms": (time.perf_counter() - t0) * 1000}
    wall = (time.perf_counter() - t0) * 1000
    ns = 1e6
    pe_ms = (d.get("prompt_eval_duration") or 0) / ns
    ev_ms = (d.get("eval_duration") or 0) / ns
    load_ms = (d.get("load_duration") or 0) / ns
    return {"ok": True, "total_ms": wall, "ttft_ms": load_ms + pe_ms, "prompt_eval_count": d.get("prompt_eval_count"),
            "prompt_eval_ms": pe_ms, "eval_count": d.get("eval_count"), "eval_ms": ev_ms, "load_ms": load_ms,
            "pp_tps": (d.get("prompt_eval_count") or 0) / (pe_ms / 1000) if pe_ms else None,
            "tg_tps": (d.get("eval_count") or 0) / (ev_ms / 1000) if ev_ms else None,
            "text": (d.get("message") or {}).get("content", "")}


def ollama_embed(model: str, texts: list[str], threads: int, opener=None) -> dict:
    t0 = time.perf_counter()
    try:
        d = http_json(f"{OLLAMA}/api/embed", {"model": model, "input": texts, "options": {"num_thread": threads},
                                              "keep_alive": "5m"}, opener=opener)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    ms = (time.perf_counter() - t0) * 1000
    n = len(d.get("embeddings") or [])
    return {"ok": n == len(texts), "ms": ms, "per_s": n / (ms / 1000) if ms else None,
            "dim": len((d.get("embeddings") or [[]])[0])}


def ollama_models(opener=None) -> list[str]:
    try:
        return [m.get("name") for m in http_json(f"{OLLAMA}/api/tags", None, timeout=5, opener=opener).get("models", [])]
    except Exception:
        return []


# ------------------------------------------------------------------ runner
class Bench:
    def __init__(self, matrix: dict, out: Path, reps: int, cooldown: float, opener=None, battery=False):
        self.m, self.out, self.reps, self.cooldown, self.opener = matrix, out, reps, cooldown, opener
        self.battery = battery
        self.runs: list[dict] = []
        self.segments: list[dict] = []
        self.samples: list[dict] = []

    def _record(self, run: dict, samples: list[dict], seg_rows: list[dict] | None = None):
        run.setdefault("status", classify(samples, [], self.battery, 0, run.get("rc", 0), run.get("devicelost", False)))
        run.update({k: v for k, v in mem_summary(samples).items() if k not in run})
        run["power_source"] = "battery" if self.battery else "ac"
        self.runs.append(run)
        for s in samples:
            self.samples.append({"run_id": run["run_id"], **s})
        for r in seg_rows or []:
            self.segments.append({"run_id": run["run_id"], **r})
        print(f"  {run['run_id']:<40} {run['status']:<16} " + ", ".join(
            f"{k}={run[k]:.1f}" for k in ("lat_p50_ms", "rtf_mean", "tg_tps") if isinstance(run.get(k), (int, float))))

    def wavs(self) -> list[Path]:
        d = Path(self.m.get("segments_dir", ROOT / "audio"))
        files = sorted(d.glob("*.wav"))
        return files[: int(self.m.get("max_segments", 50))]

    def layer_asr(self):
        cli, server = self.m.get("whisper_cli"), self.m.get("whisper_server")
        wavs = self.wavs()
        if not wavs or not (cli or server):
            return
        refs = {w: (w.with_suffix(".txt").read_text(encoding="utf-8") if w.with_suffix(".txt").exists() else None) for w in wavs}
        a = self.m.get("asr", {})
        for model in self.m.get("asr_models", []):
            for threads in a.get("threads", [4, 6]):
                for ac in a.get("audio_ctx", [0, 640]):
                    for beam in a.get("beam", [5]):
                        for mode in a.get("modes", ["cli"]):
                            if mode == "cli" and not cli or mode == "resident" and not server:
                                continue
                            for rep in range(0, self.reps + 1):    # rep 0 = warm-up
                                self._asr_run(model, threads, ac, beam, mode, rep, wavs, refs)
                                time.sleep(self.cooldown)

    def _asr_run(self, model, threads, ac, beam, mode, rep, wavs, refs):
        rid = f"asr-{model['name']}-t{threads}-ac{ac}-b{beam}-{mode}-r{rep}"
        started = time.time()
        rows, samples, rc, devlost = [], [], 0, False
        wl = wavs if rep else wavs[:3]
        if mode == "cli":
            for i, w in enumerate(wl):
                r = run_whisper_cli(self.m["whisper_cli"], model["path"], w, threads, ac, beam)
                samples += r.pop("samples")
                rc, devlost = rc or r["rc"], devlost or r["devicelost"]
                rows.append({"seg_idx": i, "audio_ms": r["audio_ms"], "asr_ms": r["asr_ms"], "rtf": r["rtf"],
                             "encode_ms": r["timings"].get("encode"), "decode_ms": r["timings"].get("decode"),
                             "cer": cer(refs[w], r["text"]) if refs.get(w) else None, "text": r["text"]})
        else:
            try:
                with ResidentWhisper(self.m["whisper_server"], model["path"], threads, ac, beam) as srv:
                    s = Sampler(srv.proc.pid)
                    s.start()
                    for i, w in enumerate(wl):
                        r = srv.transcribe(w)
                        rows.append({"seg_idx": i, "audio_ms": r["audio_ms"], "asr_ms": r["asr_ms"], "rtf": r["rtf"],
                                     "cer": cer(refs[w], r["text"]) if refs.get(w) else None, "text": r["text"]})
                    samples = s.stop()
            except Exception as exc:
                rc = 1
                print(f"  resident failed: {exc}")
        lat = pct([r["asr_ms"] for r in rows])
        cers = [r["cer"] for r in rows if r.get("cer") is not None]
        self._record({"run_id": rid, "layer": 1, "backend": "cpu", "model": model["name"], "threads": threads,
                      "audio_ctx": ac, "beam": beam, "mode": mode, "warmup": int(rep == 0), "rep": rep,
                      "started_at": started, "ended_at": time.time(), "rc": rc, "devicelost": devlost,
                      "seg_n": len(rows), "lat_p50_ms": lat["p50"], "lat_p95_ms": lat["p95"], "lat_p99_ms": lat["p99"],
                      "lat_max_ms": lat["max"],
                      "rtf_mean": sum(r["rtf"] for r in rows) / len(rows) if rows else None,
                      "rtf_ge1_count": sum(1 for r in rows if (r["rtf"] or 0) >= 1),
                      "cer": sum(cers) / len(cers) if cers else None}, samples, rows)

    def layer_llm(self):
        lm = self.m.get("llm", {})
        if not lm.get("enabled", True):
            return
        have = ollama_models(self.opener)
        sentences = self.m.get("sentences") or SAMPLE_ZH
        for model in lm.get("models", ["qwen3:4b"]):
            if have and model not in have and f"{model}:latest" not in have:
                self.runs.append({"run_id": f"llm-{model}", "layer": 2, "model": model, "status": "SKIPPED_NOT_PULLED"})
                print(f"  {model} not pulled in Ollama; skipped (no download)")
                continue
            for threads in lm.get("threads", [4]):
                for ctx in lm.get("ctx", [2048]):
                    for rep in range(0, self.reps + 1):
                        rid = f"llm-{model}-t{threads}-c{ctx}-r{rep}"
                        s = Sampler(names=("ollama",))
                        s.start()
                        res = [ollama_translate(model, zh, threads, ctx, self.opener)
                               for zh in (sentences if rep else sentences[:2])]
                        samples = s.stop()
                        ok = [r for r in res if r["ok"]]
                        lat, ttft = pct([r["total_ms"] for r in ok]), pct([r["ttft_ms"] for r in ok])
                        tg = [r["tg_tps"] for r in ok if r.get("tg_tps")]
                        pp = [r["pp_tps"] for r in ok if r.get("pp_tps")]
                        self._record({"run_id": rid, "layer": 2, "backend": "ollama", "model": model, "threads": threads,
                                      "ctx": ctx, "warmup": int(rep == 0), "rep": rep, "rc": 0 if len(ok) == len(res) else 1,
                                      "seg_n": len(res), "lat_p50_ms": lat["p50"], "lat_p95_ms": lat["p95"],
                                      "lat_p99_ms": lat["p99"], "lat_max_ms": lat["max"], "ttft_p50_ms": ttft["p50"],
                                      "ttft_p95_ms": ttft["p95"], "ttft_p99_ms": ttft["p99"], "ttft_max_ms": ttft["max"],
                                      "tg_tps": sum(tg) / len(tg) if tg else None, "pp_tps": sum(pp) / len(pp) if pp else None,
                                      "prompt_eval_count_mean": (sum(r["prompt_eval_count"] or 0 for r in ok) / len(ok)) if ok else None},
                                     samples, [{"seg_idx": i, "en_latency_ms": r.get("total_ms"), "text": r.get("text", "")}
                                               for i, r in enumerate(res)])
                        time.sleep(self.cooldown)

    def layer_embed(self):
        em = self.m.get("embed", {})
        if not em.get("enabled", True):
            return
        model = em.get("model", "qwen3-embedding:0.6b")
        have = ollama_models(self.opener)
        if have and model not in have and f"{model}:latest" not in have:
            self.runs.append({"run_id": f"emb-{model}", "layer": 2, "model": model, "status": "SKIPPED_NOT_PULLED"})
            return
        base = self.m.get("sentences") or SAMPLE_ZH
        for batch in em.get("batches", [16, 64]):
            texts = (base * (batch // len(base) + 1))[:batch]
            for rep in range(0, self.reps + 1):
                s = Sampler(names=("ollama",))
                s.start()
                r = ollama_embed(model, texts, int(em.get("threads", 2)), self.opener)
                samples = s.stop()
                self._record({"run_id": f"emb-{model}-b{batch}-r{rep}", "layer": 2, "backend": "ollama", "model": model,
                              "batch": batch, "warmup": int(rep == 0), "rep": rep, "rc": 0 if r["ok"] else 1,
                              "lat_p50_ms": r.get("ms"), "per_s": r.get("per_s"), "dim": r.get("dim")}, samples)
                time.sleep(min(self.cooldown, 5))

    def layer_concurrent(self):
        """Layer 3: real-time feed (one slice per 6 s) through resident whisper + translate queue."""
        c3 = self.m.get("concurrent", {})
        server, wavs = self.m.get("whisper_server"), self.wavs()
        if not c3.get("enabled", False) or not server or not wavs or not self.m.get("asr_models"):
            return
        model = self.m["asr_models"][0]
        minutes = float(c3.get("minutes", 10))
        llm_model = c3.get("llm_model", "qwen3:4b")
        for rep in range(1, self.reps + 1):
            rid = f"l3-{model['name']}-{llm_model}-r{rep}"
            rows, q, lock = [], [], threading.Lock()
            stop = threading.Event()
            skipped = [0]

            def translator():
                while not stop.is_set() or q:
                    with lock:
                        while len(q) > int(c3.get("queue_max", 4)):
                            q.pop(0)
                            skipped[0] += 1
                        item = q.pop(0) if q else None
                    if item is None:
                        time.sleep(0.05)
                        continue
                    idx, zh, t_end = item
                    if time.time() - t_end > float(c3.get("stale_s", 8)) and q:
                        skipped[0] += 1
                        rows[idx]["en_status"] = "skipped_backlog"
                        continue
                    r = ollama_translate(llm_model, zh or "。", int(c3.get("llm_threads", 4)), 2048, self.opener)
                    rows[idx]["en_latency_ms"] = (time.time() - t_end) * 1000
                    rows[idx]["en_status"] = "ok" if r["ok"] else "error"
            try:
                with ResidentWhisper(server, model["path"], int(c3.get("asr_threads", 6)), int(c3.get("audio_ctx", 640)),
                                     int(c3.get("beam", 5))) as srv:
                    s = Sampler(srv.proc.pid, names=("ollama",))
                    s.start()
                    th = threading.Thread(target=translator, daemon=True)
                    th.start()
                    t_start, i = time.time(), 0
                    while time.time() - t_start < minutes * 60:
                        w = wavs[i % len(wavs)]
                        due = t_start + (i + 1) * wav_ms(w) / 1000
                        time.sleep(max(0.0, due - time.time()))
                        t_end = time.time()
                        r = srv.transcribe(w)
                        rows.append({"seg_idx": i, "audio_ms": r["audio_ms"], "asr_ms": r["asr_ms"], "rtf": r["rtf"],
                                     "zh_latency_ms": (time.time() - t_end) * 1000, "queue_wait_ms": None,
                                     "en_status": "queued", "text": r["text"]})
                        with lock:
                            q.append((i, r["text"], t_end))
                        i += 1
                    stop.set()
                    th.join(60)
                    samples = s.stop()
                rc = 0
            except Exception as exc:
                print(f"  layer 3 failed: {exc}")
                samples, rc = [], 1
            zl = pct([r["zh_latency_ms"] for r in rows])
            el = pct([r["en_latency_ms"] for r in rows if r.get("en_latency_ms") is not None])
            self._record({"run_id": rid, "layer": 3, "config_id": c3.get("config_id", "A"), "model": model["name"],
                          "rep": rep, "warmup": 0, "rc": rc, "seg_n": len(rows), "lat_p50_ms": zl["p50"],
                          "lat_p95_ms": zl["p95"], "lat_p99_ms": zl["p99"], "lat_max_ms": zl["max"],
                          "en_p50_ms": el["p50"], "en_p95_ms": el["p95"], "skipped": skipped[0],
                          "rtf_mean": sum(r["rtf"] for r in rows) / len(rows) if rows else None}, samples, rows)

    def write(self, env_info: dict) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        for name, rows in (("runs.csv", self.runs), ("segments.csv", self.segments), ("samples.csv", self.samples)):
            keys: list[str] = []
            for r in rows:
                keys += [k for k in r if k not in keys]
            with open(self.out / name, "w", newline="", encoding="utf-8") as fh:
                wr = csv.DictWriter(fh, fieldnames=keys or ["run_id"])
                wr.writeheader()
                wr.writerows(rows)
        result = {"schema": "zbench/1", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "env": env_info,
                  "thresholds": TH, "runs": self.runs, "segments": self.segments,
                  "note": "No cross-run averages by design; compare rows with status=OK only."}
        path = self.out / "results.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


def collect_env(out: Path) -> dict:
    info = {"platform": platform.platform(), "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "physical_cores": psutil.cpu_count(logical=False) if psutil else None,
            "mem_total_mb": psutil.virtual_memory().total // 2**20 if psutil else None,
            "battery": on_battery(), "ollama_models": ollama_models()}
    ps1 = Path(__file__).with_name("collect_env.ps1")
    if os.name == "nt" and ps1.exists() and shutil.which("powershell"):
        envj = out / "env.json"
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Out", str(envj)],
                       check=False, timeout=180)
        if envj.exists():
            try:
                info["windows"] = json.loads(envj.read_text(encoding="utf-8-sig"))
            except ValueError:
                info["windows"] = None
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="zen-bridge local benchmark (read-only; see docstring)")
    ap.add_argument("--matrix", type=Path, default=ROOT / "matrix.json")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--cooldown", type=float, default=30.0)
    ap.add_argument("--layers", default="asr,llm,embed", help="comma list of asr,llm,embed,concurrent")
    ap.add_argument("--allow-battery", action="store_true")
    ap.add_argument("--check", action="store_true", help="only run the safety checks and list missing files")
    args = ap.parse_args(argv)

    matrix = json.loads(args.matrix.read_text(encoding="utf-8")) if args.matrix.exists() else {}
    layers = {x.strip() for x in args.layers.split(",") if x.strip()}
    matrix.setdefault("layers", {})["asr"] = "asr" in layers
    busy, why = live_room_busy()
    print(f"live room: {why}")
    if busy:
        print("中止：直播中或狀態不明。請先結束直播並關閉 zen-bridge 再跑。")
        return 3
    battery = on_battery()
    if battery and not args.allow_battery:
        print("中止：目前用電池。請插電，或加 --allow-battery（結果會標記 BATTERY）。")
        return 4
    if port_open(WHISPER_APP_PORT):
        print(f"注意：{WHISPER_APP_PORT} 有程式在聽（App 的 whisper 服務？）。本工具不會使用它。")
    print("Ollama: " + ("up" if port_open(OLLAMA_PORT) else "down（翻譯與 embedding 測試會失敗並記 FAILED）"))
    missing = missing_files(matrix) if "asr" in layers else []
    if missing:
        print("缺少以下檔案（本工具不會下載；請自行取得後放到指定位置）：")
        for m in missing:
            print(f"  - {m['name']}: {m['path']}  來源：{m.get('url')}  授權：{m.get('license', '?')}")
    if args.check:
        return 0 if not missing else 5
    if missing:
        print("ASR 層跳過（缺檔）；其他層照跑。")
        layers.discard("asr")
        layers.discard("concurrent")
    print("提示：要測 iGPU，請自行決定是否設定 OLLAMA_IGPU_ENABLE 等環境變數並重啟 Ollama；本工具不會改。")
    out = args.out or (ROOT / "results" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    env_info = collect_env(out)
    bench = Bench(matrix, out, args.reps, args.cooldown, battery=battery)
    if "asr" in layers:
        print("[layer 1] ASR")
        bench.layer_asr()
    if "llm" in layers:
        print("[layer 2] translation (Ollama)")
        bench.layer_llm()
    if "embed" in layers:
        print("[layer 2] embeddings (Ollama)")
        bench.layer_embed()
    if "concurrent" in layers:
        print("[layer 3] concurrent, real time")
        bench.layer_concurrent()
    path = bench.write(env_info)
    print(f"結果：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
