"""Silero VAD wrapper (fake ONNX session; the real model is never downloaded in tests)."""
import wave

import numpy as np
import pytest

from app import vad


class FakeSession:
    def __init__(self, probs):
        self.probs = list(probs)
        self.calls = 0

    def run(self, _names, feeds):
        assert feeds["input"].shape == (1, vad.CTX + vad.CHUNK)
        p = self.probs[self.calls % len(self.probs)]
        self.calls += 1
        return np.array([[p]], dtype=np.float32), feeds["state"]


def write_wav(path, seconds=1.0, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.zeros(int(seconds * rate)) * 0).astype(np.int16).tobytes())
    return path


def test_off_by_default_and_unknown_mode():
    assert vad.vad_from_env({}) is None
    assert vad.vad_from_env({"BREEZE_VAD": "webrtc"}) is None


def test_missing_model_falls_back_to_rms(tmp_path):
    assert vad.vad_from_env({"BREEZE_VAD": "silero", "BREEZE_VAD_MODEL": str(tmp_path / "none.onnx")}) is None


def test_sha_mismatch_falls_back_to_rms(tmp_path):
    bad = tmp_path / "silero_vad.onnx"
    bad.write_bytes(b"not the model")
    assert vad.vad_from_env({"BREEZE_VAD": "silero", "BREEZE_VAD_MODEL": str(bad)}) is None


def test_speech_ratio_and_bounds(tmp_path):
    sess = FakeSession([0.1, 0.9, 0.9, 0.1])
    gate = vad.vad_from_env({"BREEZE_VAD": "silero"}, session_factory=lambda: sess)
    res = gate.check(write_wav(tmp_path / "a.wav", 1.0))
    assert res.chunks == 31                      # 16000 / 512
    assert 0.4 < res.speech_ratio < 0.6
    assert res.first_ms == 32
    assert not gate.is_silent(res)


def test_silence_detected(tmp_path):
    gate = vad.vad_from_env({"BREEZE_VAD": "silero"}, session_factory=lambda: FakeSession([0.01]))
    res = gate.check(write_wav(tmp_path / "a.wav"))
    assert res.speech_ratio == 0.0 and gate.is_silent(res)


def test_wrong_format_and_errors_mean_no_opinion(tmp_path):
    gate = vad.vad_from_env({"BREEZE_VAD": "silero"}, session_factory=lambda: FakeSession([0.9]))
    assert gate.check(write_wav(tmp_path / "b.wav", rate=8000)) is None
    (tmp_path / "junk.wav").write_bytes(b"RIFFjunk")
    assert gate.check(tmp_path / "junk.wav") is None

    class Broken:
        def run(self, *a):
            raise RuntimeError("onnx")
    gate2 = vad.vad_from_env({"BREEZE_VAD": "silero"}, session_factory=lambda: Broken())
    assert gate2.check(write_wav(tmp_path / "c.wav")) is None and gate2.errors == 1


def test_threshold_env(tmp_path):
    gate = vad.vad_from_env({"BREEZE_VAD": "silero", "BREEZE_VAD_THRESHOLD": "0.95",
                             "BREEZE_VAD_MIN_SPEECH_RATIO": "0.5"}, session_factory=lambda: FakeSession([0.9]))
    res = gate.check(write_wav(tmp_path / "a.wav"))
    assert res.speech_ratio == 0.0 and gate.is_silent(res)


def test_pinned_model_constants():
    assert len(vad.MODEL_SHA256) == 64 and vad.MODEL_URL.startswith("https://")
    assert "v6.2.3" in vad.MODEL_URL


def test_fetch_script_rejects_hash_mismatch(tmp_path, monkeypatch):
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("fetch_silero_vad", vad.Path(__file__).parents[1] / "scripts" / "fetch_silero_vad.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda url, timeout=0: R(b"tampered"))
    dest = tmp_path / "silero_vad.onnx"
    assert mod.main(["--dest", str(dest)]) == 2
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


@pytest.mark.anyio
async def test_pipeline_skips_asr_when_vad_says_silent(monkeypatch, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from tests.test_pipeline_repair import app_for, push, stop, token_of

    class SilentGate:
        skipped = 0

        def check(self, wav):
            assert wav.exists()
            return vad.VadResult(0.0, None, None, 30)

        def is_silent(self, res):
            return True
    app = app_for()
    app.state.pipeline.vad = SilentGate()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            audio = write_wav(tmp_path / "s.wav", 1.0).read_bytes()
            r = await push(client, token, "class", "s", 1, audio)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "silent"
            assert r.json().get("speech_ratio") in (None, 0.0)
    finally:
        await stop(app)
