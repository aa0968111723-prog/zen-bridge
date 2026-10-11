"""Benchmark tool (scripts/bench/zbench.py): pure helpers + an end-to-end run with fake
whisper-cli and a fake Ollama. No network, no real models."""
import importlib.util
import json
import os
import stat
import sys
import wave
from pathlib import Path

import pytest

from tests.local_fakes import FakeOpener

ZB = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "zbench.py"


@pytest.fixture(scope="module")
def zb():
    spec = importlib.util.spec_from_file_location("zbench", ZB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pct(zb):
    p = zb.pct([1, 2, 3, 4, 100])
    assert p["p50"] == 3 and p["max"] == 100 and p["n"] == 5
    assert zb.pct([])["n"] == 0


def test_cer(zb):
    assert zb.cer("因緣具足。", "因緣具足") == 0.0
    assert zb.cer("因緣具足", "因圓具足") == 0.25
    assert zb.cer("", "x") is None


def test_audio_ctx(zb):
    assert zb.audio_ctx_for(6, "linear") == 428
    assert zb.audio_ctx_for(6) == 704 or zb.audio_ctx_for(6) >= 640


def test_parse_timings(zb):
    err = "whisper_print_timings:   encode time =  1234.50 ms /  1 runs\nwhisper_print_timings:   decode time =    10.00 ms"
    assert zb.parse_timings(err) == {"encode": 1234.5, "decode": 10.0}


def test_classify(zb):
    quiet = [{"bg_cpu": 2.0}] * 10
    assert zb.classify(quiet, [], False, 0, 0) == "OK"
    assert zb.classify(quiet, [], False, 0, 1) == "FAILED"
    assert zb.classify(quiet, [], False, 0, 0, devicelost=True) == "FAILED"
    assert zb.classify(quiet, [], True, 0, 0) == "BATTERY"
    assert zb.classify([{"bg_cpu": 40.0}] * 10, [], False, 0, 0) == "UNKNOWN_BG_LOAD"
    assert zb.classify(quiet, [], False, 1, 0) == "THROTTLED"
    perf = [100.0] * 40 + [70.0] * 41
    assert zb.classify(quiet, perf, False, 0, 0) == "THROTTLED"


def test_live_room_busy_rules(zb, monkeypatch):
    monkeypatch.setattr(zb, "port_open", lambda port, **k: False)
    assert zb.live_room_busy()[0] is False
    monkeypatch.setattr(zb, "port_open", lambda port, **k: True)

    def get_streaming(url, timeout=0, headers=None, **k):
        return {"token": "t"} if url.endswith("host-token") else {"listeners": 3}
    assert zb.live_room_busy(get_streaming)[0] is True

    def get_idle(url, timeout=0, headers=None, **k):
        return {"token": "t"} if url.endswith("host-token") else {"listeners": 0, "pending": 0}
    assert zb.live_room_busy(get_idle)[0] is False

    def get_err(url, **k):
        raise OSError("x")
    assert zb.live_room_busy(get_err)[0] is True        # unknown state = do not run


def test_never_probes_reserved_port(zb):
    assert zb.port_open(8645) is False


def test_missing_files_listed_not_downloaded(zb, tmp_path):
    m = {"whisper_cli": str(tmp_path / "nope.exe"), "asr_models": [{"name": "b", "path": str(tmp_path / "m.bin")}],
         "segments_dir": str(tmp_path)}
    names = {x["name"] for x in zb.missing_files(m)}
    assert names == {"whisper_cli", "b", "audio segments"}


def _wav(path, seconds=1.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * seconds))
    return path


