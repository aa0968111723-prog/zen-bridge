"""round4 #5 backend: X-ASR draft recognizer (fake recognizer; no model, no download)."""
import pytest

from app import draft_asr as da


class FakeRec:
    def __init__(self):
        self.text, self.end = "", False

    def create_stream(self):
        return self

    def accept_waveform(self, sr, samples):
        assert sr == 16000
        self.text += "们"

    def input_finished(self):
        self.end = True

    def is_ready(self, s):
        return False

    def get_result(self, s):
        return self.text

    def is_endpoint(self, s):
        return self.end

    def reset(self, s):
        self.text, self.end = "", False


def test_threads_default_one_and_partials_converted():
    d = da.XAsrDraft(recognizer=FakeRec(), convert=lambda t: t.replace("们", "們"))
    assert d.threads == 1
    out = d.feed(b"\x00\x00" * 1600)
    assert out and out[0].text == "們" and not out[0].final
    assert d.finish()[-1].is_endpoint


def test_precision_auto_prefers_int8_and_missing_raises(tmp_path):
    with pytest.raises(da.DraftAsrUnavailable, match="不會自動下載"):
        da.xasr_precision(tmp_path)
    for f in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (tmp_path / f).write_text("")
    assert da.xasr_precision(tmp_path) == "fp32"
    for f in ("encoder.int8.onnx", "joiner.int8.onnx"):
        (tmp_path / f).write_text("")
    assert da.xasr_precision(tmp_path) == "int8"
    assert da.xasr_precision(tmp_path, "fp32") == "fp32"


def test_env_xasr_missing_model_falls_back_to_null(tmp_path):
    d = da.draft_from_env({"BREEZE_DRAFT_ASR": "xasr", "BREEZE_DRAFT_MODEL_DIR": str(tmp_path)})
    assert isinstance(d, da.NullDraft)
    assert isinstance(da.draft_from_env({}), da.NullDraft)          # off by default
    with pytest.raises(ValueError):
        da.draft_from_env({"BREEZE_DRAFT_ASR": "whisper"})
