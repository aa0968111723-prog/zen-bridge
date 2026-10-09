import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts" / "device_check.py"
spec = importlib.util.spec_from_file_location("device_check", path)
assert spec is not None and spec.loader is not None
device_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device_check)


GOOD = """1
00:00:00,000 --> 00:00:06,000
甲

2
00:00:06,000 --> 00:00:12,500
乙
"""

OVERLAP = """1
00:00:00,000 --> 00:00:06,000
甲

2
00:00:05,000 --> 00:00:10,000
乙
"""

BAD_INDEX = """1
00:00:00,000 --> 00:00:01,000
甲

3
00:00:01,000 --> 00:00:02,000
乙
"""

ZERO = """1
00:00:01,000 --> 00:00:01,000
甲
"""

BAD_STAMP = """1
00:00:00.000 --> 00:00:01.000
甲
"""


def _marks(report):
    return {item["name"]: item["ok"] for item in report["checks"]}


def test_validate_srt_accepts_monotonic_cues():
    marks = _marks(device_check.validate_srt(GOOD))
    assert marks == {
        "timestamps": True,
        "indices": True,
        "end_after_start": True,
        "monotonic": True,
        "no_overlap": True,
    }


def test_validate_srt_flags_overlap_bad_index_and_zero_length():
    overlap = _marks(device_check.validate_srt(OVERLAP))
    assert overlap["no_overlap"] is False
    assert overlap["monotonic"] is True
    assert overlap["end_after_start"] is True
    assert _marks(device_check.validate_srt(BAD_INDEX))["indices"] is False
    assert _marks(device_check.validate_srt(ZERO))["end_after_start"] is False
    assert _marks(device_check.validate_srt(BAD_STAMP))["timestamps"] is False


def test_validate_rows_reports_duplicate_seq_and_gaps():
    rows = [
        {"session_id": "s", "seq": 1, "zh": "甲"},
        {"session_id": "s", "seq": 1, "zh": "甲"},
        {"session_id": "s", "seq": 3, "zh": "丙"},
        {"session_id": "t", "seq": 1, "zh": "丁"},
    ]
    marks = {item["name"]: item for item in device_check.validate_rows(rows)["checks"]}
    assert marks["seq_unique"]["ok"] is False
    assert "s:1" in marks["seq_unique"]["detail"]
    assert marks["gaps"]["ok"] is False
    assert "缺 2" in marks["gaps"]["detail"]
    clean = device_check.validate_rows([
        {"session_id": "s", "seq": 1},
        {"session_id": "s", "seq": 2},
    ])
    assert all(item["ok"] for item in clean["checks"])


def test_format_report_redacts_token():
    text = device_check.format_report(
        [{"name": "indices", "ok": False, "detail": "secret-token-value leaked"}],
        "secret-token-value",
    )
    assert "secret-token-value" not in text
    assert "[redacted]" in text
    assert text.startswith("FAIL")
    assert "整體 FAIL" in text
