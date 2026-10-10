# 觀眾端無障礙與可讀性規格（AUDIENCE_UX）

- 作者：uiux-b（UI/UX B），2026-10-10（台北時間）
- 適用：`sidecar/live-room/app/static/room.html`（觀眾頁）。每場只有一種譯文：zh→en **或** zh→ja（`optimization-round2.md:5`）。
- 觀眾裝置：手機（直式／橫式）、平板、筆電接投影機。主持端是筆電桌面版（BRIEF）。
- 可直接套用的樣式：`app/static/room_a11y.css`（本檔每一節都標了對應的 CSS 節次）。測試：`tests/test_room_a11y_css.py`。
- 行號以 base `6ec5012`（`origin/feat/local-translation-backend`）為準。
- 標示方式：〔WCAG〕＝W3C WCAG 2.2；〔JLREQ〕＝W3C《日文排版需求》；〔MDN〕＝MDN Web Docs；「box 量測（Xeon，非 5600H）」＝在 box 上用 Chrome 154 headless 截圖或用測試程式計算，不是在柏能的筆電上量的。

## 0. 現況（讀程式得到的事實）

| 項目 | 現況 | 位置 |
|---|---|---|
| 頁面語言 | `<html lang="zh-Hant">` | `room.html:2` |
| 譯文容器 | `<div class="en" id="en" lang="en">`，寫死 en；ja 場次要靠 round4 T8 把 `en.lang` 改成 `tgt_lang`，並加別名 class `.tgt` | `room.html:125`；`round4.md:265` |
| 中文容器 | `<div class="zh" id="zh">`（繼承 zh-Hant） | `room.html:126` |
| 外觀 class | 同時加在 `<html>` 和 `<body>`：`dark`、`project`、`zh-only`、`en-only`、`size-28/34/46/64` | `room.html:145-155`；`room_prefs.js:71-80` |
| 字級 | `--size` 依 size class 用 `clamp(rem, vw, rem)` | `room.html:9,14-17` |
| 投影字級 | `max(--size, min(--size×1.8, --project-vw, --project-vh))` | `room.html:20-28` |
| 色彩 | 亮：`--bg #fbf6ee`、`--fg #2a2118`、`--muted #6b5644`；暗／投影：`#16130f`／`#f6f1e7`／`#d9d0c3` | `room.html:9,13` |
| 狀態 | `.stage.idle`、`.stage.stale`（字變 `--muted`） | `room.html:53,223-224` |
| 最新行底部對齊 | 非投影：flex column + `justify-content:flex-end` + `overflow:hidden`；投影只在 ≤671px | `room.html:29-31,55-58` |
| 連線狀態 | `#state role="status" aria-live="polite"`、`#toast role="status"` | `room.html:105,131` |
| 字型 | 只有 `"Segoe UI","PingFang TC"` | `room.html:12` |
| 減少動態 | 只在 `no-preference` 才有 0.2s 透明度過渡 | `room.html:76-78` |

### 0.1 發現的問題（本規格要修的）

1. **投影字級在 1280×720 幾乎沒有分級**：vh 上限 10、9、8.57、9.444（`room.html:20-23`）不是遞增，算出來「小／中／大／特大」＝50.4／61.2／61.7／68.0 px，「中→大」只多 0.8%（box 量測（Xeon，非 5600H）：`tests/test_room_a11y_css.py` 同一套公式計算）。
2. **手機橫式字太大**：844×390 時「特大」＝64 px（`room.html:17` 的 `11vw` 被 4rem 上限截住），扣掉標頭後一屏不到 4 行（box 量測（Xeon，非 5600H））。
3. **有 `<ruby>`／`<wbr>` 時版面會散掉**：譯文容器是 flex（`room.html:56`），行內元素會被各自變成一列。Chrome 154 截圖：「今日はまず、／般若／の意味…」，「般若」被擠成獨立一行（box 量測（Xeon，非 5600H））。round4 T9／T10 一加 `<wbr>` 和 `<ruby>` 就會踩到。
4. 第一次進房、系統是深色時會先閃一下亮色：預先繪製腳本在沒有存過偏好時直接 `return`（`room.html:88`），要等 module 跑完才加 `.dark`。
5. 橫式 iPhone 瀏海在左右兩側，字幕區只有固定 20px 內距（`room.html:68-69`），沒有 `safe-area-inset-left/right`。
6. 投影模式寬於 671px 時沒有底部對齊規則（`room.html:29`），只靠外層 `.stage` 的 `align-items:flex-end`。
7. 沒有日文字型堆疊、沒有 `:lang(ja)` 禁則；`room.html:12` 的字型在 Windows 上 ja 文字會落到中文字型（同碼不同形）。
8. 過期字幕只靠顏色區分（`room.html:53`），違反〔WCAG〕1.4.1 只用顏色傳達資訊。
9. 沒有 Windows 高對比（forced-colors）處理。

