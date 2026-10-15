import json
import math
import random
import struct
import wave

import pytest

from tools import mic_soundcheck as mc


def _write(path, samples, rate=16000, width=2, channels=1):
    full = (1 << (8 * width - 1)) - 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        frames = bytearray()
        for s in samples:
            v = max(-full - 1, min(full, int(round(s * full))))
            for _ in range(channels):
                if width == 1:
                    frames += bytes([v + 128])
                else:
                    frames += v.to_bytes(width, "little", signed=True)
        w.writeframes(bytes(frames))
    return path


def _speech_like(seconds=6.0, rate=16000, amp=0.3, noise=0.003, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(int(seconds * rate)):
        t = i / rate
        talking = (int(t * 2) % 2) == 0  # 0.5 s on / 0.5 s off
        s = amp * math.sin(2 * math.pi * 220 * t) if talking else 0.0
        out.append(s + rng.uniform(-noise, noise))
    return out


def test_good_recording_is_ok(tmp_path):
    p = _write(tmp_path / "good.wav", _speech_like())
    before = p.read_bytes()
    r = mc.soundcheck(p)
    assert r["verdict"] == "ok", r
    assert r["metrics"]["snr_db"] > 20
    assert r["advice_zh"] == []
    assert p.read_bytes() == before  # read-only


def test_clipping_fails_with_chinese_advice(tmp_path):
    p = _write(tmp_path / "clip.wav", [max(-1, min(1, 3 * s)) for s in _speech_like(amp=0.9)])
    r = mc.soundcheck(p)
    assert r["verdict"] == "fail"
    levels = {c["check"]: c["level"] for c in r["checks"]}
    assert levels["clipping"] == "fail"
    assert any("爆音" in a for a in r["advice_zh"])


def test_quiet_speech_warns_or_fails(tmp_path):
    p = _write(tmp_path / "quiet.wav", _speech_like(amp=0.01, noise=0.0002))
    r = mc.soundcheck(p)
    levels = {c["check"]: c["level"] for c in r["checks"]}
    assert levels["speech_level"] in ("warn", "fail")
    assert any("音量偏小" in a for a in r["advice_zh"])


def test_noisy_room_low_snr(tmp_path):
    p = _write(tmp_path / "noisy.wav", _speech_like(amp=0.1, noise=0.08))
    r = mc.soundcheck(p)
    levels = {c["check"]: c["level"] for c in r["checks"]}
    assert levels["snr"] in ("warn", "fail")
    assert levels["noise_floor"] in ("warn", "fail")


def test_dc_offset_detected(tmp_path):
    p = _write(tmp_path / "dc.wav", [s + 0.08 for s in _speech_like(amp=0.2)])
    r = mc.soundcheck(p)
    levels = {c["check"]: c["level"] for c in r["checks"]}
    assert levels["dc_offset"] == "fail"


def test_silence_and_short(tmp_path):
    silent = mc.soundcheck(_write(tmp_path / "s.wav", [0.0] * 16000 * 4))
    assert silent["verdict"] == "fail"
    assert any(c["check"] == "signal" for c in silent["checks"])
    short = mc.soundcheck(_write(tmp_path / "short.wav", _speech_like(seconds=0.5)))
    assert short["verdict"] == "fail"
    assert short["checks"][0]["check"] == "duration"


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_sample_widths_and_stereo(tmp_path, width):
    p = _write(tmp_path / f"w{width}.wav", _speech_like(seconds=4), width=width, channels=2)
    r = mc.soundcheck(p)
    assert r["metrics"]["sample_rate"] == 16000
    assert abs(r["metrics"]["duration_s"] - 4.0) < 0.01
    assert r["verdict"] in ("ok", "warn")


def test_cli_exit_codes_and_no_overwrite(tmp_path, capsys):
    good = _write(tmp_path / "good.wav", _speech_like())
    out = tmp_path / "r.json"
    assert mc.main(["--wav", str(good), "--json", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "ok"
    out.write_text("keep", encoding="utf-8")
    assert mc.main(["--wav", str(good), "--json", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "keep"
    clip = _write(tmp_path / "clip.wav", [max(-1, min(1, 3 * s)) for s in _speech_like(amp=0.9)])
    assert mc.main(["--wav", str(clip)]) == 1
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    assert mc.main(["--wav", str(bad)]) == 2
    capsys.readouterr()


def test_strict_turns_warn_into_failure(tmp_path):
    p = _write(tmp_path / "w.wav", _speech_like(seconds=2.0))  # 1–3 s → duration warn
    r = mc.soundcheck(p)
    assert r["verdict"] == "warn"
    assert mc.main(["--wav", str(p)]) == 0
    assert mc.main(["--wav", str(p), "--strict"]) == 1


def test_no_network_or_app_imports():
    src = (mc.__file__ and open(mc.__file__, encoding="utf-8").read())
    for banned in ("socket", "urllib", "http.client", "requests", "import app", "from app", "8645"):
        assert banned not in src
