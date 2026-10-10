"""optimization-round2 #1-#6: native worker tuning, timings/log, priority/EcoQoS, VAD trim."""
import json
import subprocess
import sys
import types
import wave

import numpy as np
import pytest

from app import asr_tuning as at
from app import native_worker as nw
from app import vad
from app.native_asr import NativeResidentAsr


# #2 #4 defaults ---------------------------------------------------------------
def test_defaults_are_the_research_recommendations():
    t = at.NativeTuning.from_env({})
    assert (t.flash_attn, t.temperature_inc, t.ctx_min, t.max_tokens) == (False, 0.0, 640, 0)
    assert t.repeat_guard and t.timings and t.priority == "above_normal" and t.ecoqos_off
    kw = nw.model_kwargs(nw.parse_args(["--model", "m"]))
    assert kw["context_params"]["flash_attn"] is False
    assert kw["greedy"] == {"best_of": 1} and kw["temperature_inc"] == 0.0
    assert kw["params_sampling_strategy"] == 0 and kw["no_context"] is True


def test_env_overrides():
    t = at.NativeTuning.from_env({"BREEZE_ASR_FLASH_ATTN": "1", "BREEZE_ASR_TEMPERATURE_INC": "0.2",
                                  "BREEZE_ASR_AUDIO_CONTEXT_MIN": "448", "BREEZE_ASR_MAX_TOKENS": "96",
                                  "BREEZE_ASR_REPEAT_GUARD": "0", "BREEZE_ASR_PRIORITY": "normal",
                                  "BREEZE_ASR_ECOQOS_OFF": "0", "BREEZE_ASR_TIMINGS": "off"})
    args = nw.parse_args(["--model", "m", "--best", "3"] + t.argv())
    kw = nw.model_kwargs(args)
    assert kw["context_params"]["flash_attn"] is True and kw["temperature_inc"] == 0.2
    assert kw["greedy"] == {"best_of": 3}
    assert (args.context_min, args.max_tokens, args.repeat_guard, args.priority, args.ecoqos_off, args.timings) == \
        (448, 96, 0, "normal", 0, 0)
    assert at.NativeTuning.from_env({"BREEZE_ASR_PRIORITY": "realtime"}).priority == "above_normal"


# #3 audio_ctx / max_tokens / loop guard ---------------------------------------
@pytest.mark.parametrize("seconds,expected", [(6, 640), (2, 640), (12, 832), (30, 1500), (45, 1500)])
def test_audio_ctx_from_clip_length(seconds, expected):
    assert at.audio_ctx_for(seconds) == expected


def test_audio_ctx_fixed_and_floor():
    assert at.audio_ctx_for(6, fixed=768) == 768
    assert at.audio_ctx_for(6, ctx_min=448) == 448
    assert at.audio_ctx_for(6, fixed=4000) == 1500


def test_max_tokens_cap():
    assert at.max_tokens_for(6) == 68 and at.max_tokens_for(0.5) == 32 and at.max_tokens_for(60) == 224
    assert at.max_tokens_for(6, fixed=50) == 50


@pytest.mark.parametrize("text,loop", [
    ("南無南無南無南無南無南無南無", True),
    ("謝謝大家。謝謝大家。謝謝大家。謝謝大家。", True),
    ("今天我們講因緣具足的道理，大家坐好。", False),
    ("謝謝謝謝，今天就講到這裡，下週再見。", False),
    ("", False),
])
def test_repetition_loop(text, loop):
    assert at.repetition_loop(text) is loop


class FakeEngine:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.timings = 0

    def transcribe(self, pcm, **params):
        self.calls.append(params)
        return [types.SimpleNamespace(text=self.replies.pop(0))]

    def print_timings(self):
        self.timings += 1


def test_loop_retried_once_at_full_context(capsys):
    eng = FakeEngine(["南無南無南無南無南無南無", "南無阿彌陀佛"])
    args = nw.parse_args(["--model", "m"])
    out = nw.transcribe_clip(eng, np.zeros(16000 * 6, dtype=np.float32), "", "zh", args)
    assert out["text"] == "南無阿彌陀佛" and out["stats"]["retried_full_ctx"] is True
    assert eng.calls[0]["audio_ctx"] == 640 and eng.calls[1]["audio_ctx"] == 1500
    assert eng.calls[0]["max_tokens"] == 68
    # #1 per-clip timing on stderr, never the transcript
    err = capsys.readouterr().err
    assert "BREEZE_TIMING" in err and "南無" not in err and eng.timings == 1
    stats = json.loads(err.split("BREEZE_TIMING ", 1)[1].splitlines()[0])
    assert stats["audio_s"] == 6.0 and stats["audio_ctx"] == 640


def test_persistent_loop_returns_blank():
    eng = FakeEngine(["哈哈哈哈哈哈哈哈", "哈哈哈哈哈哈哈哈"])
    out = nw.transcribe_clip(eng, np.zeros(16000 * 6, dtype=np.float32), "", "zh", nw.parse_args(["--model", "m"]))
    assert out["text"] == ""


def test_no_retry_when_guard_off_or_full_ctx():
    eng = FakeEngine(["哈哈哈哈哈哈哈哈"])
    nw.transcribe_clip(eng, np.zeros(16000, dtype=np.float32), "", "zh",
                       nw.parse_args(["--model", "m", "--repeat-guard", "0", "--timings", "0"]))
    assert len(eng.calls) == 1


# #5 priority / EcoQoS ---------------------------------------------------------
def test_boost_is_noop_off_windows():
    out = at.boost_current_process(platform="linux")
    assert out == {"platform": "linux", "priority": None, "ecoqos_off": None}


