#!/usr/bin/env python3
"""Offline SRT/WebVTT caption checks.

Usage: python tools/srt_vtt_lint.py FILE --lang {en,ja} [--json]

All caption defects are errors except untranslated-Chinese warnings. Exit codes
are 0 (no errors), 1 (caption errors), and 2 (invalid arguments or unreadable
input). JSON reports contain file, format, lang, cue_count, errors, warnings,
and issues; each issue has cue_index, timestamp, severity, code, and message.
Unavailable timestamps are null; file-level issues use cue index 0.
Lengths include spaces but exclude markup and line breaks. Japanese lengths
and reading speeds count ASCII as half a character, other characters as one.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from html.parser import HTMLParser


TRADITIONAL = set("這們說會來與對讓嗎呢吧")
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|--!>|\Z)", re.DOTALL)
STAMP = {
    "srt": r"[0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3}",
    "vtt": r"(?:[0-9]{2,}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3}",
}


def timestamp_ms(value: str) -> int:
    parts = value.replace(",", ".").split(":")
    seconds, millis = parts[-1].split(".")
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    if minutes >= 60 or int(seconds) >= 60:
        raise ValueError("minutes and seconds must be below 60")
    return ((hours * 60 + minutes) * 60 + int(seconds)) * 1000 + int(millis)


def character_count(text: str, lang: str) -> float:
    return sum(0.5 if lang == "ja" and ord(char) < 128 else 1 for char in text)


class CaptionText(HTMLParser):
    """Extract visible caption text without counting tags or comments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(line: str) -> str:
    parser = CaptionText()
    parser.feed(line)
    parser.close()
    return "".join(parser.parts)


