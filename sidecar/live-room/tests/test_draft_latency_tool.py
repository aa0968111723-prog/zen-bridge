"""grok/fast-draft-asr: the latency tool's scoring and real-pace loop (fake draft + fake ASR)."""
from app.draft_asr import DraftPartial
from app.endpointer import EndpointConfig
from tests.test_endpointer import silence, tone
from tools import draft_latency as dl


class Loud:
    def __init__(self):
        self.text = ""

    def feed(self, pcm):
        import array
        a = array.array("h")
        a.frombytes(pcm)
        if a and max(abs(x) for x in a) > 2000:
            self.text += "字"
            return [DraftPartial(self.text, False, 0)]
        return []

    def finish(self):
        return []

    def reset(self):
        self.text = ""


def test_percentiles():
    assert dl.pct([1, 2, 3, 4], 50) == 2.5 and dl.pct([], 50) is None
    assert dl.summary([0.5, None, 1.5])["n"] == 2


def test_score_maps_drafts_and_finals_to_lines():
    truth = [{"t0_s": 0.5, "t1_s": 2.0}, {"t0_s": 3.0, "t1_s": 4.0}]
    drafts = [{"t_s": 1.1, "zh": "a"}, {"t_s": 3.4, "zh": "b"}]
    finals = [{"t0_s": 0.3, "t1_s": 2.2, "done_s": 3.0}, {"t0_s": 2.9, "t1_s": 4.2, "done_s": 5.5}]
    rows = dl.score(truth, drafts, finals)
    assert [r["draft_s"] for r in rows] == [0.6, 0.4]
    assert [r["final_s"] for r in rows] == [1.0, 1.5]


def test_real_pace_loop_with_fakes_is_fast_enough():
    pcm = silence(0.3) + tone(1.0) + silence(0.6)
    out = dl.run_two_pass(pcm, Loud, dl.FakeAsr(), EndpointConfig(), pace=20.0)
    assert out["drafts"] and len(out["finals"]) == 1


def test_cer_ignores_punctuation():
    assert dl.cer("今天，我們。", "今天我們") == 0.0
    assert dl.cer("今天我們", "今天你們") == 0.25
    assert dl.cer("", "x") is None
