"""Offline regression checks using only hand-written, saved fake outputs."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import mt_regression_check as checker


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "mt_regression_check.py"


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def fixture_files(tmp_path, out, terms=None, forbidden=None, zh="今天先練習坐禪。"):
    regression = write_jsonl(tmp_path / "regression.jsonl", [
        {"id": "sample", "zh": zh, "must_terms": terms or [], "forbidden": forbidden or [], "notes": "fake"}
    ])
    outputs = write_jsonl(tmp_path / "outputs.jsonl", [{"id": "sample", "out": out}])
    return regression, outputs


@pytest.mark.parametrize("lang,out,terms", [
    ("en", "Practice MEDITATION.", ["meditation"]),
    ("ja", "今日は坐禅を練習します。", ["坐禅"]),
])
def test_pass_both_languages(tmp_path, lang, out, terms):
    summary = checker.check(*fixture_files(tmp_path, out, terms), lang)
    assert summary["total"] == summary["passed"] == 1
    assert summary["failed"] == 0
    assert summary["pass_rate"] == 1
    assert not any(summary["failure_counts"].values())
    assert summary["results"][0]["ratio"] == len(out) / len("今天先練習坐禪。")
    assert (summary["min_ratio"], summary["max_ratio"]) == checker.DEFAULT_BOUNDS[lang]


@pytest.mark.parametrize("char", list("這們說會來與對讓嗎呢吧") + ["ㄅ", "ㄩ", "ㆠ", "ㆿ"])
def test_japanese_residual_characters(tmp_path, char):
    summary = checker.check(*fixture_files(tmp_path, "坐禅を練習します" + char, ["坐禅"]), "ja")
    result = summary["results"][0]
    assert result["failures"] == ["residual_chinese"]
    assert result["residual_characters"] == [char]
    assert summary["failure_counts"]["residual_chinese"] == 1


@pytest.mark.parametrize("out,zh", [
    ("今日は坐禅の時間です。", "今天是坐禪時間。"),
    ("個人で練習します。", "每個人各自練習。"),
    ("還元", "還原"),
    ("為替", "匯率"),
])
def test_common_japanese_kanji_are_not_residual_chinese(tmp_path, out, zh):
    summary = checker.check(*fixture_files(tmp_path, out, zh=zh), "ja")
    assert summary["passed"] == 1
    assert summary["failure_counts"]["residual_chinese"] == 0
    assert summary["results"][0]["failures"] == []


def test_residual_heuristic_is_japanese_only(tmp_path):
    summary = checker.check(*fixture_files(tmp_path, "meditation 這ㄅㆠ", ["meditation"]), "en")
    assert summary["passed"] == 1


@pytest.mark.parametrize("lang,out,terms", [
    ("en", "Practice meditation.", ["meditation", "breathing"]),
    ("ja", "今日は坐禅を練習します。", ["坐禅", "呼吸"]),
    ("ja", "今日はZENを練習します。", ["zen"]),
])
def test_all_must_terms_required(tmp_path, lang, out, terms):
    summary = checker.check(*fixture_files(tmp_path, out, terms), lang)
    assert summary["results"][0]["failures"] == ["must_terms"]
    assert summary["results"][0]["missing_terms"] == [terms[-1]]


@pytest.mark.parametrize("lang,out,forbidden", [
    ("en", "Do not HOLD your breath.", ["hold"]),
    ("ja", "今日は坐禅を録音します。", ["録音"]),
])
def test_forbidden_terms(tmp_path, lang, out, forbidden):
    summary = checker.check(*fixture_files(tmp_path, out, forbidden=forbidden), lang)
    assert summary["results"][0]["failures"] == ["forbidden"]
    assert summary["results"][0]["forbidden_terms"] == forbidden


@pytest.mark.parametrize("lang", ["en", "ja"])
@pytest.mark.parametrize("out", ["", " \t\n"])
def test_empty_and_whitespace_outputs(tmp_path, lang, out):
    summary = checker.check(*fixture_files(tmp_path, out, ["呼吸"]), lang)
    assert summary["passed"] == 0
    assert summary["results"][0]["failures"] == ["empty_output", "must_terms", "length_ratio"]


@pytest.mark.parametrize("lang,length,passed", [
    ("en", 7, False), ("en", 8, True), ("en", 60, True), ("en", 61, False),
    ("ja", 5, False), ("ja", 6, True), ("ja", 30, True), ("ja", 31, False),
])
def test_default_ratio_boundaries(tmp_path, lang, length, passed):
    summary = checker.check(*fixture_files(tmp_path, "a" * length, zh="呼" * 10), lang)
    assert summary["results"][0]["passed"] is passed
    assert summary["failure_counts"]["length_ratio"] == int(not passed)


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_custom_ratio_bounds(tmp_path, lang):
    files = fixture_files(tmp_path, "a" * 40, zh="呼" * 10)
    assert checker.check(*files, lang, min_ratio=4, max_ratio=4)["passed"] == 1
    assert checker.check(*files, lang, min_ratio=4.1, max_ratio=5)["failed"] == 1
    assert checker.check(*files, lang, min_ratio=0, max_ratio=3.9)["failed"] == 1


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_missing_and_extra_ids(tmp_path, lang):
    regression = write_jsonl(tmp_path / "regression.jsonl", [
        {"id": "first", "zh": "呼吸練習", "must_terms": []},
        {"id": "second", "zh": "呼吸練習", "must_terms": []},
    ])
    outputs = write_jsonl(tmp_path / "outputs.jsonl", [
        {"id": "extra", "out": "呼吸練習"},
        {"id": "second", "out": "呼吸練習"},
    ])
    summary = checker.check(regression, outputs, lang)
    assert (summary["total"], summary["passed"], summary["failed"], summary["pass_rate"]) == (2, 1, 1, 0.5)
    assert summary["extra_ids"] == ["extra"]
    assert summary["results"][0] == {
        "id": "first", "ratio": None, "failures": ["missing_id"], "passed": False,
    }
    outputs.write_text("", encoding="utf-8")
    summary = checker.check(regression, outputs, lang)
    assert summary["failure_counts"]["missing_id"] == 2
    assert summary["pass_rate"] == 0


@pytest.mark.parametrize("lower,upper", [(-1, 3), (4, 3), (float("nan"), 3), (0, float("inf"))])
def test_invalid_ratio_bounds(tmp_path, lower, upper):
    with pytest.raises(ValueError, match="ratio bounds"):
        checker.check(*fixture_files(tmp_path, "meditation"), "en", lower, upper)


@pytest.mark.parametrize("kind,records,message", [
    ("regression", [], "at least one"),
    ("regression", [{"id": "a", "zh": "", "must_terms": []}], "zh must not be empty"),
    ("regression", [{"id": "a", "zh": "呼吸", "must_terms": "呼吸"}], "must_terms"),
    ("regression", [{"id": "a", "zh": "呼吸", "must_terms": [""]}], "must_terms"),
    ("regression", [{"id": "a", "zh": "呼吸", "must_terms": [], "forbidden": [2]}], "forbidden"),
    ("outputs", [{"id": "sample", "out": None}], "out must be a string"),
    ("outputs", [{"id": "", "out": "text"}], "id must be"),
    ("outputs", [[]], "expected an object"),
])
def test_invalid_schema(tmp_path, kind, records, message):
    regression, outputs = fixture_files(tmp_path, "meditation")
    write_jsonl(regression if kind == "regression" else outputs, records)
    with pytest.raises(ValueError, match=message):
        checker.check(regression, outputs, "en")


@pytest.mark.parametrize("kind", ["regression", "outputs"])
def test_duplicate_ids_and_malformed_json(tmp_path, kind):
    regression, outputs = fixture_files(tmp_path, "meditation")
    path = regression if kind == "regression" else outputs
    path.write_text(path.read_text(encoding="utf-8") * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate id"):
        checker.check(regression, outputs, "en")
    path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":1: invalid JSON"):
        checker.check(regression, outputs, "en")


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_cli_threshold_and_reports(tmp_path, lang):
    regression = write_jsonl(tmp_path / "regression.jsonl", [
        {"id": str(index), "zh": "呼吸練習", "must_terms": ["breath" if lang == "en" else "呼吸"]}
        for index in range(10)
    ])
    outputs = write_jsonl(tmp_path / "outputs.jsonl", [
        {"id": str(index), "out": "BREATHE" if lang == "en" else "呼吸の練習"}
        for index in range(9)
    ])
    command = [sys.executable, str(SCRIPT), str(regression), str(outputs), "--lang", lang]
    result = subprocess.run(command + ["--fail-under", "0.9", "--json"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["pass_rate"] == 0.9
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0
    assert f"PASS {lang}: 9/10" in result.stdout
    assert "missing_id=1" in result.stdout
    assert "9: missing_id" in result.stdout
    result = subprocess.run(command + ["--fail-under", "0.91"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "FAIL" in result.stdout
    for args in (["--fail-under", "nan"], ["--fail-under", "1.1"], ["--min-ratio", "4", "--max-ratio", "3"]):
        result = subprocess.run(command + args, capture_output=True, text=True)
        assert result.returncode == 2
        assert "error:" in result.stderr


def test_cli_ratio_override_and_invalid_file(tmp_path):
    regression, outputs = fixture_files(tmp_path, "a" * 40, zh="呼" * 10)
    command = [sys.executable, str(SCRIPT), str(regression), str(outputs), "--lang", "ja"]
    assert subprocess.run(command, capture_output=True).returncode == 1
    result = subprocess.run(command + ["--max-ratio", "4"], capture_output=True, text=True)
    assert result.returncode == 0
    outputs.write_text("not JSON\n", encoding="utf-8")
    assert subprocess.run(command, capture_output=True).returncode == 2
    outputs.unlink()
    assert subprocess.run(command, capture_output=True).returncode == 2


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_committed_corpus_schema_and_fake_outputs(tmp_path, lang):
    path = ROOT / "tests" / "data" / f"mt_regression_{lang}.jsonl"
    records = checker._read_jsonl(path, "regression")
    assert len(path.read_text(encoding="utf-8").splitlines()) == len(records) == 50
    for record in records.values():
        assert set(record) == {"id", "zh", "must_terms", "forbidden", "notes"}
        assert record["must_terms"]
        assert record["notes"].strip()
    # Fake strings validate fixture/checker compatibility, not translation quality.
    outputs = write_jsonl(tmp_path / "outputs.jsonl", [
        {"id": record_id, "out": " ".join(record["must_terms"])}
        for record_id, record in records.items()
    ])
    summary = checker.check(path, outputs, lang, min_ratio=0, max_ratio=100)
    assert summary["passed"] == 50