## 1. 字級分級（CSS §6）

### 1.1 原則
- 相鄰兩級至少大 **10%**，否則觀眾分不出差別（uiux-b 前次審查 `breeze-pipeline-notes/reviews/uiux-b-pr25-recheck*.md` 的結論；本專案自訂門檻）。
- 文字不小於 16 px（iOS Safari 在 16 px 以下的輸入框會自動放大；觀眾頁所有說明字都已是 16px，`room.html:36,45`）。
- 使用者可放大到 200% 不失功能〔WCAG〕1.4.4 <https://www.w3.org/TR/WCAG22/#resize-text>；寬 320 CSS px 不出現橫向捲軸〔WCAG〕1.4.10 <https://www.w3.org/TR/WCAG22/#reflow>。
- 一律用 CSS px 計算，`1rem = 16px`（瀏覽器預設）。

### 1.2 公式

| 視窗條件 | 譯文字級（`.en` / `.tgt`） | 來源 |
|---|---|---|
| 一般（直式、平板、桌面） | 小 `clamp(1.15rem, 5.5vw, 1.75rem)`；中 `clamp(1.5rem, 7vw, 2.125rem)`；大 `clamp(1.75rem, 8.5vw, 2.875rem)`；特大 `clamp(2rem, 11vw, 4rem)` | `room.html:14-17`（不改） |
| 橫式且高度 ≤ 500px（手機橫式） | 小 `clamp(1.15rem, 6.2vh, 1.75rem)`；中 `clamp(1.5rem, 7.8vh, 2.125rem)`；大 `clamp(1.75rem, 9vh, 2.875rem)`；特大 `clamp(2rem, 10.5vh, 4rem)` | `room_a11y.css` §6b |
| 投影模式 | `max(--size, min(--size×1.8, Nvw, Mvh))`；N／M：小 5.5／7、中 6.5／8、大 7.5／9.5、特大 9／11 | `room.html:27` 公式；上限值 `room_a11y.css` §6a |
| 中文副行 | 一般模式 `--size × 0.62`；投影和「只看中文」與譯文同大 | `room.html:19,28,69` |

### 1.3 各參考視窗的實際字級（px，小／中／大／特大；括號是相鄰級倍率）

box 量測（Xeon，非 5600H）：由 `tests/test_room_a11y_css.py::_font_px` 用上面公式計算，測試會逐格檢查 ≥ 1.10。

| 視窗 | 一般模式 | 投影模式 |
|---|---|---|
| 320×568 | 18.4／24.0／28.0／35.2（1.30、1.17、1.26） | 同左 |
| 390×844 | 21.4／27.3／33.1／42.9（1.27、1.21、1.29） | 同左 |
| 568×320 | 19.8／25.0／28.8／33.6（1.26、1.15、1.17） | 22.4／25.6／30.4／35.2（1.14、1.19、1.16） |
| 844×390 | 24.2／30.4／35.1／40.9（1.26、1.15、1.17） | 27.3／31.2／37.0／42.9（1.14、1.19、1.16） |
| 768×1024 | 28.0／34.0／46.0／64.0（1.21、1.35、1.39） | 42.2／49.9／57.6／69.1（1.18、1.15、1.20） |
| 1280×720 | 28.0／34.0／46.0／64.0 | 50.4／57.6／68.4／79.2（1.14、1.19、1.16）（修正前 50.4／61.2／61.7／68.0） |
| 1920×1080 | 28.0／34.0／46.0／64.0 | 50.4／61.2／82.8／115.2（1.21、1.35、1.39） |

### 1.4 裝置分級（預設「中」）
手機直式 390×844 **27.3** → 手機橫式 844×390 **30.4**（×1.11）→ 平板 768×1024 **34.0**（×1.12）→ 投影 1280×720 **57.6**（×1.69）→ 1920×1080 **61.2**。嚴格遞增，前四級每級 ≥ 10%（測試 `test_device_tiers_increase_at_least_10_percent`）。

