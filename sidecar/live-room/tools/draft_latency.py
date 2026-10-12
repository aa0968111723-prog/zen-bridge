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


_PUNCT = set("，。！？、；：「」『』（）,.!?;:\"'()[] \n\t-—…")


def _norm(text: str) -> str:
    return "".join(ch for ch in text if ch not in _PUNCT).lower()


def cer(ref: str, hyp: str) -> float | None:
    r, h = _norm(ref), _norm(hyp)
    if not r:
        return None
    prev = list(range(len(h) + 1))
    for i, a in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, b in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b))
        prev = cur
    return round(prev[-1] / len(r), 4)


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
    asr.adaptive_ctx = bool(args.adaptive_ctx)
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

    def __init__(self, asr, t_start: float, on_done=None):
        super().__init__(daemon=True)
        self.asr, self.t_start, self.q, self.done = asr, t_start, queue.Queue(), []
        self.on_done = on_done

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
            if self.on_done:
                self.on_done(self.done[-1], wav)


class FastAsr:
    """BREEZE_FAST_FINAL engine behind the pipeline's ``transcribe(wav, prompt)`` shape."""

    def __init__(self, kind: str, model_dir: Path, threads: int):
        from app.fast_final import SherpaOfflineFinal
        self.engine = SherpaOfflineFinal(kind, model_dir, threads=threads)

    def transcribe(self, wav, prompt):
        from app.asr import AsrResult
        text = self.engine.transcribe(Path(wav))
        return AsrResult(ok=True, text=text)


class Corrections(threading.Thread):
    """Breeze re-hears fast finals one at a time; backlog > max_pending drops the oldest (app rule)."""

    def __init__(self, asr, t_start: float, on_fix, max_pending: int = 3):
        super().__init__(daemon=True)
        self.asr, self.t_start, self.on_fix, self.max_pending = asr, t_start, on_fix, max_pending
        self.pending: list = []
        self.cv = threading.Condition()
        self.closed = False
        self.done: list[dict] = []
        self.skipped = 0

    def submit(self, rec: dict, wav: bytes):
        with self.cv:
            self.pending.append((rec, wav))
            while len(self.pending) > self.max_pending:
                self.pending.pop(0)
                self.skipped += 1
            self.cv.notify()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify()

    def run(self):
        from app.fast_final import _norm as same
        while True:
            with self.cv:
                while not self.pending and not self.closed:
                    self.cv.wait()
                if not self.pending:
                    return
                rec, wav = self.pending.pop(0)
            t = time.monotonic()
            res = self.asr.transcribe(write_tmp(wav), PROMPT)
            end = time.monotonic()
            text = getattr(res, "text", "") or ""
            changed = bool(text.strip()) and same(text) != same(rec["text"])
            row = {"tag": rec["tag"], "t0_s": rec["t0_s"], "t1_s": rec["t1_s"], "asr_s": round(end - t, 3),
                   "done_s": round(end - self.t_start, 3), "text": text if changed else rec["text"],
                   "changed": changed}
            self.done.append(row)
            if changed:
                self.on_fix(row)


class Translations(threading.Thread):
    """One local MT worker, FIFO (the pipeline's translate loop); a correction re-queues its line."""

    def __init__(self, mt, lang: str, t_start: float):
        super().__init__(daemon=True)
        self.mt, self.lang, self.t_start, self.q, self.done = mt, lang, t_start, queue.Queue(), []

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            t = time.monotonic()
            res = self.mt.translate(item["text"], tgt_lang=self.lang)
            end = time.monotonic()
            self.done.append({"tag": item["tag"], "t1_s": item["t1_s"], "mt_s": round(end - t, 3),
                              "done_s": round(end - self.t_start, 3), "status": res.status,
                              "en": getattr(res, "text", ""),
                              "zh": item["text"], "corrected": item.get("changed", False)})


