"""Tests for the offline RMS VAD sweep planner; no ASR or external tools are used."""

import csv
import json
import math
import struct
import wave

import pytest

from tools import vad_sweep_plan as planner


def write_wav(path, parts, *, channels=1, sample_rate=16000, low_noise=True):
    """Write ``(duration_seconds, amplitude)`` parts, using low deterministic noise for silence."""
    samples = []
    for duration, amplitude in parts:
        count = int(duration * sample_rate)
        for index in range(count):
            if amplitude:
                value = int(amplitude * math.sin(2 * math.pi * 440 * index / sample_rate))
            else:
                value = (index % 7) - 3 if low_noise else 0
            samples.extend([value] * channels)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def test_profile_reports_rms_percentiles_for_mono_and_stereo(tmp_path):
    mono = write_wav(tmp_path / "mono.wav", [(1, 0), (1, 10000)])
    stereo = write_wav(tmp_path / "stereo.wav", [(1, 0), (1, 10000)], channels=2)

    mono_profile = planner.read_energy_profile(mono)
    stereo_profile = planner.read_energy_profile(stereo)

    assert mono_profile["frames"] == 100
    assert mono_profile["channels"] == 1
    assert mono_profile["noise_floor_p10"] < 5
    assert mono_profile["speech_p90"] > 6000
    assert stereo_profile["channels"] == 2
    assert stereo_profile["speech_p90"] == pytest.approx(mono_profile["speech_p90"])


def test_plan_simulates_candidates_and_marks_one_caption_length_row(tmp_path):
    wav = write_wav(
        tmp_path / "caption.wav",
        [(1.0, 0), (3.0, 10000), (0.8, 0), (3.0, 10000), (1.0, 0)],
    )

    rows, summaries = planner.build_plan([wav])

    assert summaries[0]["duration_ms"] == 8800
    assert len(rows) == 5 * 3 * 3 * 3
    assert {row["min_speech_ms"] for row in rows} == {200, 300, 500}
    assert {row["min_silence_ms"] for row in rows} == {300, 500, 800}
    assert {row["pad_ms"] for row in rows} == {0, 100, 200}
    assert all(0 <= row["fraction_above_threshold"] <= 1 for row in rows)
    assert sum(row["recommended"] for row in rows) == 1
    recommended = next(row for row in rows if row["recommended"])
    assert 2000 <= recommended["mean_segment_length_ms"] <= 8000
    assert recommended["recommendation_note"] == "targets_2_to_8_seconds"
    assert recommended["expected_segment_count"] == 2


def test_cli_writes_csv_and_json_without_overwriting(tmp_path, capsys):
    wav = write_wav(tmp_path / "audio.wav", [(1.0, 0), (3.0, 10000)])
    output = tmp_path / "plan.csv"

    assert planner.main([str(wav), "--out", str(output), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["rows"]) == 5 * 3 * 3 * 3
    with output.open(encoding="utf-8", newline="") as csv_file:
        csv_rows = list(csv.DictReader(csv_file))
    assert len(csv_rows) == len(report["rows"])
    assert sum(row["recommended"] == "True" for row in csv_rows) == 1

    output.write_text("keep me", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        planner.main([str(wav), "--out", str(output)])
    assert error.value.code == 2
    assert output.read_text(encoding="utf-8") == "keep me"


def test_invalid_audio_and_empty_inputs_are_rejected(tmp_path):
    bad = tmp_path / "invalid.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(ValueError, match="cannot read WAV"):
        planner.read_energy_profile(bad)
    with pytest.raises(ValueError, match="at least one"):
        planner.build_plan([])


def test_rejects_non_16_bit_or_multichannel_wav(tmp_path):
    wav_path = tmp_path / "unsupported.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(3)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 6)

    with pytest.raises(ValueError, match="mono or stereo"):
        planner.read_energy_profile(wav_path)


def test_silence_profile_still_has_a_single_marked_fallback_row(tmp_path):
    wav = write_wav(tmp_path / "silence.wav", [(2.0, 0)], low_noise=False)

    rows, _ = planner.build_plan([wav])

    assert len(rows) == 27
    assert sum(row["recommended"] for row in rows) == 1
    assert next(row for row in rows if row["recommended"])["recommendation_note"].startswith(
        "closest_available"
    )


def test_padding_does_not_merge_segments_separated_by_minimum_silence():
    count, mean_length = planner.segment_stats(
        [1, 1, 1, 0, 0, 0, 1, 1, 1],
        threshold=0.5,
        frame_ms=100,
        min_speech_ms=200,
        min_silence_ms=300,
        pad_ms=200,
    )

    assert count == 2
    assert mean_length == 500


def test_plan_uses_exported_rounded_threshold_for_statistics(monkeypatch):
    monkeypatch.setattr(
        planner,
        "read_energy_profile",
        lambda path: {
            "input": str(path),
            "sample_rate": 16000,
            "channels": 1,
            "frames": 25,
            "frame_ms": 20,
            "frame_durations_ms": [20] * 25,
            "duration_ms": 500,
            "energies": [0.1002] * 25,
            "noise_floor_p10": 0.1,
            "speech_p90": 0.2,
        },
    )
    monkeypatch.setattr(planner, "candidate_thresholds", lambda profile: [0.1004])

    rows, _ = planner.build_plan(["rounded-threshold.wav"])

    assert {row["energy_threshold"] for row in rows} == {0.1}
    assert all(row["fraction_above_threshold"] == 1 for row in rows)
    assert all(row["expected_segment_count"] == 1 for row in rows)


def test_partial_final_frame_uses_its_actual_duration(tmp_path):
    wav = write_wav(tmp_path / "partial.wav", [(0.045, 10000)])

    profile = planner.read_energy_profile(wav)
    count, mean_length = planner.segment_stats(
        profile["energies"],
        threshold=0,
        frame_ms=profile["frame_ms"],
        min_speech_ms=40,
        min_silence_ms=0,
        pad_ms=0,
        frame_durations_ms=profile["frame_durations_ms"],
    )

    assert profile["duration_ms"] == 45
    assert profile["frame_durations_ms"] == pytest.approx([20, 20, 5])
    assert count == 1
    assert mean_length == pytest.approx(45)