### 1.5 每行字數參考
Netflix 日文字幕每行最多 13 個全形字、最多 2 行（`optimization-round2.md:248`，<https://partnerhelp.netflixstudios.com/hc/en-us/articles/215767517-Japanese-Timed-Text-Style-Guide>）。直播不分行，所以我們不限字數，改用「底部對齊＋從上方裁掉」（§7），讓最新的話永遠在畫面上。

## 2. 行高與字距（CSS §3）

| 文字 | 行高 | 字距 | 理由 |
|---|---|---|---|
| en（`#en:lang(en)`） | 1.4 | 0.01em；字間 0.02em | `room.html:68` 原為 1.35；〔WCAG〕1.4.8 建議段落行高 ≥ 1.5（AAA），字幕是短段落，取 1.4 兼顧行數 <https://www.w3.org/TR/WCAG22/#visual-presentation> |
| zh（`.zh`） | 1.5 | 0 | `room.html:69` 原為 1.45；中文ベタ組み（不加字距） |
| ja（`#en:lang(ja)`） | 1.6 | 0 | round4 T10 指定固定 1.6，避免加 ruby 時行高跳動（`round4.md:267`）；〔JLREQ〕和文原則上ベタ組み（字間不加空） <https://www.w3.org/TR/jlreq/#basics_of_japanese_composition> |
| ruby `rt` | 1 | 0 | 見 §5 |

- 必須容忍使用者自訂字距（字距 0.12em、字間 0.16em、行高 1.5）不遺失內容〔WCAG〕1.4.12 <https://www.w3.org/TR/WCAG22/#text-spacing>。我們不用固定高度裝文字；超出時從上方裁掉舊行是刻意設計（§7），最新行不受影響。

## 3. 對比（CSS §1、§10、§11、§14）

門檻：一般文字 ≥ **4.5:1**〔WCAG〕1.4.3 <https://www.w3.org/TR/WCAG22/#contrast-minimum>；介面元件、焦點框、狀態標記 ≥ **3:1**〔WCAG〕1.4.11 <https://www.w3.org/TR/WCAG22/#non-text-contrast>。公式用 WCAG 相對亮度，與 `room_view.js:7-21` 的 `contrast()` 相同；數值由 `tests/test_room_a11y_css.py::contrast` 計算（box 量測（Xeon，非 5600H））。

| 用途 | 前景 | 背景 | 對比 | 門檻 | 來源 |
|---|---|---|---|---|---|
| 亮色 字幕 | `#2a2118` | `#fbf6ee` | 14.68 | 4.5 | `room.html:9` |
| 亮色 中文小字／過期／說明 | `#6b5644` | `#fbf6ee` | 6.42 | 4.5 | `room.html:9,53` |
| 深色／投影 字幕 | `#f6f1e7` | `#16130f` | 16.45 | 4.5 | `room.html:13` |
| 深色／投影 中文小字／過期 | `#d9d0c3` | `#16130f` | 12.13 | 4.5 | `room.html:13` |
| 亮色 按鈕文字 | `#ffffff` | `#8a5424` | 6.22 | 4.5 | `room.html:36` |
| 深色 按鈕文字 | `#16130f` | `#c9843f` | 6.04 | 4.5 | `room.html:37` |
| 亮色 按鈕外形 vs 頁面 | `#8a5424` | `#fbf6ee` | 5.78 | 3 | 同上 |
| 深色 按鈕外形 vs 頁面 | `#c9843f` | `#16130f` | 6.04 | 3 | 同上 |
| 亮色 焦點框（新） | `#8a5424` | `#fbf6ee` | 5.78 | 3 | `room_a11y.css` §10（原 `#c47a32` 只有 3.16，`room.html:38`） |
| 深色 焦點框（新） | `#e8b878` | `#16130f` | 10.19 | 3 | `room_a11y.css` §10 |
| 過期虛線（亮／暗） | `#6b5644`／`#d9d0c3` | 頁面底色 | 6.42／12.13 | 3 | `room_a11y.css` §11 |
| 提示條 `#toast`（反白） | `#fbf6ee` | `#2a2118` | 14.68 | 4.5 | `room.html:74` |
| 增加對比 `--muted`（亮） | `#4a3a2c` | `#fbf6ee` | 10.10 | 7（自訂） | `room_a11y.css` §14 |

