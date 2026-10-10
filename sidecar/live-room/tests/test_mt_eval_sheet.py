import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import mt_eval_sheet as evaluator


def _inputs(tmp_path):
    src = tmp_path / "sentences.tsv"
    model_a = tmp_path / "outputs_a.jsonl"
    model_b = tmp_path / "outputs_b.jsonl"
    rows = [{"id": str(index), "source_zh": f"來源句子 {index}"} for index in range(1, 21)]
    with src.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("id", "source_zh"), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    model_a.write_text(
        "".join(json.dumps({"id": row["id"], "translation": f"A-{row['id']}"}) + "\n" for row in rows),
        encoding="utf-8",
    )
    model_b.write_text(
        "".join(json.dumps({"id": row["id"], "translation": f"B-{row['id']}"}) + "\n" for row in rows),
        encoding="utf-8",
    )
    return src, model_a, model_b


def _make(tmp_path, seed=4):
    src, model_a, model_b = _inputs(tmp_path)
    out = tmp_path / "sheet.csv"
    key_path = evaluator.make_sheet(src, model_a, model_b, out, seed)
    return out, key_path


def _filled_sheet(tmp_path, name, key, *, preferred="1", score="5"):
    path = tmp_path / name
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=evaluator.SHEET_COLUMNS)
        writer.writeheader()
        for sentence_id in key["rows"]:
            row = {column: "" for column in evaluator.SHEET_COLUMNS}
            row.update({"id": sentence_id, "preferred": preferred})
            for position in evaluator.POSITION_COLUMNS:
                for dimension in evaluator.DIMENSIONS:
                    row[f"{position}_{dimension}"] = score
            writer.writerow(row)
    return path


def test_make_writes_utf8_bom_and_expected_columns(tmp_path):
    out, _ = _make(tmp_path)
    content = out.read_bytes()
    assert content.startswith(b"\xef\xbb\xbf")
    with out.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(evaluator.SHEET_COLUMNS)
        assert len(list(reader)) == 20


def test_make_blinds_model_names_and_separates_key(tmp_path):
    out, key_path = _make(tmp_path)
    text = out.read_text(encoding="utf-8-sig")
    assert "outputs_a" not in text
    assert "outputs_b" not in text
    assert key_path.name == "sheet.key.json"
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert key["models"] == {"a": "outputs_a", "b": "outputs_b"}


def test_make_randomizes_order_deterministically(tmp_path):
    first, key_path = _make(tmp_path, seed=22)
    first_order = json.loads(key_path.read_text(encoding="utf-8"))["rows"]
    second = tmp_path / "again.csv"
    second_key = evaluator.make_sheet(*_inputs(tmp_path), second, 22)
    second_order = json.loads(second_key.read_text(encoding="utf-8"))["rows"]
    assert first_order == second_order
    assert all(mapping["output_1"] != mapping["output_2"] for mapping in first_order.values())
    assert first.read_bytes() == second.read_bytes()


