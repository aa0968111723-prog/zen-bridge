#!/usr/bin/env python3
"""Pre-session microphone soundcheck from a WAV file; stdlib only, read-only.

  python tools/mic_soundcheck.py --wav PATH [--json OUT] [--strict]

Reads a short test recording (PCM 8/16/24/32-bit WAV, any channel count; the
channels are averaged) and reports, on the full-scale dBFS scale:

* rms_dbfs        overall loudness
* peak_dbfs       loudest sample
* clip_ratio      share of samples at or within 0.1% of full scale
* noise_floor_dbfs  10th percentile of 50 ms window RMS (the quiet parts)
* speech_dbfs     90th percentile of 50 ms window RMS (the loud parts)
* snr_db          speech_dbfs - noise_floor_dbfs
* dc_offset       mean sample value as a fraction of full scale

Each metric gets ok / warn / fail with a Traditional Chinese suggestion, and the
overall verdict is the worst one. The tool never writes to the input, never
opens a microphone and never uses the network. JSON goes to stdout; --json also
writes it to a NEW file (an existing file is never overwritten). Exit code is 0
for ok/warn, 1 for fail, 2 for unreadable input; --strict makes warn exit 1.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import wave

WINDOW_MS = 50
SILENCE_DBFS = -120.0

# (warn, fail) thresholds. For "low" metrics a value BELOW the limit is bad.
THRESHOLDS = {
    "speech_dbfs_low": (-30.0, -42.0),
    "speech_dbfs_high": (-6.0, -2.0),
    "peak_dbfs": (-1.0, -0.1),
    "clip_ratio": (0.0005, 0.005),
    "noise_floor_dbfs": (-50.0, -40.0),
    "snr_db": (20.0, 10.0),
    "dc_offset": (0.01, 0.05),
}

ADVICE = {
    "speech_low": "說話音量偏小：請把麥克風靠近講者（約 10–20 公分）或調高輸入增益。",
    "speech_high": "說話音量偏大：請調低輸入增益，避免辨識時失真。",
    "peak": "峰值太接近滿刻度：請調低增益，留一點餘裕。",
    "clip": "偵測到爆音（削波）：請調低增益或讓講者稍微遠離麥克風。",
    "noise": "背景雜訊偏高：請關掉風扇／冷氣出風口附近的位置，或改用指向性麥克風。",
    "snr": "人聲和背景雜訊差距太小：請靠近麥克風、降低環境噪音。",
    "dc": "訊號有直流偏移：請換一個 USB 孔或音訊介面，或檢查麥克風線材。",
    "short": "錄音太短：請錄 5–10 秒正常說話再檢查。",
    "silent": "幾乎沒有聲音：請確認選到正確的麥克風、沒有靜音。",
}

LEVEL_ORDER = {"ok": 0, "warn": 1, "fail": 2}


def to_dbfs(value: float) -> float:
    if value <= 0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(value))


def _decode(raw: bytes, width: int) -> list[float]:
    if width == 1:  # unsigned 8-bit
        return [(b - 128) / 128.0 for b in raw]
    full = float(1 << (8 * width - 1))
    out = []
    for i in range(0, len(raw) - width + 1, width):
        out.append(int.from_bytes(raw[i:i + width], "little", signed=True) / full)
    return out


def read_wav(path: Path) -> tuple[list[float], int]:
    """Return mono samples in [-1, 1] and the sample rate."""
    with wave.open(str(path), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError("only uncompressed PCM WAV is supported")
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if width not in (1, 2, 3, 4):
            raise ValueError(f"unsupported sample width {width}")
        raw = w.readframes(w.getnframes())
    samples = _decode(raw, width)
    if channels > 1:
        frames = len(samples) // channels
        samples = [sum(samples[i * channels:(i + 1) * channels]) / channels
                   for i in range(frames)]
    return samples, rate


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def measure(samples: list[float], rate: int) -> dict:
    n = len(samples)
    duration = n / rate if rate else 0.0
    if n == 0:
        return {"duration_s": 0.0, "samples": 0}
    mean = sum(samples) / n
    rms = math.sqrt(sum(s * s for s in samples) / n)
    peak = max(abs(s) for s in samples)
    clipped = sum(1 for s in samples if abs(s) >= 0.999)
    win = max(1, int(rate * WINDOW_MS / 1000))
    window_rms = []
    for i in range(0, n - win + 1, win):
        chunk = samples[i:i + win]
        window_rms.append(math.sqrt(sum(s * s for s in chunk) / win))
    if not window_rms:
        window_rms = [rms]
    noise = to_dbfs(_percentile(window_rms, 0.10))
    speech = to_dbfs(_percentile(window_rms, 0.90))
    return {
        "duration_s": round(duration, 3),
        "samples": n,
        "sample_rate": rate,
        "rms_dbfs": round(to_dbfs(rms), 2),
        "peak_dbfs": round(to_dbfs(peak), 2),
        "clip_ratio": round(clipped / n, 6),
        "noise_floor_dbfs": round(noise, 2),
        "speech_dbfs": round(speech, 2),
        "snr_db": round(speech - noise, 2),
        "dc_offset": round(mean, 5),
    }


def _grade_high(value: float, key: str) -> str:
    warn, fail = THRESHOLDS[key]
    return "fail" if value >= fail else "warn" if value >= warn else "ok"


def _grade_low(value: float, key: str) -> str:
    warn, fail = THRESHOLDS[key]
    return "fail" if value < fail else "warn" if value < warn else "ok"


def evaluate(m: dict) -> dict:
    checks: list[dict] = []

    def add(name, level, advice_key, value):
        checks.append({"check": name, "level": level, "value": value,
                       "advice": ADVICE[advice_key] if level != "ok" else ""})

    if m.get("samples", 0) == 0 or m.get("duration_s", 0) < 1.0:
        add("duration", "fail", "short", m.get("duration_s", 0.0))
    elif m["duration_s"] < 3.0:
        add("duration", "warn", "short", m["duration_s"])
    if m.get("samples", 0):
        if m["peak_dbfs"] <= -60.0:
            add("signal", "fail", "silent", m["peak_dbfs"])
        else:
            low = _grade_low(m["speech_dbfs"], "speech_dbfs_low")
            high = _grade_high(m["speech_dbfs"], "speech_dbfs_high")
            if low != "ok":
                add("speech_level", low, "speech_low", m["speech_dbfs"])
            elif high != "ok":
                add("speech_level", high, "speech_high", m["speech_dbfs"])
            else:
                add("speech_level", "ok", "speech_low", m["speech_dbfs"])
            add("peak", _grade_high(m["peak_dbfs"], "peak_dbfs"), "peak", m["peak_dbfs"])
            add("clipping", _grade_high(m["clip_ratio"], "clip_ratio"), "clip", m["clip_ratio"])
            add("noise_floor", _grade_high(m["noise_floor_dbfs"], "noise_floor_dbfs"),
                "noise", m["noise_floor_dbfs"])
            add("snr", _grade_low(m["snr_db"], "snr_db"), "snr", m["snr_db"])
            add("dc_offset", _grade_high(abs(m["dc_offset"]), "dc_offset"), "dc", m["dc_offset"])
    verdict = max((c["level"] for c in checks), key=LEVEL_ORDER.__getitem__, default="fail")
    advice = [c["advice"] for c in checks if c["advice"]]
    summary = {"ok": "麥克風狀態良好，可以開始。",
               "warn": "可以開始，但建議先調整：",
               "fail": "請先處理以下問題再開始："}[verdict]
    return {"verdict": verdict, "summary_zh": summary, "advice_zh": advice, "checks": checks}


def soundcheck(path: Path) -> dict:
    samples, rate = read_wav(path)
    metrics = measure(samples, rate)
    return {"file": path.name, "metrics": metrics, **evaluate(metrics)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wav", required=True, type=Path)
    ap.add_argument("--json", type=Path, help="also write the report to this NEW file")
    ap.add_argument("--strict", action="store_true", help="treat warn as failure")
    args = ap.parse_args(argv)
    try:
        report = soundcheck(args.wav)
    except (OSError, EOFError, ValueError, wave.Error) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        try:
            with open(args.json, "x", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except FileExistsError:
            print(f"refusing to overwrite {args.json}", file=sys.stderr)
            return 2
    if report["verdict"] == "fail" or (args.strict and report["verdict"] == "warn"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
