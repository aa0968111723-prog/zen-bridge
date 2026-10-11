import ast
import csv
import json
from pathlib import Path

import pytest

from tools import mt_model_bench_plan as planner


def test_plan_covers_targets_backends_quantizations_and_threads():
    rows = planner.build_plan({"vulkan_devices": []})

    assert len(rows) == 2 * 2 * 3 * 3
    assert {row["target_lang"] for row in rows} == {"en", "ja"}
    assert {row["backend"] for row in rows} == {"llama.cpp", "Ollama"}
    assert {row["quant"] for row in rows} == {"Q4_K_M", "Q5_K_M", "Q8_0"}
    assert {row["threads"] for row in rows} == {1, 2, 4}
    assert {row["gpu_layers"] for row in rows} == {0}
    assert all(list(row) == list(planner.CSV_COLUMNS) for row in rows)
    assert all(
        row["notes"] == "Try Q8_0 first for ja."
        for row in rows
        if row["target_lang"] == "ja" and row["quant"] == "Q8_0"
    )


def test_vulkan_rows_are_only_added_when_probe_reports_a_device():
    rows = planner.build_plan({"vulkan_devices": [" AMD Radeon "]})

    assert {row["gpu_layers"] for row in rows} == {0, -1}
    assert len(rows) == 2 * 2 * 3 * 3 * 2
    assert {row["gpu_layers"] for row in planner.build_plan({"vulkan_devices": [""]})} == {0}


def test_over_budget_rows_are_kept_and_marked():
    rows = planner.build_plan({}, ram_budget_gb=3.0)

    q8_rows = [row for row in rows if row["quant"] == "Q8_0"]
    q4_rows = [row for row in rows if row["quant"] == "Q4_K_M"]
    assert len(q8_rows) == 2 * 2 * 3
    assert all(row["skip_reason"] for row in q8_rows)
    assert all(not row["skip_reason"] for row in q4_rows)


def test_custom_quant_ram_estimates_are_used():
    rows = planner.build_plan(
        {},
        ram_budget_gb=2.0,
        models={"quant_ram_gb": {"Q4_K_M": 1.75}},
    )
    q4 = [row for row in rows if row["quant"] == "Q4_K_M"]
    q5 = [row for row in rows if row["quant"] == "Q5_K_M"]

    assert {row["est_ram_gb"] for row in q4} == {"1.75"}
    assert all(not row["skip_reason"] for row in q4)
    assert all(row["skip_reason"] for row in q5)


def test_cli_writes_csv_and_refuses_to_overwrite(tmp_path):
    hardware_path = tmp_path / "hw.json"
    output_path = tmp_path / "plan.csv"
    hardware_path.write_text(json.dumps({"vulkan_devices": ["GPU"]}), encoding="utf-8")

    assert planner.main(["--hw", str(hardware_path), "--out", str(output_path)]) == 0
    with output_path.open(encoding="utf-8", newline="") as output_file:
        rows = list(csv.DictReader(output_file))
    assert len(rows) == 2 * 2 * 3 * 3 * 2
    before = output_path.read_bytes()

    with pytest.raises(SystemExit) as error:
        planner.main(["--hw", str(hardware_path), "--out", str(output_path)])
    assert error.value.code == 2
    assert output_path.read_bytes() == before


def test_planner_has_no_process_or_network_imports():
    source = Path(planner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported.intersection({"subprocess", "socket", "urllib"})