- **只用顏色，不用透明度**表示狀態（過期、草稿）：透明度疊在不同底色上算不出穩定對比。`room_a11y.css` 不含 `opacity`（測試會檢查）。
- **不用 `color-mix()`**：iOS Safari 16.2 才支援 <https://developer.mozilla.org/en-US/docs/Web/CSS/color_value/color-mix>。`room.html:35` 已先寫一行純色 fallback。

## 4. 日文斷行（CSS §4）

| 規則 | 寫法 | 來源 |
|---|---|---|
| 禁則（行頭不放「。、」）」小假名、長音；行末不放「（「」） | `line-break: strict` | 〔JLREQ〕<https://www.w3.org/TR/jlreq/#characters_not_starting_a_line>、<https://www.w3.org/TR/jlreq/#characters_not_ending_a_line>；〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/line-break> |
| 語節換行 | `@supports (word-break: auto-phrase) { word-break: auto-phrase }`，只對 `lang="ja"` 有效（Chrome 119+） | 〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/word-break> |
| 伺服器已插 `<wbr>`（BudouX，round4 T9） | `:has(wbr)` 時 `word-break: keep-all`，只在 `<wbr>` 與標點換行 | `round4.md:266`；<https://github.com/google/budoux> |
| 預設 fallback | `word-break: normal`（沒有 `<wbr>` 時不能用 keep-all，否則整句不換行） | 推論；box 截圖確認 |
| 網址、長英數 | `overflow-wrap: anywhere` | 〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/overflow-wrap> |
| 和英間距 | `@supports (text-autospace: normal)` | 〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/text-autospace> |
| 行頭括號半形 | `@supports (text-spacing-trim: trim-start)` | 〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/text-spacing-trim>；〔JLREQ〕<https://www.w3.org/TR/jlreq/#line_composition_rules_for_punctuation_marks> |

- 中文副行同樣用 `line-break: strict`（避免「，。」在行頭）。
- **XSS**：`<wbr>`／`<ruby>` 一律由前端用 DOM 建立，不用 innerHTML（`round4.md:266-268`）。CSS 不受影響。

## 5. 振假名 ruby（CSS §5）

- 只標詞表裡有讀音的術語，同一場只在第一次出現時標（`round4.md:267`；Netflix I.20，`optimization-round2.md:251`）。
- `ruby { ruby-position: over }`〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/ruby-position>；`@supports (ruby-align: center)` 時置中〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/ruby-align>。
- `rt { font-size: 50%; line-height: 1 }`：〔JLREQ〕〈Choice of Size for Ruby Characters〉ruby 字級原則上為親文字的一半 <https://www.w3.org/TR/jlreq/#choice_of_size_for_ruby_characters>；行距安排見 <https://www.w3.org/TR/jlreq/#line_gap_arrangement_with_ruby_and_other_objects>；投影模式 45%（`round4.md:267`）。
- **不壓到上一行的條件**：兩行之間的空白＝（行高 − 1）em＝0.6em ≥ rt 高度 0.5em × 1 ＝0.5em，所以行高 1.6 放得下（測試 `test_ja_line_height_leaves_room_for_ruby`）。第一行上方另有 `.en` 的 12px 內距（`room.html:68`），有 ruby 時改成 `max(12px, .5em)`。
- `rp` 隱藏（支援 ruby 的瀏覽器不需要括號）。
- **關閉振假名**（選用）：整合者在 `<html>`／`<body>` 加 `.ruby-off` 即可，`room_prefs.js` 目前沒有這個選項（要改 `room_prefs.js`，不在本次範圍）。
- **行內排版**：flex 容器會把 `<ruby>` 變成獨立一列（§0.1-3）。`room_a11y.css` §7b 在 `#en:has(ruby, wbr, span)` 時改成 `display:block; align-content: unsafe end`，保留行內排版且仍底部對齊。block 容器的 `align-content` 支援：Chrome 123、Firefox 125、Safari 17.4〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/align-content>。舊瀏覽器用 `@supports (content-visibility: auto) and (transition-behavior: allow-discrete)` 當代理條件（不成立時維持原本 flex）。

## 6. 字型（CSS §2）

