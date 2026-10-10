"""uiux-b: room_a11y.css 的靜態檢查（不需瀏覽器、不加相依套件）。

- 必要規則存在（ja 禁則、ruby、減少動態、深色、高對比、safe-area …）
- 字級分級：每個參考視窗下 小<中<大<特大 每級 ≥ 10%，裝置分級也 ≥ 10%
- 色票對比：文字 ≥ 4.5:1、介面元件 ≥ 3:1（WCAG 2.2 SC 1.4.3／1.4.11）
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "app" / "static" / "room_a11y.css"
ROOM_PATH = ROOT / "app" / "static" / "room.html"
DOC_PATH = ROOT / "docs" / "AUDIENCE_UX.zh-TW.md"
REM = 16.0
SIZES = ("28", "34", "46", "64")


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


@pytest.fixture(scope="module")
def css() -> str:
    assert CSS_PATH.is_file(), CSS_PATH
    return _strip_comments(CSS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def room() -> str:
    return ROOM_PATH.read_text(encoding="utf-8")


def _block(text: str, header_re: str) -> str:
    """回傳第一個符合 header_re 的 { … } 區塊內容（處理巢狀大括號）。"""
    m = re.search(header_re + r"\s*\{", text)
    assert m, f"找不到區塊：{header_re}"
    depth, i = 1, m.end()
    while depth:
        c = text[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
    return text[m.end(): i - 1]


def _rule_body(text: str, selector_re: str) -> str:
    m = re.search(selector_re + r"[^{}]*\{([^{}]*)\}", text)
    assert m, f"找不到規則：{selector_re}"
    return m.group(1)


def _len_px(token: str, w: float, h: float) -> float:
    token = token.strip()
    for unit, scale in (("rem", REM), ("px", 1.0), ("vw", w / 100), ("vh", h / 100)):
        if token.endswith(unit):
            return float(token[: -len(unit)]) * scale
    raise ValueError(token)


def _clamp(expr: str, w: float, h: float) -> float:
    m = re.fullmatch(r"\s*clamp\(([^,]+),([^,]+),([^)]+)\)\s*", expr)
    assert m, expr
    lo, mid, hi = (_len_px(x, w, h) for x in m.groups())
    return max(lo, min(mid, hi))


# ---------- 必要規則 ----------

REQUIRED = [
    (r":lang\(ja\)[^{]*\{[^}]*line-break:\s*strict", "ja 禁則 line-break: strict"),
    (r"@supports\s*\(word-break:\s*auto-phrase\)", "auto-phrase 漸進"),
    (r"word-break:\s*keep-all", "keep-all（有 <wbr> 時）"),
    (r":lang\(ja\)[^{]*\{[^}]*overflow-wrap:\s*anywhere", "ja 長字串 overflow-wrap"),
    (r"@supports\s*\(text-autospace", "text-autospace 漸進"),
    (r"@supports\s*\(text-spacing-trim", "text-spacing-trim 漸進"),
    (r"ruby\s*\{[^}]*ruby-position:\s*over", "ruby-position: over"),
    (r"rt\s*\{[^}]*font-size:\s*50%", "rt 50%"),
    (r"@supports\s*\(ruby-align", "ruby-align 漸進"),
    (r"\.ruby-off\s+rt", "關閉振假名 hook"),
    (r"@media\s*\(prefers-reduced-motion:\s*reduce\)", "減少動態"),
    (r"@media\s*\(prefers-color-scheme:\s*dark\)", "系統深色"),
    (r"html\.dark", "明確深色 class"),
    (r"@media\s*\(forced-colors:\s*active\)", "Windows 高對比"),
    (r"@media\s*\(prefers-contrast:\s*more\)", "增加對比"),
    (r"env\(safe-area-inset-left\)", "safe-area 左"),
    (r"env\(safe-area-inset-right\)", "safe-area 右"),
    (r"env\(safe-area-inset-bottom\)", "safe-area 下"),
    (r"\[hidden\]\s*\{\s*display:\s*none\s*!important", "[hidden] 隱藏"),
    (r"\.stage\.stale\s*\{[^}]*border-left:[^;]*dashed", "過期字幕的非顏色標記"),
    (r"justify-content:\s*flex-end", "最新行底部對齊"),
    (r"min-height:\s*44px", "44px 觸控"),
    (r":has\(ruby, wbr, span\)\s*\{\s*display:\s*block;\s*align-content:\s*unsafe end", "ruby/wbr 時保留行內排版且底部對齊"),
    (r"html:not\(\.zh-only\) #en:has", "只看中文模式不可把 #en 顯示回來"),
    (r"color-scheme:\s*dark", "表單元件深色"),
]


@pytest.mark.parametrize("pattern,why", REQUIRED, ids=[w for _, w in REQUIRED])
def test_required_rule_present(css, pattern, why):
    assert re.search(pattern, css, flags=re.S), why


def test_ja_font_stack_is_japanese_only(css):
    body = _rule_body(css, r"(?m)^:lang\(ja\)\s*")
    fonts = re.search(r"font-family:([^;]+);", body).group(1)
    for jp in ("Yu Gothic UI", "Meiryo", "Hiragino Sans", "Noto Sans JP"):
        assert jp in fonts
    for zh in ("JhengHei", "PingFang", "Noto Sans TC", "Noto Sans CJK TC", "Microsoft YaHei"):
        assert zh not in fonts, f"日文字型堆疊不能含中文字型：{zh}"


def test_zh_font_stack(css):
    fonts = re.search(r"font-family:([^;]+);", _rule_body(css, r"(?m)^:lang\(zh\)\s*")).group(1)
    for name in ("Microsoft JhengHei", "PingFang TC", "Noto Sans TC"):
        assert name in fonts


def test_no_network_or_risky_features(css):
    assert "@import" not in css
    assert "url(" not in css, "不可載入外部資源"
    assert "@font-face" not in css
    assert "color-mix(" not in css, "iOS < 16.2 沒有 color-mix"
    assert not re.search(r"opacity\s*:", css), "狀態用顏色，不用透明度"


def test_progressive_features_are_guarded(css):
    """auto-phrase / text-autospace / text-spacing-trim / ruby-align 只能出現在 @supports 裡。"""
    outside = css
    for head in (r"@supports\s*\([^)]*\)", r"@supports\s+selector\([^)]*\)\)"):
        while re.search(head + r"\s*\{", outside):
            inner = _block(outside, head)
            outside = outside.replace(inner, "", 1)
            outside = re.sub(head + r"\s*\{\s*\}", "", outside, count=1)
    for prop in (r"word-break:\s*auto-phrase", r"text-autospace:", r"text-spacing-trim:", r"ruby-align:"):
        assert not re.search(prop, outside), prop


def test_ja_line_height_leaves_room_for_ruby(css):
    # ja 行高宣告
    m = re.search(r"#en:lang\(ja\), \.tgt:lang\(ja\)\s*\{\s*line-height:\s*([\d.]+)", css)
    assert m
    lh = float(m.group(1))
    rt = float(re.search(r"(?m)^rt\s*\{[^}]*font-size:\s*([\d.]+)%", css).group(1)) / 100
    # 兩行之間的空間（lh − 1）em 要容得下 rt（rt 字級 × rt 行高 1）
    assert lh - 1 >= rt, (lh, rt)


# ---------- 字級分級 ----------

def _base_sizes(room: str) -> dict:
    out = {}
    for s in SIZES:
        m = re.search(r"html\.size-%s, body\.size-%s \{ --size: (clamp\([^;]+\)); \}" % (s, s), room)
        assert m, f"room.html 找不到 size-{s}"
        out[s] = m.group(1)
    return out


def _landscape_sizes(css: str) -> dict:
    block = _block(css, r"@media \(orientation: landscape\) and \(max-height: 500px\)")
    out = {}
    for s in SIZES:
        m = re.search(r"html body\.size-%s[^{]*\{\s*--size:\s*(clamp\([^;]+\));" % s, block)
        assert m, f"橫式 size-{s}"
        out[s] = m.group(1)
    return out


def _project_caps(css: str) -> dict:
    out = {}
    for s in SIZES:
        m = re.search(r"html\.project\.size-%s body, body\.project\.size-%s \{ --project-vw: ([\d.]+)vw; --project-vh: ([\d.]+)vh; \}" % (s, s), css)
        assert m, f"投影 size-{s}"
        out[s] = (float(m.group(1)), float(m.group(2)))
    return out


def _font_px(css, room, size, w, h, project):
    expr = _landscape_sizes(css)[size] if (w > h and h <= 500) else _base_sizes(room)[size]
    base = _clamp(expr, w, h)
    if not project:
        return base
    scale = float(re.search(r"--project-scale:\s*([\d.]+)", room).group(1))
    vw, vh = _project_caps(css)[size]
    return max(base, min(base * scale, vw * w / 100, vh * h / 100))


VIEWPORTS = [(320, 568), (390, 844), (568, 320), (844, 390), (768, 1024), (1280, 720), (1920, 1080)]


@pytest.mark.parametrize("project", [False, True], ids=["normal", "project"])
@pytest.mark.parametrize("w,h", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
def test_size_steps_at_least_10_percent(css, room, w, h, project):
    px = [_font_px(css, room, s, w, h, project) for s in SIZES]
    for a, b in zip(px, px[1:]):
        assert b >= a * 1.10 - 1e-9, (w, h, project, [round(x, 2) for x in px])


def test_device_tiers_increase_at_least_10_percent(css, room):
    """預設字級（中＝34）下：手機直式 < 手機橫式 < 平板 < 投影（1280×720）。"""
    tiers = [
        _font_px(css, room, "34", 390, 844, False),
        _font_px(css, room, "34", 844, 390, False),
        _font_px(css, room, "34", 768, 1024, False),
        _font_px(css, room, "34", 1280, 720, True),
    ]
    for a, b in zip(tiers, tiers[1:]):
        assert b >= a * 1.10 - 1e-9, [round(x, 2) for x in tiers]
    assert _font_px(css, room, "34", 1920, 1080, True) >= tiers[-1]


def test_text_never_below_16px(css, room):
    for w, h in VIEWPORTS:
        for s in SIZES:
            assert _font_px(css, room, s, w, h, False) >= 16


# ---------- 對比 ----------

def _lum(hex_: str) -> float:
    hex_ = hex_.lstrip("#")
    def ch(v):
        v = int(v, 16) / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(hex_[i:i + 2]) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_wcag():
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#777777", "#ffffff"), 2) == 4.48


def _palette(css: str) -> dict:
    root = _rule_body(css, r"(?m)^:root\s*")
    return dict(re.findall(r"--(a11y-[\w-]+):\s*(#[0-9a-fA-F]{6})", root))


PAIRS = [  # (前景, 背景, 門檻, 用途)
    ("a11y-fg-light", "a11y-bg-light", 4.5, "亮色 字幕"),
    ("a11y-muted-light", "a11y-bg-light", 4.5, "亮色 中文小字／過期"),
    ("a11y-fg-dark", "a11y-bg-dark", 4.5, "深色／投影 字幕"),
    ("a11y-muted-dark", "a11y-bg-dark", 4.5, "深色 中文小字／過期"),
    ("a11y-btn-fg-light", "a11y-btn-bg-light", 4.5, "亮色 按鈕文字"),
    ("a11y-btn-fg-dark", "a11y-btn-bg-dark", 4.5, "深色 按鈕文字"),
    ("a11y-focus-light", "a11y-bg-light", 3.0, "亮色 焦點框"),
    ("a11y-focus-dark", "a11y-bg-dark", 3.0, "深色 焦點框"),
    ("a11y-stale-light", "a11y-bg-light", 3.0, "亮色 過期虛線"),
    ("a11y-stale-dark", "a11y-bg-dark", 3.0, "深色 過期虛線"),
    ("a11y-btn-bg-light", "a11y-bg-light", 3.0, "亮色 按鈕外形"),
    ("a11y-btn-bg-dark", "a11y-bg-dark", 3.0, "深色 按鈕外形"),
]


@pytest.mark.parametrize("fg,bg,minimum,why", PAIRS, ids=[p[3] for p in PAIRS])
def test_palette_contrast(css, fg, bg, minimum, why):
    pal = _palette(css)
    assert contrast(pal[fg], pal[bg]) >= minimum, (why, pal[fg], pal[bg], round(contrast(pal[fg], pal[bg]), 2))


def test_palette_matches_room_html(css, room):
    """色票要和 room.html 的實際顏色一致，否則算出來的對比沒意義。"""
    pal = _palette(css)
    for key, needle in (("a11y-bg-light", "--bg: %s"), ("a11y-fg-light", "--fg: %s"), ("a11y-muted-light", "--muted: %s"),
                        ("a11y-fg-dark", "--fg: %s"), ("a11y-muted-dark", "--muted: %s")):
        assert needle % pal[key] in room, key
    assert "background: %s" % pal["a11y-btn-bg-light"] in room
    assert "background: %s; color: %s" % (pal["a11y-btn-bg-dark"], pal["a11y-btn-fg-dark"]) in room


def test_high_contrast_muted_override(css):
    block = _block(css, r"@media \(prefers-contrast: more\)")
    for value in re.findall(r"--muted:\s*(#[0-9a-fA-F]{6})", block):
        bg = "#fbf6ee" if value != "#f6f1e7" else "#16130f"
        assert contrast(value, bg) >= 7.0, value


def test_spec_doc_exists_and_cites_sources():
    text = DOC_PATH.read_text(encoding="utf-8")
    for needle in ("w3.org/TR/WCAG22", "w3.org/TR/jlreq", "developer.mozilla.org", "room.html:", "box 量測"):
        assert needle in text, needle
    for vp in ("320×568", "390×844", "568×320", "844×390", "768×1024", "1280×720", "1920×1080"):
        assert vp in text, vp
