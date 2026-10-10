"""ARCH-01 / CTO-01: the draft-ASR interface stub (not wired into the pipeline yet)."""
import struct

import pytest

from app import draft_asr


def test_default_is_off():
    assert isinstance(draft_asr.draft_from_env({}), draft_asr.NullDraft)
    assert draft_asr.NullDraft().feed(b"\x00\x00" * 160) == []


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        draft_asr.draft_from_env({"BREEZE_DRAFT_ASR": "whisper"})


def test_sherpa_missing_falls_back_to_null(tmp_path):
    d = draft_asr.draft_from_env({"BREEZE_DRAFT_ASR": "sherpa", "BREEZE_DRAFT_MODEL_DIR": str(tmp_path)})
    assert isinstance(d, draft_asr.NullDraft)


class FakeStream:
    def __init__(self):
        self.n = 0
        self.done = False

    def accept_waveform(self, sr, samples):
        assert sr == 16000
        self.n += len(samples)

    def input_finished(self):
        self.done = True


class FakeRec:
    """Emits '因为' after 0.5 s and an endpoint after 1 s."""
    def create_stream(self):
        return FakeStream()

    def is_ready(self, s):
        return False

    def decode_stream(self, s):
        pass

    def get_result(self, s):
        return "因为条件" if s.n >= 8000 else ""

    def is_endpoint(self, s):
        return s.n >= 16000

    def reset(self, s):
        s.n = 0


def test_partials_are_converted_and_never_final():
    conv = {"因为条件": "因為條件"}.get
    d = draft_asr.SherpaParaformerDraft(recognizer=FakeRec(), convert=lambda t: conv(t, t))
    chunk = struct.pack("<4000h", *([1000] * 4000))         # 0.25 s
    out = []
    for _ in range(4):
        out += d.feed(chunk)
    assert out and out[0].text == "因為條件" and out[-1].is_endpoint
    assert all(p.final is False for p in out)
    assert out[-1].t_ms == 1000
