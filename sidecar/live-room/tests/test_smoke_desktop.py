"""tools/smoke_desktop.py: desktop-flow smoke (fake mode end to end + negative paths)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "smoke_desktop.py"
_spec = importlib.util.spec_from_file_location("smoke_desktop", TOOL)
smoke = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("smoke_desktop", smoke)   # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(smoke)


def run_tool(tmp_path: Path, *args: str, timeout: float = 120, extra_env: dict | None = None) -> tuple[int, dict, str, float]:
    report = tmp_path / "report.json"
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(extra_env or {})
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, str(TOOL), "--json", str(report), "--work-dir", str(tmp_path / "work"), *args],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    elapsed = time.monotonic() - t0
    data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    return proc.returncode, data, proc.stdout + proc.stderr, elapsed


def statuses(report: dict) -> dict[str, str]:
    return {s["name"]: s["status"] for s in report.get("steps", [])}


# ------------------------------------------------------------------ units
def test_fake_wav_is_valid_pcm_and_carries_text():
    data = smoke.wav_bytes(0.5, text="因緣具足。")
    with wave.open(io.BytesIO(data), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 8000
    assert smoke.wav_text(data) == "因緣具足。"
    assert smoke.wav_text(smoke.wav_bytes(0.1)) is None
    assert smoke.wav_text(b"not a wav") is None


def test_slice_wav_wraps_short_file(tmp_path):
    src = tmp_path / "s.wav"
    src.write_bytes(smoke.wav_bytes(1.0))
    out = smoke.slice_wav(src, 4, 0.75)
    assert [(t0, t1) for _, t0, t1 in out] == [(0, 750), (750, 1500), (1500, 2250), (2250, 3000)]
    for audio, _, _ in out:
        with wave.open(io.BytesIO(audio), "rb") as w:
            assert w.getnframes() == 12000


def test_free_port_is_loopback_free_and_never_8645():
    for _ in range(20):
        port = smoke.free_port()
        assert port != 8645
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))


def test_translation_gates_per_language():
    zh = smoke.FAKE_LINES[0]
    assert smoke.accept_translation(smoke.FAKE_EN[zh], "en", zh)
    assert smoke.accept_translation(smoke.FAKE_JA[zh], "ja", zh)
    assert not smoke.accept_translation(smoke.FAKE_EN[zh], "ja", zh)     # English is not a ja caption
    assert not smoke.accept_translation(zh, "ja", zh)                     # copied Chinese
    assert not smoke.accept_translation("", "en", zh)


def test_fake_mt_reply_picks_the_line_not_the_context():
    import threading
    logic = smoke._FakeMtHandler("ok", "ja", threading.Event())
    content = f"【背景信息】\n{smoke.FAKE_LINES[0]}\n\n请结合背景信息将以下文本翻译为日语。\n\n【待翻译文本】\n{smoke.FAKE_LINES[1]}"
    code, raw = logic.reply("/v1/chat/completions", {"messages": [{"role": "user", "content": content}]})
    assert code == 200
    assert json.loads(raw)["choices"][0]["message"]["content"] == smoke.FAKE_JA[smoke.FAKE_LINES[1]]


def test_ja_adapter_passes_target_language():
    seen = {}

    class Backend:
        model = "m"

        def translate(self, zh, *, tgt_lang, glossary=None, context=None, deadline=None, cancel=None):
            seen.update(zh=zh, tgt_lang=tgt_lang, context=context)
            from app.translate import TranslateResult
            return TranslateResult("こんばんは。", "ok")
    adapter = smoke.JaPipelineAdapter(Backend())
    assert adapter.translate("晚安", context=["前句"]).text == "こんばんは。"
    assert seen == {"zh": "晚安", "tgt_lang": "ja", "context": ["前句"]}


def test_cli_rejects_8645_and_real_without_audio(tmp_path):
    assert smoke.main(["--port", "8645", "--quiet"]) == 2
    assert smoke.main(["--real", "--quiet"]) == 2
    assert smoke.main(["--real", "--audio", str(tmp_path / "missing.wav"), "--quiet"]) == 2
    assert smoke.main(["--lang", "fr"]) == 2
    assert smoke.main(["--timeout", "0", "--quiet"]) == 2
    assert smoke.main(["--room", "bad room!", "--quiet"]) == 2
    assert smoke.main(["_serve-fake", "--port", "8645", "--work-dir", str(tmp_path)]) == 2


# ------------------------------------------------------------------ end to end (fake mode, subprocess)
@pytest.mark.parametrize("lang", ["en", "ja"])
def test_fake_flow_every_step_passes(tmp_path, lang):
    rc, report, out, _ = run_tool(tmp_path, "--fake", "--lang", lang, "--save-srt", str(tmp_path / "out.srt"))
    assert rc == 0, out
    assert report["ok"] is True and report["failed_step"] == "" and report["lang"] == lang
    assert [s["name"] for s in report["steps"]] == list(smoke.STEPS)
    assert set(statuses(report).values()) == {"PASS"}, report["steps"]
    assert 0 < report["port"] != 8645
    srt = (tmp_path / "out.srt").read_text(encoding="utf-8")
    table = smoke.FAKE_JA if lang == "ja" else smoke.FAKE_EN
    for i in (0, 1, 3):
        assert smoke.FAKE_LINES[i] in srt and table[smoke.FAKE_LINES[i]] in srt
    assert smoke.FAKE_LINES[smoke.PAUSED_INDEX] not in srt          # private pause never exported
    assert "[PASS] shutdown" in out and "RESULT: PASS" in out
    assert "Bearer" not in out and '"token"' not in json.dumps(report)
    assert "session=smoke-" in report["steps"][2]["detail"]           # unique session per run


def test_piped_ansi_codepage_stdout_does_not_crash(tmp_path):
    """Windows regression (筆電 DESKTOP-P8RGA3A): piped stdout was cp1252 and the first Chinese
    step line raised UnicodeEncodeError, so no report was written."""
    rc, report, out, _ = run_tool(tmp_path, "--fake-fail", "start", extra_env={"PYTHONIOENCODING": "cp1252"})
    assert rc == 1 and report["failed_step"] == "start_backend", out
    assert "UnicodeEncodeError" not in out and "RESULT: FAIL at step 'start_backend'" in out


def test_backend_start_failure_is_reported_and_later_steps_skip(tmp_path):
    rc, report, out, elapsed = run_tool(tmp_path, "--fake-fail", "start", "--timeout", "20")
    assert rc == 1
    st = statuses(report)
    assert report["failed_step"] == "start_backend" and st["start_backend"] == "FAIL"
    assert all(st[name] == "SKIP" for name in smoke.STEPS[1:-1])
    detail = report["steps"][0]["detail"]
    assert "exit 3" in detail and "simulated start failure" in detail
    assert "RESULT: FAIL at step 'start_backend'" in out
    assert elapsed < 20


def test_port_already_in_use_fails_start(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        rc, report, out, _ = run_tool(tmp_path, "--port", str(port), "--timeout", "20")
    assert rc == 1 and report["failed_step"] == "start_backend", out
    assert "提早結束" in report["steps"][0]["detail"]


@pytest.mark.parametrize("mode", ["down", "empty"])
def test_missing_translation_fails_the_translation_step(tmp_path, mode):
    rc, report, out, _ = run_tool(tmp_path, "--fake-mt", mode, "--mt-timeout", "8")
    assert rc == 1, out
    st = statuses(report)
    assert report["failed_step"] == "translation"
    assert st["start_backend"] == st["captions_zh"] == "PASS"      # Chinese is not blocked by MT failure
    assert st["translation"] == "FAIL" and st["pause_resume"] == "SKIP"
    assert st["shutdown"] == "PASS"
    assert "[FAIL] translation" in out and "RESULT: FAIL at step 'translation'" in out


def test_overall_timeout_is_honored(tmp_path):
    limit = 5.0
    rc, report, out, elapsed = run_tool(tmp_path, "--fake-mt", "slow", "--timeout", str(limit))
    assert rc == 1, out
    assert report["failed_step"] == "translation"
    assert "--timeout" in next(s for s in report["steps"] if s["name"] == "translation")["detail"]
    # timeout + shutdown budget + process start/teardown slack (loaded box)
    assert elapsed < limit + smoke.SHUTDOWN_BUDGET_S + 10, elapsed
    assert statuses(report)["shutdown"] == "PASS"


# ------------------------------------------------------------------ real mode (Windows host only)
@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("ZEN_SMOKE_REAL_AUDIO"),
                    reason="real mode needs the Windows desktop install (models, ffmpeg) and ZEN_SMOKE_REAL_AUDIO=<speech.wav>")
def test_real_mode_on_windows_host(tmp_path):
    lang = os.environ.get("ZEN_SMOKE_REAL_LANG", "en")
    rc, report, out, _ = run_tool(tmp_path, "--real", "--lang", lang, "--audio", os.environ["ZEN_SMOKE_REAL_AUDIO"],
                                  "--timeout", "300", "--start-timeout", "200", timeout=420)
    assert rc == 0, out
