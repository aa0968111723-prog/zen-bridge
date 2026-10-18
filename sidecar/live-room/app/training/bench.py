"""收錄開／關的額外負擔量測：python -m app.training.bench [--minutes 60] [--pace 0.1] [--asr-ms 900]

在同一個行程裡跑真正的 live server（create_app、/api/push、pipeline、收錄背景執行緒），只把 Breeze 換成
「睡固定毫秒」的假 ASR（不吃 CPU，讓量到的差異只來自收錄）。每段 6 秒、以 pace 倍速送出：
* 每段字幕延遲（送出 → 定稿回來）的中位數／p95／最大：收錄關 vs 開
* 行程 CPU 秒數、RSS 峰值、training 資料夾寫入量 → 換算成「每小時課」
* 課後整理（切段＋聲紋）花的時間 → 換算成「每小時課」
不會碰正式的 training 資料夾（用暫存資料夾）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path


def _pcts(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    return {"median_ms": round(statistics.median(xs), 1), "p95_ms": round(xs[int(len(xs) * 0.95) - 1], 1),
            "max_ms": round(xs[-1], 1), "n": len(xs)}


def _rss() -> int:
    try:
        from app.server import rss_bytes
        return int(rss_bytes() or 0)
    except Exception:
        return 0


async def _run(mode: str, n: int, pace: float, asr_ms: int, wavs: list[bytes], root: Path) -> dict:
    os.environ["ZEN_TRAINING_DIR"] = str(root)
    from httpx import ASGITransport, AsyncClient
    from app.asr import AsrResult
    from app.server import create_app
    from app.settings import Settings
    from app.training import capture, harvest, registry
    from app.training import db as tdb
    from app.translate import Translator

    capture._reset_for_tests(root)
    if mode == "on":
        c = tdb.connect(root)
        s = registry.new_speaker(c, "量測講者")["id"]
        registry.record_consent(c, s, scope_asr=True, scope_mt=False, harvest_live=True, recorded_by="bench",
                                confirm_text=True)
        harvest.set_room(c, room_id="bench", speaker_id=s, collect=True)
        c.close()

    class SleepAsr:
        def transcribe(self, wav, prompt):
            time.sleep(asr_ms / 1000)
            return AsrResult(ok=True, text="今天我們繼續講止觀，先調身調息，再把心安住在呼吸上")

    def decoder(src, work):
        out = work / "a.wav"
        out.write_bytes(src.read_bytes())
        return out

    app = create_app(Settings(allow_testclient=True), asr=SleepAsr(), translator=Translator(enabled=False, key=""),
                     decoder=decoder)
    lat, rss_peak = [], _rss()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=60) as cl:
        tok = (await cl.get("/api/host-token")).json()["token"]
        h = {"authorization": f"Bearer {tok}", "origin": "http://127.0.0.1"}
        await cl.post("/api/rooms/open", json={"room_id": "bench"}, headers={**h, "content-type": "application/json"})
        if mode == "on":
            for _ in range(200):
                if capture.armed("bench"):
                    break
                await asyncio.sleep(0.05)
        start = time.perf_counter()
        inflight = []

        async def push(i):
            t = time.perf_counter()
            r = await cl.post("/api/push", headers=h, data={"room_id": "bench", "session_id": "s", "seq": str(i + 1),
                                                           "t0_ms": str(i * 6000), "t1_ms": str(i * 6000 + 6000),
                                                           "wait_translation": "0"},
                              files={"audio": ("a.wav", wavs[i % len(wavs)], "audio/wav")})
            if r.status_code == 200:
                lat.append((time.perf_counter() - t) * 1000)

        for i in range(n):
            due = start + i * 6.0 * pace
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            inflight.append(asyncio.create_task(push(i)))
            if i % 20 == 0:
                rss_peak = max(rss_peak, _rss())
        await asyncio.gather(*inflight)
        await cl.post("/api/session/end", json={"room_id": "bench", "session_id": "s", "flush_s": 0},
                      headers={**h, "content-type": "application/json"})
    t_end = time.perf_counter()
    if mode == "on":
        await asyncio.to_thread(capture.flush, 120)
        c = tdb.connect(root)
        for _ in range(600):
            if c.execute("SELECT stopped_at FROM class_sessions").fetchone()[0]:
                break
            await asyncio.sleep(0.1)
        c.close()
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0
    rss_peak = max(rss_peak, _rss())
    await app.state.shutdown()
    disk = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
    out = {"mode": mode, "latency": _pcts(lat), "cpu_s": round(cpu, 2), "wall_s": round(wall, 1),
           "push_phase_s": round(t_end - wall0, 1), "rss_peak_mb": round(rss_peak / 1e6, 1),
           "disk_mb": round(disk / 1e6, 2), "capture": capture.stats()}
    capture._reset_for_tests(None)
    return out


def _derive_cost(root: Path) -> dict:
    from app.training import derive
    from app.training import db as tdb
    c = tdb.connect(root)
    try:
        rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
        cpu0, t0 = time.process_time(), time.perf_counter()
        r = derive.derive_recording(c, root, rid)
        return {"wall_s": round(time.perf_counter() - t0, 1), "cpu_s": round(time.process_time() - cpu0, 1),
                "backend": r.get("backend"), "segments": r.get("segments"), "speaker_check_s": r.get("speaker_check_s")}
    finally:
        c.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.training.bench")
    ap.add_argument("--minutes", type=float, default=60.0, help="模擬幾分鐘的課（6 秒一段）")
    ap.add_argument("--pace", type=float, default=0.1, help="1 = 真實速度；0.1 = 10 倍速")
    ap.add_argument("--asr-ms", type=int, default=900, help="假 ASR 每段花的毫秒（不吃 CPU）")
    ap.add_argument("--wav", help="用這個 16k 單聲道 WAV 的內容（切 6 秒）代替合成語音")
    ap.add_argument("--no-derive", action="store_true")
    ap.add_argument("--json", help="結果另存成這個檔")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.training import audioq, classaudio
    pieces = []
    if a.wav:
        pcm = audioq.read_pcm(Path(a.wav)).tobytes()
        step = 6 * 32000
        pieces = [classaudio._header(step) + pcm[i:i + step] for i in range(0, len(pcm) - step + 1, step)]
    if not pieces:
        import array
        import math
        import random
        for seed in range(1, 9):                        # speech-like tone bursts over a quiet floor
            rnd, f, out = random.Random(seed), 150 + seed * 23, array.array("h")
            for i in range(96000):
                t = i / 16000
                v = rnd.randint(-60, 60)
                if (int(t * 4) % 3) != 2:
                    v += int(8000 * math.sin(2 * math.pi * f * t) * (0.6 + 0.4 * math.sin(6 * math.pi * t)))
                out.append(max(-32768, min(32767, v)))
            pieces.append(classaudio._header(len(out) * 2) + out.tobytes())
    n = max(3, int(a.minutes * 10))
    res = {"segments": n, "class_minutes": round(n * 6 / 60, 1), "pace": a.pace, "asr_ms": a.asr_ms}
    with tempfile.TemporaryDirectory(prefix="zen-bench-") as tmp:
        for mode in ("off", "on", "off2", "on2"):
            root = Path(tmp) / mode
            res[mode] = asyncio.run(_run(mode.rstrip("2"), n, a.pace, a.asr_ms, pieces, root))
            print(json.dumps({mode: res[mode]["latency"], "cpu_s": res[mode]["cpu_s"]}, ensure_ascii=False), flush=True)
        if not a.no_derive:
            res["derive"] = _derive_cost(Path(tmp) / "on")
    hours = n * 6 / 3600
    on_cpu = (res["on"]["cpu_s"] + res["on2"]["cpu_s"]) / 2
    off_cpu = (res["off"]["cpu_s"] + res["off2"]["cpu_s"]) / 2
    res["per_class_hour"] = {
        "extra_cpu_s": round((on_cpu - off_cpu) / hours, 1),
        "extra_cpu_pct_of_one_core": round((on_cpu - off_cpu) / (hours * 3600) * 100, 3),
        "disk_mb": round(res["on"]["disk_mb"] / hours, 1),
        "extra_rss_mb": round(max(res["on"]["rss_peak_mb"], res["on2"]["rss_peak_mb"]) -
                              max(res["off"]["rss_peak_mb"], res["off2"]["rss_peak_mb"]), 1),
        "derive_wall_s": round(res["derive"]["wall_s"] / hours, 1) if "derive" in res else None,
    }
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
