"""grok/fast-draft-asr: real-time latency of the two-pass caption path (draft -> Breeze final).

Plays a 16 kHz mono WAV at real pace in 100 ms packets through the *app's own* ``DraftHub`` +
``Endpointer`` (same code as ``/ws/draft`` with BREEZE_ENDPOINT_FINAL=1). Each endpoint-cut line
goes to one Breeze worker in a thread (like the pipeline's single ASR worker). With ``--baseline``
the same clip is also cut into fixed 6 s slices (today's host ``periodMs: 6000``) for comparison.

Truth (``--truth``, JSON list of ``{"t0_s", "t1_s", "text"}``) gives where each spoken line
starts/ends in the clip; for a TTS clip the generator knows these exactly.

Reported per line, p50/p95 over lines:
* ``draft_s``  = speech onset -> first grey draft for that line
* ``final_s``  = end of that line's speech -> Breeze final for the last cut covering it
* ``final_from_onset_s`` = speech onset -> that final
* baseline ``final_s`` for fixed 6 s slices (slice end + Breeze time - speech end)

No network, no cloud API, no model download: the draft model folder and the Breeze model /
whisper-server folder are passed in. ``--asr fake`` skips Breeze (draft-only numbers).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.draft_hub import DraftHub, FinalUtterance  # noqa: E402
from app.endpointer import EndpointConfig, Endpointer, pcm16_to_wav  # noqa: E402

PACKET = 1600                                     # 100 ms @ 16 kHz
PROMPT = "以下是普通話的句子，請用繁體中文輸出。"


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None and math.isfinite(x))
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def summary(xs: list[float]) -> dict:
    return {"n": len([x for x in xs if x is not None]), "p50": pct(xs, 50), "p95": pct(xs, 95),
            "max": pct(xs, 100)}


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit("需要 16 kHz mono PCM16 WAV")
        return w.readframes(w.getnframes())


class FakeAsr:
    def transcribe(self, wav, prompt):
        from app.asr import AsrResult
        return AsrResult(ok=True, text="")


def build_breeze(args):
    os.environ.setdefault("BREEZE_ASR_GPU", args.gpu)
    if args.whisper_dir:
        os.environ["BREEZE_WHISPER_DIR"] = str(args.whisper_dir)
    from app.asr_gpu import GpuAsrConfig, VulkanServerAsr
    cfg = GpuAsrConfig.from_env()
    if cfg is None or cfg.errors or cfg.server_bin() is None:
        raise SystemExit(f"Breeze GPU 設定不完整：{getattr(cfg, 'errors', None)}")
    asr = VulkanServerAsr(cfg, Path(args.model), port=args.port)
    started = asr.start()
    if not started.ok:
        raise SystemExit(f"whisper-server 啟動失敗：{started.error}")
    asr.transcribe(write_tmp(pcm16_to_wav(b"\0\0" * 16000)), PROMPT)     # warm-up, not timed
    return asr


_tmp = Path(tempfile.mkdtemp(prefix="draft-latency-"))
_n = [0]


def write_tmp(data: bytes) -> Path:
    _n[0] += 1
    p = _tmp / f"u{_n[0]}.wav"
    p.write_bytes(data)
    return p


class Worker(threading.Thread):
    """One Breeze worker, FIFO, like the pipeline's ASR worker."""

    def __init__(self, asr, t_start: float):
        super().__init__(daemon=True)
        self.asr, self.t_start, self.q, self.done = asr, t_start, queue.Queue(), []

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            tag, wav, t0_ms, t1_ms, queued = item
            t = time.monotonic()
            res = self.asr.transcribe(write_tmp(wav), PROMPT)
            end = time.monotonic()
            self.done.append({"tag": tag, "t0_s": t0_ms / 1000, "t1_s": t1_ms / 1000,
                              "queued_s": round(queued - self.t_start, 3), "asr_s": round(end - t, 3),
                              "done_s": round(end - self.t_start, 3),
                              "text": getattr(res, "text", ""), "ok": getattr(res, "ok", False)})