def run_two_pass(pcm: bytes, factory, asr, cfg: EndpointConfig, pace: float = 1.0, *,
                 correct_asr=None, mt=None, mt_lang: str = "en") -> dict:
    drafts: list[dict] = []
    hub = DraftHub(factory, publish=lambda e: None)
    sess = hub.open("bench", "s1", endpointer=Endpointer(cfg))
    if sess is None:
        raise SystemExit("草稿模型無法載入（sherpa-onnx／OpenCC／模型資料夾）")
    t_start = time.monotonic()
    mtw = Translations(mt, mt_lang, t_start) if mt is not None else None
    fixer = (Corrections(correct_asr, t_start, on_fix=lambda row: mtw and mtw.q.put(row))
             if correct_asr is not None else None)

    def finished(rec, wav):
        if mtw and rec["text"].strip():
            mtw.q.put(rec)
        if fixer:
            fixer.submit(rec, wav)

    worker = Worker(asr, t_start, on_done=finished)
    for th in (mtw, fixer, worker):
        if th:
            th.start()
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
    if fixer:
        fixer.close()
        fixer.join()
    if mtw:
        mtw.q.put(None)
        mtw.join()
    audio_s = len(pcm) / 2 / 16000
    return {"drafts": drafts, "finals": worker.done, "draft_cpu_rtf": round(feed_s / audio_s, 3),
            "audio_s": round(audio_s, 2), "corrections": fixer.done if fixer else None,
            "corrections_skipped": fixer.skipped if fixer else None, "mt": mtw.done if mtw else None}


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
    ap.add_argument("--adaptive-ctx", action="store_true", help="per-request audio_ctx sized to each line")
    ap.add_argument("--fast-final", choices=["sense_voice", "paraformer"],
                    help="BREEZE_FAST_FINAL engine answers each cut; Breeze (--asr breeze) corrects it")
    ap.add_argument("--fast-final-model", type=Path)
    ap.add_argument("--fast-final-threads", type=int, default=4)
    ap.add_argument("--no-correct", action="store_true", help="fast final without Breeze correction")
    ap.add_argument("--mt-url", help="local llama-server /v1 base (Hy-MT2), translate every final")
    ap.add_argument("--mt-lang", default="en", choices=["en", "ja"])
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
    first_asr, correct_asr = asr, None
    if args.fast_final:
        first_asr = FastAsr(args.fast_final, args.fast_final_model, args.fast_final_threads)
        first_asr.transcribe(write_tmp(pcm16_to_wav(b"\0\0" * 16000)), PROMPT)     # warm-up
        correct_asr = None if (args.no_correct or args.asr != "breeze") else asr
    mt = None
    if args.mt_url:
        from app.mt_backend import HyMtLlamaServer
        mt = HyMtLlamaServer(base_url=args.mt_url)
        mt.translate("今天天氣很好。", tgt_lang=args.mt_lang)                         # warm-up
    try:
        two = run_two_pass(pcm, factory, first_asr, EndpointConfig(), correct_asr=correct_asr, mt=mt,
                           mt_lang=args.mt_lang)
        rows = score(truth, two["drafts"], two["finals"])
        report = {"clip": str(args.wav.name), "audio_s": two["audio_s"], "lines": len(truth),
                  "draft_cpu_rtf": two["draft_cpu_rtf"], "endpoint": EndpointConfig().__dict__,
                  "two_pass": {"draft_s": summary([r["draft_s"] for r in rows]),
                               "final_s": summary([r["final_s"] for r in rows]),
                               "final_from_onset_s": summary([r["final_from_onset_s"] for r in rows]),
                               "breeze_asr_s": summary([f["asr_s"] for f in two["finals"]]),
                               "cuts": len(two["finals"]),
                               "cer": cer("".join(t.get("text", "") for t in truth),
                                          "".join(f["text"] for f in two["finals"]))},
                  "adaptive_ctx": bool(args.adaptive_ctx), "fast_final": args.fast_final,
                  "per_line": rows, "finals": two["finals"], "drafts": two["drafts"][-200:]}
        if two["corrections"] is not None:
            fixed = {c["tag"]: c for c in two["corrections"]}
            settled = [dict(f, **({"text": fixed[f["tag"]]["text"], "done_s": fixed[f["tag"]]["done_s"]}
                                  if f["tag"] in fixed and fixed[f["tag"]]["changed"] else {}))
                       for f in two["finals"]]
            srows = score(truth, [], settled)
            report["corrected"] = {
                "settled_s": summary([r["final_s"] for r in srows]),
                "breeze_asr_s": summary([c["asr_s"] for c in two["corrections"]]),
                "changed": sum(1 for c in two["corrections"] if c["changed"]),
                "skipped": two["corrections_skipped"],
                "cer": cer("".join(t.get("text", "") for t in truth), "".join(f["text"] for f in settled)),
                "rows": two["corrections"]}
        if two["mt"] is not None:
            first_mt = {}
            for m in two["mt"]:
                first_mt.setdefault(m["tag"], m)
            report["mt"] = {"lang": args.mt_lang, "mt_s": summary([m["mt_s"] for m in two["mt"]]),
                            "final_plus_mt_s": summary([round(m["done_s"] - m["t1_s"], 3) for m in first_mt.values()]),
                            "rows": two["mt"]}
        if args.baseline and args.asr == "breeze":
            base = run_baseline(pcm, asr)
            brows = score(truth, [], base)
            report["baseline_6s"] = {"final_s": summary([r["final_s"] for r in brows]),
                                     "final_from_onset_s": summary([r["final_from_onset_s"] for r in brows]),
                                     "breeze_asr_s": summary([b["asr_s"] for b in base]),
                                     "cer": cer("".join(t.get("text", "") for t in truth),
                                                "".join(b["text"] for b in base)),
                                     "slices": base}
    finally:
        close = getattr(asr, "close", None)
        if close:
            close()
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tp = report["two_pass"]
    print(f"draft  p50={tp['draft_s']['p50']}s p95={tp['draft_s']['p95']}s (onset -> first grey draft)")
    print(f"final  p50={tp['final_s']['p50']}s p95={tp['final_s']['p95']}s (speech end -> Breeze final) "
          f"asr p50={tp['breeze_asr_s']['p50']}s cer={tp['cer']}{' (fast final)' if args.fast_final else ''}")
    if "corrected" in report:
        c = report["corrected"]
        print(f"fixed  p50={c['settled_s']['p50']}s p95={c['settled_s']['p95']}s (speech end -> line settled "
              f"after Breeze correction) changed={c['changed']} skipped={c['skipped']} cer={c['cer']}")
    if "mt" in report:
        m = report["mt"]
        print(f"mt     {m['lang']} p50={m['mt_s']['p50']}s; speech end -> first translation "
              f"p50={m['final_plus_mt_s']['p50']}s p95={m['final_plus_mt_s']['p95']}s")
    if "baseline_6s" in report:
        b = report["baseline_6s"]["final_s"]
        print(f"6s     p50={b['p50']}s p95={b['p95']}s (speech end -> final, fixed 6 s slices) "
              f"cer={report['baseline_6s']['cer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
