#!/usr/bin/env python3
"""Create and score blind A/B machine-translation evaluation sheets."""

import argparse
import csv
import itertools
import json
import math
import random
import sys
from pathlib import Path


DIMENSIONS = ("fidelity", "fluency", "terminology")
POSITION_COLUMNS = ("output_1", "output_2")
SHEET_COLUMNS = (
    "id",
    "source_zh",
    "output_1",
    "output_2",
    "output_1_fidelity",
    "output_1_fluency",
    "output_1_terminology",
    "output_2_fidelity",
    "output_2_fluency",
    "output_2_terminology",
    "preferred",
    "comment",
)
ID_COLUMNS = ("id", "sentence_id", "identifier")
SOURCE_COLUMNS = ("source_zh", "source", "chinese", "zh")
TRANSLATION_COLUMNS = ("output", "translation", "translated_text", "text", "target")


class EvaluationError(ValueError):
    """Raised when an input file does not match the evaluation format."""


def _first_value(row, names, description):
    for name in names:
        if name in row:
            value = str(row[name] or "").strip()
            if value:
                return value
    raise EvaluationError(f"missing {description}")


def _read_sources(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames:
            raise EvaluationError(f"{path}: expected a TSV header")
        rows = []
        seen = set()
        for line_number, row in enumerate(reader, start=2):
            sentence_id = _first_value(row, ID_COLUMNS, f"id at line {line_number}")
            source = _first_value(row, SOURCE_COLUMNS, f"source_zh at line {line_number}")
            if sentence_id in seen:
                raise EvaluationError(f"{path}: duplicate id {sentence_id!r}")
            seen.add(sentence_id)
            rows.append({"id": sentence_id, "source_zh": source})
    if len(rows) != 20:
        raise EvaluationError(f"{path}: expected exactly 20 sentences, found {len(rows)}")
    return rows


def _read_outputs(path):
    outputs = {}
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise EvaluationError(f"{path}:{line_number}: invalid JSON: {error.msg}") from error
            if not isinstance(value, dict):
                raise EvaluationError(f"{path}:{line_number}: expected a JSON object")
            sentence_id = _first_value(value, ID_COLUMNS, f"id at line {line_number}")
            output = _first_value(value, TRANSLATION_COLUMNS, f"translation at line {line_number}")
            if sentence_id in outputs:
                raise EvaluationError(f"{path}: duplicate id {sentence_id!r}")
            outputs[sentence_id] = output
    return outputs


def _default_key_path(out_path):
    path = Path(out_path)
    return path.with_name(f"{path.stem}.key.json")


def make_sheet(src, a, b, out, seed, key_path=None, a_name=None, b_name=None):
    """Build the blinded sheet and its separate model-position key."""
    sources = _read_sources(src)
    output_a = _read_outputs(a)
    output_b = _read_outputs(b)
    source_ids = {row["id"] for row in sources}
    for path, outputs in ((a, output_a), (b, output_b)):
        if set(outputs) != source_ids:
            missing = sorted(source_ids - set(outputs))
            extra = sorted(set(outputs) - source_ids)
            raise EvaluationError(f"{path}: ids do not match source (missing={missing}, extra={extra})")

    names = {
        "a": a_name or Path(a).stem,
        "b": b_name or Path(b).stem,
    }
    if not names["a"] or not names["b"] or names["a"] == names["b"]:
        raise EvaluationError("model names must be non-empty and distinct")

    rng = random.Random(seed)
    key_rows = {}
    sheet_rows = []
    for source in sources:
        first_is_a = rng.choice((True, False))
        position_models = {
            "output_1": "a" if first_is_a else "b",
            "output_2": "b" if first_is_a else "a",
        }
        outputs = {"a": output_a[source["id"]], "b": output_b[source["id"]]}
        row = {
            **source,
            "output_1": outputs[position_models["output_1"]],
            "output_2": outputs[position_models["output_2"]],
            "preferred": "",
            "comment": "",
        }
        for position in POSITION_COLUMNS:
            for dimension in DIMENSIONS:
                row[f"{position}_{dimension}"] = ""
        sheet_rows.append(row)
        key_rows[source["id"]] = position_models

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sheet_rows)

    actual_key_path = Path(key_path) if key_path else _default_key_path(out_path)
    actual_key_path.parent.mkdir(parents=True, exist_ok=True)
    key = {"models": names, "rows": key_rows}
    actual_key_path.write_text(
        json.dumps(key, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return actual_key_path


def sign_test_p_value(wins, losses):
    """Return a two-sided exact binomial sign-test p-value, excluding ties."""
    trials = wins + losses
    if trials == 0:
        return 1.0
    tail = min(wins, losses)
    probability = sum(math.comb(trials, index) for index in range(tail + 1)) / (2**trials)
    return min(1.0, 2 * probability)


def _load_key(path):
    try:
        key = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError(f"{path}: cannot read key JSON: {error}") from error
    if not isinstance(key, dict) or not isinstance(key.get("models"), dict) or not isinstance(
        key.get("rows"), dict
    ):
        raise EvaluationError(f"{path}: key must contain 'models' and 'rows' objects")
    models = key["models"]
    if set(models) != {"a", "b"} or not all(str(models[name]).strip() for name in ("a", "b")):
        raise EvaluationError(f"{path}: 'models' must map distinct a and b names")
    if models["a"] == models["b"]:
        raise EvaluationError(f"{path}: model names must be distinct")
    for sentence_id, mapping in key["rows"].items():
        if not isinstance(mapping, dict) or set(mapping) != set(POSITION_COLUMNS):
            raise EvaluationError(f"{path}: invalid position mapping for id {sentence_id!r}")
        if set(mapping.values()) != {"a", "b"}:
            raise EvaluationError(f"{path}: positions for id {sentence_id!r} must map a and b once")
    return key


def _read_filled_sheet(path, key):
    ratings = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"id", "preferred"} | {
            f"{position}_{dimension}"
            for position in POSITION_COLUMNS
            for dimension in DIMENSIONS
        }
        missing_columns = sorted(required - set(reader.fieldnames or ()))
        if missing_columns:
            raise EvaluationError(f"{path}: missing columns: {', '.join(missing_columns)}")
        for line_number, row in enumerate(reader, start=2):
            sentence_id = (row.get("id") or "").strip()
            if not sentence_id:
                raise EvaluationError(f"{path}: missing id at line {line_number}")
            if sentence_id not in key["rows"]:
                raise EvaluationError(f"{path}: id {sentence_id!r} is absent from key")
            if sentence_id in ratings:
                raise EvaluationError(f"{path}: duplicate id {sentence_id!r}")
            preferred = (row.get("preferred") or "").strip().lower()
            if preferred not in ("", "1", "2", "tie"):
                raise EvaluationError(f"{path}: preferred must be 1, 2, or tie at line {line_number}")
            score_values = {}
            for position in POSITION_COLUMNS:
                for dimension in DIMENSIONS:
                    column = f"{position}_{dimension}"
                    raw_score = (row.get(column) or "").strip()
                    if not raw_score:
                        continue
                    try:
                        score = int(raw_score)
                    except ValueError as error:
                        raise EvaluationError(f"{path}: {column} must be an integer at line {line_number}") from error
                    if not 1 <= score <= 5:
                        raise EvaluationError(f"{path}: {column} must be from 1 to 5 at line {line_number}")
                    model = key["rows"][sentence_id][position]
                    score_values.setdefault(model, {})[dimension] = score
            ratings[sentence_id] = {"preferred": preferred, "scores": score_values}
    return ratings


def score_sheets(sheet_paths, key_path):
    """Calculate score means, preference outcomes, sign test, and rater agreement."""
    key = _load_key(key_path)
    model_names = key["models"]
    sheets = [(str(path), _read_filled_sheet(path, key)) for path in sheet_paths]
    if not sheets:
        raise EvaluationError("at least one filled sheet is required")

    totals = {model: {dimension: [0, 0] for dimension in DIMENSIONS} for model in ("a", "b")}
    outcomes = {model: {"wins": 0, "ties": 0, "losses": 0} for model in ("a", "b")}
    all_preferences = []
    for rater, ratings in sheets:
        preferences = {}
        for sentence_id, rating in ratings.items():
            preferred = rating["preferred"]
            preferences[sentence_id] = preferred
            for model in ("a", "b"):
                for dimension, value in rating["scores"].get(model, {}).items():
                    totals[model][dimension][0] += value
                    totals[model][dimension][1] += 1
            if preferred:
                if preferred == "tie":
                    outcomes["a"]["ties"] += 1
                    outcomes["b"]["ties"] += 1
                else:
                    chosen_model = key["rows"][sentence_id][f"output_{preferred}"]
                    other_model = "b" if chosen_model == "a" else "a"
                    outcomes[chosen_model]["wins"] += 1
                    outcomes[other_model]["losses"] += 1
        all_preferences.append((rater, preferences))

    agreement = []
    for (name_a, choices_a), (name_b, choices_b) in itertools.combinations(all_preferences, 2):
        shared = set(choices_a) & set(choices_b)
        comparable = [sentence_id for sentence_id in shared if choices_a[sentence_id] and choices_b[sentence_id]]
        matched = sum(choices_a[sentence_id] == choices_b[sentence_id] for sentence_id in comparable)
        agreement.append(
            {
                "raters": (name_a, name_b),
                "compared": len(comparable),
                "agreement": matched / len(comparable) if comparable else None,
            }
        )

    a_outcomes = outcomes["a"]
    p_value = sign_test_p_value(a_outcomes["wins"], a_outcomes["losses"])
    return {
        "models": model_names,
        "means": {
            model: {
                dimension: {
                    "mean": (total / count if count else None),
                    "count": count,
                }
                for dimension, (total, count) in dimensions.items()
            }
            for model, dimensions in totals.items()
        },
        "outcomes": outcomes,
        "sign_test_p_value": p_value,
        "agreement": agreement,
    }


def render_markdown(report):
    """Format a score report as a Markdown summary."""
    lines = [
        "# MT 評估摘要",
        "",
        "| 模型 | 忠實平均分 | 流暢平均分 | 術語平均分 | 勝 | 平手 | 負 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model in ("a", "b"):
        means = report["means"][model]
        outcomes = report["outcomes"][model]
        formatted = [
            "—" if means[dimension]["mean"] is None else f'{means[dimension]["mean"]:.2f}'
            for dimension in DIMENSIONS
        ]
        lines.append(
            f"| {report['models'][model]} | {' | '.join(formatted)} | "
            f"{outcomes['wins']} | {outcomes['ties']} | {outcomes['losses']} |"
        )
    lines.extend(
        [
            "",
            f"雙尾 sign-test p 值（排除平手）：{report['sign_test_p_value']:.6g}",
            "",
            "## 評分者一致性",
            "",
        ]
    )
    if not report["agreement"]:
        lines.append("只有一份評分表，無法計算評分者間一致性。")
    else:
        lines.extend(
            [
                "| 評分者 | 比較題數 | 偏好欄一致率 |",
                "|---|---:|---:|",
            ]
        )
        for pair in report["agreement"]:
            raters = " / ".join(pair["raters"])
            agreement = "無可比較題目" if pair["agreement"] is None else f"{pair['agreement']:.1%}"
            lines.append(f"| {raters} | {pair['compared']} | {agreement} |")
    return "\n".join(lines) + "\n"


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    make = subparsers.add_parser("make", help="create a blinded 20-sentence evaluation sheet")
    make.add_argument("--src", required=True, help="source TSV with id and source_zh columns")
    make.add_argument("--a", required=True, help="model A JSONL output")
    make.add_argument("--b", required=True, help="model B JSONL output")
    make.add_argument("--out", required=True, help="output CSV path")
    make.add_argument("--seed", required=True, type=int, help="seed for per-row position randomization")
    make.add_argument("--key", help="key JSON path (default: <out stem>.key.json)")
    make.add_argument("--a-name", help="private display name for model A (default: input filename stem)")
    make.add_argument("--b-name", help="private display name for model B (default: input filename stem)")
    score = subparsers.add_parser("score", help="summarize one or more completed sheets")
    score.add_argument("--sheet", action="append", nargs="+", required=True, help="filled CSV; repeat for raters")
    score.add_argument("--key", required=True, help="key JSON produced by make")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "make":
            key_path = make_sheet(
                args.src,
                args.a,
                args.b,
                args.out,
                args.seed,
                key_path=args.key,
                a_name=args.a_name,
                b_name=args.b_name,
            )
            print(f"Evaluation sheet: {args.out}\nPrivate key: {key_path}")
        else:
            sheet_paths = [path for group in args.sheet for path in group]
            report = score_sheets(sheet_paths, args.key)
            sys.stdout.write(render_markdown(report))
    except (EvaluationError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    # UTF-8 also works when invoked by the bundled Windows interpreter, whose
    # redirected output otherwise defaults to an ANSI code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
