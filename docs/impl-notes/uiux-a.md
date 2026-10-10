# uiux-a｜後台「即時效能」頁 交件說明

- **代號**：uiux-a（UI/UX A）
- **base commit**：`bea2e5c`（`grok/integrate`，round4 #2；來源 `/workspace/zen-impl/uiux-a-src/integrate.bundle`）
- **工作區**：`/tmp/impl-uiux-a-v2`，分支 `impl/uiux-a`，commit `fe7ec5a`（1 個 commit）
- **patch**：
  - 單一 mbox：`/workspace/zen-impl/uiux-a/uiux-a.patch`
  - format-patch 目錄：`/workspace/zen-impl/uiux-a/patches/0001-feat-admin-admin-perf-A2-A6-p50-p95-latency-bars-RTF.patch`
  - SHA256（兩個檔案內容相同）：`1947dd3f4c05784a3822c7929e0cf435a96d4f1a3bc2e0fd9d9bc7f399b40789`
  - 已在乾淨的 `bea2e5c` clone 上跑 `git apply --check`：可以直接套用（box 實測）。
- 一開始在 `6ec5012`（`origin/feat/local-translation-backend`）上做過第一版（`/tmp/impl-uiux-a`，沒有 commit）。父代理改了 base 之後，第一版作廢，以本版為準。

## 做了什麼

1. **新路由模組 `app/admin/perf_routes.py`**：沒有修改 `app/admin/server.py`、`app/server.py`、`pipeline.py`。
   - `GET /admin/perf`：回傳 `static/perf.html`。和 `/admin` 一樣，頁面本身不需登入，資料端點才需要。
   - `GET /admin/perf/static/{name}`：自己的白名單，只放 `perf.js`、`perf_logic.js`、`perf.css`、`perf_fixture.json`，其他一律回 404 problem+json。既有的 `/admin/static/{name}` 白名單沒有改。
   - `GET /admin/api/v1/perf/metrics`：需要 `need("viewer")` 權限。用後台既有的 live client 呼叫 `live.metrics()`（也就是 8780 的 `/api/metrics`），回傳前只留下 `latency`（A2–A6）、RTF 和積壓欄位。token 用量、價格、聽眾數、room_id、session_id 都不會傳出去。8780 連不上時，交給 app 既有的 `LiveDown` handler 回 503 problem+json，`detail` 是「直播服務（8780）目前連不上」。
   - **為什麼不用 SSE**：`/admin/api/v1/live/stream` 用 `keep` 清單過濾欄位（`server.py` 約 905–907 行），`latency` 不在清單裡。照規定不改 server.py，所以本頁改成每 2 秒輪詢上面這個新端點。
2. **頁面**（都是新檔，放在 `app/admin/static/`，沒有用任何外部 JS 函式庫）：
   - `perf.html`：
     - 設了 viewport meta 和 `lang="zh-Hant"`，有「跳到主要內容」連結。
     - 只有一個隱藏的 `role="status"` 播報區。
     - 圖表加了 `aria-hidden`，同樣的數字另外放在 `<details>` 裡的語意化 `<table>`（有 caption 和 th scope）。
     - 沒有 inline script、`on*=` 或 `style=`。
   - `perf_logic.js`：純函式 ES module，不碰 DOM、不連網，負責門檻判斷、形狀轉換、狀態機、遲滯和播報。
   - `perf.js`：負責 DOM 和輪詢。
     - 只用 `textContent`、`classList`，以及 `style.setProperty('--w'|'--x')` 設定 CSS 自訂屬性。
     - fetch 帶 `credentials:"same-origin"`，5 s 逾時。
     - `document.hidden` 時暫停輪詢並中止進行中的請求，切回前景時立刻抓一次。
     - 失敗時依 2、4、8、10 s 退避重試。收到 401 就停止輪詢，顯示「前往登入」。
   - `perf.css`：
     - 用 `:focus-visible` 畫 3px `#c47a32` 焦點框，觸控目標至少 44px。
     - ≤600px 時改成單欄，360px 寬可以正常使用（有截圖）。
     - 動畫只在 `prefers-reduced-motion: no-preference` 時啟用。
   - `perf_fixture.json`：示範用的假數字，形狀和真實的 `latency.snapshot()`、RTF 欄位相同。只有網址帶 `?demo=` 時才會讀，畫面會標「示範資料（假資料）」。
