"""Japanese subtitle line breaking.

The breaker keeps ruby annotations, katakana words, Latin words, and numbers
with their units together. It applies basic kinsoku rules, prefers boundaries
after punctuation and particles, and counts East Asian wide/fullwidth
characters as one display column and other characters as half a column.

繁體中文說明
------------
本模組將日文字幕分成適合即時顯示的行，避免行首出現禁則符號，並盡量
在標點或助詞後換行。振假名標記、片假名詞、英文字與數字單位會保持
完整；字寬依全形一字、半形半字計算。超過行數上限時，保留最新字幕行，
並由 ``break_ja_ex`` 的 ``overflow`` 欄位指出是否曾發生截斷。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_NO_LINE_START = frozenset(
    "、。，．・？！ゝゞー」』）】ぁぃぅぇぉっゃゅょゎ"
    "ァィゥェォッャュョヮヵヶ〃々〻ヽヾ"
)
_NO_LINE_END = frozenset("「『（【〈《〔〖〘〚")
_PARTICLES = ("から", "まで", "は", "が", "を", "に", "で", "と", "も", "へ")
_NUMBER_SEPARATORS = frozenset(".,:：/／-−")
_NUMBER_UNITS = tuple(
    sorted(
        (
            "時間", "キロ", "メートル", "センチ", "ミリ", "ドル",
            "kg", "mg", "km", "cm", "mm", "ml", "L", "％", "%",
            "℃", "°C", "°", "年", "月", "日", "時", "分", "秒",
            "個", "本", "枚", "人", "円", "回", "歳", "階", "台",
        ),
        key=len,
        reverse=True,
    )
)
_RUBY = re.compile(r"\{[^{}|]+\|[^{}|]+\}")
_COMMON_ENDINGS = ("ませんでした", "でしょう", "ました", "ません", "です", "ます")


@dataclass(frozen=True)
class JaLinebreakResult:
    """Wrapped display lines and whether older lines were discarded."""

    lines: list[str]
    overflow: bool


def _width(text: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(char) in {"F", "W"} else 0.5
        for char in text
    )


def _is_katakana(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x30A0 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xFF66 <= codepoint <= 0xFF9F
    )


def _is_latin_letter(char: str) -> bool:
    return (
        unicodedata.category(char).startswith("L")
        and "LATIN" in unicodedata.name(char, "")
    )


def _number_end(text: str, start: int) -> int:
    index = start
    while index < len(text):
        if text[index].isdecimal():
            index += 1
            continue
        if (
            text[index] in _NUMBER_SEPARATORS
            and index > start
            and index + 1 < len(text)
            and text[index - 1].isdecimal()
            and text[index + 1].isdecimal()
        ):
            index += 1
            continue
        break
    suffix = text[index:]
    for unit in _NUMBER_UNITS:
        if suffix.startswith(unit):
            index += len(unit)
            break
    return index


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        ruby = _RUBY.match(text, index)
        if ruby:
            tokens.append(ruby.group())
            index = ruby.end()
            continue
        ending = next((item for item in _COMMON_ENDINGS if text.startswith(item, index)), None)
        if ending:
            tokens.append(ending)
            index += len(ending)
            continue
        char = text[index]
        if char.isdecimal():
            end = _number_end(text, index)
        elif _is_katakana(char):
            end = index + 1
            while end < len(text) and _is_katakana(text[end]):
                end += 1
        elif _is_latin_letter(char):
            end = index + 1
            while end < len(text) and _is_latin_letter(text[end]):
                end += 1
        else:
            end = index + 1
        tokens.append(text[index:end])
        index = end
    return tokens


def _preferred_boundary(text: str, following: str = "") -> bool:
    if text.endswith(("、", "。")):
        return True
    if text.endswith("で") and following.startswith("す"):
        return False
    return any(text.endswith(particle) for particle in _PARTICLES)


def _wrap(text: str, max_chars: float) -> list[str]:
    tokens = _tokens(text)
    lines: list[str] = []
    start = 0
    while start < len(tokens):
        candidates: list[tuple[int, str, float, bool]] = []
        current: list[str] = []
        width = 0.0
        end = start
        while end < len(tokens):
            current.append(tokens[end])
            width += _width(tokens[end])
            end += 1
            line = "".join(current)
            next_char = tokens[end][0] if end < len(tokens) else ""
            if line[-1] in _NO_LINE_END or next_char in _NO_LINE_START:
                continue
            candidates.append(
                (end, line, width, _preferred_boundary(line, next_char))
            )
            if width > max_chars:
                break
        if not candidates:
            # Kinsoku may require carrying punctuation or an opening bracket
            # past the nominal width to reach a legal boundary.
            while end < len(tokens):
                current.append(tokens[end])
                width += _width(tokens[end])
                end += 1
                line = "".join(current)
                next_char = tokens[end][0] if end < len(tokens) else ""
                if line[-1] not in _NO_LINE_END and next_char not in _NO_LINE_START:
                    candidates.append(
                        (end, line, width, _preferred_boundary(line, next_char))
                    )
                    break
            if not candidates:
                candidates.append((end, "".join(current), width, False))

        fitting = [candidate for candidate in candidates if candidate[2] <= max_chars]
        pool = fitting or candidates[:1]
        widest = pool[-1][2]
        preferred = [
            candidate for candidate in pool
            if candidate[3] and candidate[2] >= widest - 2.0
        ]
        chosen = preferred[-1] if preferred else pool[-1]
        lines.append(chosen[1])
        start = chosen[0]
    return lines


def break_ja_ex(
    text: str, max_chars: int = 16, max_lines: int = 2
) -> JaLinebreakResult:
    """Break Japanese subtitle text and report whether older lines were trimmed."""
    if not text:
        return JaLinebreakResult([], False)
    if max_lines < 0:
        raise ValueError("max_lines must be non-negative")
    lines = _wrap(text, max_chars)
    overflow = len(lines) > max_lines
    return JaLinebreakResult(lines[-max_lines:] if max_lines else [], overflow)


def break_ja(text: str, max_chars: int = 16, max_lines: int = 2) -> list[str]:
    """Return the newest display lines from Japanese subtitle text."""
    return break_ja_ex(text, max_chars=max_chars, max_lines=max_lines).lines
