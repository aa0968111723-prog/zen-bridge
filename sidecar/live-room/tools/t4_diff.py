"""Compare saved T4 benchmark results and flag performance regressions."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

METRICS = {
    "asr": ("rtf_p50", "rtf_p95"),
    "draft": ("rtf_p50", "rtf_p95", "first_partial_p50"),
    "mt": ("mt_wall_p50", "mt_wall_p95"),
    "concurrent": ("a3_p95_s", "a5_p95_s", "a6_p95_s", "b2_rtf_p95"),
}
METADATA = {
    "layer", "id", "n", "fallbacks", "cer", "gate_fail", "deterministic",
    "deterministic_clips", "on_ac", "ev37", "power_plan", "ts", "warning",
    "a3_ok",
}
ALL_METRICS = {name for names in METRICS.values() for name in names} | {
    "rtf_p50", "rtf_p95", "first_partial_p50", "mt_wall_p50", "mt_wall_p95",
    "a3_p95_s", "a5_p95_s", "a6_p95_s", "b2_rtf_p95", "gate_fail",
}
ROW_MEASUREMENTS = ALL_METRICS | {"encode_ms_p50", "decode_ms_p50", "gen_tok_s_p50"}


class InputError(ValueError):
    pass


def _parse_rows(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise InputError(f"{path}: cannot read result file: {exc}") from exc
    if not text.strip():
        raise InputError(f"{path}: result file is empty")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InputError(
                    f"{path}:{line_number}: invalid JSON at column {exc.colno}: {exc.msg}"
                ) from exc
            rows.append(row)
    else:
        rows = data if isinstance(data, list) else [data]

    if not rows:
        raise InputError(f"{path}: result file contains no rows")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise InputError(f"{path}: row {index} must be a JSON object")
        if not isinstance(row.get("layer"), str) or not row["layer"]:
            raise InputError(f"{path}: row {index} must have a non-empty layer")
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise InputError(f"{path}: row {index} must have a non-empty id")
        for name in ALL_METRICS:
            value = row.get(name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise InputError(f"{path}: row {index} metric {name} must be a finite number or null")
            if name != "gate_fail" and value is not None and value < 0:
                raise InputError(f"{path}: row {index} metric {name} must not be negative")
        if row.get("gate_fail") is not None and (
            not isinstance(row["gate_fail"], int) or isinstance(row["gate_fail"], bool)
            or row["gate_fail"] < 0
        ):
            raise InputError(f"{path}: row {index} gate_fail must be a non-negative integer")
    return rows


def _configuration(row: dict) -> dict:
    configuration = {
        name: value for name, value in row.items()
        if name not in METADATA and name not in ROW_MEASUREMENTS
    }
    if not configuration:
        configuration = {"id": row["id"]}
    return configuration


def _row_key(row: dict) -> tuple[str, str]:
    configuration = _configuration(row)
    encoded = json.dumps(configuration, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return row["layer"], encoded


def _pct_delta(base: float, new: float) -> float | None:
    if base == 0:
        return 0.0 if new == 0 else None
    return (new - base) / base * 100


def _metric_result(name: str, base, new, threshold_pct: float) -> dict:
    if base is None or new is None:
        return {"base": base, "new": new, "delta": None, "delta_pct": None, "status": "UNAVAILABLE"}
    delta = new - base
    pct = _pct_delta(base, new)
    if name == "gate_fail":
        status = "REGRESSION" if delta > 0 else "IMPROVED" if delta < 0 else "UNCHANGED"
    elif base == 0:
        status = "REGRESSION" if new > 0 else "UNCHANGED"
    elif pct > threshold_pct:
        status = "REGRESSION"
    elif pct < -threshold_pct:
        status = "IMPROVED"
    else:
        status = "UNCHANGED"
    return {
        "base": base,
        "new": new,
        "delta": round(delta, 6),
        "delta_pct": round(pct, 4) if pct is not None else None,
        "status": status,
    }


def compare(base_rows: list[dict], new_rows: list[dict], threshold_pct: float = 10) -> dict:
    def indexed(rows, label):
        result = {}
        for row in rows:
            key = _row_key(row)
            if key in result:
                raise InputError(f"{label}: duplicate configuration {row['layer']} / {row['id']}")
            result[key] = row
        return result

    baseline = indexed(base_rows, "baseline")
    candidate = indexed(new_rows, "new result")
    common = sorted(baseline.keys() & candidate.keys())
    added_keys = sorted(candidate.keys() - baseline.keys())
    removed_keys = sorted(baseline.keys() - candidate.keys())
    comparisons = []
    for key in common:
        old, new = baseline[key], candidate[key]
        metric_names = list(METRICS.get(old["layer"], ()))
        if old["layer"] not in METRICS:
            metric_names = sorted(ALL_METRICS - {"gate_fail"})
        if "gate_fail" in old or "gate_fail" in new:
            metric_names.append("gate_fail")
        metrics = {
            name: _metric_result(name, old.get(name), new.get(name), threshold_pct)
            for name in metric_names
            if name in old or name in new
        }
        comparisons.append({
            "layer": old["layer"],
            "id": new["id"],
            "configuration": _configuration(new),
            "metrics": metrics,
        })
    return {
        "threshold_pct": threshold_pct,
        "added": [
            {"layer": candidate[k]["layer"], "id": candidate[k]["id"], "configuration": _configuration(candidate[k])}
            for k in added_keys
        ],
        "removed": [
            {"layer": baseline[k]["layer"], "id": baseline[k]["id"], "configuration": _configuration(baseline[k])}
            for k in removed_keys
        ],
        "comparisons": comparisons,
        "regressions": sum(
            metric["status"] == "REGRESSION"
            for comparison in comparisons for metric in comparison["metrics"].values()
        ),
    }


def _format_value(value) -> str:
    return "—" if value is None else f"{value:.6g}" if isinstance(value, float) else str(value)


def _report(result: dict) -> str:
    lines = []
    for comparison in result["comparisons"]:
        config = comparison["configuration"]
        lines.append(f"{comparison['layer']} {comparison['id']} [{json.dumps(config, sort_keys=True, ensure_ascii=False)}]")
        for name, metric in comparison["metrics"].items():
            delta = _format_value(metric["delta"])
            percent = "—" if metric["delta_pct"] is None else f"{metric['delta_pct']:+.2f}%"
            lines.append(
                f"  {name}: base={_format_value(metric['base'])} new={_format_value(metric['new'])} "
                f"delta={delta} delta%={percent} {metric['status']}"
            )
    for label in ("added", "removed"):
        if result[label]:
            lines.append(f"{label.capitalize()} configurations:")
            lines.extend(f"  {row['layer']} {row['id']}" for row in result[label])
    lines.append(f"Regressions: {result['regressions']}")
    return "\n".join(lines)


def _threshold(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("threshold must be a finite non-negative number")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--json", dest="json_out", type=Path, help="write comparison JSON to a new file")
    parser.add_argument("--threshold-pct", type=_threshold, default=10)
    args = parser.parse_args(argv)
    try:
        result = compare(_parse_rows(args.baseline), _parse_rows(args.new), args.threshold_pct)
        if args.json_out is not None:
            try:
                output = args.json_out.open("x", encoding="utf-8", newline="\n")
            except FileExistsError as exc:
                raise InputError(f"JSON output already exists (refusing to overwrite): {args.json_out}") from exc
            with output:
                json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
    except (InputError, OSError, TypeError, ValueError) as exc:
        print(f"t4_diff: {exc}", file=sys.stderr)
        return 2
    print(_report(result))
    return 1 if result["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
