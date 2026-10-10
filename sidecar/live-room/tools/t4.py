"""round4 §3.3: T4 measurement matrix (``python tools/rtf_check.py t4 ...``).

Layers: ASR (resident Breeze, per plan row), draft (X-ASR streaming, determinism check),
MT (llama-server, per language), concurrent (ASR + draft + MT at real pace). One JSONL row per
plan row; a Markdown table at the end for docs/TEST-REPORT.md.

Safety (same as zbench §6.1): AC power required unless the plan says otherwise (exit 3);
a live room that is streaming - or whose state is unknown - stops the run (exit 4); nothing
is downloaded (missing files are listed, exit 6); a plan error exits 2. Running the real
matrix also needs ``--approved`` (benchmark hold: only with 柏能's OK), else exit 5.
Only durations, counts and CER numbers are written - never transcripts or audio.

Every engine is injected through ``Engines`` so the whole flow runs with fakes in tests.
"""
from __future__ import annotations

import json
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]

EXIT_OK, EXIT_PLAN, EXIT_BATTERY, EXIT_BUSY, EXIT_HOLD, EXIT_MISSING = 0, 2, 3, 4, 5, 6
ROW_KEYS = ("layer", "id", "n", "rtf_p50", "rtf_p95", "fallbacks", "cer", "first_partial_p50",
            "mt_wall_p50", "mt_wall_p95", "gate_fail", "deterministic", "on_ac", "ev37", "power_plan", "ts")
TOP_REQUIRED = {"segments": int, "segment_s": (int, float), "reps": int}
TOP_OPTIONAL = {"cooldown_s": (int, float), "require_ac": bool, "abort_on_ev37": bool, "asr": list,
                "draft": list, "mt": list, "concurrent": dict, "mt_sentences": int, "det_runs": int}
ASR_KEYS = {"id": str, "model": str, "threads": int, "audio_ctx": int, "beam": int, "prompt": str,
            "no_fallback": bool, "flash_attn": bool}
DRAFT_KEYS = {"id": str, "dir": str, "threads": int, "precision": str}
MT_KEYS = {"id": str, "gguf": str, "threads": int, "langs": list, "profile": str,
           "ngl": int, "server": str}       # ngl/server: GPU (Vulkan) llama-server rows
MT_SAMPLE = [
    "今天我們來談因緣具足的道理。", "禪修的時候，先把呼吸放慢。", "請大家把手機調成靜音。",
    "這個問題我們下課後再討論。", "佛法不離世間覺。", "我們每個人都有自己的功課。",
    "如果聽不清楚，請舉手讓我知道。", "這一段經文的意思是放下執著。", "般若的意思是智慧。",
    "菩提心就是覺悟的心。", "請把講義翻到第三頁。", "我們先休息十分鐘。",
    "空性不是什麼都沒有。", "要常常觀照自己的念頭。", "感謝大家今天來參加。",
    "下週我們繼續講這一品。", "有沒有人想分享心得？", "慈悲和智慧要一起修。",
    "這句話出自金剛經。", "願大家吉祥平安。",
]


class PlanError(ValueError):
    pass


# ------------------------------------------------------------------ plan
def _expand(value: str, env) -> str:
    return re.sub(r"%([A-Z_][A-Z0-9_]*)%", lambda m: env.get(m.group(1), m.group(0)), value)


def _check_row(row, keys: dict, where: str, required=("id",)) -> None:
    if not isinstance(row, dict):
        raise PlanError(f"{where} 每一列都必須是物件")
    for k in required:
        if k not in row:
            raise PlanError(f"{where} 缺少欄位 {k}")
    for k, v in row.items():
        if k not in keys:
            raise PlanError(f"{where}.{row.get('id', '?')} 有未知欄位 {k}")
        if not isinstance(v, keys[k]) or (keys[k] is int and isinstance(v, bool)):
            raise PlanError(f"{where}.{row.get('id', '?')}.{k} 型別不對")


