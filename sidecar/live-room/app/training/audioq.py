"""16 kHz mono PCM16 WAV：讀寫、品質量測（音量、削波、靜音、SNR）、切段、串接。只用標準函式庫。"""
from __future__ import annotations

import array
import hashlib
import io
import math
import shutil
import subprocess
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

RATE = 16000
FRAME = 320                 # 20 ms
CLIP_LEVEL = 32000          # |x| >= 這個值算削波
SILENCE_DBFS = -45.0


class AudioFormatError(ValueError):
    pass


@dataclass
class Quality:
    duration_s: float
    rms_dbfs: float
    peak_dbfs: float
    clip_ratio: float
    silence_ratio: float
    snr_db: float
    problems: list

    def as_dict(self) -> dict:
        return asdict(self)


def _db(x: float) -> float:
    return round(20 * math.log10(x / 32768.0), 1) if x > 0 else -120.0


def read_pcm(data_or_path) -> array.array:
    """Return int16 samples; the file must already be 16 kHz mono PCM16."""
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else open(data_or_path, "rb")
    try:
        with wave.open(src, "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != RATE:
                raise AudioFormatError(f"需要 16 kHz 單聲道 16-bit WAV（收到 {w.getframerate()} Hz、"
                                       f"{w.getnchannels()} 聲道、{w.getsampwidth() * 8}-bit）")
            raw = w.readframes(w.getnframes())
    except wave.Error as exc:
        raise AudioFormatError(f"不是可讀的 WAV：{exc}") from exc
    finally:
        src.close()
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm


def write_wav(path: Path, pcm: array.array) -> str:
    """Write atomically; returns sha256 of the file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = pcm
    if sys.byteorder == "big":
        data = array.array("h", pcm)
        data.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(path)
    return hashlib.sha256(buf.getvalue()).hexdigest()


def quality(pcm: array.array, *, min_s: float = 1.0, max_s: float = 30.0) -> Quality:
    n = len(pcm)
    dur = n / RATE
    if n == 0:
        return Quality(0.0, -120.0, -120.0, 0.0, 1.0, 0.0, ["empty"])
    peak = max(abs(min(pcm)), abs(max(pcm)))
    clipped = sum(1 for x in pcm if x >= CLIP_LEVEL or x <= -CLIP_LEVEL)
    total_sq = 0.0
    frames = []
    for i in range(0, n, FRAME):
        seg = pcm[i:i + FRAME]
        sq = sum(x * x for x in seg)
        total_sq += sq
        frames.append(math.sqrt(sq / len(seg)))
    rms = math.sqrt(total_sq / n)
    silent = sum(1 for f in frames if _db(f) < SILENCE_DBFS)
    fs = sorted(frames)
    noise = fs[max(0, int(len(fs) * 0.1) - 1)] or 1.0
    signal = fs[min(len(fs) - 1, int(len(fs) * 0.9))]
    snr = round(20 * math.log10(max(signal, 1.0) / max(noise, 1.0)), 1)
    q = Quality(round(dur, 3), _db(rms), _db(peak), round(clipped / n, 5), round(silent / len(frames), 3), snr, [])
    if dur < min_s:
        q.problems.append("too_short")
    if dur > max_s:
        q.problems.append("too_long")
    if q.clip_ratio > 0.001:
        q.problems.append("clipping")
    if q.rms_dbfs < -40:
        q.problems.append("too_quiet")
    if q.silence_ratio > 0.8:
        q.problems.append("mostly_silence")
    if q.snr_db < 10:
        q.problems.append("low_snr")
    return q


PROBLEM_TEXT = {
    "empty": "沒有聲音資料", "too_short": "太短（少於 1 秒）", "too_long": "太長（超過 30 秒）",
    "clipping": "音量太大，有削波破音", "too_quiet": "音量太小", "mostly_silence": "大部分是靜音",
    "low_snr": "背景噪音太大（SNR 偏低）",
}


def cut(pcm: array.array, t0_ms: int, t1_ms: int) -> array.array:
    a = max(0, int(t0_ms * RATE / 1000))
    b = min(len(pcm), int(t1_ms * RATE / 1000))
    return pcm[a:b]


def concat(paths: list[Path], gap_ms: int = 300) -> tuple[array.array, list[int]]:
    """Concatenate clips with a short silence; returns (pcm, start offset ms of each clip)."""
    out = array.array("h")
    offsets = []
    gap = array.array("h", [0]) * int(RATE * gap_ms / 1000)
    for p in paths:
        offsets.append(int(len(out) * 1000 / RATE))
        out.extend(read_pcm(p))
        out.extend(gap)
    return out, offsets


def ffmpeg_bin() -> str | None:
    root = Path(__file__).resolve().parents[2]          # sidecar/live-room（安裝後 tools\ffmpeg.exe）
    try:
        from app.audio import ffmpeg_bin as _bin
        return _bin(root)
    except Exception:
        return shutil.which("ffmpeg")


def convert_any(src: Path, dst: Path, timeout: int = 600) -> Path:
    """Any audio/video file -> 16 kHz mono PCM16 WAV with the local ffmpeg (no network)."""
    ff = ffmpeg_bin()
    if not ff:
        raise AudioFormatError("找不到本機 ffmpeg（安裝版在 tools\\ffmpeg.exe）")
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([ff, "-nostdin", "-y", "-i", str(src), "-vn", "-ar", str(RATE), "-ac", "1",
                           "-c:a", "pcm_s16le", str(dst)], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not dst.exists():
        raise AudioFormatError((proc.stderr or "ffmpeg 轉檔失敗")[-300:])
    return dst
