"""全站 D9 (non-UTF-8 reply -> bad_response, TM fallback still works) and D11 (VAD env range)."""
import pytest

from app import vad
from app.translate import Translator
from tests.test_vad_silero import FakeSession


class Raw:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


def test_non_utf8_reply_is_bad_response():
    t = Translator(enabled=True, key="k", opener=lambda req, timeout=0: Raw(b'{"choices":[{"message":{"content":"Hi"}}]}\xff\xfe'))
    out = t.translate("我們開始")
    assert out.status == "bad_response" and out.text == ""


@pytest.mark.parametrize("name,value", [("BREEZE_VAD_MIN_SPEECH_RATIO", "1.5"), ("BREEZE_VAD_MIN_SPEECH_RATIO", "nan"),
                                        ("BREEZE_VAD_THRESHOLD", "nan"), ("BREEZE_VAD_THRESHOLD", "-1"),
                                        ("BREEZE_VAD_THRESHOLD", "inf")])
def test_vad_env_out_of_range_uses_default(name, value):
    gate = vad.vad_from_env({"BREEZE_VAD": "silero", name: value}, session_factory=lambda: FakeSession([0.9]))
    assert gate is not None
    assert gate.min_speech_ratio == 0.05 and gate.vad.threshold == 0.5
