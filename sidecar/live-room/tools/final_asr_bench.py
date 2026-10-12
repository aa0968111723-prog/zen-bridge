"""grok/fast-draft-asr round 2: per-sentence latency + CER of candidate *final* ASR engines.

Each sentence WAV (16 kHz mono) is decoded once after a warm-up, the same way an endpoint-cut
line would be. Reported per engine: decode p50/p95 (s), CER over all sentences (punctuation
removed, output converted to Taiwan Traditional with OpenCC s2twp for models that emit
Simplified), separate CER for the mixed zh/en lines, CPU cores busy while decoding, RSS.

Engines (``--engine`` repeatable, ``name=spec``):
  sherpa:sense_voice:<dir>     sherpa:paraformer:<dir>     sherpa:zipformer_ctc:<dir>
  sherpa:fire_red_ctc:<dir>    sherpa:qwen3:<dir>
  whisper:<server.exe>:<model.bin>[:extra args separated by ,]   (whisper-server, warm, loopback)

No downloads, no network except the local whisper-server on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.draft_latency import _norm, cer, pct  # noqa: E402

try:
    import psutil  # type: ignore
except ImportError:          # pragma: no cover - optional
    psutil = None


def read_pcm(path: Path):
    import array
    with wave.open(str(path), "rb") as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return [x / 32768.0 for x in a], w.getnframes() / w.getframerate()


def s2twp():
    try:
        import opencc  # type: ignore
        return opencc.OpenCC("s2twp").convert
    except Exception:
        return lambda s: s


class SherpaEngine:
    def __init__(self, kind: str, d: str, threads: int):
        import sherpa_onnx  # type: ignore
        d = Path(d)
        R = sherpa_onnx.OfflineRecognizer
        model = next((d / n for n in ("model.int8.onnx", "model.onnx") if (d / n).is_file()), None)
        tokens = str(d / "tokens.txt")
        if kind == "sense_voice":
            self.rec = R.from_sense_voice(model=str(model), tokens=tokens, num_threads=threads, use_itn=True, language="auto")
        elif kind == "paraformer":
            self.rec = R.from_paraformer(paraformer=str(model), tokens=tokens, num_threads=threads)
        elif kind == "zipformer_ctc":
            self.rec = R.from_zipformer_ctc(model=str(model), tokens=tokens, num_threads=threads)
        elif kind == "fire_red_ctc":
            self.rec = R.from_fire_red_asr_ctc(model=str(model), tokens=tokens, num_threads=threads)
        elif kind == "qwen3":
            self.rec = R.from_qwen3_asr(conv_frontend=str(d / "conv_frontend.onnx"), encoder=str(d / "encoder.int8.onnx"),
                                        decoder=str(d / "decoder.int8.onnx"), tokenizer=str(d / "tokenizer"),
                                        num_threads=threads)
        else:
            raise SystemExit(f"unknown sherpa kind {kind}")
        self.convert = s2twp()
        self.proc = psutil.Process() if psutil else None

    def transcribe(self, wav: Path) -> str:
        samples, _ = read_pcm(wav)
        s = self.rec.create_stream()
        s.accept_waveform(16000, samples)
        self.rec.decode_stream(s)
        return self.convert(s.result.text)

    def rss_mb(self):
        return int(self.proc.memory_info().rss / 2**20) if self.proc else None

    def cpu_s(self):
        t = self.proc.cpu_times() if self.proc else os.times()
        return t.user + t.system

    def close(self):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WhisperServerEngine:
    def __init__(self, server: str, model: str, extra: list[str], threads: int):
        self.port = free_port()
        args = [server, "-m", model, "--host", "127.0.0.1", "--port", str(self.port), "-t", str(threads), "-l", "zh"] + extra
        import tempfile
        self.log = tempfile.TemporaryFile()     # never a pipe: an unread pipe fills up and stalls the server
        self.p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=self.log, cwd=str(Path(server).parent))
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                with self._opener().open(f"http://127.0.0.1:{self.port}/", timeout=2):
                    break
            except Exception:
                if self.p.poll() is not None:
                    self.log.seek(0)
                    raise SystemExit("whisper-server exited: " + self.log.read().decode("utf-8", "replace")[-800:])
                time.sleep(0.5)
        else:
            raise SystemExit("whisper-server did not come up in 240 s")
        self.ps = psutil.Process(self.p.pid) if psutil else None

    @staticmethod
    def _opener():
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def transcribe(self, wav: Path) -> str:
        b = "b" + secrets.token_hex(8)
        parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode(),
                 f"--{b}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n".encode(),
                 f"--{b}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n以下是普通話的句子，請用繁體中文輸出。\r\n".encode(),
                 (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + b"\r\n", f"--{b}--\r\n".encode()]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with self._opener().open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8")).get("text", "").strip()

    def rss_mb(self):
        return int(self.ps.memory_info().rss / 2**20) if self.ps else None

    def cpu_s(self):
        if not self.ps:
            return 0.0
        t = self.ps.cpu_times()
        return t.user + t.system

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except Exception:
            self.p.kill()


def build(spec: str, threads: int):
    kind, _, rest = spec.partition(":")
    if kind == "sherpa":
        sub, _, d = rest.partition(":")
        return SherpaEngine(sub, d, threads)
    if kind == "whisper":
        # server:model[:extra,args]  (Windows paths contain ':' -> split on '|')
        bits = rest.split("|")
        extra = bits[2].split(",") if len(bits) > 2 and bits[2] else []
        return WhisperServerEngine(bits[0], bits[1], extra, threads)
    raise SystemExit(f"bad engine {spec}")


def run(name, spec, items, threads):
    t0 = time.monotonic()
    eng = build(spec, threads)
    load_s = time.monotonic() - t0
    try:
        eng.transcribe(items[0][0])                    # warm-up
        times, hyps, cpu, audio = [], [], 0.0, 0.0
        for wav, ref, mixed in items:
            c0, w0 = eng.cpu_s(), time.monotonic()
            hyp = eng.transcribe(wav)
            dt = time.monotonic() - w0
            cpu += eng.cpu_s() - c0
            times.append(dt)
            hyps.append({"wav": wav.name, "ref": ref, "hyp": hyp, "s": round(dt, 3), "mixed": mixed,
                         "cer": cer(ref, hyp)})
            audio += read_pcm(wav)[1]
        zh = [h for h in hyps if not h["mixed"]]
        mx = [h for h in hyps if h["mixed"]]
        return {"engine": name, "spec": spec, "load_s": round(load_s, 1), "n": len(hyps),
                "decode_p50": pct(times, 50), "decode_p95": pct(times, 95), "rtf": round(sum(times) / audio, 3),
                "cer_zh": cer("".join(h["ref"] for h in zh), "".join(h["hyp"] for h in zh)),
                "cer_mixed": cer("".join(h["ref"] for h in mx), "".join(h["hyp"] for h in mx)) if mx else None,
                "cpu_cores": round(cpu / max(1e-6, sum(times)), 2), "rss_mb": eng.rss_mb(), "rows": hyps}
    finally:
        eng.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=Path, required=True, help="JSON list of {wav, text, mixed}")
    ap.add_argument("--engine", action="append", required=True, help="name=spec")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    base = a.set.parent
    items = [(base / r["wav"], r["text"], bool(r.get("mixed"))) for r in json.loads(a.set.read_text(encoding="utf-8"))]
    results = json.loads(a.out.read_text(encoding="utf-8")) if a.out.exists() else []
    for e in a.engine:
        name, _, spec = e.partition("=")
        try:
            r = run(name, spec, items, a.threads)
        except BaseException as exc:            # keep going: one broken engine must not lose the others
            r = {"engine": name, "spec": spec, "error": f"{type(exc).__name__}: {exc}"[:400]}
        results = [x for x in results if x.get("engine") != name] + [r]
        a.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in r.items() if k != "rows"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