def load_plan(source, env=None) -> dict:
    env = os.environ if env is None else env
    plan = json.loads(Path(source).read_text(encoding="utf-8-sig")) if not isinstance(source, dict) else json.loads(json.dumps(source))
    if not isinstance(plan, dict):
        raise PlanError("plan 必須是 JSON 物件")
    for k, t in TOP_REQUIRED.items():
        if k not in plan:
            raise PlanError(f"plan 缺少欄位 {k}")
        if not isinstance(plan[k], t) or isinstance(plan[k], bool):
            raise PlanError(f"plan.{k} 型別不對")
    for k in plan:
        if k not in TOP_REQUIRED and k not in TOP_OPTIONAL:
            raise PlanError(f"plan 有未知欄位 {k}")
        if k in TOP_OPTIONAL and not isinstance(plan[k], TOP_OPTIONAL[k]):
            raise PlanError(f"plan.{k} 型別不對")
    if plan["segments"] < 1 or plan["reps"] < 1 or plan["segment_s"] <= 0:
        raise PlanError("segments、reps 要 >= 1，segment_s 要 > 0")
    seen: set[str] = set()
    for layer, keys, req in (("asr", ASR_KEYS, ("id", "model")), ("draft", DRAFT_KEYS, ("id", "dir")),
                             ("mt", MT_KEYS, ("id", "gguf"))):
        for row in plan.get(layer, []):
            _check_row(row, keys, layer, req)
            if row["id"] in seen:
                raise PlanError(f"id 重複：{row['id']}")
            seen.add(row["id"])
            for k in ("dir", "gguf", "model", "server"):
                if k in row:
                    row[k] = _expand(row[k], env)
        if layer == "draft":
            for row in plan.get("draft", []):
                if row.get("threads", 1) > 1 and row.get("precision", "auto") != "fp32" and "fp32" not in row["dir"]:
                    row["_warn"] = "int8 threads>1 在 box 上不穩定（D1）"
        if layer == "mt":
            for row in plan.get("mt", []):
                langs = row.get("langs", ["en", "ja"])
                if not langs or any(x not in ("en", "ja") for x in langs):
                    raise PlanError(f"mt.{row['id']}.langs 只能是 en／ja")
                if row.get("profile", "hymt") not in ("hymt", "index"):
                    raise PlanError(f"mt.{row['id']}.profile 只能是 hymt／index")
    conc = plan.get("concurrent") or {}
    if conc:
        for k in conc:
            if k not in ("enabled", "pairs", "minutes"):
                raise PlanError(f"concurrent 有未知欄位 {k}")
        ids = {"asr": {r["id"] for r in plan.get("asr", [])}, "draft": {r["id"] for r in plan.get("draft", [])},
               "mt": {r["id"] for r in plan.get("mt", [])}}
        for pair in conc.get("pairs", []):
            if not (isinstance(pair, list) and len(pair) == 3):
                raise PlanError("concurrent.pairs 每一組是 [asr_id, draft_id, mt_id]")
            for layer, pid in zip(("asr", "draft", "mt"), pair):
                if layer == "mt" and pid == "none":
                    continue                     # no local MT model on this machine: ASR + draft only
                if pid not in ids[layer]:
                    raise PlanError(f"concurrent.pairs 的 {layer} id 不存在：{pid}")
        if not isinstance(conc.get("minutes", 5), (int, float)) or conc.get("minutes", 5) <= 0:
            raise PlanError("concurrent.minutes 要 > 0")
    return plan


def missing_files(plan: dict, models_dir: Path) -> list[str]:
    out = []
    for row in plan.get("asr", []):
        p = model_path(row["model"], models_dir)
        if not p.is_file():
            out.append(str(p))
    for row in plan.get("draft", []):
        d = Path(row["dir"])
        if not (d / "tokens.txt").is_file():
            out.append(str(d / "tokens.txt"))
    for row in plan.get("mt", []):
        if not Path(row["gguf"]).is_file():
            out.append(row["gguf"])
    return out


def model_path(model: str, models_dir: Path) -> Path:
    p = Path(model)
    if p.suffix == ".bin" or p.is_absolute():
        return p
    return Path(models_dir) / f"ggml-{model}.bin"


# ------------------------------------------------------------------ helpers
def pct(xs) -> tuple[float | None, float | None]:
    a = sorted(float(x) for x in xs if x is not None)
    if not a:
        return None, None

    def q(p):
        k = (len(a) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(a) - 1)
        return a[lo] + (a[hi] - a[lo]) * (k - lo)
    return round(q(0.5), 4), round(q(0.95), 4)


