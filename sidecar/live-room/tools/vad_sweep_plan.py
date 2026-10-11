#!/usr/bin/env python3
"""Create an offline RMS-based VAD parameter plan from PCM WAV files; never run ASR."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import wave
from pathlib import Path


FRAME_MS = 20
MIN_SPEECH_MS = (200, 300, 500)
MIN_SILENCE_MS = (300, 500, 800)
PADDING_MS = (0, 100, 200)
CSV_COLUMNS = (
    "input",
    "sample_rate",
    "channels",
    "frames",
    "duration_ms",
    "noise_floor_p10",
    "speech_p90",
    "energy_threshold",
    "fraction_above_threshold",
    "min_speech_ms",
    "min_silence_ms",
    "pad_ms",
    "expected_segment_count",
    "mean_segment_length_ms",
    "recommended",
    "recommendation_note",
)


def percentile(values: list[float], p: float) -> float:
    """Linearly interpolated percentile; ``values`` must be non-empty and sorted."""
    position = (len(values) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def read_energy_profile(path: str | Path) -> dict:
    """Read 16-bit PCM mono/stereo and return one RMS value per 20 ms frame."""
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            if (wav.getcomptype() != "NONE" or wav.getsampwidth() != 2
                    or channels not in (1, 2) or sample_rate < 1):
                raise ValueError(f"{source}: expected uncompressed 16-bit PCM mono or stereo WAV")
            pcm = wav.readframes(frame_count)
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError(f"{source}: cannot read WAV: {exc}") from exc

    bytes_per_frame = channels * 2
    if len(pcm) != frame_count * bytes_per_frame:
        raise ValueError(f"{source}: truncated PCM data")

    sample_frames = max(1, sample_rate * FRAME_MS // 1000)
    energies = []
    frame_durations_ms = []
    total_samples = frame_count
    for frame_start in range(0, total_samples, sample_frames):
        frame_end = min(total_samples, frame_start + sample_frames)
        square_sum = 0
        sample_count = 0
        for sample_frame in range(frame_start, frame_end):
            offset = sample_frame * bytes_per_frame
            for channel in range(channels):
                sample = struct.unpack_from("<h", pcm, offset + channel * 2)[0]
                square_sum += sample * sample
                sample_count += 1
        energies.append(math.sqrt(square_sum / sample_count) if sample_count else 0.0)
        frame_durations_ms.append((frame_end - frame_start) * 1000.0 / sample_rate)

    ordered = sorted(energies)
    return {
        "input": str(source),
        "sample_rate": sample_rate,
        "channels": channels,
        "frames": len(energies),
        "frame_ms": sample_frames * 1000.0 / sample_rate,
        "frame_durations_ms": frame_durations_ms,
        "duration_ms": frame_count * 1000.0 / sample_rate,
        "energies": energies,
        "noise_floor_p10": percentile(ordered, 0.10) if ordered else 0.0,
        "speech_p90": percentile(ordered, 0.90) if ordered else 0.0,
    }


def candidate_thresholds(profile: dict) -> list[float]:
    """Build five evenly spaced RMS thresholds from the p10-to-p90 energy range."""
    low = profile["noise_floor_p10"]
    high = profile["speech_p90"]
    return sorted({low + (high - low) * index / 4 for index in range(5)})


def segment_stats(energies: list[float], threshold: float, frame_ms: float,
                  min_speech_ms: int, min_silence_ms: int, pad_ms: int,
                  frame_durations_ms: list[float] | None = None) -> tuple[int, float | None]:
    """Simulate thresholded segments, short-silence joining, minimum length, and padding."""
    durations = frame_durations_ms or [frame_ms] * len(energies)
    boundaries = [0.0]
    for duration in durations:
        boundaries.append(boundaries[-1] + duration)

    runs = []
    start = None
    for index, energy in enumerate(energies):
        if energy > threshold and start is None:
            start = index
        elif energy <= threshold and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(energies)))

    joined = []
    for run_start, run_end in runs:
        if joined and boundaries[run_start] - boundaries[joined[-1][1]] < min_silence_ms:
            joined[-1] = (joined[-1][0], run_end)
        else:
            joined.append((run_start, run_end))

    padded_lengths = []
    for run_start, run_end in joined:
        if boundaries[run_end] - boundaries[run_start] < min_speech_ms:
            continue
        segment_start = max(0.0, boundaries[run_start] - pad_ms)
        segment_end = min(boundaries[-1], boundaries[run_end] + pad_ms)
        padded_lengths.append(segment_end - segment_start)

    if not padded_lengths:
        return 0, None
    return len(padded_lengths), sum(padded_lengths) / len(padded_lengths)


def build_plan(paths: list[str | Path]) -> tuple[list[dict], list[dict]]:
    """Return plan rows and per-file profile summaries, marking exactly one row."""
    if not paths:
        raise ValueError("provide at least one WAV file")
    rows = []
    summaries = []
    for path in paths:
        profile = read_energy_profile(path)
        summaries.append({key: profile[key] for key in (
            "input", "sample_rate", "channels", "frames", "duration_ms",
            "noise_floor_p10", "speech_p90",
        )})
        thresholds = sorted({round(value, 3) for value in candidate_thresholds(profile)} or {0.0})
        energies = profile["energies"]
        for threshold in thresholds:
            fraction = (sum(energy > threshold for energy in energies) / len(energies)
                        if energies else 0.0)
            for min_speech in MIN_SPEECH_MS:
                for min_silence in MIN_SILENCE_MS:
                    for padding in PADDING_MS:
                        count, mean_length = segment_stats(
                            energies, threshold, profile["frame_ms"],
                            min_speech, min_silence, padding,
                            profile["frame_durations_ms"],
                        )
                        rows.append({
                            "input": profile["input"],
                            "sample_rate": profile["sample_rate"],
                            "channels": profile["channels"],
                            "frames": profile["frames"],
                            "duration_ms": round(profile["duration_ms"], 3),
                            "noise_floor_p10": round(profile["noise_floor_p10"], 3),
                            "speech_p90": round(profile["speech_p90"], 3),
                            "energy_threshold": threshold,
                            "fraction_above_threshold": round(fraction, 6),
                            "min_speech_ms": min_speech,
                            "min_silence_ms": min_silence,
                            "pad_ms": padding,
                            "expected_segment_count": count,
                            "mean_segment_length_ms": (
                                round(mean_length, 3) if mean_length is not None else ""
                            ),
                            "recommended": False,
                            "recommendation_note": "",
                        })

    def recommendation_score(row):
        length = row["mean_segment_length_ms"]
        in_target = isinstance(length, (int, float)) and 2000 <= length <= 8000
        distance = abs(length - 5000) if isinstance(length, (int, float)) else math.inf
        return (not in_target, distance, row["input"], row["energy_threshold"],
                row["min_speech_ms"], row["min_silence_ms"], row["pad_ms"])

    recommendation = min(rows, key=recommendation_score)
    recommendation["recommended"] = True
    recommendation["recommendation_note"] = (
        "targets_2_to_8_seconds"
        if isinstance(recommendation["mean_segment_length_ms"], (int, float))
        and 2000 <= recommendation["mean_segment_length_ms"] <= 8000
        else "closest_available; no row targets_2_to_8_seconds"
    )
    return rows, summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", nargs="+", help="16-bit PCM mono/stereo WAV input(s)")
    parser.add_argument("--out", required=True, help="new CSV output path (must not exist)")
    parser.add_argument("--json", action="store_true", help="print the full plan as JSON")
    args = parser.parse_args(argv)

    try:
        rows, summaries = build_plan(args.wav)
        with open(args.out, "x", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps({"files": summaries, "rows": rows}, sort_keys=True))
    else:
        recommended = next(row for row in rows if row["recommended"])
        print(
            f"Wrote {len(rows)} plan rows for {len(summaries)} WAV file(s) to {args.out}; "
            f"recommended mean segment length: {recommended['mean_segment_length_ms']} ms "
            f"({recommended['recommendation_note']})."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