3. **畫面內容**：
   - **A2–A6 橫條圖**：
     - 實心段是 p50，斜線延伸段是 p95，虛線是目標上限。
     - 有刻度（0 到 1／2／5×10ⁿ 等分四格），每列右側用文字標「p50／p95｜狀態」，例如「930 ms／3.30 s｜嚴重超標」。
     - 顏色依 p95 判斷，每個狀態都有對應文字，不只靠顏色。
   - **RTF 卡**：依 p95 分級，<0.7「跟得上」、0.7–0.9「接近上限」、≥0.9「跟不上，字幕會延遲」。除了顏色，也有 ●▲■ 圖示和文字。卡上同時顯示 p50 和樣本數。
   - **積壓卡**：顯示 `backlog_s` 和 `pending`，不分級上色。
   - **各種狀態**：
     - 載入：骨架列。
     - 空：「開始聽之後才有數字」。
     - 錯誤：顯示 problem+json 的 `detail`，附「立即重試」按鈕。
     - 暫停更新：卡片和圖表改虛線框、改成 muted 文字，標「（數字暫停更新）最後更新 HH:MM:SS」，舊數字保留不清空。
     - 401：顯示「登入已過期」和登入連結。
   - **播報**：只在以下情況更新 `role="status"`：RTF 穩定等級改變（要連續 3 筆相同才切換，依 ui.md §3.1，暫定）、進入或離開暫停更新、出現錯誤。同一句話 30 s 內不重複（ui.md §3.4）。每次輪詢不會播報。
   - **對比**（box 用 WCAG 公式計算）：
     - 文字：狀態字和底色 5.53–6.53:1，muted 文字 8.06:1。
     - 圖形：條形色對白底，綠 5.13、黃 4.4、紅 5.62、灰 3.45:1。都達到 AA（文字 ≥4.5:1、圖形 ≥3:1）。

## 掛載（整合者要做的唯一改動）

在 `sidecar/live-room/app/admin/server.py` 的 `create_admin_app()` 裡、`zinfo.register(app, SimpleNamespace(...))` 這個呼叫結束之後（`bea2e5c` 的第 941 行 `llama_probe=llama_probe or (lambda: _tcp_probe("127.0.0.1", 8080))))` 下一行）、`app.add_middleware(LoopbackOnly, port=port)` 之前，加這兩行（縮排 4 格）：

```python
    from app.admin import perf_routes      # uiux-a 即時效能頁
    perf_routes.register(app, SimpleNamespace(api=API, need=need, live=live, Problem=Problem, clock=clock))
```

- `register()` 內部做的就是 `app.include_router(perf_routes.build_router(api=..., need=..., live=..., Problem=..., clock=...))`。
- 因為掛在 `LoopbackOnly` 之前，所有路由一樣受 LoopbackOnly、CSP 標頭和 `need(role)` 保護。
- **已驗證**：在乾淨的 `bea2e5c` clone 上套 patch、加這兩行後：
  - `/admin/perf` 回 200，`/admin/perf/static/perf.js` 回 200。
  - `perf/metrics` 沒帶憑證回 401，帶 Bearer 回 200 和裁切後的 JSON。
  - `test_admin_api/info/hardening` 102 passed。
- 建議另外在 `app/admin/static/index.html` 的導覽列加 `<li><a href="/admin/perf">即時效能</a></li>`。這不是必要的，我沒有改既有檔案。

## metrics 形狀（真實資料，有標來源）

- **各段**：8780 `/api/metrics` 的 `latency` 欄位，由 `app/pipeline.py:395` 寫入（`payload["latency"] = self.latency.snapshot()`）。內容是 `app/latency.py` `StageLatency.snapshot()` 的輸出：
  `{"A2":{"name":"upload","n":12,"p50_ms":120,"p95_ms":260}, "A3":{"name":"queue",…}, "A4":{"name":"decode_vad",…}, "A5":{"name":"asr",…}, "A6":{"name":"mt",…}}`
  沒有樣本時是 `n:0`、`p50_ms:null`、`p95_ms:null`，頁面顯示「尚無資料」。
- **各段定義**（`app/latency.py` 第 3–9 行）：
  - A2：`t_recv − t_slice_end`。
  - A3：`t_asr0 − t_recv`，**包含解碼／VAD 和等待辨識器的時間**，所以和 A4 有重疊。頁面的說明文字已經註明。
  - A4：解碼加 Silero VAD。
  - A5：ASR 本身。
  - A6：只算翻譯呼叫，不含排隊。
- **RTF**：`app/rtf.py` `RtfMeter.snapshot()`。有 `rtf.session`（最近更新的那一場）就用它的 `rtf.p50/p95` 和 `count`；沒有就用扁平欄位 `asr_rtf_p50`、`asr_rtf_p95`、`asr_samples`。`count` 為 0 時不顯示上一場的數字，和主持頁一致。舊版只給單一數字 `rtf` 的格式也能讀。
- **門檻**（`perf_logic.js` 的 `STAGES`）：