def run_two_pass(pcm: bytes, factory, asr, cfg: EndpointConfig, pace: float = 1.0) -> dict:
    drafts: list[dict] = []
    hub = DraftHub(factory, publish=lambda e: None)
    sess = hub.open("bench", "s1", endpointer=Endpointer(cfg))
    if sess is None:
        raise SystemExit("草稿模型無法載入（sherpa-onnx／OpenCC／模型資料夾）")
    t_start = time.monotonic()
    worker = Worker(asr, t_start)
    worker.start()
    feed_s = 0.0

    def handle(event, rows):
        now = time.monotonic() - t_start
        if event:
            drafts.append({"t_s": round(now, 3), "seq": event["seq"], "zh": event["zh"]})
        for u in rows:
            worker.q.put((u.seq, u.wav, u.t0_ms, u.t1_ms, time.monotonic()))

    for k in range(0, len(pcm), PACKET * 2):
        due = t_start + (k // 2) / 16000 / pace + PACKET / 16000 / pace   # packet is available at its end
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        t = time.monotonic()
        handle(*hub.feed_full(sess, pcm[k:k + PACKET * 2]))
        feed_s += time.monotonic() - t
    handle(None, hub.flush(sess))
    worker.q.put(None)
    worker.join()
    audio_s = len(pcm) / 2 / 16000
    return {"drafts": drafts, "finals": worker.done, "draft_cpu_rtf": round(feed_s / audio_s, 3),
            "audio_s": round(audio_s, 2)}


def run_baseline(pcm: bytes, asr, slice_s: float = 6.0) -> list[dict]:
    """Fixed slices: slice i is available at its end; one worker; returns done times."""
    out, free_at = [], 0.0
    step = int(slice_s * 16000) * 2
    for i, k in enumerate(range(0, len(pcm), step)):
        chunk = pcm[k:k + step]
        ready = (k + len(chunk)) / 2 / 16000
        t = time.monotonic()
        res = asr.transcribe(write_tmp(pcm16_to_wav(chunk)), PROMPT)
        asr_s = time.monotonic() - t
        start = max(ready, free_at)
        free_at = start + asr_s
        out.append({"tag": i + 1, "t0_s": k / 2 / 16000, "t1_s": ready, "asr_s": round(asr_s, 3),
                    "done_s": round(free_at, 3), "text": getattr(res, "text", "")})
    return out


def score(truth: list[dict], drafts: list[dict], finals: list[dict]) -> list[dict]:
    rows = []
    for i, line in enumerate(truth):
        t0, t1 = float(line["t0_s"]), float(line["t1_s"])
        nxt = float(truth[i + 1]["t0_s"]) if i + 1 < len(truth) else float("inf")
        first = next((d["t_s"] for d in drafts if t0 <= d["t_s"] < nxt and d["zh"].strip()), None)
        cover = [f for f in finals if f["t0_s"] < t1 and f["t1_s"] > t0]
        done = max((f["done_s"] for f in cover), default=None)
        rows.append({"line": i + 1, "text": line.get("text", ""), "t0_s": t0, "t1_s": t1,
                     "draft_s": None if first is None else round(first - t0, 3),
                     "final_s": None if done is None else round(done - t1, 3),
                     "final_from_onset_s": None if done is None else round(done - t0, 3)})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wav", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--draft-model", type=Path, required=True)
    ap.add_argument("--draft-kind", default="xasr", choices=["xasr", "sherpa"])
    ap.add_argument("--draft-threads", type=int, default=1)
    ap.add_argument("--asr", default="breeze", choices=["breeze", "fake"])
    ap.add_argument("--model", type=Path)
    ap.add_argument("--whisper-dir", type=Path)
    ap.add_argument("--gpu", default="vulkan")
    ap.add_argument("--port", type=int, default=8199)
    ap.add_argument("--baseline", action="store_true", help="also time fixed 6 s slices")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    os.environ["BREEZE_DRAFT_ASR"] = args.draft_kind
    os.environ["BREEZE_DRAFT_MODEL_DIR"] = str(args.draft_model)
    os.environ["BREEZE_DRAFT_THREADS"] = str(args.draft_threads)
    from app.draft_asr import NullDraft, draft_from_env
    probe = draft_from_env()
    if isinstance(probe, NullDraft):
        raise SystemExit("草稿模型無法載入（sherpa-onnx／OpenCC／模型資料夾）")
    engines = iter([probe])
    factory = lambda: next(engines, None) or draft_from_env()  # noqa: E731

    pcm = read_wav(args.wav)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    asr = build_breeze(args) if args.asr == "breeze" else FakeAsr()
    try:
        two = run_two_pass(pcm, factory, asr, EndpointConfig())
        rows = score(truth, two["drafts"], two["finals"])
        report = {"clip": str(args.wav.name), "audio_s": two["audio_s"], "lines": len(truth),
                  "draft_cpu_rtf": two["draft_cpu_rtf"], "endpoint": EndpointConfig().__dict__,
                  "two_pass": {"draft_s": summary([r["draft_s"] for r in rows]),
                               "final_s": summary([r["final_s"] for r in rows]),
                               "final_from_onset_s": summary([r["final_from_onset_s"] for r in rows]),
                               "breeze_asr_s": summary([f["asr_s"] for f in two["finals"]]),
                               "cuts": len(two["finals"])},
                  "per_line": rows, "finals": two["finals"], "drafts": two["drafts"][-200:]}
        if args.baseline and args.asr == "breeze":
            base = run_baseline(pcm, asr)
            brows = score(truth, [], base)
            report["baseline_6s"] = {"final_s": summary([r["final_s"] for r in brows]),
                                     "final_from_onset_s": summary([r["final_from_onset_s"] for r in brows]),
                                     "breeze_asr_s": summary([b["asr_s"] for b in base]), "slices": base}
    finally:
        close = getattr(asr, "close", None)
        if close:
            close()
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tp = report["two_pass"]
    print(f"draft  p50={tp['draft_s']['p50']}s p95={tp['draft_s']['p95']}s (onset -> first grey draft)")
    print(f"final  p50={tp['final_s']['p50']}s p95={tp['final_s']['p95']}s (speech end -> Breeze final)")
    if "baseline_6s" in report:
        b = report["baseline_6s"]["final_s"]
        print(f"6s     p50={b['p50']}s p95={b['p95']}s (speech end -> final, fixed 6 s slices)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
