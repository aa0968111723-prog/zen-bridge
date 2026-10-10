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


def test_r1_int8_is_forced_to_one_thread_unless_overridden():
    from app.draft_asr import int8_safe_threads
    assert int8_safe_threads("int8", 4, env={}) == 1
    assert int8_safe_threads("int8", 4, env={"BREEZE_DRAFT_INT8_MT_OK": "1"}) == 4
    assert int8_safe_threads("fp32", 4, env={}) == 4
    assert int8_safe_threads(None, 0, env={}) == 1


def test_r2_partials_carry_utterance_index_and_start():
    from app.draft_asr import SherpaParaformerDraft

    class Rec:
        def __init__(self):
            self.script = ["", "", "今天", "今天好", "今天好。", "", "明"]
            self.ends = [False, False, False, False, True, False, False]
            self.i = -1

        def create_stream(self):
            return object()

        def is_ready(self, st):
            return False

        def decode_stream(self, st):
            pass

        def get_result(self, st):
            return self.script[self.i]

        def is_endpoint(self, st):
            return self.ends[self.i]

        def reset(self, st):
            pass

    rec = Rec()
    d = SherpaParaformerDraft(recognizer=rec, convert=lambda t: t)
    d.stream = type("S", (), {"accept_waveform": lambda self, sr, x: None})()
    got = []
    for _ in range(7):
        rec.i += 1
        got += d.feed(b"\0\0" * 1600)                  # 100 ms per packet
    first, end, nxt = got[0], [p for p in got if p.is_endpoint][0], got[-1]
    assert (first.utt_index, first.t_start_ms) == (0, 200)      # text first appeared in packet 3 (200 ms)
    assert (end.utt_index, end.t_start_ms, end.t_ms) == (0, 200, 500)
    assert (nxt.text, nxt.utt_index, nxt.t_start_ms) == ("明", 1, 600)
