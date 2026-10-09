"""The device RTF tool: fake recognizer, and /api/metrics on a local test server."""
from __future__ import annotations

import importlib.util
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.asr import AsrResult
from app.rtf import RtfMeter
from app.server import create_app
from app.settings import Settings
from app.translate import Translator
from tests.test_rtf_metrics import copy_decoder, wave_bytes

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rtf_check", ROOT / "tools" / "rtf_check.py")
assert _spec is not None and _spec.loader is not None
rtf_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtf_check)


def test_render_pass_fail_and_unverified_note():
    slow, slow_code = rtf_check.render(rtf_check.snapshot_from_pairs([(0.9, 1.0), (1.2, 1.0)]), "假樣本")
    assert slow_code == 1
    assert "FAIL" in slow
    assert "尚未驗證" in slow
    assert "p95" in slow
    fast, fast_code = rtf_check.render(rtf_check.snapshot_from_pairs([(0.2, 1.0)]), "假樣本")
    assert fast_code == 0
    assert "PASS" in fast
    assert "0.200" in fast
    assert "尚未驗證" in fast
    empty, empty_code = rtf_check.render(rtf_check.snapshot_from_pairs([]), "假樣本")
    assert empty_code == 1
    assert "沒有 RTF 樣本" in empty
    for report in (slow, fast, empty):
        assert "辨識逾時（不計入 RTF）：0" in report
        assert "辨識錯誤（不計入 RTF）：0" in report
        assert "尚未驗證" in report


def test_render_fails_when_there_is_no_sample_even_if_p95_is_zero():
    snap = rtf_check.snapshot_from_pairs([])
    snap["asr_rtf_p95"] = 0
    snap["asr_errors"] = 10
    snap["rtf"]["session"]["count"] = 0
    snap["rtf"]["session"]["rtf"]["p95"] = 0
    snap["rtf"]["window"]["count"] = 0
    snap["rtf"]["window"]["rtf"]["p95"] = 0
    text, code = rtf_check.render(snap, "全錯")
    assert code != 0
    assert "結果：PASS" not in text
    assert "沒有 RTF 樣本" in text
    assert "辨識錯誤（不計入 RTF）：10" in text


def test_render_lists_each_room_without_counting_timeouts_as_samples():
    meter = RtfMeter()
    meter.record(0.2, 1.0, ("east", "live"))
    meter.record(0.4, 1.0, ("west", "live"))
    meter.note_timeout(("west", "live"))
    text, code = rtf_check.render(meter.snapshot(), "兩房")
    assert code == 0
    assert "辨識逾時（不計入 RTF）：1" in text
    assert "房間 east/live：1 段，RTF p95 0.200" in text
    assert "房間 west/live：1 段，RTF p95 0.400" in text
    assert "尚未驗證" in text


def test_run_with_fake_asr_prints_pass_and_hides_transcript(monkeypatch, capsys, tmp_path):
    wav = tmp_path / "slice.wav"
    wav.write_bytes(wave_bytes(1.0))
    seen = {}

    class Fake:
        def transcribe(self, path, prompt):
            del path, prompt
            time.sleep(0.05)
            return AsrResult(ok=True, text="不要印出這句逐字稿XYZ")

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(rtf_check, "build_asr", lambda: Fake())
    code = rtf_check.main(["run", "--n", "3", "--audio", str(wav)])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASS" in captured.out
    assert "p95" in captured.out
    assert "尚未驗證" in captured.out
    assert "XYZ" not in captured.out and "XYZ" not in captured.err
    assert seen.get("closed") is True


def test_run_refuses_a_missing_model_without_cloud(monkeypatch, tmp_path):
    monkeypatch.setenv("BREEZE_MODEL", str(tmp_path / "missing.bin"))
    monkeypatch.setenv("BREEZE_ASR", "cli")
    wav = tmp_path / "slice.wav"
    wav.write_bytes(wave_bytes(0.5))
    with pytest.raises(SystemExit) as exc:
        rtf_check.main(["run", "--n", "1", "--audio", str(wav)])
    assert "不會改走雲端" in str(exc.value)