| 段 | 黃（超過目標） | 紅（嚴重超標） | 來源 |
|---|---|---|---|
| A2 上傳 | ≥ 0.3 s | ≥ 0.6 s | 目標 p95<0.3 s：round4 §3.2（round4.md:170）；紅為暫定（×2） |
| A3 排隊 | ≥ 1 s | ≥ 6 s | round4.md:171（>6 s 代表 RTF>1） |
| A4 解碼＋VAD | ≥ 0.15 s | ≥ 0.3 s | round4.md:172；紅為暫定 |
| A5 辨識 | ≥ 4.2 s | ≥ 5.4 s | 換算：RTF 0.7／0.9（host.html:470-476）× 6 s 片（round4.md:169 `periodMs: 6000`） |
| A6 翻譯 | ≥ 1.5 s（ja 2 s） | ≥ 3 s（ja 4 s） | round4.md:174；紅為暫定。`/api/metrics` 目前沒有 `tgt_lang`，所以實際一律套 en 門檻 |
| RTF p95 | ≥ 0.7 | ≥ 0.9 | host.html:470-476、ui.md:16 |

- `perf_fixture.json` 和截圖裡的數字都是**假數字**，不是量測值。

## 新增檔案（8 個，全是新檔，沒有改任何既有檔）

```
sidecar/live-room/app/admin/perf_routes.py
sidecar/live-room/app/admin/static/perf.html
sidecar/live-room/app/admin/static/perf.js
sidecar/live-room/app/admin/static/perf_logic.js
sidecar/live-room/app/admin/static/perf.css
sidecar/live-room/app/admin/static/perf_fixture.json
sidecar/live-room/tests/test_admin_perf_page.py
sidecar/live-room/tests/admin_perf.test.mjs
```

## 測試（box 量測，Xeon 8 vCPU，非 5600H）

- **`tests/test_admin_perf_page.py`：9 passed。**
  - 用 monkeypatch 包住 `info.register`，模擬上面的掛載方式，所以會經過真的 `create_admin_app`、LoopbackOnly 和 CSP。
  - 檢查 HTML 符合 CSP：每個 `<script` 都帶 `src=`、沒有 inline 內容、沒有 `on*=`、`style=`、`<style`、`javascript:`；viewport、`role="status"` 恰好一個、有 table 和 details。
  - 檢查靜態白名單：4 個允許檔名，加上路徑穿越和非白名單檔名回 404 problem+json；既有白名單不受影響。
  - 檢查 JS 裡沒有 innerHTML、eval、外部 URL。
  - 檢查 fixture 和真實 `StageLatency.snapshot()` 同形狀。
  - 檢查端點：未登入回 401，裁切真實形狀時不洩漏 room/session id 和 tokens，舊版沒有 latency 的回應也能處理，8780 斷線回 503 problem+json。
  - 用 Node 跑下面的 `.mjs` 測試。
- **`tests/admin_perf.test.mjs`**（`node tests/admin_perf.test.mjs` → `admin_perf.test.mjs: ok`）：
  - RTF 和各段門檻的邊界值，包含 ja 版 A6。
  - 格式化和刻度。
  - 真實 latency 形狀、`n=0` 空狀態、只有部分段有資料、`rtf.session` 優先、新一場 `count=0`、舊版格式、垃圾輸入。
  - problem+json 錯誤訊息。
  - 狀態機：loading、error、ready、stale、恢復、401。
  - 3 筆遲滯。
  - 播報只在等級或狀態改變時觸發，以及 30 s 去重。
- **Node 版本**：box 上是 node v20.19.2。既有的 `tests/test_node_suites.py` 在 node 22 以下會整批 skip，所以我沒有改那支檔案，另外在 `test_admin_perf_page.py` 裡用 node ≥18 直接跑 `admin_perf.test.mjs`。在 box 上這支會實際執行，不會被 skip。
- **後台相關測試**：`test_admin_api.py`、`test_admin_hardening.py`、`test_admin_info.py`、`test_admin_perf_page.py`、`test_stage_latency.py` 合計 **114 passed**。
- **全套**：在 `sidecar/live-room` 下執行 `/workspace/zen-sandbox/venv/bin/python -m pytest tests -q`，結果 **1012 passed, 38 skipped, 1 failed**。
  - 失敗的是 `tests/test_sim_backlog.py::test_100min_latency_no_drift`（計時斷言 25.4 s > 20 s）。
  - 單獨重跑 `test_sim_backlog.py`，換成同檔另一支 `test_100min_session_never_waits` 失敗。
  - **在沒有本 patch 的乾淨 `bea2e5c` 上跑同一個檔案，也一樣失敗**（1 failed, 15 passed）。
  - 判斷：這是 box 負載下的計時測試，跑測試期間 load average 約 5–6（8 vCPU，有其他代理同時在跑），和本 patch 無關。需要在筆電上再確認。
