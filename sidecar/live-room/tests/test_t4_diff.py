import json
from pathlib import Path

from tools import t4_diff


DATA = Path(__file__).resolve().parent / "data" / "t4_diff"
BASELINE = DATA / "baseline.json"


def run_diff(new_file, capsys, *options):
    return t4_diff.main([str(BASELINE), str(DATA / new_file), *options])


def test_regression_and_json_report(capsys, tmp_path):
    output = tmp_path / "comparison.json"
    assert run_diff("regression.json", capsys, "--json", str(output)) == 1
    report = capsys.readouterr().out
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert "REGRESSION" in report
    assert saved["regressions"] == 2
    draft = next(row for row in saved["comparisons"] if row["layer"] == "draft")
    assert draft["metrics"]["rtf_p50"] == {
        "base": 0.5, "new": 0.6, "delta": 0.1,
        "delta_pct": 20.0, "status": "REGRESSION",
    }


def test_improvement_uses_configuration(capsys):
    assert run_diff("improvement.json", capsys) == 0
    output = capsys.readouterr().out
    assert "IMPROVED" in output
    assert "draft-int8-t1" in output
    assert "Added configurations" not in output
    assert "Removed configurations" not in output


def test_unchanged_and_added_removed_configurations(capsys):
    assert run_diff("unchanged.json", capsys) == 0
    assert "UNCHANGED" in capsys.readouterr().out

    assert run_diff("missing_config.json", capsys) == 0
    output = capsys.readouterr().out
    assert "Added configurations:" in output
    assert "Removed configurations:" in output
    assert "mt-q8-en" in output
    assert "mt-q4-en" in output


def test_gate_fail_increase_is_always_regression(capsys):
    assert run_diff("gate_fail_increase.json", capsys) == 1
    output = capsys.readouterr().out
    assert "gate_fail: base=0 new=1" in output
    assert "REGRESSION" in output


def test_threshold_override(capsys):
    assert run_diff("regression.json", capsys, "--threshold-pct", "20") == 0
    assert "UNCHANGED" in capsys.readouterr().out
    assert run_diff("regression.json", capsys, "--threshold-pct", "14") == 1
    assert "REGRESSION" in capsys.readouterr().out


def test_malformed_input_returns_two_with_clear_error(capsys):
    assert run_diff("malformed.json", capsys) == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_json_output_refuses_to_overwrite(capsys, tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("keep me", encoding="utf-8")
    assert run_diff("unchanged.json", capsys, "--json", str(output)) == 2
    assert output.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().err


def test_accepts_t4_jsonl_results(tmp_path):
    baseline = tmp_path / "baseline.json"
    new = tmp_path / "new.json"
    baseline.write_text(
        '{"layer":"asr","id":"asr-a","rtf_p50":0.5,"rtf_p95":0.8}\n',
        encoding="utf-8",
    )
    new.write_text(
        '{"layer":"asr","id":"asr-a","rtf_p50":0.5,"rtf_p95":0.8}\n',
        encoding="utf-8",
    )
    assert t4_diff.main([str(baseline), str(new)]) == 0


def test_zero_baseline_increase_is_a_regression():
    result = t4_diff._metric_result("rtf_p95", 0, 0.01, 10)
    assert result["delta_pct"] is None
    assert result["status"] == "REGRESSION"


def test_real_t4_rows_match_distinct_ids_and_same_thread_counts():
    rows = t4_diff._parse_rows(DATA / "real_t4_rows.json")
    result = t4_diff.compare(rows, rows)
    assert len(result["comparisons"]) == 7
    assert not result["added"] and not result["removed"]
    drafts = [row for row in result["comparisons"] if row["layer"] == "draft"]
    assert {row["id"] for row in drafts} == {"draft-int8-t1", "draft-fp32-t1"}

    baseline_with_future_metric = [dict(row) for row in rows]
    changed = [dict(row) for row in rows]
    for collection in (baseline_with_future_metric, changed):
        for row in collection:
            if row["layer"] == "draft":
                row["future_stage_ms"] = 1.0
    next(row for row in changed if row["id"] == "draft-int8-t1")["future_stage_ms"] = 2.0
    comparison = t4_diff.compare(baseline_with_future_metric, changed)
    draft = next(
        row for row in comparison["comparisons"]
        if row["layer"] == "draft" and row["id"] == "draft-int8-t1"
    )
    assert draft["metrics"]["future_stage_ms"]["status"] == "REGRESSION"
    assert not comparison["added"] and not comparison["removed"]