def test_run_does_not_guess_duration_from_a_non_wave_file(monkeypatch, tmp_path):
    bogus = tmp_path / "clip.bin"
    bogus.write_bytes(b"not-a-wave" * 4000)

    class Boom:
        def transcribe(self, path, prompt):
            del path, prompt
            raise AssertionError("非 WAV 不該在沒有可靠時長時送去辨識")

        def close(self):
            return None

    monkeypatch.setattr(rtf_check, "build_asr", lambda: Boom())
    with pytest.raises(SystemExit) as exc:
        rtf_check.main(["run", "--n", "1", "--audio", str(bogus)])
    message = str(exc.value)
    assert "不會用檔案大小去猜" in message or "無法解出" in message


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_metrics_command_against_test_server(capsys):
    port = _free_port()
    app = create_app(
        Settings(port=port, allow_testclient=True, translate=False, max_audio_bytes=2_000_000),
        asr=object(),
        translator=Translator(enabled=False, key=""),
        decoder=copy_decoder,
    )
    app.state.pipeline._rtf.record(0.3, 1.0, ("class", "live"))
    app.state.pipeline._rtf.record(0.5, 1.0, ("class", "live"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 5
        up = False
        while time.monotonic() < deadline:
            try:
                status, _raw = rtf_check._open(base + "/api/health", None, 0.5)
            except OSError:
                status = 0
            if status in {200, 503}:
                up = True
                break
            time.sleep(0.05)
        assert up, "test server did not start"
        code = rtf_check.main(["metrics", "--base", base])
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        assert "PASS" in captured.out
        assert "最大值" in captured.out
        assert "p50 0.400" in captured.out
        assert "p95 0.490" in captured.out
        assert "尚未驗證" in captured.out
        assert "等待辨識的音訊：0.000 秒" in captured.out
        assert app.state.token not in captured.out
        assert app.state.token not in captured.err
        room_code = rtf_check.main(["metrics", "--base", base, "--room", "class"])
        room_out = capsys.readouterr().out
        assert room_code == 0
        assert "判定房間：class" in room_out
        missing_code = rtf_check.main(["metrics", "--base", base, "--room", "north"])
        missing_out = capsys.readouterr().out
        assert missing_code == 1
        assert "FAIL" in missing_out
        assert "north" in missing_out
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(3)


def _detail_since(text: str) -> str:
    return text.split("本場以來", 1)[1].split("辨識逾時", 1)[0]


def test_verdict_uses_the_slowest_room_not_the_latest_update():
    meter = RtfMeter()
    meter.record(0.2, 1.0, ("east", "live"))
    meter.record(1.2, 1.0, ("west", "slow"))
    meter.record(0.3, 1.0, ("north", "live"))
    snap = meter.snapshot()
    assert snap["rtf"]["session"]["rtf"]["p95"] == pytest.approx(0.3)
    assert rtf_check.verdict_p95(snap) == pytest.approx(1.2)
    text, code = rtf_check.render(snap, "三房")
    assert code == 1
    assert "FAIL" in text
    assert "1.200" in text
    assert "最大值" in text
    assert "尚未開始辨識的音訊：0.000 秒" in text
    assert "正在辨識：0.000 秒" in text
    slow_last = RtfMeter()
    slow_last.record(0.2, 1.0, ("east", "live"))
    slow_last.record(0.3, 1.0, ("north", "live"))
    slow_last.record(1.2, 1.0, ("west", "slow"))
    assert slow_last.snapshot()["rtf"]["session"]["rtf"]["p95"] == pytest.approx(1.2)
    assert rtf_check.verdict_p95(slow_last.snapshot()) == pytest.approx(1.2)
    east, east_code = rtf_check.render(snap, "東", room="east")
    assert east_code == 0
    assert "PASS" in east
    assert "0.200" in east
    east_detail = _detail_since(east)
    assert "0.200" in east_detail
    assert "1.200" not in east_detail
    assert "0.300" not in east_detail
    west, west_code = rtf_check.render(snap, "西", room="west")
    assert west_code == 1
    west_detail = _detail_since(west)
    assert "1.200" in west_detail
    assert "0.200" not in west_detail
    assert "0.300" not in west_detail
    missing, missing_code = rtf_check.render(snap, "無", room="missing-room")
    assert missing_code == 1
    assert "沒有房間 missing-room" in missing
    assert "0.300" not in _detail_since(missing)
    assert "1.200" not in _detail_since(missing)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, delay: float) -> None:
        if delay > 0:
            self.t += delay


def test_cut_wav_segments_follow_the_six_second_pace(tmp_path):
    src = tmp_path / "talk.wav"
    src.write_bytes(wave_bytes(18.0))
    rows = rtf_check.cut_wav_segments(src, 6.0, tmp_path / "out")
    assert len(rows) == 3
    for path, duration in rows:
        assert duration == pytest.approx(6.0)
        assert path.is_file()
    short = tmp_path / "seven.wav"
    short.write_bytes(wave_bytes(7.0))
    parts = rtf_check.cut_wav_segments(short, 6.0, tmp_path / "seven")
    assert [round(item[1], 3) for item in parts] == [6.0, 1.0]
    bogus = tmp_path / "clip.bin"
    bogus.write_bytes(b"not-a-wave")
    with pytest.raises(SystemExit):
        rtf_check.cut_wav_segments(bogus, 6.0, tmp_path / "bad")


def test_pace_releases_a_slice_every_segment_not_back_to_back():
    clock = _Clock()
    starts: list[float] = []

    def transcribe(path, prompt):
        del path, prompt
        starts.append(clock.now())
        clock.sleep(1.0)
        return AsrResult(ok=True, text="不要印出SECRET")

    slices = [(Path(f"slice-{index}.wav"), 6.0) for index in range(3)]
    report = rtf_check.pace_transcriptions(
        transcribe, slices, workers=1, pace_s=6.0, sleep=clock.sleep, now=clock.now
    )
    assert starts == pytest.approx([0.0, 6.0, 12.0])
    assert report["max_backlog_s"] == 0
    assert report["final_lag_s"] == pytest.approx(1.0)
    assert report["cpu_p95"] is None
    assert report["cpu_source"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(report)


def test_pace_backlog_grows_when_recognition_is_slower_than_the_slice():
    clock = _Clock()
    starts: list[float] = []

    def transcribe(path, prompt):
        del path, prompt
        starts.append(clock.now())
        clock.sleep(10.0)
        return AsrResult(ok=True, text="x")

    slices = [(Path("slice.wav"), 6.0) for _ in range(3)]
    report = rtf_check.pace_transcriptions(
        transcribe, slices, workers=1, pace_s=6.0, sleep=clock.sleep, now=clock.now
    )
    assert starts == pytest.approx([0.0, 10.0, 20.0])
    assert report["max_backlog_s"] == pytest.approx(6.0)
    assert report["final_lag_s"] == pytest.approx(18.0)


def test_pace_without_the_gap_is_not_real_pacing():
    clock = _Clock()
    starts: list[float] = []

    def transcribe(path, prompt):
        del path, prompt
        starts.append(clock.now())
        clock.sleep(1.0)
        return AsrResult(ok=True, text="x")

    slices = [(Path("slice.wav"), 6.0) for _ in range(3)]
    rtf_check.pace_transcriptions(
        transcribe, slices, workers=1, pace_s=6.0, sleep=lambda delay: None, now=clock.now
    )
    assert starts == pytest.approx([0.0, 1.0, 2.0])


def test_recommend_matrix_picks_the_smallest_combo_and_ignores_unknown_cpu():
    rows = [
        {"threads": 4, "workers": 1, "rtf_p95": 1.2, "max_backlog_s": 0, "cpu_p95": None},
        {"threads": 4, "workers": 2, "rtf_p95": 0.5, "max_backlog_s": 0, "cpu_p95": None},
        {"threads": 6, "workers": 1, "rtf_p95": 0.4, "max_backlog_s": 12, "cpu_p95": None},
        {"threads": 8, "workers": 1, "rtf_p95": 0.3, "max_backlog_s": 0, "cpu_p95": 90},
        {"threads": 8, "workers": 2, "rtf_p95": 0.2, "max_backlog_s": 0, "cpu_p95": 10},
    ]
    advice = rtf_check.recommend_matrix(rows)
    assert advice["threads"] == 4
    assert advice["workers"] == 2
    assert advice["cpu_gate"] == "UNKNOWN"
    assert "尚未驗證" in advice["reason"]
    none = rtf_check.recommend_matrix([
        {"threads": 4, "workers": 1, "rtf_p95": 0.95, "max_backlog_s": 0, "cpu_p95": None},
    ])
    assert none["threads"] is None
    assert none["workers"] is None


def test_matrix_runs_on_a_fake_recognizer_without_printing_the_transcript(tmp_path):
    wav = tmp_path / "talk.wav"
    wav.write_bytes(wave_bytes(12.0))
    clock = _Clock()

    def build(threads, workers):
        assert threads in {4, 8}
        assert workers == 1

        class Fake:
            def transcribe(self, path, prompt):
                del path, prompt
                clock.sleep(1.0)
                return AsrResult(ok=True, text="不要印出SECRET逐字稿")

            def close(self):
                return None

        return Fake()

    report = rtf_check.run_matrix(
        wav,
        threads=[8, 4],
        workers=[1],
        segment_s=6.0,
        minutes=10,
        build=build,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert report["recommendation"]["threads"] == 4
    assert report["recommendation"]["workers"] == 1
    assert report["recommendation"]["cpu_gate"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(report)
    assert {row["threads"] for row in report["rows"]} == {4, 8}
    for row in report["rows"]:
        assert row["cpu_source"] == "UNKNOWN"
        assert row["cpu_p95"] is None
        assert row["segments"] == 2
        assert row["rtf_p95"] < 0.9
        assert row["max_backlog_s"] < 12
    printed = rtf_check._print_matrix(report)
    assert "SECRET" not in printed
    assert "BREEZE_ASR_THREADS=4" in printed


def test_matrix_command_refuses_a_missing_model(monkeypatch, tmp_path):
    monkeypatch.setenv("BREEZE_MODEL", str(tmp_path / "missing.bin"))
    monkeypatch.setenv("BREEZE_ASR", "cli")
    wav = tmp_path / "slice.wav"
    wav.write_bytes(wave_bytes(6.0))
    with pytest.raises(SystemExit) as exc:
        rtf_check.main(["matrix", "--audio", str(wav), "--threads", "4", "--workers", "1", "--minutes", "0.1"])
    assert "不會改走雲端" in str(exc.value)


def test_run_warns_that_a_long_file_is_smoke_not_six_second_pace(monkeypatch, capsys, tmp_path):
    wav = tmp_path / "long.wav"
    wav.write_bytes(wave_bytes(31.0))

    class Fake:
        def transcribe(self, path, prompt):
            del path, prompt
            return AsrResult(ok=True, text="不要印出SECRET")

        def close(self):
            return None

    monkeypatch.setattr(rtf_check, "build_asr", lambda: Fake())
    code = rtf_check.main(["run", "--n", "1", "--audio", str(wav)])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASS" in captured.out
    assert "6 秒" in captured.err
    assert "SECRET" not in captured.out and "SECRET" not in captured.err


def test_two_workers_start_together_instead_of_waiting_out_each_slice():
    slices = [(Path(f"{index}.wav"), 0.2) for index in range(4)]
    starts: list[float] = []
    gate = threading.Lock()

    def transcribe(path, prompt):
        del path, prompt
        with gate:
            starts.append(time.monotonic())
        time.sleep(0.2)
        return AsrResult(ok=True, text="ok")

    report = rtf_check.pace_transcriptions(transcribe, slices, workers=2, pace_s=0.05)
    assert len(report["pairs"]) == 4
    assert starts[1] - starts[0] < 0.15
    assert report["max_backlog_s"] > 0
    assert report["cpu_source"] == "UNKNOWN"


def _fmt_wav(fmt_body: bytes, seconds: float = 0.2) -> bytes:
    rate = 16000
    frames = int(round(seconds * rate))
    pcm = b"\x00\x00" * frames
    payload = fmt_body + (b"\x00" if len(fmt_body) % 2 else b"")
    chunks = b"fmt " + struct.pack("<I", len(payload)) + payload
    chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def test_non_pcm_wave_is_rejected_and_extensible_pcm_is_kept(tmp_path):
    pcm_subtype = bytes.fromhex("0100000000001000800000aa00389b71")
    float_subtype = bytes.fromhex("0300000000001000800000aa00389b71")
    float_fmt = struct.pack("<HHIIHH", 3, 1, 16000, 32000, 2, 32)
    adpcm = struct.pack("<HHIIHH", 2, 1, 16000, 8000, 2, 4)
    extensible_pcm = struct.pack("<HHIIHHHHI", 0xFFFE, 1, 16000, 32000, 2, 16, 22, 16, 0) + pcm_subtype
    extensible_float = struct.pack("<HHIIHHHHI", 0xFFFE, 1, 16000, 32000, 2, 32, 22, 32, 0) + float_subtype
    short_ext = struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
    for label, body in (
        ("float", float_fmt),
        ("adpcm", adpcm),
        ("ext-float", extensible_float),
        ("short-ext", short_ext),
    ):
        path = tmp_path / f"{label}.wav"
        path.write_bytes(_fmt_wav(body))
        dest = tmp_path / label
        with pytest.raises(SystemExit):
            rtf_check.cut_wav_segments(path, 6.0, dest)
        assert list(dest.glob("*.wav")) == []
        assert rtf_check._wave_spec(path) is None
    kept = tmp_path / "pcm-ext.wav"
    kept.write_bytes(_fmt_wav(extensible_pcm))
    rows = rtf_check.cut_wav_segments(kept, 6.0, tmp_path / "kept")
    assert len(rows) == 1
    assert rows[0][0].is_file()
    assert rtf_check._wave_spec(rows[0][0]) is not None
    assert rtf_check._is_pcm_wave(extensible_pcm) is True
    assert rtf_check._is_pcm_wave(float_fmt) is False


def test_minutes_stops_before_the_rest_of_a_long_wav_is_read(monkeypatch, tmp_path):
    src = tmp_path / "talk.wav"
    src.write_bytes(wave_bytes(30.0))
    size = src.stat().st_size
    read_bytes = {"n": 0}
    real_open = Path.open

    def tracking(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if Path(self) == src and "b" in str(mode):
            original = handle.read

            def read(n=-1, _original=original):
                data = _original(n)
                read_bytes["n"] += len(data)
                return data

            handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", tracking)
    cap = rtf_check.slice_limit(6.0, 0.1)
    assert cap == 1
    dest = tmp_path / "out"
    rows = rtf_check.cut_wav_segments(src, 6.0, dest, max_slices=cap)
    assert len(rows) == 1
    assert len(list(dest.glob("*.wav"))) == 1
    assert read_bytes["n"] < size / 2
    assert len(rtf_check.limit_slices(rows, 6.0, 0.1)) == 1
    source = (ROOT / "tools" / "rtf_check.py").read_text(encoding="utf-8")
    assert source.count("max_slices=slice_limit(") >= 2
    assert source.count("limit_slices(") >= 2


def test_pace_book_charges_only_when_every_worker_is_busy():
    book = rtf_check._PaceBook(2)
    book.enqueue(Path("a.wav"), 6.0, 0.0)
    assert book.backlog == 0
    assert book.max_backlog == 0
    assert book.dequeue()[0] == Path("a.wav")
    assert book.busy == 1
    book.enqueue(Path("b.wav"), 6.0, 1.0)
    assert book.backlog == 0
    book.finish()
    stolen = book.dequeue()
    assert stolen[0] == Path("b.wav")
    assert book.backlog == 0
    assert book.busy == 1
    book.finish()
    assert book.settled()

    full = rtf_check._PaceBook(2)
    full.enqueue(Path("a.wav"), 6.0, 0.0)
    full.dequeue()
    full.enqueue(Path("b.wav"), 6.0, 1.0)
    full.dequeue()
    full.enqueue(Path("c.wav"), 6.0, 2.0)
    assert full.backlog == pytest.approx(6.0)
    assert full.max_backlog == pytest.approx(6.0)
    full.dequeue()
    assert full.backlog == 0
    assert full.busy == 3


def test_parallel_pace_does_not_hang_when_a_worker_steals_the_queue():
    slices = [(Path(f"slice-{index}.wav"), 6.0) for index in range(12)]

    def transcribe(path, prompt):
        del path, prompt
        time.sleep(0.01)
        return AsrResult(ok=True, text="ok")

    for _ in range(10):
        holder: dict = {}

        def run() -> None:
            holder["report"] = rtf_check.pace_transcriptions(
                transcribe, slices, workers=2, pace_s=0
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert len(holder["report"]["pairs"]) == 12