只用系統內建字型，不下載（BRIEF：不加網路呼叫）。用 `:lang()` 選字型，讓瀏覽器選對字形（漢字同碼不同形，`optimization-round2.md:252`）〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/:lang>。

| lang | 堆疊 | 說明 |
|---|---|---|
| `zh`（含 zh-Hant） | `"Segoe UI", "Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Hiragino Sans TC", "Noto Sans TC", "Noto Sans CJK TC", sans-serif` | Windows 正黑體；Apple 蘋方；Android／Linux Noto |
| `en` | `"Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial,` 後接正黑體／蘋方／Noto TC | 譯文偶有沒翻的漢字，要落到繁中字型 |
| `ja` | `"Yu Gothic UI", "Yu Gothic", "Meiryo UI", "Meiryo", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Noto Sans CJK JP", sans-serif` | **不含任何中文字型**（測試檢查）；`round4.md:266` |

- Yu Gothic 在 Windows 預設字重偏細，ja 譯文固定 `font-weight: 700`。
- 前提：`#en` 的 `lang` 要跟著 `tgt_lang` 改（round4 T8）。沒改之前 ja 文字仍是 `lang="en"`，會用 en 堆疊落到中文字型——**這是整合前提，不是 CSS 能解決的**。

## 7. 最新一行永遠看得到（CSS §7、§7b）

- 字幕容器底部對齊，超出時從上方（舊的行）裁掉；不出現捲軸。非投影已在 `room.html:55-58`；`room_a11y.css` §7 補上投影模式所有寬度。
- 有行內元素時見 §5 最後一點。
- 歷史清單 `#history` 由 `historyStick()` 處理（`room_view.js:65-69`），不在 CSS 範圍。
- 抽屜（設定）不可蓋住標頭：`room.html:60` 已把 `#drawer` 放在標頭下方，本檔不動。

## 8. 深色模式（CSS §1、§1b）

- 明確切換：`html.dark`／`html.project`（`room.html:11,13`）；`room_a11y.css` 再加 `color-scheme: dark`，讓表單控制項和捲軸也變深色〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/color-scheme>。
- 系統深色：`@media (prefers-color-scheme: dark)` 只在 `<html>` 還沒有任何 `size-*` class 時生效（第一次進房、module 還沒跑），避免閃白。使用者明確選「亮色」後一定已存偏好（`writePrefs` 會寫 size），所以不會被覆蓋。

## 9. 動態、高對比（CSS §12、§13、§14）

- `prefers-reduced-motion: reduce`：所有過渡與動畫縮到 0.01ms、`scroll-behavior:auto`〔WCAG〕2.3.3 <https://www.w3.org/TR/WCAG22/#animation-from-interactions>；〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion>。
- `forced-colors: active`（Windows 對比佈景主題）：按鈕加 `ButtonText` 框、焦點 `Highlight`、過期／草稿用 `GrayText`、提示條加框〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/@media/forced-colors>。
- `prefers-contrast: more`：`--muted` 加深到 `#4a3a2c`（10.10:1），深色時改成 `#f6f1e7`；過期虛線加粗〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-contrast>。

## 10. 手機直式／橫式與安全區域（CSS §6b、§8、§9）

- 直式：譯文 60%、中文 40%（`room.html:57-58`）；歷史 28vh（`room.html:70`）。
- 橫式（高 ≤ 400px）：隱藏歷史與舞台提示（`room.html:62-67`）；字級改用 vh（§1.2）。
- 安全區域：`viewport-fit=cover` 已設（`room.html:5`）；`room_a11y.css` 補 `.stage`、`.sub`、`.stage-note`、`#hist-label`、`#live`、`#toast` 的左右 `env(safe-area-inset-*)`，投影模式補下方〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/CSS/env>。
- 觸控目標 ≥ 44×44 px：Apple HIG <https://developer.apple.com/design/human-interface-guidelines/accessibility>；〔WCAG〕2.5.5（AAA 44px）<https://www.w3.org/TR/WCAG22/#target-size-enhanced>，AA 2.5.8 只要 24px。

## 11. 過期與草稿字幕標記（CSS §11）

