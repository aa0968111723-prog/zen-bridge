from tools.bench_pipeline import classify_translate_failures


def test_skipped_backlog_is_skipped_not_other():
    """Drop-oldest lines belong in skipped, and are printed, without being counted twice."""
    rows = [
        {"en": "EN", "translate_status": "ok", "status": "ready"},
        {"en": "", "translate_status": "skipped_backlog", "status": "translate_failed"},
        {"en": "", "translate_status": "skipped_backlog", "status": "translate_failed"},
        {"en": "", "translate_status": "timeout", "status": "translate_failed"},
        {"en": "", "translate_status": "skipped", "status": "translate_failed"},
        {"en": "", "translate_status": "weird", "status": "translate_failed"},
    ]
    got = classify_translate_failures(rows)
    assert got["skipped_backlog"] == 2
    assert got["skipped"] == 3
    assert got["timeout"] == 1
    assert got["queue_full"] == 0
    assert got["error"] == 0
    assert got["other"] == 1
    assert got["total"] == 5
    assert got["rate"] == 5 / 6
