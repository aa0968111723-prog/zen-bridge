"""整堂課音檔：16 kHz 單聲道 PCM16，依直播時間軸寫進每 5 分鐘一個的 WAV（audio/chunk_0000.wav …）。

* 依 ``t0_ms`` 定位寫入（不是單純 append），所以片段晚到、重送、重疊都沒關係，時間軸永遠和字幕一致。
* 每次寫入後更新 WAV 標頭長度；當機時最多少掉最後幾秒，讀取時以檔案大小為準自動修正。
* 沒有收到音訊的時間保持靜音（零）；整個 5 分鐘都沒收到就不建檔。
"""
from __future__ import annotations

import array
import os
import struct
from pathlib import Path

RATE = 16000
BPS = 2                                   # bytes per sample
CHUNK_S = 300
CHUNK_BYTES = CHUNK_S * RATE * BPS
HDR = 44


def _header(data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * BPS, BPS, 16) + b"data" + struct.pack("<I", data_bytes))


def chunk_name(idx: int) -> str:
    return f"chunk_{idx:04d}.wav"


def pcm_from_wav_bytes(data: bytes) -> bytes | None:
    """PCM16 mono 16 kHz payload of a WAV produced by the live pipeline's ffmpeg step (or None)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt_ok = 12, False
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            fmt, ch, rate, _, _, bits = struct.unpack("<HHIIHH", data[body:body + 16])
            fmt_ok = fmt in (1, 0xFFFE) and ch == 1 and rate == RATE and bits == 16
        elif cid == b"data":
            if not fmt_ok:
                return None
            end = len(data) if size in (0, 0xFFFFFFFF) else min(len(data), body + size)
            return data[body:end - ((end - body) % 2)]
        pos = body + size + (size & 1)
    return None


class ChunkWriter:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh: dict[int, object] = {}
        self._size: dict[int, int] = {}
        self.end_byte = 0                  # furthest byte written on the class timeline

    def _open(self, idx: int):
        fh = self._fh.get(idx)
        if fh is not None:
            return fh
        if len(self._fh) >= 2:             # keep at most two chunks open (current + previous)
            old = min(self._fh)
            self._close(old)
        p = self.dir / chunk_name(idx)
        if p.exists():
            fh = open(p, "r+b")
            self._size[idx] = max(0, os.path.getsize(p) - HDR)
        else:
            fh = open(p, "w+b")
            fh.write(_header(0))
            self._size[idx] = 0
        self._fh[idx] = fh
        return fh

    def write(self, t0_ms: int, pcm: bytes) -> int:
        """Place PCM16 at t0_ms on the class timeline. Returns bytes written."""
        pos = max(0, int(t0_ms)) * RATE // 1000 * BPS
        mv = memoryview(pcm)[: len(pcm) - len(pcm) % 2]
        done = 0
        while done < len(mv):
            idx, off = divmod(pos + done, CHUNK_BYTES)
            n = min(len(mv) - done, CHUNK_BYTES - off)
            fh = self._open(idx)
            fh.seek(HDR + off)
            fh.write(mv[done:done + n])
            if off + n > self._size[idx]:
                self._size[idx] = off + n
                fh.seek(4)
                fh.write(struct.pack("<I", 36 + self._size[idx]))
                fh.seek(40)
                fh.write(struct.pack("<I", self._size[idx]))
            done += n
        self.end_byte = max(self.end_byte, pos + done)
        return done

    def flush(self, fsync: bool = False) -> None:
        for fh in self._fh.values():
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())

    def _close(self, idx: int) -> None:
        fh = self._fh.pop(idx, None)
        if fh:
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()

    def close(self) -> None:
        for idx in list(self._fh):
            self._close(idx)


def chunks(folder: Path) -> list[Path]:
    return sorted(Path(folder).glob("chunk_*.wav"))


def read_span(folder: Path, t0_ms: int, t1_ms: int) -> array.array:
    """PCM16 samples for [t0_ms, t1_ms) of the class timeline; missing audio reads as silence."""
    a = max(0, int(t0_ms)) * RATE // 1000 * BPS
    b = max(a, int(t1_ms) * RATE // 1000 * BPS)
    out = bytearray(b - a)
    pos = a
    while pos < b:
        idx, off = divmod(pos, CHUNK_BYTES)
        n = min(b - pos, CHUNK_BYTES - off)
        p = Path(folder) / chunk_name(idx)
        if p.exists():
            with open(p, "rb") as f:
                f.seek(HDR + off)
                data = f.read(n)
            out[pos - a: pos - a + len(data)] = data
        pos += n
    pcm = array.array("h")
    pcm.frombytes(bytes(out))
    return pcm


def total_seconds(folder: Path) -> float:
    cs = chunks(folder)
    if not cs:
        return 0.0
    last = cs[-1]
    idx = int(last.stem.split("_")[1])
    return (idx * CHUNK_BYTES + max(0, os.path.getsize(last) - HDR)) / (RATE * BPS)


def disk_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in chunks(folder))