- 過期（斷線、離線、連結失效、課程結束）：`.stage.stale` 字變 `--muted`（原有）**再加左側 4px 虛線**，不只靠顏色〔WCAG〕1.4.1 <https://www.w3.org/TR/WCAG22/#use-of-color>；文字說明由 `#stage-note`「連線中斷，這是最後收到的字幕」提供（`room_view.js:35`）。
- 閒置（20 秒沒新字幕，`room_view.js:3`）：只變色，因為內容沒有錯，只是舊。
- 草稿（round4 兩段式 ASR，之後才有）：`.draft` 或 `[data-status="draft"]` → `--muted`＋點狀底線；**不用斜體**（中日文沒有真斜體，會被斜拉變形）。

## 12. 讀螢幕軟體

- 連線狀態 `#state`、提示 `#toast` 用 `role="status"`（隱含 `aria-live="polite"`）——已有（`room.html:105,131`）〔WCAG〕4.1.3 <https://www.w3.org/TR/WCAG22/#status-messages>。
- **字幕本身不要放 aria-live**：每 2–6 秒一句，會一直打斷讀屏；需要讀屏的觀眾用歷史清單逐句讀〔MDN〕<https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Attributes/aria-live>。切換譯文語言時只唸一次（`optimization-round2.md:260`）。
- 語言標記：`#en` 的 `lang` 要等於場次 `tgt_lang`；歷史清單的譯文 `<span>` 也要（`room.html:247` 目前寫死 `"en"`）〔WCAG〕3.1.2 <https://www.w3.org/TR/WCAG22/#language-of-parts>。這兩處要改 `room.html`，屬 round4 T8，整合者處理。
- `[hidden]{display:none!important}`：避免 `display:flex` 等規則把 `hidden` 元素顯示出來（`room.html:8` 已有，本檔重複宣告，不衝突）。

## 13. 驗收清單（每個視窗都做）

視窗：**320×568、390×844、568×320、844×390、768×1024、1280×720、1920×1080**。工具：Chrome DevTools 裝置模式（或實機）。準備：en 場與 ja 場各一；ja 測試句含 `<ruby>般若<rt>はんにゃ</rt></ruby>`、`<wbr>`、一段長網址。

| # | 步驟 | 預期 |
|---|---|---|
| 1 | 四個字級各切一次，量 `getComputedStyle(#en).fontSize` | 與 §1.3 表一致（±0.5px）；相鄰級 ≥ 1.10 倍 |
| 2 | 投影模式重做 1 | 同上（投影欄） |
| 3 | 貼 10 句長字幕 | 最新一句完整可見（底部對齊），沒有橫向捲軸 |
| 4 | ja 場：看「般若」 | ruby 在字上方、不壓上一行、「般若」不單獨成行 |
| 5 | ja 場：看行頭 | 行頭沒有「。」「、」「」」小假名「ゃ」 |
| 6 | ja 場：DevTools Rendered Fonts | Windows：Yu Gothic UI／Meiryo；不是 Microsoft JhengHei |
| 7 | 長網址 | 在網址內斷行，不撐破版面 |
| 8 | 亮／暗／投影各截圖 | 文字對比符合 §3（DevTools 對比檢查器） |
| 9 | 系統深色、清掉 localStorage 後進房 | 第一個畫面就是深色，不閃白 |
| 10 | 主題選「亮色」、系統深色、重新整理 | 維持亮色 |
| 11 | 斷網 | 舞台左側出現虛線＋「連線中斷…」；字變灰但對比 ≥ 4.5 |
| 12 | 系統「減少動態效果」 | 字幕、提示條沒有淡入淡出 |
| 13 | Windows 對比佈景主題（或 DevTools 模擬 forced-colors） | 按鈕有框、焦點可見、過期字是 GrayText |
| 14 | iPhone 橫式（有瀏海，844×390） | 字幕不被瀏海切到；底部不被 Home 列擋住 |
| 15 | Tab 走過所有按鈕 | 焦點框清楚（亮色 5.78:1、深色 10.19:1） |
| 16 | 量按鈕大小 | 全部 ≥ 44×44 px |
| 17 | VoiceOver／TalkBack | 連線狀態變化會唸；字幕不會自動一直唸 |
| 18 | `python -m pytest sidecar/live-room/tests/test_room_a11y_css.py -q` | 0 failed |

box 上已用 Chrome 154 headless 截圖確認 390×844、844×390、1280×720 的 ja（含 ruby／wbr／長網址）、投影深色、過期深色（box 量測（Xeon，非 5600H））；Safari、Firefox、實機、Windows 字型**未測**。
