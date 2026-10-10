import difflib

import pytest

from app.admin import db
from app import tm
from tests.local_fakes import migrated_db


def memory_with_units(tmp_path, units, *, fuzzy_min=0.86):
    path = migrated_db(tmp_path)
    conn = db.connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for source, target, language, quality in units:
            tm.add_unit(conn, source, target, tgt=language, quality=quality)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return tm.TranslationMemory(lambda: db.connect(path, readonly=True), fuzzy_min=fuzzy_min)


def test_fuzzy_match_obeys_inclusive_threshold(tmp_path):
    source = "今天我們一起來學習因緣具足的道理"
    query = "今天我們一起來學習因緣具足的原理"
    score = difflib.SequenceMatcher(None, tm.norm(query), tm.norm(source)).ratio()
    memory = memory_with_units(tmp_path, [(source, "Complete conditions", "en", 4)], fuzzy_min=score)

    assert memory.exact(query, "en") is None
    assert memory.fuzzy(query, "en")[0].score == score
    memory.fuzzy_min = score + 0.001
    assert memory.fuzzy(query, "en") == []


def test_exact_and_fuzzy_matches_are_isolated_by_target_language(tmp_path):
    source = "今天我們一起來學習因緣具足的道理"
    query = "今天我們一起來學習因緣具足的原理"
    memory = memory_with_units(
        tmp_path,
        [
            (source, "Complete conditions", "en", 4),
            (source, "因縁が満ちる", "ja", 4),
        ],
    )

    assert memory.exact(source, "en").tgt == "Complete conditions"
    assert memory.exact(source, "ja").tgt == "因縁が満ちる"
    assert [hit.tgt for hit in memory.fuzzy(query, "en")] == ["Complete conditions"]
    assert [hit.tgt for hit in memory.fuzzy(query, "ja")] == ["因縁が満ちる"]


def test_disabled_quality_is_not_an_exact_match(tmp_path):
    source = "今天我們一起來學習因緣具足的道理"
    memory = memory_with_units(tmp_path, [(source, "Disabled translation", "en", 2)])

    assert memory.exact(source, "en") is None


@pytest.mark.xfail(
    strict=True,
    reason="TranslationMemory.fuzzy currently omits quality<3 disabled units from its SQL filter.",
)
def test_disabled_quality_is_not_a_fuzzy_example(tmp_path):
    source = "今天我們一起來學習因緣具足的道理"
    query = "今天我們一起來學習因緣具足的原理"
    memory = memory_with_units(tmp_path, [(source, "Disabled translation", "en", 2)])

    assert memory.fuzzy(query, "en") == []


def test_nfkc_removes_full_width_and_half_width_punctuation():
    full_width = "今日は、禪修！「重要」です。"
    half_width = "今日は,禪修!\"重要\"です."

    assert tm.norm(full_width) == tm.norm(half_width)
    assert tm.src_hash(full_width, "ja") == tm.src_hash(half_width, "ja")
