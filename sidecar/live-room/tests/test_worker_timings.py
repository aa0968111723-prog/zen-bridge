"""T4 had encode/decode ms = null: whisper timings went to C stderr only. They now land in the stats."""
import os
from types import SimpleNamespace

from app import native_worker as nw

SAMPLE = """
whisper_print_timings:     load time =   812.11 ms
whisper_print_timings:     fallbacks =   1 p /   2 h
whisper_print_timings:      mel time =    20.50 ms
whisper_print_timings:   sample time =    15.25 ms /    40 runs (     0.38 ms per run)
whisper_print_timings:   encode time =  1234.56 ms /     1 runs (  1234.56 ms per run)
whisper_print_timings:   decode time =     9.00 ms /     2 runs (     4.50 ms per run)
whisper_print_timings:   batchd time =   301.75 ms /    38 runs (     7.94 ms per run)
whisper_print_timings:   prompt time =    44.00 ms /    10 runs (     4.40 ms per run)
whisper_print_timings:    total time =  1700.00 ms
"""


def test_parse_whisper_timings():
    t = nw.parse_whisper_timings(SAMPLE)
    assert t["encode_ms"] == 1234.56 and t["decode_ms"] == 9.0 and t["batchd_ms"] == 301.75
    assert t["sample_ms"] == 15.25 and t["prompt_ms"] == 44.0 and t["total_ms"] == 1700.0
    assert t["fallbacks"] == 3
    assert nw.parse_whisper_timings("") == {}


def test_capture_native_stderr_sees_fd2_writes(capfd):
    def native_like():
        os.write(2, b"whisper_print_timings:   encode time =   12.00 ms /     1 runs\n")
    text = nw.capture_native_stderr(native_like)
    assert "encode time" in text
    assert "encode time" in capfd.readouterr().err        # still echoed to the worker log


def test_transcribe_clip_puts_encode_decode_into_stats():
    class Engine:
        def transcribe(self, pcm, **kw):
            return [SimpleNamespace(text="你好")]

        def print_timings(self):
            os.write(2, SAMPLE.encode())

    args = SimpleNamespace(context=640, context_min=256, max_tokens=0, repeat_guard=False, timings=True)
    import numpy as np
    out = nw.transcribe_clip(Engine(), np.zeros(16000, dtype="float32"), "", "zh", args)
    assert out["text"] == "你好"
    assert out["stats"]["encode_ms"] == 1234.56 and out["stats"]["decode_ms"] == 9.0
