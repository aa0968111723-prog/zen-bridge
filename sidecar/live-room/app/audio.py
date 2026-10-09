from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class AudioError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def read_limited(chunks: list[bytes], limit: int) -> bytes:
    total = 0
    out = []
    for block in chunks:
        total += len(block)
        if total > limit:
            raise AudioError(413, f"音訊超過 {limit} bytes，已拒絕")
        out.append(block)
    if total == 0:
        raise AudioError(400, "沒有收到音訊")
    return b"".join(out)


def ffmpeg_bin(root: Path) -> str | None:
    local = root / "tools" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    found = shutil.which("ffmpeg")
    return found


def convert_to_wav(src: Path, work: Path, ffmpeg: str, timeout: int = 40) -> Path:
    wav = work / "audio.wav"
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(422, "ffmpeg 轉檔逾時") from exc
    if proc.returncode != 0 or not wav.exists():
        raise AudioError(422, (proc.stderr or "ffmpeg 轉檔失敗")[-400:])
    return wav


def _wav_pcm_span(wav: Path) -> tuple[int, int, int] | None:
    """Return (pcm_offset, pcm_bytes, bytes_per_second) for RIFF/WAVE.

    ffmpeg writes a LIST chunk before data, so PCM does not start at byte 44.
    Counting those bytes makes a max-length file look over the limit, and it
    makes digital silence look loud enough to skip the silence status.
    """
    try:
        handle = wav.open("rb")
    except OSError:
        return None
    with handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        bps = 32000
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                return None
            chunk_id = chunk[:4]
            chunk_size = int.from_bytes(chunk[4:8], "little")
            pos = handle.tell()
            if chunk_id == b"fmt " and chunk_size >= 16:
                fmt = handle.read(16)
                if len(fmt) >= 16:
                    channels = int.from_bytes(fmt[2:4], "little")
                    rate = int.from_bytes(fmt[4:8], "little")
                    width = int.from_bytes(fmt[14:16], "little") // 8
                    if 0 < channels <= 8 and 0 < rate <= 384000 and width > 0:
                        bps = channels * rate * width
            elif chunk_id == b"data":
                return pos, chunk_size, bps if bps > 0 else 32000
            step = chunk_size + (chunk_size & 1)
            nxt = pos + step
            if nxt <= pos:
                return None
            try:
                handle.seek(nxt)
            except OSError:
                return None


def riff_duration_seconds(wav: Path) -> float | None:
    """Seconds from a RIFF/WAVE header, or None when this file is not WAVE.

    Callers that already decoded a slice should prefer this over a size guess.
    """
    if not wav.exists():
        return None
    span = _wav_pcm_span(wav)
    if span is None:
        return None
    offset, size, bps = span
    available = max(0, wav.stat().st_size - offset)
    return min(max(0, size), available) / bps


def wav_duration_seconds(wav: Path) -> float | None:
    if not wav.exists():
        return None
    duration = riff_duration_seconds(wav)
    if duration is not None:
        return duration
    if wav.stat().st_size < 44:
        return 0.0
    # Not a WAVE container. Callers still need a size-based ceiling.
    payload = max(0, wav.stat().st_size - 44)
    return payload / 32000


def _pcm_rms(pcm: bytes) -> float:
    import array

    samples = array.array("h")
    samples.frombytes(pcm)
    count = len(samples)
    if count == 0:
        return 0.0
    # Stride long windows. The pipeline does not call this when the silence gate is off.
    step = 8 if count > 4000 else 1
    total = 0
    seen = 0
    for index in range(0, count, step):
        sample = samples[index]
        total += sample * sample
        seen += 1
    return (total / seen) ** 0.5


def _loudest_window_rms(pcm: bytes, window: int) -> float | None:
    usable = pcm[: len(pcm) - (len(pcm) % 2)]
    if len(usable) < 2:
        return None
    window = max(2, window - (window % 2))
    best = 0.0
    saw = False
    for offset in range(0, len(usable), window):
        chunk = usable[offset:offset + window]
        if len(chunk) < 2:
            continue
        if len(chunk) % 2:
            chunk = chunk[:-1]
        best = max(best, _pcm_rms(chunk))
        saw = True
    return best if saw else None


def wav_rms(wav: Path) -> float | None:
    """RMS of the loudest 1s window. The first second alone drops later speech."""
    if not wav.exists() or wav.stat().st_size < 46:
        return None
    span = _wav_pcm_span(wav)
    if span is None:
        return _loudest_window_rms(wav.read_bytes()[44:], 32000)
    offset, size, bps = span
    available = max(0, wav.stat().st_size - offset)
    size = min(max(0, size), available)
    size -= size % 2
    if size < 2:
        return None
    try:
        handle = wav.open("rb")
    except OSError:
        return None
    with handle:
        handle.seek(offset)
        # One window at a time so a long file is not loaded just to measure silence.
        window = max(2, bps - (bps % 2))
        left = size
        best = 0.0
        saw = False
        while left >= 2:
            chunk = handle.read(min(window, left))
            if len(chunk) < 2:
                break
            if len(chunk) % 2:
                chunk = chunk[:-1]
            best = max(best, _pcm_rms(chunk))
            saw = True
            left -= len(chunk)
        return best if saw else None


async def convert_to_wav_async(src: Path, work: Path, ffmpeg: str, timeout: int = 40) -> Path:
    """Run ffmpeg off the event loop and kill it when the timeout fires."""
    import asyncio

    from app.aio import wait_bounded

    wav = work / "audio.wav"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await wait_bounded(proc.communicate(), timeout)
    except TimeoutError as exc:
        raise AudioError(422, "ffmpeg 轉檔逾時") from exc
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
    if proc.returncode != 0 or not wav.exists():
        err = (stderr or b"").decode(errors="replace")[-400:] or "ffmpeg 轉檔失敗"
        raise AudioError(422, err)
    return wav