def test_make_rows_contain_a_and_b_once(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert all(set(row.values()) == {"a", "b"} for row in key["rows"].values())


def test_make_supports_explicit_key_and_model_names(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    out = tmp_path / "custom.csv"
    custom_key = tmp_path / "private" / "models.json"
    result = evaluator.make_sheet(
        src, model_a, model_b, out, 1, key_path=custom_key, a_name="Model A", b_name="Model B"
    )
    assert result == custom_key
    assert json.loads(result.read_text(encoding="utf-8"))["models"] == {"a": "Model A", "b": "Model B"}


def test_make_requires_exactly_twenty_source_rows(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    with src.open("r", encoding="utf-8") as handle:
        content = handle.readlines()[:-1]
    src.write_text("".join(content), encoding="utf-8")
    with pytest.raises(evaluator.EvaluationError, match="exactly 20"):
        evaluator.make_sheet(src, model_a, model_b, tmp_path / "sheet.csv", 1)


def test_make_rejects_mismatched_ids(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    model_b.write_text(model_b.read_text(encoding="utf-8").replace('"id": "20"', '"id": "21"'), encoding="utf-8")
    with pytest.raises(evaluator.EvaluationError, match="ids do not match"):
        evaluator.make_sheet(src, model_a, model_b, tmp_path / "sheet.csv", 1)


def test_make_rejects_duplicate_source_ids(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    data = src.read_text(encoding="utf-8").splitlines()
    data[-1] = data[-2]
    src.write_text("\n".join(data) + "\n", encoding="utf-8")
    with pytest.raises(evaluator.EvaluationError, match="duplicate id"):
        evaluator.make_sheet(src, model_a, model_b, tmp_path / "sheet.csv", 1)


def test_make_rejects_invalid_jsonl(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    model_a.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(evaluator.EvaluationError, match="invalid JSON"):
        evaluator.make_sheet(src, model_a, model_b, tmp_path / "sheet.csv", 1)


def test_sign_test_handles_no_non_tied_votes():
    assert evaluator.sign_test_p_value(0, 0) == 1.0


def test_sign_test_is_two_sided_exact():
    assert evaluator.sign_test_p_value(10, 0) == pytest.approx(2 / 1024)


def test_sign_test_is_symmetric():
    assert evaluator.sign_test_p_value(7, 3) == evaluator.sign_test_p_value(3, 7)


def test_score_maps_positions_back_to_models_and_calculates_means(tmp_path):
    out, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "filled.csv", key, preferred="1")
    with filled.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames
    for row in rows:
        for position in evaluator.POSITION_COLUMNS:
            score = "5" if key["rows"][row["id"]][position] == "a" else "1"
            row.update({f"{position}_{dimension}": score for dimension in evaluator.DIMENSIONS})
    with filled.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    report = evaluator.score_sheets([filled], key_path)
    assert report["means"]["a"]["fidelity"]["count"] == 20
    assert sorted(report["means"][model]["fidelity"]["mean"] for model in ("a", "b")) == [1.0, 5.0]
    assert sum(report["outcomes"][model]["wins"] for model in ("a", "b")) == 20
    assert out.exists()


def test_score_counts_ties_for_both_models(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "ties.csv", key, preferred="tie")
    report = evaluator.score_sheets([filled], key_path)
    assert report["outcomes"]["a"]["ties"] == 20
    assert report["outcomes"]["b"]["ties"] == 20
    assert report["sign_test_p_value"] == 1.0


def test_score_ignores_blank_scores_and_preferences(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "empty.csv", key, preferred="", score="")
    report = evaluator.score_sheets([filled], key_path)
    assert report["means"]["a"]["fluency"] == {"mean": None, "count": 0}
    assert report["outcomes"]["a"]["wins"] == 0


def test_score_rejects_non_integer_score(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "bad.csv", key)
    content = filled.read_text(encoding="utf-8-sig").replace(",5,5,5,", ",high,5,5,", 1)
    filled.write_text(content, encoding="utf-8-sig")
    with pytest.raises(evaluator.EvaluationError, match="must be an integer"):
        evaluator.score_sheets([filled], key_path)


def test_score_rejects_out_of_range_score(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "bad.csv", key, score="6")
    with pytest.raises(evaluator.EvaluationError, match="from 1 to 5"):
        evaluator.score_sheets([filled], key_path)


def test_score_rejects_invalid_preference(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "bad.csv", key, preferred="3")
    with pytest.raises(evaluator.EvaluationError, match="preferred must be"):
        evaluator.score_sheets([filled], key_path)


def test_score_rejects_sheet_id_missing_from_key(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "bad.csv", key)
    with filled.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames
        rows = list(reader)
    rows[0]["id"] = "unknown"
    with filled.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(evaluator.EvaluationError, match="absent from key"):
        evaluator.score_sheets([filled], key_path)


def test_multiple_raters_get_pairwise_preference_agreement(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    first = _filled_sheet(tmp_path, "rater1.csv", key, preferred="1")
    second = _filled_sheet(tmp_path, "rater2.csv", key, preferred="1")
    report = evaluator.score_sheets([first, second], key_path)
    assert report["agreement"][0]["compared"] == 20
    assert report["agreement"][0]["agreement"] == 1.0
    assert "100.0%" in evaluator.render_markdown(report)


def test_multiple_raters_disagreement_is_reported(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    first = _filled_sheet(tmp_path, "rater1.csv", key, preferred="1")
    second = _filled_sheet(tmp_path, "rater2.csv", key, preferred="2")
    report = evaluator.score_sheets([first, second], key_path)
    assert report["agreement"][0]["agreement"] == 0.0


def test_markdown_includes_mean_score_and_sign_test(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "filled.csv", key)
    rendered = evaluator.render_markdown(evaluator.score_sheets([filled], key_path))
    assert "忠實平均分" in rendered
    assert "sign-test p 值" in rendered
    assert "評分者一致性" in rendered


def test_cli_make_and_score_smoke(tmp_path):
    src, model_a, model_b = _inputs(tmp_path)
    out = tmp_path / "sheet.csv"
    script = Path(evaluator.__file__)
    subprocess.run(
        [
            sys.executable, str(script), "make", "--src", str(src), "--a", str(model_a),
            "--b", str(model_b), "--out", str(out), "--seed", "8",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    key_path = tmp_path / "sheet.key.json"
    key = json.loads(key_path.read_text(encoding="utf-8"))
    filled = _filled_sheet(tmp_path, "filled.csv", key)
    result = subprocess.run(
        [sys.executable, str(script), "score", "--sheet", str(filled), "--key", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "MT 評估摘要" in result.stdout


def test_cli_score_accepts_repeated_sheet_options(tmp_path):
    _, key_path = _make(tmp_path)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    first = _filled_sheet(tmp_path, "rater1.csv", key)
    second = _filled_sheet(tmp_path, "rater2.csv", key)
    result = evaluator.main(["score", "--sheet", str(first), "--sheet", str(second), "--key", str(key_path)])
    assert result == 0