def lint(text: str, fmt: str, lang: str, *, max_cps: float | None = None,
         max_line: float | None = None, min_dur: float = 0.7,
         max_dur: float = 7.0) -> dict:
    """Lint decoded captions, recovering at cue boundaries after parse errors."""
    max_cps = max_cps if max_cps is not None else (20 if lang == "en" else 8)
    max_line = max_line if max_line is not None else (42 if lang == "en" else 16)
    lines = text.lstrip("\ufeff").splitlines()
    issues = []
    cues = []

    def issue(index, timestamp, code, message, severity="error"):
        issues.append({"cue_index": index, "timestamp": timestamp,
                       "severity": severity, "code": code, "message": message})

    timing_pattern = re.compile(
        rf"({STAMP[fmt]})[ \t]+-->[ \t]+({STAMP[fmt]})"
        + (r"(?:[ \t]+[^\r\n]+)?" if fmt == "vtt" else "")
    )
    def timing_candidate(line):
        prefix, arrow, _ = line.partition("-->")
        return bool(arrow) and (
            not prefix.strip() or re.fullmatch(r"[0-9][0-9:.,]*", prefix.strip()) is not None
        )
    pos = 0
    header_error = False
    valid_header = False
    if fmt == "vtt":
        if lines and re.fullmatch(r"WEBVTT(?:[ \t]+[^\r\n]*)?", lines[0]) and "-->" not in lines[0]:
            valid_header = True
            pos = 1
            if pos < len(lines) and lines[pos].strip():
                if "-->" in lines[pos] or (pos + 1 < len(lines) and "-->" in lines[pos + 1]):
                    header_error = True
                else:
                    while pos < len(lines) and lines[pos].strip() and "-->" not in lines[pos]:
                        pos += 1
                    header_error = pos < len(lines) and bool(lines[pos].strip())
        else:
            header_error = True

    while pos < len(lines):
        if not lines[pos].strip():
            pos += 1
            continue
        if fmt == "vtt" and (
            re.match(r"NOTE(?:[ \t]|$)", lines[pos]) or lines[pos] in {"STYLE", "REGION"}
        ):
            while pos < len(lines) and lines[pos].strip():
                pos += 1
            continue
        ordinal = len(cues) + 1
        index = ordinal
        index_line = None
        if "-->" not in lines[pos]:
            index_line = lines[pos].strip()
            pos += 1
        timestamp = lines[pos].strip() if pos < len(lines) and lines[pos].strip() else None
        if timestamp is not None:
            pos += 1
        if fmt == "srt":
            if (index_line is None or not re.fullmatch(r"[0-9]+", index_line)
                    or index_line.lstrip("0") != str(ordinal)):
                issue(index, timestamp, "bad_index", f"Expected SRT index {ordinal}.")
        match = timing_pattern.fullmatch(timestamp or "")
        start = end = None
        try:
            if not match:
                raise ValueError("invalid timestamp format")
            start, end = map(timestamp_ms, match.groups())
        except ValueError:
            issue(index, timestamp, "bad_timestamp", "Invalid cue timestamp format or range.")

        body = []
        missing_blank = False
        while pos < len(lines) and lines[pos].strip():
            if timing_candidate(lines[pos]):
                missing_blank = True
                break
            if (pos + 1 < len(lines) and timing_candidate(lines[pos + 1])
                    and (body or (fmt == "srt" and re.fullmatch(r"[0-9]+", lines[pos].strip())))):
                missing_blank = True
                break
            body.append(lines[pos])
            pos += 1
        if missing_blank:
            issue(index, timestamp, "missing_blank_line", "Missing blank line after cue.")
        cues.append((index, timestamp, start, end, body))

    if header_error:
        index, timestamp = cues[0][:2] if cues else (0, None)
        code = "missing_blank_line" if valid_header else "missing_header"
        issue(index, timestamp, code, "WebVTT requires a WEBVTT header followed by a blank line.")
    if not cues:
        issue(0, None, "no_cues", "No caption cues found.")

    previous = None
    for index, timestamp, start, end, body in cues:
        payload = HTML_COMMENT.sub(
            lambda match: "\n" * match.group().count("\n"), "\n".join(body)
        )
        visible = [visible_text(line) for line in payload.split("\n")]
        if not any(line.strip() for line in visible):
            issue(index, timestamp, "empty_text", "Cue text is empty.")
        if len(body) > 2:
            issue(index, timestamp, "too_many_lines", "Cue has more than two lines.")
        for number, line in enumerate(visible, 1):
            length = character_count(line, lang)
            if length > max_line:
                issue(index, timestamp, "line_too_long",
                      f"Line {number} has {length:g} characters (maximum {max_line:g}).")
        joined = "".join(visible)
        if lang == "ja" and any(
            char in TRADITIONAL or "\u3100" <= char <= "\u312f" or "\u31a0" <= char <= "\u31bf"
            for char in joined
        ):
            issue(index, timestamp, "untranslated_chinese",
                  "Possible untranslated Chinese or Bopomofo in Japanese cue.", "warning")
        if start is None or end is None:
            continue
        if previous is not None:
            if start < previous[0]:
                issue(index, timestamp, "non_monotonic_start", "Start precedes previous cue's start.")
            if start < previous[1]:
                issue(index, timestamp, "overlap", "Cue overlaps the previous cue.")
        previous = (start, end)
        duration = end - start
        if duration <= 0:
            issue(index, timestamp, "end_not_after_start", "End must be after start.")
            continue
        if duration < min_dur * 1000:
            issue(index, timestamp, "duration_too_short", f"Duration is below {min_dur:g} seconds.")
        if duration > max_dur * 1000:
            issue(index, timestamp, "duration_too_long", f"Duration exceeds {max_dur:g} seconds.")
        if character_count(joined, lang) > max_cps * duration / 1000:
            issue(index, timestamp, "reading_speed", f"Reading speed exceeds {max_cps:g} characters/s.")

    return {"format": fmt, "lang": lang, "cue_count": len(cues),
            "errors": sum(item["severity"] == "error" for item in issues),
            "warnings": sum(item["severity"] == "warning" for item in issues),
            "issues": issues}


def nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def positive(value: str) -> float:
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--lang", required=True, choices=("en", "ja"))
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument("--max-cps", type=positive)
    parser.add_argument("--max-line", type=positive)
    parser.add_argument("--min-dur", type=nonnegative, default=0.7)
    parser.add_argument("--max-dur", type=positive, default=7.0)
    args = parser.parse_args(argv)
    if args.min_dur > args.max_dur:
        parser.error("--min-dur must not exceed --max-dur")
    fmt = args.file.suffix.lower().lstrip(".")
    if fmt not in STAMP:
        parser.error("FILE must have an .srt or .vtt extension")
    try:
        text = args.file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        print(f"{args.file}: {exc}", file=sys.stderr)
        return 2
    report = {"file": str(args.file), **lint(
        text, fmt, args.lang, max_cps=args.max_cps, max_line=args.max_line,
        min_dur=args.min_dur, max_dur=args.max_dur)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for item in report["issues"]:
            print(f"{item['severity'].upper()} cue {item['cue_index']} "
                  f"[{item['timestamp'] or 'unknown timestamp'}] "
                  f"{item['code']}: {item['message']}")
        print(f"{report['cue_count']} cues: {report['errors']} errors, {report['warnings']} warnings")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