- **注意執行目錄**：BRIEF 的寫法是在 repo 根目錄執行 `python -m pytest sidecar/live-room/tests -q`。舊 base `6ec5012` 這樣跑，有 10 支既有測試因為用相對路徑讀 `app/static/host.html`、`app/pipeline.py` 而 FileNotFoundError。這是既有問題，不是本 patch 造成的。請在 `sidecar/live-room` 目錄下執行。

## 截圖（headless Chrome，假數字）

存在 `/workspace/zen-impl/uiux-a/`：

- `perf-1280.png`、`perf-360.png`：示範資料（`?demo=1`）。
- `perf-1280-stale.png`：暫停更新狀態（`?demo=stale`）。
- `perf-360-empty.png`：空狀態（`?demo=empty`）。
- `perf-360-error.png`：錯誤狀態（`?demo=error`）。
- `perf-1280-latency-shape.png`：用暫時的 uvicorn（127.0.0.1:18799，已關閉）從新端點讀假的真實形狀資料，其中 A6 `n=0` 顯示「尚無資料」。

## 已知限制

- 採用輪詢，不用 SSE，原因見上文。之後如果 `live/stream` 的 `keep` 清單加入 `latency` 和 `asr_rtf_*`，可以改用 SSE，`perf_logic.js` 不用改。
- A6 的 ja 門檻要等 `/api/metrics` 提供 `tgt_lang` 才會生效，目前一律用 en 門檻。
- 紅色門檻中標「暫定」的（A2、A4、A6），以及 3 筆遲滯，都要在 5600H 實測後調整。
- 既有的 `admin_logic.js` `rtfColor()` 用 0.5／0.9 當門檻，和主持頁、本頁的 0.7／0.9 不一致。我沒有改，建議整合者統一。
- 圖表是 `aria-hidden`，讀屏使用者要展開「以表格查看數字」才能讀到數字。每段的摘要文字已經準備好（`buildView` 的 `rows[].aria`），但目前沒有用到。
- 沒有做 ui.md 的 30 分鐘趨勢線和心跳條，不在這次範圍內。

## BLOCKED

- 沒有被擋住的項目。
- 沒有 push、沒有開 PR、沒有用 Cloud Agent、沒有刪檔、沒有加網路呼叫，也沒有碰筆電和 8645 port。
- 沒有修改 `/workspace/zen-sandbox/repo` 的內容。只做了 `git worktree add` 建立第一版的 `/tmp/impl-uiux-a`（分支 `impl/uiux-a`，已作廢，裡面有沒 commit 的舊檔，可以不理）。

## 筆電實機套用（DESKTOP-P8RGA3A，2026-10-10 23:5x）
- worktree：`C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\uiux-a`，branch `impl/uiux-a`，base `bea2e5c`（grok/integrate）。
- 以 `git am` 套用 `uiux-a.patch`（SHA256 `1947dd3f4c05784a3822c7929e0cf435a96d4f1a3bc2e0fd9d9bc7f399b40789`）→ 筆電 commit **`a69d855`**（內容同 box 的 fe7ec5a；sha 不同因 committer 不同）。
- 測試（筆電、5600H、共用 `.venv`，只跑本人測試檔）：`cd sidecar\live-room; ..\..\..\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_admin_perf_page.py -q` → **8 passed, 1 skipped**。skip 是 Node 測試：筆電 PATH 上沒有 `node`（box 上 node v20 已實跑通過）。
- 全套由整合者跑。box 上全套 1 failed 為 `test_sim_backlog.py` 計時測試，乾淨 bea2e5c 也會失敗（box 負載），請在筆電確認。
- **要讓 app 看得到**：整合者 merge `impl/uiux-a` 後，在 `app/admin/server.py` 的 `create_admin_app()` 加上方「掛載」兩行，重啟後台後開 `http://127.0.0.1:8791/admin/perf`（需先登入後台）。可選：在 `index.html` 加一個「即時效能」連結。
- 本 NOTES.md 刻意不 commit，避免與其他代理的 NOTES 衝突。
