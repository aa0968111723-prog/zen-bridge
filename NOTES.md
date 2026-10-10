# fullstack NOTES：OBS 字幕疊加層（zen-bridge 禪釋桌面翻譯）

## 做了什麼
新增 OBS 瀏覽器來源用的透明字幕疊加層：只顯示最新 2 段字幕、en/ja 字型（含中文原文字型）、URL 參數控制外觀、沿用既有觀眾 WebSocket `/ws/listen` 並自動退避重連。沒有 logo、沒有外部 CDN、只用 `textContent`。**未修改 server.py 或 BRIEF 禁碰的任何檔案。**

- 分支：`impl/fullstack`（worktree `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\fullstack`）
- 基準：`grok/integrate` @ `bea2e5c`（round4 #2: T4 prompt 'current' = live app prompt）
- 程式 commit：`727f35d`（完整 SHA 見文末）；本 NOTES.md 另一個 commit
- 新增檔案（皆在 `sidecar/live-room/` 下，純新增、不改既有檔）：
  - `app/overlay_routes.py`：FastAPI `APIRouter`，`GET /overlay`、`GET /overlay/{room_id}`
  - `app/static/overlay.html`、`app/static/overlay.js`
  - `tests/test_overlay_routes.py`（pytest 9 項）、`tests/overlay.test.mjs`（Node `node:test` 17 項）

## 測試結果
- 筆電（DESKTOP-P8RGA3A，共用 venv Python 3.12.10）：`python -m pytest tests/test_overlay_routes.py -q` → **8 passed, 1 skipped**（skip = 筆電沒有 node，Node 測試項目跳過）。依指示未跑全套。
- box（Xeon，非 5600H；相同檔案，基準為 box 沙盒 `4b84597`）：`node --test tests/overlay.test.mjs` → **17/17 pass**（Node 20.19.2 與 22.23.3 都過）；pytest 同檔 9 passed。
- box 全套（參考用，基準 `4b84597`）：966 passed、24 skipped；`test_sim_srt.py`／`test_sim_backlog.py` 的 100 分鐘模擬時間門檻在 box 高負載（load avg ~12）下失敗，與 overlay 無關（overlay 純新增檔、未被它們匯入）；未能在 box 完成基準對照即被改派到筆電。
- 另在 box 驗證：真實 `create_app()` 掛上 router 後，帶同源 `Origin` 連 `/ws/listen` 會通過 origin 檢查（收到 `room_unavailable`，非 1008 拒絕）。

## 接線（整合者，`app/server.py` 的 `create_app()` 內、`app.mount("/static", ...)` 之後）

疊加層網址：`http://127.0.0.1:<port>/overlay/<房間代號>`（`<port>` 為 app 的服務埠，與 host 頁相同）。

```python
from app.overlay_routes import router as overlay_router
app.include_router(overlay_router)
```

`overlay.js` 由既有 `/static` 掛載（`RevalidatingStaticFiles`，`no-cache`）提供，不需其他接線。
字幕走既有觀眾端 WebSocket `/ws/listen`（同 room.html 的協定：`hello` / caption / `caption_deleted` / `captions_cleared` / `captions_expired` / `ping`→`pong` / `room_unavailable`），**沒有新增資料路徑或端點**。

## OBS 設定

來源 → ＋ → 「瀏覽器」→ URL 填 `http://127.0.0.1:<port>/overlay/<房間代號>?lang=en&size=56`，寬高設成場景解析度（如 1920×1080），勾「頁面不可見時關閉來源」可省資源；不需要自訂 CSS（背景本身就是透明）。
若房間有觀眾金鑰，加上 `&k=<listen key>`（就是 QR 碼網址裡 `?k=` 的值）。

## URL 參數

所有參數都會驗證；不合法就用預設值，數字超出範圍就夾到邊界。查詢字串**不會**反射到 HTML（頁面是靜態檔）。

| 參數 | 值 | 預設 | 說明 |
|---|---|---|---|
| 路徑 `/overlay/{room_id}` 或 `room` | `[A-Za-z0-9_-]{1,64}` | `class` | 房間代號；路徑優先。路徑不合法回 400 |
| `k` | `[A-Za-z0-9_\-.~]{1,256}` | 空 | 觀眾金鑰（room 有設時必填） |
| `lang` | `en` \| `ja` | `en` | 本場目標語言，決定字型與 `lang` 屬性 |
| `show` | `tgt` \| `zh` \| `both` | `tgt` | 顯示譯文／中文原文／兩者（原文 0.8em 在上） |
| `lines` | 1–2（整數） | 2 | 畫面上最多幾段字幕，硬上限 2 |
| `size` | 12–160 | 48 | 字級 px |
| `scale` | 0.25–4 | 1 | 乘在 `size` 上；最終字級夾在 8–320 px |
| `pos` | `bottom` \| `top` \| `middle` | `bottom` | 垂直位置 |
| `align` | `center` \| `left` \| `right` | `center` | 文字對齊 |
| `color` | `#rgb` / `#rrggbb`（`#` 可省、可寫 `%23`） | `#ffffff` | 字色 |
| `outline` | 同上 | `#000000` | 描邊色 |
| `ow` | 0–12 | 3 | 描邊寬 px（8 方向 text-shadow），0 = 無描邊 |
| `shadow` | 0 \| 1 | 1 | 額外的柔和陰影 |
| `margin` | 0–400 | 40 | 距上／下緣 px |
| `maxw` | 30–100 | 90 | 字幕區寬度（vw） |
| `hide` | 0–600 秒 | 0 | 無新字幕多久後清空畫面；0 = 不清 |