@pytest.mark.skipif(os.name == "nt", reason="fake whisper-cli is a POSIX shebang script")
def test_end_to_end_with_fakes(zb, tmp_path, monkeypatch):
    fake = tmp_path / "whisper-cli"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint('因緣具足')\n"
                    "sys.stderr.write('system_info: n_threads = 4\\nwhisper_print_timings:   encode time =   50.00 ms\\n')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    audio = tmp_path / "audio"
    audio.mkdir()
    for i in range(2):
        _wav(audio / f"{i}.wav")
        (audio / f"{i}.txt").write_text("因緣具足", encoding="utf-8")
    matrix = {"whisper_cli": str(fake), "segments_dir": str(audio), "asr_models": [{"name": "fake", "path": str(model)}],
              "asr": {"threads": [2], "audio_ctx": [640], "beam": [1], "modes": ["cli"]},
              "llm": {"models": ["qwen3:4b"], "threads": [4], "ctx": [2048]},
              "embed": {"model": "qwen3-embedding:0.6b", "batches": [4]}, "sentences": ["你好", "再見"]}
    chat = {"message": {"content": "Hello."}, "prompt_eval_count": 30, "prompt_eval_duration": 30_000_000,
            "eval_count": 3, "eval_duration": 60_000_000, "load_duration": 0}

    class Router(FakeOpener):
        def __call__(self, req, timeout=None):
            url = req.full_url
            self.replies = [{"models": [{"name": "qwen3:4b"}, {"name": "qwen3-embedding:0.6b"}]}] if url.endswith("/api/tags") \
                else [chat] if url.endswith("/api/chat") else [{"embeddings": [[0.1, 0.2]] * 4}]
            return super().__call__(req, timeout)
    opener = Router({})
    bench = zb.Bench(matrix, tmp_path / "out", reps=1, cooldown=0, opener=opener)
    bench.layer_asr()
    bench.layer_llm()
    bench.layer_embed()
    path = bench.write({"test": True})
    data = json.loads(path.read_text(encoding="utf-8"))
    asr = [r for r in data["runs"] if r["layer"] == 1 and not r["warmup"]][0]
    assert asr["status"] in ("OK", "UNKNOWN_BG_LOAD") and asr["rc"] == 0
    assert asr["cer"] == 0.0 and asr["seg_n"] == 2 and asr["rtf_mean"] > 0
    llm = [r for r in data["runs"] if r["run_id"].startswith("llm-") and not r["warmup"]][0]
    assert llm["tg_tps"] == pytest.approx(50.0) and llm["lat_p50_ms"] is not None
    assert any(r["run_id"].startswith("emb-") and r.get("dim") == 2 for r in data["runs"])
    chat_req = [r for r in opener.requests if r["url"].endswith("/api/chat")][0]["body"]
    assert chat_req["options"]["num_thread"] == 4 and chat_req["options"]["num_ctx"] == 2048
    for name in ("runs.csv", "segments.csv", "samples.csv", "results.json"):
        assert (tmp_path / "out" / name).exists()
    assert all(r["url"].startswith("http://127.0.0.1:11434") for r in opener.requests)


def test_unpulled_model_is_skipped_not_downloaded(zb, tmp_path):
    opener = FakeOpener({"models": [{"name": "other"}]})
    bench = zb.Bench({"llm": {"models": ["qwen3:4b"]}, "embed": {"enabled": False}}, tmp_path, 1, 0, opener=opener)
    bench.layer_llm()
    assert bench.runs[0]["status"] == "SKIPPED_NOT_PULLED"
    assert all(not r["url"].endswith("/api/pull") for r in opener.requests)


def test_check_mode_aborts_when_live_streaming(zb, monkeypatch, tmp_path):
    monkeypatch.setattr(zb, "live_room_busy", lambda: (True, "streaming"))
    assert zb.main(["--matrix", str(tmp_path / "none.json"), "--check"]) == 3


def test_battery_aborts_without_flag(zb, monkeypatch, tmp_path):
    monkeypatch.setattr(zb, "live_room_busy", lambda: (False, "ok"))
    monkeypatch.setattr(zb, "on_battery", lambda: True)
    assert zb.main(["--matrix", str(tmp_path / "none.json"), "--check"]) == 4
