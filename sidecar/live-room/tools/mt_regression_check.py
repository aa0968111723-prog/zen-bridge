#!/usr/bin/env python3
"""Check saved translations offline; this tool never calls a model or service.

Usage:
  python tools/mt_regression_check.py tests/data/mt_regression_en.jsonl outputs.jsonl --lang en
  python tools/mt_regression_check.py tests/data/mt_regression_ja.jsonl outputs.jsonl --lang ja --json

All must_terms are required substrings; forbidden terms are forbidden substrings.
English matching uses casefold. Ratios count Unicode characters, including spaces
and punctuation. Japanese residual detection is only an explicit heuristic, not a
complete Japanese-kanji validator, and can flag characters in legitimate quotes.
Exit codes: 0 meets --fail-under, 1 falls below it, 2 invalid input/configuration.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


DEFAULT_BOUNDS = {"en": (0.8, 6.0), "ja": (0.6, 3.0)}
TRADITIONAL_ONLY = frozenset("這們說會個來時為與對還讓嗎呢吧")
RULES = ("missing_id", "empty_output", "residual_chinese", "must_terms", "forbidden", "length_ratio")


def _read_jsonl(path: str | Path, kind: str) -> dict[str, dict]:
    records = {}
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            location = f"{path}:{line_number}"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{location}: invalid JSON") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{location}: expected an object")
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id.strip():
                raise ValueError(f"{location}: id must be a nonempty string")
            if record_id in records:
                raise ValueError(f"{location}: duplicate id {record_id!r}")
            field = "zh" if kind == "regression" else "out"
            if not isinstance(record.get(field), str):
                raise ValueError(f"{location}: {field} must be a string")
            if kind == "regression":
                if not record["zh"].strip():
                    raise ValueError(f"{location}: zh must not be empty")
                for field in ("must_terms", "forbidden"):
                    terms = record.get(field, [] if field == "forbidden" else None)
                    if not isinstance(terms, list) or any(
                        not isinstance(term, str) or not term.strip() for term in terms
                    ):
                        raise ValueError(f"{location}: {field} must be a list of nonempty strings")
            records[record_id] = record
    return records


def check(
    regression_path: str | Path,
    outputs_path: str | Path,
    lang: str,
    min_ratio: float | None = None,
    max_ratio: float | None = None,
) -> dict:
    """Return summary counts and per-record results, including all failed rules.

    Missing outputs fail and remain in the denominator. Unrecognized output IDs
    are reported but never increase the number of passing regression records.
    Invalid JSON, schemas, duplicate IDs, and empty regression sets raise ValueError.
    """
    if lang not in DEFAULT_BOUNDS:
        raise ValueError("lang must be en or ja")
    default_min, default_max = DEFAULT_BOUNDS[lang]
    lower = default_min if min_ratio is None else min_ratio
    upper = default_max if max_ratio is None else max_ratio
    if not math.isfinite(lower) or not math.isfinite(upper) or not 0 <= lower <= upper:
        raise ValueError("ratio bounds must be finite and satisfy 0 <= min <= max")
    regressions = _read_jsonl(regression_path, "regression")
    outputs = _read_jsonl(outputs_path, "outputs")
    if not regressions:
        raise ValueError("regression file must contain at least one record")

    counts = dict.fromkeys(RULES, 0)
    results = []
    for record_id, record in regressions.items():
        failures = []
        result = {"id": record_id, "ratio": None, "failures": failures}
        if record_id not in outputs:
            failures.append("missing_id")
        else:
            out = outputs[record_id]["out"]
            if not out.strip():
                failures.append("empty_output")
            residual = sorted({
                char for char in out
                if char in TRADITIONAL_ONLY or "\u3100" <= char <= "\u312f"
                or "\u31a0" <= char <= "\u31bf"
            }) if lang == "ja" else []
            if residual:
                failures.append("residual_chinese")
                result["residual_characters"] = residual
            text = out.casefold() if lang == "en" else out
            normalize = str.casefold if lang == "en" else str
            missing = [term for term in record["must_terms"] if normalize(term) not in text]
            forbidden = [term for term in record.get("forbidden", []) if normalize(term) in text]
            if missing:
                failures.append("must_terms")
                result["missing_terms"] = missing
            if forbidden:
                failures.append("forbidden")
                result["forbidden_terms"] = forbidden
            ratio = len(out) / len(record["zh"])
            result["ratio"] = ratio
            if not lower <= ratio <= upper:
                failures.append("length_ratio")
        for failure in failures:
            counts[failure] += 1
        result["passed"] = not failures
        results.append(result)

    passed = sum(result["passed"] for result in results)
    total = len(regressions)
    return {
        "lang": lang,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total,
        "min_ratio": lower,
        "max_ratio": upper,
        "failure_counts": counts,
        "extra_ids": sorted(outputs.keys() - regressions.keys()),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regression", type=Path, help="Regression JSONL with id, zh, must_terms")
    parser.add_argument("outputs", type=Path, help="Saved output JSONL with id, out")
    parser.add_argument("--lang", required=True, choices=DEFAULT_BOUNDS)
    parser.add_argument("--min-ratio", type=float)
    parser.add_argument("--max-ratio", type=float)
    parser.add_argument("--fail-under", type=float, default=0.9)
    parser.add_argument("--json", action="store_true", help="Print the summary dict as JSON")
    args = parser.parse_args(argv)
    if not math.isfinite(args.fail_under) or not 0 <= args.fail_under <= 1:
        parser.error("--fail-under must be finite and between 0 and 1")
    try:
        summary = check(args.regression, args.outputs, args.lang, args.min_ratio, args.max_ratio)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    failed = summary["pass_rate"] < args.fail_under
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(
            f"{'FAIL' if failed else 'PASS'} {args.lang}: "
            f"{summary['passed']}/{summary['total']} passed "
            f"({summary['pass_rate']:.1%}); fail-under={args.fail_under:.1%}"
        )
        print("Failed rules: " + ", ".join(f"{key}={value}" for key, value in summary["failure_counts"].items()))
        for result in summary["results"]:
            if not result["passed"]:
                print(f"  {result['id']}: {', '.join(result['failures'])}")
        if summary["extra_ids"]:
            print("Unrecognized output IDs: " + ", ".join(summary["extra_ids"]))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
