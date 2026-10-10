"""Offline caption linting with tiny local fixtures; no app or network imports."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "srt_vtt_lint.py"
spec = importlib.util.spec_from_file_location("srt_vtt_lint", TOOL)
assert spec is not None and spec.loader is not None
linter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linter)


def run(tmp_path, capsys, text, *, fmt="srt", lang="en", options=()):
    path = tmp_path / f"captions.{fmt}"
    path.write_bytes(text.encode("utf-8"))
    code = linter.main([str(path), "--lang", lang, "--json", *options])
    captured = capsys.readouterr()
    assert not captured.err
    report = json.loads(captured.out)
    return code, report


def codes(report):
    return {item["code"] for item in report["issues"]}


def cue(text="Hello", start="00:00:00,000", end="00:00:02,000"):
    return f"1\n{start} --> {end}\n{text}\n\n"


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
@pytest.mark.parametrize("lang,text", [("en", "Hello"), ("ja", "こんにちは")])
@pytest.mark.parametrize("bom,ending", [("", "\n"), ("\ufeff", "\r\n")])
def test_clean_formats_languages_and_encoding(tmp_path, capsys, fmt, lang, text, bom, ending):
    content = cue(text)
    if fmt == "vtt":
        content = "WEBVTT\n\n" + content.replace(",", ".")
    code, report = run(tmp_path, capsys, bom + content.replace("\n", ending), fmt=fmt, lang=lang)
    assert code == 0
    assert report["issues"] == []
    assert report["cue_count"] == 1


@pytest.mark.parametrize("index", ["0", "2", "-1", "one", "1.0", "", "9" * 5000])
def test_bad_index(tmp_path, capsys, index):
    content = cue().split("\n", 1)[1]
    if index:
        content = index + "\n" + content
    code, report = run(tmp_path, capsys, content)
    assert code == 1
    assert codes(report) == {"bad_index"}
    assert report["issues"][0]["cue_index"] == 1
    assert report["issues"][0]["timestamp"] == "00:00:00,000 --> 00:00:02,000"


@pytest.mark.parametrize("text", ["Go --> home", "Hello\nGo --> home"])
def test_literal_arrow_in_srt_text(tmp_path, capsys, text):
    code, report = run(tmp_path, capsys, cue(text))
    assert code == 0
    assert report["cue_count"] == 1
    assert report["issues"] == []


def test_markup_and_comments_do_not_count_as_visible_characters(tmp_path, capsys):
    text = '<i title="a > b">' + "x" * 20 + "</i><!-- hidden --!>"
    code, report = run(tmp_path, capsys, cue(text, end="00:00:01,000"))
    assert code == 0
    assert report["issues"] == []


@pytest.mark.parametrize("fmt,start", [
    ("srt", "00:00:00.000"), ("srt", "00:00,000"), ("srt", "0:00:00,000"),
    ("srt", "00:60:00,000"), ("srt", "00:00:60,000"), ("srt", "00:00:00,00"),
    ("vtt", "00:00:00,000"), ("vtt", "00:60.000"), ("vtt", "0:00.000"),
    ("vtt", "00:00:00.000x"), ("srt", "garbage"),
])
def test_bad_timestamps(tmp_path, capsys, fmt, start):
    end = "00:00:02,000" if fmt == "srt" else "00:02.000"
    content = cue(start=start, end=end)
    if fmt == "vtt":
        content = "WEBVTT\n\n" + content
    code, report = run(tmp_path, capsys, content, fmt=fmt)
    assert code == 1
    assert codes(report) == {"bad_timestamp"}


@pytest.mark.parametrize("content", ["1\n\n", "1\nnot a timestamp\nHello\n\n"])
def test_missing_or_malformed_timestamp(tmp_path, capsys, content):
    code, report = run(tmp_path, capsys, content)
    assert code == 1
    assert "bad_timestamp" in codes(report)


@pytest.mark.parametrize("fmt,identifier", [("srt", "2\n"), ("vtt", ""), ("vtt", "second\n")])
def test_missing_blank_line_recovers_next_cue(tmp_path, capsys, fmt, identifier):
    content = cue().rstrip() + "\n" + identifier + "00:00:03,000 --> 00:00:05,000\nWorld\n"
    if fmt == "vtt":
        content = "WEBVTT\n\n" + content.replace(",", ".")
    code, report = run(tmp_path, capsys, content, fmt=fmt)
    assert code == 1
    assert report["cue_count"] == 2
    assert codes(report) == {"missing_blank_line"}
    assert report["issues"][0]["cue_index"] == 1


@pytest.mark.parametrize("prefix", ["", "WEBVTTbad\n\n", " WEBVTT\n\n"])
def test_vtt_missing_header(tmp_path, capsys, prefix):
    code, report = run(tmp_path, capsys, prefix + cue().replace(",", "."), fmt="vtt")
    assert code == 1
    assert "missing_header" in codes(report)
    assert next(item for item in report["issues"] if item["code"] == "missing_header")["cue_index"] == 1


def test_vtt_header_requires_blank_line(tmp_path, capsys):
    code, report = run(tmp_path, capsys, "WEBVTT\n" + cue().replace(",", "."), fmt="vtt")
    assert code == 1
    assert codes(report) == {"missing_blank_line"}


def test_vtt_identifiers_settings_and_noncue_blocks(tmp_path, capsys):
    content = (
        "WEBVTT captions\nLanguage: en\n\nNOTE a comment\nignore this\n\n"
        "STYLE\n::cue { color: lime; }\n\nREGION\nid:bottom\n\n"
        "first cue\n00:00.000 --> 00:02.000 align:start position:10%\n"
        "<v Speaker><i>Hello &amp; goodbye</i>\n\n"
        "00:02.000 --> 00:04.000\nWorld\n"
    )
    code, report = run(tmp_path, capsys, content, fmt="vtt")
    assert code == 0
    assert report["cue_count"] == 2


@pytest.mark.parametrize("end", ["00:00:01,000", "00:00:00,500"])
def test_end_not_after_start(tmp_path, capsys, end):
    code, report = run(tmp_path, capsys, cue(start="00:00:01,000", end=end))
    assert code == 1
    assert codes(report) == {"end_not_after_start"}


@pytest.mark.parametrize("start,expected", [
    ("00:00:00,500", {"overlap", "non_monotonic_start"}),
    ("00:00:01,000", {"overlap"}),
    ("00:00:02,000", {"overlap"}),
    ("00:00:03,000", set()),
])
def test_order_and_overlap(tmp_path, capsys, start, expected):
    content = cue(start="00:00:01,000", end="00:00:03,000")
    content += f"2\n{start} --> 00:00:04,000\nWorld\n"
    code, report = run(tmp_path, capsys, content)
    assert code == bool(expected)
    assert codes(report) == expected
    assert all(item["cue_index"] == 2 and item["timestamp"].startswith(start) for item in report["issues"])


@pytest.mark.parametrize("end,expected", [
    ("00:00:00,699", {"duration_too_short"}), ("00:00:00,700", set()),
    ("00:00:07,000", set()), ("00:00:07,001", {"duration_too_long"}),
])
def test_duration_boundaries(tmp_path, capsys, end, expected):
    code, report = run(tmp_path, capsys, cue("Hi", end=end))
    assert code == bool(expected)
    assert codes(report) == expected


@pytest.mark.parametrize("lang,text,expected", [
    ("en", "x" * 42, set()), ("en", "x" * 43, {"line_too_long"}),
    ("ja", "あ" * 16, set()), ("ja", "あ" * 17, {"line_too_long"}),
    ("ja", "x" * 32, set()), ("ja", "x" * 33, {"line_too_long"}),
    ("ja", "あ" * 15 + "ab", set()), ("ja", "あ" * 15 + "abc", {"line_too_long"}),
])
def test_line_length_boundaries(tmp_path, capsys, lang, text, expected):
    code, report = run(tmp_path, capsys, cue(text, end="00:00:07,000"), lang=lang)
    assert code == bool(expected)
    assert codes(report) == expected


@pytest.mark.parametrize("lang,text,expected", [
    ("en", "x" * 20, set()), ("en", "x" * 21, {"reading_speed"}),
    ("ja", "あ" * 8, set()), ("ja", "あ" * 9, {"reading_speed"}),
    ("ja", "x" * 16, set()), ("ja", "x" * 17, {"reading_speed"}),
    ("en", "x" * 10 + "\n" + "x" * 10, set()),
    ("en", "x" * 10 + "\n" + "x" * 11, {"reading_speed"}),
    ("en", "x" * 13 + " ", set()),
])
def test_reading_speed_boundaries(tmp_path, capsys, lang, text, expected):
    end = "00:00:00,700" if text.endswith(" ") else "00:00:01,000"
    code, report = run(tmp_path, capsys, cue(text, end=end), lang=lang)
    assert code == bool(expected)
    assert codes(report) == expected


@pytest.mark.parametrize("text,expected", [
    ("One\nTwo", set()), ("One\nTwo\nThree", {"too_many_lines"}),
    ("", {"empty_text"}), (" \t", {"empty_text"}), ("<i></i>", {"empty_text"}),
])
def test_line_count_and_empty_text(tmp_path, capsys, text, expected):
    code, report = run(tmp_path, capsys, cue(text))
    assert code == bool(expected)
    assert codes(report) == expected


@pytest.mark.parametrize("character", list("這們說會來與對讓嗎呢吧") + ["ㄅ", "ㆠ"])
def test_japanese_untranslated_warning(tmp_path, capsys, character):
    code, report = run(tmp_path, capsys, cue(character), lang="ja")
    assert code == 0
    assert codes(report) == {"untranslated_chinese"}
    assert report["errors"] == 0 and report["warnings"] == 1
    assert report["issues"][0]["severity"] == "warning"


def test_english_does_not_warn_and_japanese_kanji_are_allowed(tmp_path, capsys):
    for lang, text in [("en", "這ㄅ"), ("ja", "日本語の字幕です")]:
        code, report = run(tmp_path, capsys, cue(text), lang=lang)
        assert code == 0
        assert report["issues"] == []


@pytest.mark.parametrize("text", ["会議の時間です", "個人情報", "行為を還元する"])
def test_standard_japanese_kanji_do_not_warn(tmp_path, capsys, text):
    code, report = run(tmp_path, capsys, cue(text), lang="ja")
    assert code == 0
    assert report["warnings"] == 0
    assert report["issues"] == []


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_all_threshold_overrides(tmp_path, capsys, lang):
    options = ("--max-cps", "30", "--max-line", "30", "--min-dur", "0.5", "--max-dur", "8")
    content = cue("x" * 15, end="00:00:00,500")
    content += "2\n00:00:01,000 --> 00:00:09,000\n" + "あ" * 30 + "\n"
    code, report = run(tmp_path, capsys, content, lang=lang, options=options)
    assert code == 0
    assert report["issues"] == []
    _, default_report = run(tmp_path, capsys, content, lang=lang)
    assert {"duration_too_short", "duration_too_long"} <= codes(default_report)


def test_stricter_threshold_overrides(tmp_path, capsys):
    code, report = run(tmp_path, capsys, cue("Hello"), options=(
        "--max-cps", "2", "--max-line", "4", "--min-dur", "3", "--max-dur", "4"))
    assert code == 1
    assert codes(report) == {"reading_speed", "line_too_long", "duration_too_short"}
    code, report = run(tmp_path, capsys, cue(), options=("--min-dur", "0", "--max-dur", "1"))
    assert code == 1
    assert codes(report) == {"duration_too_long"}


def test_parse_error_does_not_hide_later_cue_errors(tmp_path, capsys):
    content = "1\nbad timestamp\nHello\n\n2\n00:00:01,000 --> 00:00:01,500\nWorld\n"
    code, report = run(tmp_path, capsys, content)
    assert code == 1
    assert report["cue_count"] == 2
    assert codes(report) == {"bad_timestamp", "duration_too_short"}
    assert report["issues"][-1]["cue_index"] == 2


def test_srt_index_sequence_and_bad_end_timestamp(tmp_path, capsys):
    content = cue() + "1\n00:00:03,000 --> 00:00:60,000\nWorld\n"
    code, report = run(tmp_path, capsys, content)
    assert code == 1
    assert codes(report) == {"bad_index", "bad_timestamp"}
    assert all(item["cue_index"] == 2 for item in report["issues"])


def test_json_schema_multiple_errors_and_warnings(tmp_path, capsys):
    code, report = run(tmp_path, capsys, cue("這" * 17, end="00:00:00,500"), lang="ja")
    assert code == 1
    assert set(report) == {"file", "format", "lang", "cue_count", "errors", "warnings", "issues"}
    assert report["file"] == str(tmp_path / "captions.srt")
    assert report["format"] == "srt" and report["lang"] == "ja"
    assert report["errors"] == 3 and report["warnings"] == 1
    for item in report["issues"]:
        assert set(item) == {"cue_index", "timestamp", "severity", "code", "message"}
        assert item["cue_index"] == 1
        assert item["timestamp"] == "00:00:00,000 --> 00:00:00,500"
        assert isinstance(item["message"], str) and item["message"]


@pytest.mark.parametrize("fmt,text", [("srt", ""), ("vtt", "WEBVTT\n\n")])
def test_empty_file_is_caption_error(tmp_path, capsys, fmt, text):
    code, report = run(tmp_path, capsys, text, fmt=fmt)
    assert code == 1
    assert report["cue_count"] == 0
    assert codes(report) == {"no_cues"}
    assert report["issues"][0]["timestamp"] is None


@pytest.mark.parametrize("options", [
    (), ("--lang", "zh"), ("--lang", "en", "--max-cps", "0"),
    ("--lang", "en", "--max-line", "-1"), ("--lang", "en", "--min-dur", "-1"),
    ("--lang", "en", "--max-dur", "0"), ("--lang", "en", "--max-cps", "nan"),
    ("--lang", "en", "--max-line", "inf"), ("--lang", "en", "--min-dur", "oops"),
    ("--lang", "en", "--min-dur", "8"), ("--lang", "en", "--unknown"),
])
def test_invalid_usage(tmp_path, capsys, options):
    with pytest.raises(SystemExit) as exc:
        linter.main([str(tmp_path / "captions.srt"), *options])
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_invalid_input(tmp_path, capsys):
    path = tmp_path / "missing.srt"
    assert linter.main([str(path), "--lang", "en"]) == 2
    assert capsys.readouterr().err
    path.write_bytes(b"\xff")
    assert linter.main([str(path), "--lang", "en"]) == 2
    assert capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        linter.main([str(tmp_path / "captions.txt"), "--lang", "en"])
    assert exc.value.code == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize("text,lang,expected", [
    (cue(), "en", 0), (cue("這"), "ja", 0),
    (cue(end="00:00:00,500"), "en", 1),
])
def test_cli_process_exit_codes_and_text_output(tmp_path, text, lang, expected):
    path = tmp_path / "captions.srt"
    path.write_text(text, encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), str(path), "--lang", lang],
                            capture_output=True, text=True, cwd=ROOT, check=False)
    assert result.returncode == expected
    assert not result.stderr
    if expected or lang == "ja":
        assert "cue 1 [00:00:00,000 -->" in result.stdout
        assert ("WARNING" if lang == "ja" else "ERROR") in result.stdout
    else:
        assert "1 cues: 0 errors, 0 warnings" in result.stdout


def test_cli_process_invalid_input_and_json(tmp_path):
    path = tmp_path / "captions.VTT"
    path.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nHello\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(TOOL), str(path), "--lang", "en", "--json"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["issues"] == []
    path.unlink()
    result = subprocess.run([sys.executable, str(TOOL), str(path), "--lang", "en"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2 and result.stderr
