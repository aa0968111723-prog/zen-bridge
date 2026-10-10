from __future__ import annotations

import unicodedata

import pytest

from app.ja_linebreak import break_ja_ex


_CHINESE_ONLY = frozenset(
    "，：；"
    "这个们说为头门车东长书爱"
    "這們說"
)
_JAPANESE_ENDINGS = frozenset("。？！」』）】〉》〕〗〙〛")

_HANDWRITTEN_MT_OUTPUTS = (
    ("今天是晴天，我们去公园吧。", "今日は晴れです。公園へ行きましょう！"),
    ("请现在开始。", "それでは、始めましょう。"),
    ("不要担心，我们会解决这个问题。", "心配しないでください。こちらで解決します。"),
    ("去东京。", "東京へ行きます。"),
    ("这是个人意见。", "個人の意見です。"),
    ("坐电车来。", "電車で来ました。"),
)


def _chinese_only_leftovers(text: str) -> list[str]:
    """Find known Chinese-only punctuation and characters in a Japanese caption."""
    return sorted(set(text) & _CHINESE_ONLY)


def _fullwidth_columns(text: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(char) in {"F", "W"} else 0.5
        for char in text
    )


def _assert_caption_quality(caption: str) -> None:
    leftovers = _chinese_only_leftovers(caption)
    assert not leftovers, f"Chinese-only leftovers: {leftovers!r}"
    stripped = caption.rstrip()
    assert stripped and stripped[-1] in _JAPANESE_ENDINGS, (
        "caption must end with Japanese sentence punctuation or a closing bracket"
    )

    result = break_ja_ex(
        caption, max_chars=16, max_lines=max(1, len(caption))
    )
    assert not result.overflow
    assert "".join(result.lines) == caption
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines), (
        f"display line exceeds 16 full-width columns: {result.lines!r}"
    )


@pytest.mark.parametrize(("source", "caption"), _HANDWRITTEN_MT_OUTPUTS)
def test_handwritten_zh_to_ja_outputs_meet_caption_quality(
    source: str, caption: str
) -> None:
    assert source
    _assert_caption_quality(caption)


@pytest.mark.parametrize(
    ("caption", "leftover"),
    [
        ("今日は，始めます。", "，"),
        ("これは重要：確認してください。", "："),
        ("这是良い機会です。", "这"),
        ("說明を始めます。", "說"),
    ],
)
def test_chinese_only_characters_and_punctuation_are_detected(
    caption: str, leftover: str
) -> None:
    assert leftover in _chinese_only_leftovers(caption)
    with pytest.raises(AssertionError, match="Chinese-only leftovers"):
        _assert_caption_quality(caption)


@pytest.mark.parametrize("caption", ["今日は晴れです", "始めましょう,"])
def test_caption_without_japanese_sentence_ending_is_rejected(caption: str) -> None:
    with pytest.raises(AssertionError, match="Japanese sentence punctuation"):
        _assert_caption_quality(caption)


def test_display_line_over_16_fullwidth_columns_is_rejected() -> None:
    caption = "ア" * 17 + "。"
    # The breaker keeps this oversized katakana token and its closing punctuation together.
    with pytest.raises(AssertionError, match="display line exceeds 16"):
        _assert_caption_quality(caption)


def test_line_break_api_splits_long_captions_into_display_lines() -> None:
    caption = "今日はとても良い天気なので散歩に出かけましょう。"
    result = break_ja_ex(caption, max_chars=16, max_lines=len(caption))

    assert not result.overflow
    assert "".join(result.lines) == caption
    assert len(result.lines) > 1
    assert all(_fullwidth_columns(line) <= 16 for line in result.lines)