def test_boost_calls_win32_apis_with_fake_kernel32():
    calls = []

    class K32:
        class _F:
            def __init__(self, name, ret):
                self.name, self.ret = name, ret

            def __call__(self, *a):
                calls.append((self.name, a))
                return self.ret
        GetCurrentProcess = _F("GetCurrentProcess", -1)
        SetPriorityClass = _F("SetPriorityClass", 1)
        SetProcessInformation = _F("SetProcessInformation", 1)
    out = at.boost_current_process("above_normal", True, platform="win32", kernel32=K32())
    assert out["priority"] is True and out["ecoqos_off"] is True, out
    names = [c[0] for c in calls]
    assert "SetPriorityClass" in names and "SetProcessInformation" in names
    prio = [c for c in calls if c[0] == "SetPriorityClass"][0][1]
    assert prio[1] == at.ABOVE_NORMAL_PRIORITY_CLASS
    info = [c for c in calls if c[0] == "SetProcessInformation"][0][1]
    assert info[1] == at.ProcessPowerThrottling


# #1 worker stderr -> rotating log; tuning reaches the worker command -------------
def test_worker_stderr_goes_to_rotating_log_and_argv_carries_tuning(tmp_path, monkeypatch):
    helper = tmp_path / "worker.py"
    helper.write_text(
        "import json,sys\n"
        "print('BREEZE_SYSTEM_INFO AVX2=1', file=sys.stderr, flush=True)\n"
        "print('BREEZE_RESULT '+json.dumps({'ready':True}),flush=True)\n"
        "for line in sys.stdin:\n"
        " print('whisper_print_timings: encode time = 1.0 ms', file=sys.stderr, flush=True)\n"
        " print('BREEZE_RESULT '+json.dumps({'ok':True,'text':'中文','stats':{'rtf':0.5}}),flush=True)\n",
        encoding="utf-8")
    seen = {}
    real = subprocess.Popen

    def launch(command, **kw):
        seen["cmd"] = command
        return real([sys.executable, "-u", str(helper)], **kw)
    monkeypatch.setattr("app.native_asr.subprocess.Popen", launch)
    log = tmp_path / "logs" / "asr-worker.log"
    asr = NativeResidentAsr(tmp_path / "model.bin", startup_timeout_s=5, inference_timeout_s=5, log_path=log)
    try:
        assert asr.start().ok
        assert asr.transcribe(tmp_path / "a.wav", "").text == "中文"
        assert asr.last_stats == {"rtf": 0.5}
    finally:
        asr.close()
    text = log.read_text(encoding="utf-8")
    assert "BREEZE_SYSTEM_INFO" in text and "encode time" in text
    cmd = " ".join(seen["cmd"])
    assert "--flash-attn 0" in cmd and "--temperature-inc 0.0" in cmd and "--priority above_normal" in cmd


def test_log_path_resolution(tmp_path):
    assert at.default_log_path({"BREEZE_ASR_LOG": "off"}) is None
    assert at.default_log_path({"ZEN_DATA_DIR": str(tmp_path)}) == tmp_path / "logs" / "asr-worker.log"
    assert at.default_log_path({"BREEZE_ASR_LOG": str(tmp_path / "x.log")}) == tmp_path / "x.log"


# #6 VAD trim ------------------------------------------------------------------
def _wav(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.arange(int(seconds * 16000), dtype=np.int16).tobytes())
    return path


def _dur(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / 16000


def test_trim_wav_cuts_head_and_tail(tmp_path):
    src = _wav(tmp_path / "a.wav", 6.0)
    out = vad.trim_wav(src, vad.VadResult(0.3, 2000, 3000, 187), pad_ms=200)
    assert out is not None and abs(_dur(out) - 1.4) < 0.01
    assert vad.trim_wav(src, vad.VadResult(0.9, 100, 5900, 187)) is None          # saves < 500 ms
    assert vad.trim_wav(src, vad.VadResult(0.0, None, None, 187)) is None
    assert vad.trim_wav(src, None) is None


@pytest.mark.anyio
async def test_pipeline_sends_trimmed_audio_and_cleans_up(monkeypatch, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from app.asr import AsrResult
    from tests.test_pipeline_repair import app_for, push, stop, token_of

    class Gate:
        skipped = 0

        def check(self, wav):
            return vad.VadResult(0.3, 2000, 3000, 187)

        def is_silent(self, res):
            return False

    class RecAsr:
        def __init__(self):
            self.paths = []

        def transcribe(self, wav, prompt, *a, **k):
            self.paths.append((wav, _dur(wav)))
            return AsrResult(ok=True, text="有聲音")

        def health(self):
            return True

        def close(self):
            pass
    rec = RecAsr()
    app = app_for(asr=rec)
    app.state.pipeline.vad = Gate()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            r = await push(client, token, "class", "s", 1, _wav(tmp_path / "s.wav", 6.0).read_bytes())
            assert r.status_code == 200, r.text
    finally:
        await stop(app)
    assert rec.paths and abs(rec.paths[0][1] - 1.4) < 0.01
    assert not rec.paths[0][0].exists()                      # trimmed copy removed


@pytest.mark.anyio
async def test_trim_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("BREEZE_VAD_TRIM", "0")
    from app import pipeline
    assert pipeline._vad_trim_enabled() is False
    monkeypatch.setenv("BREEZE_VAD_TRIM_PAD_MS", "x")
    assert pipeline._vad_trim_pad_ms() == 200