字型（無外部 CDN，全用本機字型）：
- en：`Segoe UI, Helvetica Neue, Arial, Noto Sans, Liberation Sans, sans-serif`
- ja：`Noto Sans JP, Noto Sans CJK JP, Source Han Sans JP, Yu Gothic UI, Yu Gothic, Meiryo UI, Meiryo, Hiragino Sans, Hiragino Kaku Gothic ProN, MS PGothic, sans-serif`（`line-break: strict`）
- zh（原文）：`Noto Sans TC, Noto Sans CJK TC, Source Han Sans TC, Microsoft JhengHei UI, Microsoft JhengHei, PingFang TC, Heiti TC, sans-serif`

## 行為

- 只顯示最新 2 段（依 `session_ord`→`seq`→`cursor` 排序）；同一段以 `version` 較新者覆蓋；尚未翻好或翻譯失敗（`status` = missing/error/timeout/cancelled）的段在 `tgt` 模式不顯示，所以畫面停在上一段完成的譯文。
- 每段最多約兩行視覺列；容器溢出時從上方裁切，最新文字一定在畫面內。
- 重連：`[0.5,1] × min(30 s, 1 s·2^(n-1))`，伺服器的 `retry_after_ms` 當下限（最多 60 s）；`4401`（金鑰失效）每 60 s 試一次；`ended` 每 15 s 試一次（下一場同房間會自動接上）。永不停止。
- 收到 `ping` 回 `pong`，避免伺服器閒置逾時斷線。
- 只用 `textContent`／`createElement`；連線狀態寫在 `body[data-state]`（畫面上不顯示任何狀態文字）。
- 回應標頭：`Cache-Control: no-cache`（同 repo 慣例）、`X-Content-Type-Options: nosniff`、CSP `default-src 'none'; script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'`…

## 測試

```bash
cd sidecar/live-room
node --test tests/overlay.test.mjs                 # Node ≥18 即可（node:test），不用絕對路徑
python -m pytest tests/test_overlay_routes.py -q    # 也會呼叫上面的 node 測試；無 node 則 skip
python -m pytest tests -q                           # 全套
```


## 已知限制、假設與待決問題

1. **ja 譯文欄位**：目前觀眾端白名單（`app/dispatch.py` `_LISTENER_PASS`）只有 `zh`、`en`，中→日場次的譯文推測也放在 `en` 欄。overlay 在 `lang=ja` 時若事件帶 `ja` 欄會優先用，否則用 `en`。若之後改成 `tgt`/`tgt_lang` 欄位，需要小改 `lineText()`。
2. **佔用觀眾席位**：overlay 是一個普通觀眾連線，會計入 `max_listeners`。若要 OBS 不佔位，需要後端另開 host 專用頻道（未做，因不能改 server.py）。
3. **Origin**：OBS 從同一 host 載入頁面，Origin 與 Host 相同，符合 `audience_origin_allowed`。若 OBS 用 `127.0.0.1` 而 share host 是 LAN IP，也是同源請求，不受影響。
4. 未在真的 OBS／CEF 上手動驗證（box 無 OBS）；建議整合者在 5600H 上開一次確認描邊與日文字型。
5. `/overlay` 頁面沒有走 `RevalidatingStaticFiles` 的 `?v=` 戳記（為避免 overlay_routes 匯入 server.py 造成循環匯入）；靠 `no-cache` 重新驗證，已足夠。

## BLOCKED
- 筆電沒有 Node.js，`tests/overlay.test.mjs` 無法在筆電執行（pytest 包裝會自動 skip）；已在 box 用 Node 20／22 跑過 17/17。若整合者要在筆電跑，需要可攜式 Node ≥18（未自行下載安裝）。
- 無 OBS 可做實機目視驗證（描邊、日文字型 fallback 實際落在哪套字型）。

## 完整 commit SHA
- 程式 commit：`727f35d8444b6aae5a4bb0a242c0e38af5109283`
- 基準：`bea2e5c3cac2ab7d3f35bdaa25cf2702c1b8894d`（grok/integrate）
