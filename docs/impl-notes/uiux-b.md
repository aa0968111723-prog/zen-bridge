# uiux-b（UI/UX B）交件說明 — 觀眾端無障礙與可讀性

- 時間：2026-10-10 23:40（台北）
- **筆電交件（正式）**：DESKTOP-P8RGA3A，worktree `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\uiux-b`，分支 `impl/uiux-b`
  - base：`grok/integrate` = `bea2e5c3cac2ab7d3f35bdaa25cf2702c1b8894d`
  - commit：`847920ebba48298f26381fce9cca2dd52c9857e8`（只新增 3 個檔，680 行）
  - `app/static/` 在 bea2e5c 與 6ec5012 之間沒有任何差異（`git diff --stat 6ec5012 HEAD -- sidecar/live-room/app/static/` 為空），所以選擇器以 box 讀到的 room.html 為準即可。
- box 草稿（備查，非正式交件）：`/tmp/impl-uiux-b` 分支 `impl/uiux-b` commit `bec19d9`（base 6ec5012），patch `/workspace/zen-impl/uiux-b/uiux-b.patch`，SHA256 `011cd4d67b932a04376e0ed5af0d2274496477c70fea9299c6985a57f8ee6ea7`。三個檔內容與筆電 commit 相同。

## 做了什麼（新檔，沒有改任何既有檔）
1. `sidecar/live-room/docs/AUDIENCE_UX.zh-TW.md`：規格。字級分級（7 個參考視窗×4 級的實際 px 表、裝置分級）、行高／字距（zh／en／ja）、對比表（13 組色對、算好的比值）、ja 斷行（strict、auto-phrase、keep-all＋<wbr>、overflow-wrap、text-autospace、text-spacing-trim）、ruby（50%、ruby-position、行高 1.6 的空間計算、關閉振假名 hook）、字型堆疊（`:lang()`，ja 堆疊不含中文字型）、深色、減少動態、forced-colors、prefers-contrast、直／橫式與 safe-area、最新行永遠可見、過期／草稿標記、讀屏規則、18 項驗收清單。每個數字附 WCAG／JLREQ／MDN 網址、`room.html:行號`，或「box 量測（Xeon，非 5600H）」。
2. `sidecar/live-room/app/static/room_a11y.css`：可直接掛上的樣式，只用 room.html 現有的選擇器與 class。修到的實際問題：
   - 投影字級在 1280×720「中→大」只差 0.8%（50.4／61.2／61.7／68.0 px）→ 改成 50.4／57.6／68.4／79.2，每級 ≥ 10%。
   - 手機橫式 844×390「特大」64 px → 改用 vh，40.9 px。
   - 有 `<ruby>`／`<wbr>` 時 flex 容器把「般若」擠成單獨一行（Chrome 154 截圖確認）→ 改用 block＋`align-content: unsafe end`，保持底部對齊。
   - 第一次進房系統深色會閃白、焦點框亮色只有 3.16:1（改 5.78:1）、橫式瀏海左右 safe-area、投影寬螢幕的底部對齊、過期字幕只靠顏色（加虛線）、Windows 高對比、日文字型與禁則。
3. `sidecar/live-room/tests/test_room_a11y_css.py`：62 個 pytest（無新相依）。檢查必要規則、漸進特性都在 `@supports` 內、無網路／`color-mix`／`opacity`、ja 字型不含中文字型、ruby 行高空間、7 個視窗×一般／投影的字級每級 ≥ 10%、裝置分級 ≥ 10%、色票對比（文字 4.5、UI 3），並核對色票與 room.html 實際顏色一致。

## 給整合者：要加的一行
在 `sidecar/live-room/app/static/room.html` **第 79 行 `</style>` 之後、第 80 行 `<script>`（預先繪製腳本）之前**插入：

```html
<link rel="stylesheet" href="/static/room_a11y.css">
```

（要放在內嵌 style 後面才能覆寫；放在預先繪製 script 前面，第一次繪製就套用。檔案由既有的 `/static` 掛載提供，不需改 server.py。）加了之後 app 才看得到變化——依規則我不能改 room.html。

## 測試
- 筆電（正式）：`cd ...\zen-bridge-impl\uiux-b\sidecar\live-room; ...\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_room_a11y_css.py -q` → **62 passed, 0 failed（0.31 s）**。依 BRIEF 沒有在筆電跑全套。
- box（6ec5012，參考用）：
  - 新測試：62 passed。
  - 全套從 `sidecar/live-room` 跑：1029 passed、38 skipped、**1 failed**（`test_sim_backlog.py::test_100min_session_never_waits`，模擬時間門檻；單獨重跑該檔時又有 2 個時間相關的失敗，當時 box load 約 4–6，有其他代理同時在跑 pytest，跟 CSS／文件無關）。
  - 加 patch 前從 repo 根目錄跑（基準）：956 passed、38 skipped、12 failed——都是測試用相對路徑（`app/static/host.html`、`docs/DEVICE-ACCEPTANCE.md`）或 `No module named 'app.ledger'`，要從 `sidecar/live-room` 跑才會過，是原本就有的問題。加 patch 後從根目錄的第二次全套跑到 53% 時被外部 SIGTERM（exit 143）中斷，沒有完整數字。
- 畫面確認：box Chrome 154 headless 截圖 390×844、844×390、1280×720（ja 含 ruby／wbr／長網址、投影深色、過期深色）。

## 已知限制
- ja 字型與讀屏語言要等 round4 T8 把 `#en.lang` 改成 `tgt_lang`（還有 `room.html:247` 歷史清單 `<span lang="en">`）；在那之前 ja 文字會被當成 en，套 en 字型堆疊。
- 振假名開關只提供 `.ruby-off` class hook；選單選項要改 `room_prefs.js`（不在本次範圍）。
- block 容器的 `align-content` 沒有可靠的功能查詢，用 `content-visibility`＋`transition-behavior` 當代理條件；Chrome 117–122 可能誤判（落回原本的 flex，最壞情況是 ruby 單獨成行，不會遮住最新行）。
- Safari／Firefox／實機 iPhone／Windows 實際字型（Yu Gothic UI、正黑體）都沒有測，只在 box Chrome 上用 Noto 字型截圖。
- 字級數字是公式計算＋box Chrome 截圖，不是 5600H 實機量測。

## 作業紀錄（需要知道的）
- box 上我用 `pkill -f "pytest sidecar/live-room/tests"` 停掉自己重複啟動的一次全套；這個樣式理論上也會匹配到其他代理用同一條指令跑的 pytest。當時 `pgrep` 只看到我自己的那一個符合，但無法完全排除影響。
- box 上做 apply 檢查時建了一個臨時 detached worktree（`/tmp/tmp.oxnTILqGxp`，base 6ec5012），檢查後已 `git worktree remove`；沒有動其他任何 worktree 或檔案。

## BLOCKED
無。
