"""Synthetic 16 kHz speech-like audio for the training-data tests (bursts of tone over a quiet floor)."""
import array
import io
import math
import random
import wave

RATE = 16000


def speechy(seconds: float = 3.0, amp: int = 8000, seed: int = 1, clip: bool = False) -> array.array:
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    out = array.array("h")
    f = 180 + seed * 7
    for i in range(n):
        t = i / RATE
        on = (int(t * 4) % 3) != 2 and 0.2 < t < seconds - 0.2
        v = rnd.randint(-60, 60)
        if on:
            v += int(amp * math.sin(2 * math.pi * f * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t)))
        if clip:
            v *= 6
        out.append(max(-32768, min(32767, v)))
    return out


def wav_bytes(pcm: array.array) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