def cer(ref: str, hyp: str) -> float | None:
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


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def wav_pcm16_mono16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            raise PlanError(f"{path.name} 要是 16 kHz 單聲道 16-bit WAV")
        return w.readframes(w.getnframes())


def load_clips(audio_dir: Path, ref_dir: Path | None, n: int) -> list[tuple[Path, str]]:
    wavs = sorted(Path(audio_dir).glob("*.wav"))[:n]
    if not wavs:
        raise PlanError(f"{audio_dir} 沒有 .wav")
    out = []
    for w in wavs:
        ref = ""
        if ref_dir is not None and (Path(ref_dir) / (w.stem + ".txt")).is_file():
            ref = (Path(ref_dir) / (w.stem + ".txt")).read_text(encoding="utf-8").strip()
        out.append((w, ref))
    return out


def mean_or_none(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


# ------------------------------------------------------------------ engines (injected)
@dataclass
class Engines:
    asr: Callable                      # row -> object with transcribe(wav, prompt) / close() / last_stats
    draft: Callable                    # row -> DraftAsr (feed / finish / reset)
    mt: Callable                       # row -> (backend with translate(zh, tgt_lang=...), close())
    on_battery: Callable = lambda: False
    busy: Callable = lambda: (False, "")
    ev37: Callable = lambda seconds: None
    power_plan: Callable = lambda: None
    clock: Callable = time.monotonic
    sleep: Callable = time.sleep


def _base_row(layer: str, rid: str, ctx: dict) -> dict:
    row = {k: None for k in ROW_KEYS}
    row.update(layer=layer, id=rid, on_ac=ctx["on_ac"], power_plan=ctx["power_plan"],
               ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return row


def _prompt(row: dict) -> str:
    p = row.get("prompt", "")
    if p == "current":                       # the live app's prompt (tools/rtf_check.PROMPT)
        try:
            from tools.rtf_check import PROMPT
        except Exception:
            return ""
        return PROMPT
    return p


def _asr_layer(row: dict, plan: dict, clips, eng: Engines, ctx: dict) -> dict:
    out = _base_row("asr", row["id"], ctx)
    asr = eng.asr(row)
    rtfs, cers, fallbacks, enc, dec = [], [], 0, [], []
    try:
        for _ in range(plan["reps"]):
            for wav, ref in clips:
                secs = wav_seconds(wav)
                t0 = eng.clock()
                res = asr.transcribe(wav, _prompt(row))
                wall = eng.clock() - t0
                if not getattr(res, "ok", False):
                    continue
                rtfs.append(wall / secs if secs > 0 else None)
                stats = getattr(asr, "last_stats", {}) or {}
                fallbacks += int(stats.get("fallbacks", 0) or 0)
                enc.append(stats.get("encode_ms"))
                dec.append(stats.get("decode_ms"))
                if ref:
                    cers.append(cer(ref, getattr(res, "text", "")))
    finally:
        close = getattr(asr, "close", None)
        if close:
            close()
    out["n"] = len(rtfs)
    out["rtf_p50"], out["rtf_p95"] = pct(rtfs)
    out["fallbacks"] = fallbacks
    out["cer"] = mean_or_none(cers)
    out["encode_ms_p50"], _ = pct(enc)
    out["decode_ms_p50"], _ = pct(dec)
    return out


def run_draft_once(draft, pcm: bytes, chunk_ms: int = 100):
    """Feed 100 ms packets as fast as possible. Returns (text, first_partial_s, cpu_wall_s)."""
    step = 16000 * chunk_ms // 1000 * 2
    texts, first, last_partial = [], None, ""
    draft.reset()
    t0 = time.perf_counter()
    for i in range(0, len(pcm), step):
        for p in draft.feed(pcm[i:i + step]):
            if p.text and first is None:
                first = (i + step) / 32000.0
            if p.is_endpoint:
                if p.text:
                    texts.append(p.text)
                last_partial = ""
            else:
                last_partial = p.text
    for p in draft.finish():
        last_partial = p.text or last_partial
    if last_partial:
        texts.append(last_partial)
    return " ".join(texts), first, time.perf_counter() - t0


def _draft_layer(row: dict, plan: dict, clips, eng: Engines, ctx: dict) -> dict:
    out = _base_row("draft", row["id"], ctx)
    draft = eng.draft(row)
    runs = int(plan.get("det_runs", 3))
    rtfs, firsts, cers, same = [], [], [], 0
    for wav, ref in clips:
        pcm = wav_pcm16_mono16k(wav)
        secs = len(pcm) / 32000.0
        outs = []
        for r in range(runs):
            text, first, wall = run_draft_once(draft, pcm)
            outs.append(text)
            if r == 0:
                rtfs.append(wall / secs if secs else None)
                firsts.append(first)
                if ref:
                    cers.append(cer(ref, text))
        same += int(len(set(outs)) == 1)
    out["n"] = len(clips)
    out["rtf_p50"], out["rtf_p95"] = pct(rtfs)
    out["first_partial_p50"], _ = pct(firsts)
    out["cer"] = mean_or_none(cers)
    out["deterministic"] = same == len(clips)
    out["deterministic_clips"] = f"{same}/{len(clips)}"
    out["threads"] = row.get("threads", 1)
    if row.get("_warn"):
        out["warning"] = row["_warn"]
    return out


def _mt_layer(row: dict, plan: dict, sentences, eng: Engines, ctx: dict) -> list[dict]:
    rows = []
    backend, close = eng.mt(row)
    try:
        for lang in row.get("langs", ["en", "ja"]):
            out = _base_row("mt", f"{row['id']}:{lang}", ctx)
            walls, gate_fail, toks = [], 0, []
            for zh in sentences:
                t0 = eng.clock()
                res = backend.translate(zh, tgt_lang=lang)
                wall = eng.clock() - t0
                walls.append(wall)
                if getattr(res, "status", "") != "ok":
                    gate_fail += 1
                ct = getattr(res, "completion_tokens", None)
                if ct and wall > 0:
                    toks.append(ct / wall)
            out["n"] = len(walls)
            out["mt_wall_p50"], out["mt_wall_p95"] = pct(walls)
            out["gate_fail"] = gate_fail
            out["gen_tok_s_p50"], _ = pct(toks)
            rows.append(out)
    finally:
        if close:
            close()
    return rows


def _concurrent_layer(pair, plan: dict, clips, eng: Engines, ctx: dict) -> dict:
    """Breeze + draft + MT together; one slice released every segment_s (real pace)."""
    by = {layer: {r["id"]: r for r in plan.get(layer, [])} for layer in ("asr", "draft", "mt")}
    asr_row, draft_row = by["asr"][pair[0]], by["draft"][pair[1]]
    mt_row = by["mt"].get(pair[2])               # None when the pair says "none"
    conc = plan["concurrent"]
    seg_s = float(plan["segment_s"])
    total_s = float(conc.get("minutes", 5)) * 60.0
    n_slices = max(1, int(total_s // seg_s))
    out = _base_row("concurrent", "+".join(pair), ctx)
    asr = eng.asr(asr_row)
    draft = eng.draft(draft_row)
    mt, mt_close = eng.mt(mt_row) if mt_row is not None else (None, None)
    lang = (mt_row or {}).get("langs", ["en"])[0]
    a3, a5, a6, b2 = [], [], [], []
    lock = threading.Lock()
    jobs: list = []
    stop = threading.Event()
    cv = threading.Condition(lock)

    def asr_worker():
        while True:
            with cv:
                while not jobs and not stop.is_set():
                    cv.wait(0.05)
                if not jobs:
                    return
                released, wav = jobs.pop(0)
            started = eng.clock()
            a3.append(started - released)
            res = asr.transcribe(wav, _prompt(asr_row))
            a5.append(eng.clock() - started)
            text = getattr(res, "text", "") or ""
            if text and mt is not None:
                t0 = eng.clock()
                mt.translate(text, tgt_lang=lang)
                a6.append(eng.clock() - t0)

    worker = threading.Thread(target=asr_worker, daemon=True)
    worker.start()
    try:
        start = eng.clock()
        for i in range(n_slices):
            wav, _ = clips[i % len(clips)]
            due = start + i * seg_s
            delay = due - eng.clock()
            if delay > 0:
                eng.sleep(delay)
            with cv:
                jobs.append((eng.clock(), wav))
                cv.notify()
            pcm = wav_pcm16_mono16k(wav)
            _, _, wall = run_draft_once(draft, pcm)
            b2.append(wall / (len(pcm) / 32000.0) if pcm else None)
        stop.set()
        worker.join(timeout=max(30.0, seg_s * 4))
    finally:
        stop.set()
        for obj in (asr,):
            close = getattr(obj, "close", None)
            if close:
                close()
        if mt_close:
            mt_close()
    out["n"] = len(a5)
    out["a3_p95_s"] = pct(a3)[1]
    out["a5_p95_s"] = pct(a5)[1]
    out["a6_p95_s"] = pct(a6)[1]
    out["b2_rtf_p95"] = pct(b2)[1]
    out["rtf_p95"] = out["b2_rtf_p95"]
    out["a3_ok"] = out["a3_p95_s"] is not None and out["a3_p95_s"] < 6.0
    return out


def run_t4(plan: dict, audio_dir: Path, ref_dir: Path | None, out_path: Path, eng: Engines) -> tuple[int, list[dict]]:
    if plan.get("require_ac", True) and eng.on_battery():
        print("沒有插電（require_ac）。請接上電源後再跑。", file=sys.stderr)
        return EXIT_BATTERY, []
    busy, why = eng.busy()
    if busy:
        print(f"直播服務正在使用中：{why}。請在沒有上課時再跑。", file=sys.stderr)
        return EXIT_BUSY, []
    ctx = {"on_ac": not eng.on_battery(), "power_plan": eng.power_plan()}
    clips = load_clips(audio_dir, ref_dir, plan["segments"])
    refs = [r for _, r in clips if r]
    sentences = (refs + MT_SAMPLE)[: int(plan.get("mt_sentences", 20))]
    rows: list[dict] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cooldown = float(plan.get("cooldown_s", 30))
    jobs = ([("asr", r) for r in plan.get("asr", [])] + [("draft", r) for r in plan.get("draft", [])]
            + [("mt", r) for r in plan.get("mt", [])])
    conc = plan.get("concurrent") or {}
    if conc.get("enabled", False):
        jobs += [("concurrent", p) for p in conc.get("pairs", [])]
    code = EXIT_OK
    with out_path.open("a", encoding="utf-8") as fh:
        for i, (layer, row) in enumerate(jobs):
            if i and cooldown > 0:
                eng.sleep(cooldown)
            t0 = eng.clock()
            if layer == "asr":
                produced = [_asr_layer(row, plan, clips, eng, ctx)]
            elif layer == "draft":
                produced = [_draft_layer(row, plan, clips, eng, ctx)]
            elif layer == "mt":
                produced = _mt_layer(row, plan, sentences, eng, ctx)
            else:
                produced = [_concurrent_layer(row, plan, clips, eng, ctx)]
            ev = eng.ev37(max(1.0, eng.clock() - t0))
            for r in produced:
                r["ev37"] = ev
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                rows.append(r)
            if ev and plan.get("abort_on_ev37", True):
                print(f"偵測到 CPU 降頻事件 37（{ev} 次），停止。", file=sys.stderr)
                code = EXIT_BUSY
                break
    return code, rows


def markdown(rows: list[dict], machine: str = "") -> str:
    head = ("| layer | id | n | RTF p50 | RTF p95 | CER | 第一個草稿字 p50 (s) | MT p50 (s) | MT p95 (s) | "
            "gate fail | 一致 | 插電 | ev37 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [f"T4 結果（{machine or platform.node()}；電源計畫：{(rows[0].get('power_plan') if rows else None) or 'UNKNOWN'}）", "", head]

    def f(v):
        return "" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
    for r in rows:
        lines.append("| " + " | ".join(f(r.get(k)) for k in ("layer", "id", "n", "rtf_p50", "rtf_p95", "cer",
                     "first_partial_p50", "mt_wall_p50", "mt_wall_p95", "gate_fail", "deterministic",
                     "on_ac", "ev37")) + " |")
    conc = [r for r in rows if r["layer"] == "concurrent"]
    for r in conc:
        lines.append(f"\n同時跑 {r['id']}：A3 p95 {f(r.get('a3_p95_s'))} s、A5 p95 {f(r.get('a5_p95_s'))} s、"
                     f"A6 p95 {f(r.get('a6_p95_s'))} s、草稿 RTF p95 {f(r.get('b2_rtf_p95'))}；A3 < 6 s：{r.get('a3_ok')}")
    return "\n".join(lines)


# ------------------------------------------------------------------ real engines (laptop)
def _windows_power_plan(runner=subprocess.run) -> str | None:
    if os.name != "nt":
        return None
    try:
        out = runner(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=10, check=False)
        m = re.search(r"\((.+)\)\s*$", (out.stdout or "").strip())
        return m.group(1) if m else (out.stdout or "").strip()[:80] or None
    except Exception:
        return None


def real_engines(models_dir: Path, llama_server: Path | None) -> Engines:
    sys.path.insert(0, str(ROOT / "scripts" / "bench"))
    import zbench  # noqa: E402
    from app.asr_tuning import NativeTuning
    from app.native_asr import NativeResidentAsr

    def asr(row):
        env = dict(os.environ)
        if row.get("no_fallback"):
            env["BREEZE_ASR_TEMPERATURE_INC"] = "0"
        else:
            env["BREEZE_ASR_TEMPERATURE_INC"] = "0.2"
        env["BREEZE_ASR_FLASH_ATTN"] = "1" if row.get("flash_attn") else "0"
        engine = NativeResidentAsr(model_path(row["model"], models_dir), threads=int(row.get("threads", 6)),
                                   audio_context=int(row.get("audio_ctx", 0)), beam_size=int(row.get("beam", 0)),
                                   tuning=NativeTuning.from_env(env))
        started = engine.start()
        if not started.ok:
            raise SystemExit(f"Breeze 啟動失敗：{started.error}")
        return engine

    def draft(row):
        from app.draft_asr import XAsrDraft, opencc_s2twp
        return XAsrDraft(row["dir"], threads=int(row.get("threads", 1)), precision=row.get("precision", "auto"),
                         convert=opencc_s2twp())

    def mt(row):
        from app.mt_backend import HyMtLlamaServer, IndexTranslateLlamaServer
        exe = Path(row["server"]) if row.get("server") else llama_server
        if exe is None or not Path(exe).is_file():
            raise SystemExit("找不到 llama-server（--llama-server）。不會自動下載。")
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port == 8645:
            port += 1
        proc = subprocess.Popen([str(exe), "-m", row["gguf"], "-t", str(row.get("threads", 2)), "-c", "2048",
                                 "-np", "1", "--host", "127.0.0.1", "--port", str(port),
                                 "-ngl", str(int(row.get("ngl", 0)))],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=__import__("app.gpu_env", fromlist=["worker_env"]).worker_env())
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SystemExit("llama-server 啟動後結束")
            try:
                if zbench.http_json(base + "/health", timeout=2).get("status") == "ok":
                    break
            except Exception:
                time.sleep(0.5)
        cls = IndexTranslateLlamaServer if row.get("profile") == "index" else HyMtLlamaServer
        backend = cls(base_url=base + "/v1")

        def close():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return backend, close

    return Engines(asr=asr, draft=draft, mt=mt, on_battery=zbench.on_battery, busy=zbench.live_room_busy,
                   ev37=zbench.ev37_since, power_plan=_windows_power_plan)


def det_check(draft_factory, clips, threads: list[int], runs: int = 3) -> list[dict]:
    """round4 D1: same clip decoded ``runs`` times per thread count must give identical text."""
    out = []
    for th in threads:
        d = draft_factory(th)
        same = 0
        for wav, _ in clips:
            pcm = wav_pcm16_mono16k(wav)
            texts = {run_draft_once(d, pcm)[0] for _ in range(runs)}
            same += int(len(texts) == 1)
        out.append({"threads": th, "clips": len(clips), "identical": same, "deterministic": same == len(clips)})
    return out
