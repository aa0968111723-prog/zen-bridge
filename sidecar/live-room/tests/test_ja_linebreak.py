from __future__ import annotations

import pytest

from app.ja_linebreak import JaLinebreakResult, break_ja, break_ja_ex


@pytest.mark.parametrize(
    ("text", "max_chars", "expected"),
    [
        ("", 4, []),
        ("日本語", 4, ["日本語"]),
        ("あいうえお", 3, ["あいう", "えお"]),
        ("こんにちは世界", 5, ["こんにちは", "世界"]),
        ("今日は晴れです", 4, ["今日は", "晴れです"]),
        ("花が咲く", 3, ["花が", "咲く"]),
        ("水を飲む", 3, ["水を", "飲む"]),
        ("駅に行く", 3, ["駅に", "行く"]),
        ("家で休む", 3, ["家で", "休む"]),
        ("友達と話す", 4, ["友達と", "話す"]),
        ("私も行く", 3, ["私も", "行く"]),
        ("学校へ行く", 4, ["学校へ", "行く"]),
        ("東京から大阪", 6, ["東京から", "大阪"]),
        ("駅まで歩く", 4, ["駅まで", "歩く"]),
        ("今日は、晴れです", 5, ["今日は、", "晴れです"]),
        ("晴れです。明日も", 5, ["晴れです。", "明日も"]),
        ("あいう、えお", 4, ["あいう、", "えお"]),
        ("あいう。「えお", 4, ["あいう。", "「えお"]),
        ("「こんにちは」", 4, ["「こんに", "ちは」"]),
        ("『日本語』です", 4, ["『日本", "語』です"]),
        ("カタカナテスト", 4, ["カタカナテスト"]),
        ("スーパーで買う", 5, ["スーパーで", "買う"]),
        ("ﾃｽﾄをする", 3, ["ﾃｽﾄを", "する"]),
        ("ABCDEFです", 3, ["ABCDEF", "です"]),
        ("caféを飲む", 3, ["caféを", "飲む"]),
        ("Aあ B", 2, ["Aあ ", "B"]),
        ("12時に集合", 4, ["12時に", "集合"]),
        ("3.14です", 2, ["3.14", "です"]),
        ("12:30開始", 3, ["12:30", "開始"]),
        ("100%です", 3, ["100%", "です"]),
        ("25kgです", 3, ["25kg", "です"]),
        ("A1B2C3", 2, ["A1B2", "C3"]),
        ("漢字{東京|とうきょう}です", 5, ["漢字", "{東京|とうきょう}", "です"]),
        ("{複合語|ふくごうご}です", 3, ["{複合語|ふくごうご}", "です"]),
        ("ab（cd）ef", 3, ["ab", "（cd）", "ef"]),
        ("ｶﾅﾀｶﾅ", 2, ["ｶﾅﾀｶﾅ"]),
        ("A B C", 2, ["A B ", "C"]),
        ("", 1, []),
    ],
)
def test_break_ja_examples(text: str, max_chars: int, expected: list[str]) -> None:
    assert break_ja(text, max_chars=max_chars, max_lines=10) == expected


def test_kinsoku_attaches_closing_punctuation_to_previous_line() -> None:
    lines = break_ja("あいう、次", max_chars=4, max_lines=5)
    assert lines == ["あいう、", "次"]
    assert all(not line.startswith("、。，．・？！ゝゞー」』）】") for line in lines)


def test_opening_punctuation_stays_with_following_text() -> None:
    lines = break_ja("あいう「えお", max_chars=4, max_lines=5)
    assert lines == ["あいう", "「えお"]
    assert all(not line.endswith("「『（【") for line in lines)


def test_halfwidth_characters_count_as_half() -> None:
    assert break_ja("ABCD", max_chars=2, max_lines=5) == ["ABCD"]


def test_overflow_keeps_newest_lines_and_is_reported() -> None:
    result = break_ja_ex("あいうえおかきくけこ", max_chars=2, max_lines=2)
    assert result == JaLinebreakResult(["きく", "けこ"], True)
    assert break_ja("あいうえおかきくけこ", max_chars=2, max_lines=2) == result.lines


def test_exact_line_limit_does_not_overflow() -> None:
    assert break_ja_ex("あいうえ", max_chars=2, max_lines=2) == JaLinebreakResult(
        ["あい", "うえ"], False
    )


def test_zero_max_lines_returns_no_lines_and_reports_content_overflow() -> None:
    assert break_ja_ex("字幕", max_chars=2, max_lines=0) == JaLinebreakResult([], True)


def test_negative_max_lines_is_rejected() -> None:
    with pytest.raises(ValueError):
        break_ja("字幕", max_lines=-1)


def test_very_long_input_is_bounded_to_recent_lines() -> None:
    result = break_ja_ex("日" * 10_000, max_chars=10, max_lines=2)
    assert result.overflow
    assert result.lines == ["日" * 10, "日" * 10]
