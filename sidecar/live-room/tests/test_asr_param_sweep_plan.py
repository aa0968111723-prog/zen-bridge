import ast
import csv
import json
from pathlib import Path

from tools import asr_param_sweep_plan as planner


def _write_hardware(path, hardware):
    path.write_text(json.dumps(hardware), encoding="utf-8")
    return path


def _run_cli(tmp_path, hardware, *options, capsys):
    hw_path = _write_hardware(tmp_path / "hw.json", hardware)
    out_path = tmp_path / "plan.csv"
    assert planner.main(["--hw", str(hw_path), "--out", str(out_path), *options]) == 0
    summary = json.loads(capsys.readouterr().out)
    with out_path.open(encoding="utf-8", newline="") as output_file:
        rows = list(csv.DictReader(output_file))
    return rows, summary


def test_csv_columns_and_summary(tmp_path, capsys):
    rows, summary = _run_cli(
        tmp_path,
        {"physical_cores": 6, "logical_cores": 12, "vulkan_devices": ["Radeon"]},
        capsys=capsys,
    )

    assert list(rows[0]) == list(planner.CSV_COLUMNS)
    assert summary["count"] == len(rows)
    assert summary["ranges"]["threads"] == {"min": 1, "max": 10}
    assert rows[0]["run_id"] == "run-001"


def test_thread_counts_leave_a_core_free_on_both_targets():
    for physical, logical, expected_max in ((6, 12, 10), (4, 8, 6)):
        rows = planner.build_plan(
            {"physical_cores": physical, "logical_cores": logical}
        )
        counts = {row["threads"] for row in rows}
        assert counts and max(counts) == expected_max
        assert all(1 <= count <= logical for count in counts)


def test_vulkan_flag_only_when_a_device_is_reported():
    without_device = planner.build_plan({"vulkan_devices": []})
    with_device = planner.build_plan({"vulkan_devices": ["AMD Radeon"]})

    assert {row["use_vulkan"] for row in without_device} == {"false"}
    assert {row["use_vulkan"] for row in with_device} == {"true"}


def test_japanese_prefers_q8():
    rows = planner.build_plan({}, lang="ja")

    assert rows[0]["mt_quant"] == "Q8"
    assert {row["mt_quant"] for row in rows} == {"Q4", "Q8"}


def test_max_runs_keeps_deterministic_grid_prefix():
    full_plan = planner.build_plan({})
    limited_plan = planner.build_plan({}, max_runs=7)

    assert limited_plan == full_plan[:7]
    assert planner.build_plan({}, max_runs=0) == []


def test_cli_max_runs_keeps_first_rows(tmp_path, capsys):
    rows, summary = _run_cli(tmp_path, {}, "--max-runs", "7", capsys=capsys)

    assert len(rows) == 7
    assert summary["count"] == 7
    assert [row["run_id"] for row in rows] == [f"run-{i:03d}" for i in range(1, 8)]


def test_missing_hardware_keys_use_safe_defaults():
    rows = planner.build_plan({})

    assert rows
    assert max(row["threads"] for row in rows) == 10
    assert {row["use_vulkan"] for row in rows} == {"false"}


def test_cli_output_is_deterministic(tmp_path, capsys):
    hardware = {"physical_cores": 4, "logical_cores": 8, "vulkan_devices": ["GPU"]}
    first_rows, first_summary = _run_cli(tmp_path, hardware, capsys=capsys)
    second_rows, second_summary = _run_cli(tmp_path, hardware, capsys=capsys)

    assert second_rows == first_rows
    assert second_summary == first_summary


def test_planner_does_not_import_subprocess_or_socket():
    source = Path(planner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported.intersection({"subprocess", "socket"})
