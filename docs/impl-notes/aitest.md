# aitest（AI 代理測試專家）— 示意圖 V2 交件說明

- 分支：`impl/aitest`（worktree：`C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\aitest`）
- base commit：`bea2e5c`（grok/integrate，「round4 #2: T4 prompt 'current' = live app prompt」）
- 程式 commit：
  - `9d562b02ceab43ff1f94f56dc5de4eb54c264a52` feat(visual): V2 visual_routes router + independent visual channel + viewer page
  - `6da10c7b0dc72081ad13549f892437cebebdc1ca` feat(visual): viewer forwards ?k= listen key to /ws/visual for ws_guard
- 本 NOTES.md 另以一個 commit 提交在上述兩個 commit 之後（`git log -1 impl/aitest` 即是）。
- V1 來源（唯讀複製）：`zen-bridge-visual-v1` 分支 `grok-bot/visual-v1` commit `721b2f0` 的 `sidecar/live-room/app/visual.py`。兩棵樹都沒有 `docs/VISUAL.md`，所以介面依 V1 模組本身（`visual_draft` schema、`VisualLLM.complete(messages, *, timeout_s)`）。

## 做了什麼

新增檔案（全部在 `sidecar/live-room/`，沒有改任何既有檔案）：

| 檔案 | 內容 |
|---|---|
| `app/visual.py` | 從 V1 移植。V2 新增：`strip_think()`（去掉 `<think>…</think>`、未閉合的 `<think>`、只有結尾的 `</think>`）；去重改用**正規化雜湊**（NFKC＋casefold，去掉空白／標點／符號）且只在 `dedupe_window_ms`（預設 10 分鐘，環境變數 `BREEZE_VISUAL_DEDUPE_WINDOW_MS`）內有效；`is_presentable()`（空卡、只有佔位標題、殘留 think 標籤一律不廣播）；`build_llm_from_env()` 改為**只接受 loopback**（127.x / localhost / ::1），非本機 URL 一律停用，不會把字幕送出筆電。 |
| `app/visual_routes.py` | `VisualSubscriber`（每位觀看者有界佇列、滿了丟最舊）、`VisualChannel`（獨立的每房訂閱者集合與重播歷史；只收 `visual_draft` / `visual_hello`，字幕事件丟進來會 `ValueError`）、`VisualHub`（每房一個 `VisualGenerator`：觸發、每房冷卻、去重、LLM 逾時／錯誤改用術語卡；`feed()` 可從任何執行緒呼叫，會 `call_soon_threadsafe` 回事件迴圈）、`build_visual_router()`（APIRouter）。 |
| `app/static/visual.html`＋`visual.js` | 獨立觀看頁 `/visual?room=<房間>[&k=<listen key>]`，連 `/ws/visual`，即時顯示示意圖卡（標題中英、內文、術語、Mermaid 原始碼）。全部用 `textContent`，不解析模型輸出為 HTML。`visual.js` 是 ES module（有 `export`，符合 `test_static_cache.py` 對 `static/*.js` 的要求）。 |
| `tests/test_visual_routes.py` | 30 個測試（假 LLM、假時鐘，無網路、無模型）。 |

Router 端點：

- `GET /visual` 觀看頁、`GET /visual/visual.js`
- `GET /api/visual/{room_id}`：`{room_id, enabled, subscribers, skipped, dropped_jobs, history}`
- `POST /api/visual/{room_id}/trigger[?force=1]`：主持人手動觸發；預設 `host_guard` 只允許本機（loopback）用戶端
- `WS /ws/visual?room_id=...[&k=...]`：先送 `{"type":"visual_hello","room_id","enabled","history":[...]}`，之後每張新卡送一則 `visual_draft`（V1 schema）。與 `/ws/listen` 完全分開。

## 測試指令與結果

```powershell
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\aitest\sidecar\live-room
C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_visual_routes.py -q -p no:cacheprovider
```

結果（筆電 DESKTOP-P8RGA3A 實測，2026-10-10 23:39 台北時間前後）：**30 passed, 0 failed, 1 warning，1.23–1.29 s**；本次開發共跑 6 次，最後兩次（最終程式碼）與先前 4 次皆 30 passed（檢查非偶發）。warning 是 starlette 的 `httpx` 棄用提示，與本模組無關。依規定只跑本檔，**沒有跑全套**。

涵蓋：觸發（定稿滿 30 s 才觸發、非 final／壞事件忽略）、冷卻（假時鐘前進前後）、正規化去重與時間窗過期、手動 force、每房冷卻＋房間隔離、壞房號、停用時不動作、頻道分離（字幕型別被拒、router 沒有 `/ws/listen`）、慢訂閱者丟最舊且不拖累快訂閱者、發佈內容是獨立副本、LLM 例外→術語卡、LLM 逾時（0.05 s）→術語卡、無術語時失敗不廣播、6 種垃圾輸出不廣播、think 剝除（含 code fence、欄位內 think）、不安全 Mermaid 降級、跨執行緒 feed、預設 LLM 只限本機且預設關閉、觀看頁／JS 有供應、WebSocket hello＋即時卡＋房間隔離＋晚到重播、ws_guard 拒絕、手動觸發端點與預設本機限定。

## 掛載方式（由整合者改 server.py；我沒有動 server.py / pipeline.py）

在 `app/server.py` 的 `create_app()` 內：

1. 檔頭 import：
   ```python
   from app.visual_routes import VisualHub, build_visual_router
   ```
2. `bus = RoomBus(...)`（約第 882 行）之後建立 hub：
   ```python
   visual_hub = VisualHub.from_env(glossary_provider=<回傳該房術語 rows 的函式，可為 async；沒有就省略>)
   ```
   沒有設定 `BREEZE_VISUAL_LLM_BASE_URL`＋`BREEZE_VISUAL_LLM_MODEL` 時 hub 是停用的（頁面顯示「示意圖未啟用」），不影響字幕。
3. `app.mount("/static", ...)`（約第 1290 行）之後：
   ```python
   app.state.visual_hub = visual_hub
   app.include_router(build_visual_router(visual_hub, ws_guard=_visual_ws_guard))
   ```
   建議的 `_visual_ws_guard`（放在 `/ws/listen` 定義附近，重用既有檢查）：
   ```python
   def _visual_ws_guard(ws, room_id):
       if not audience_origin_allowed(ws.headers.get("origin"), ws.headers.get("host", ""), settings, _audience_extra_hosts()):
           return False
       room = book.get(room_id)
       return room is None or _listener_authorized(ws, room, ws.query_params.get("k", ""))
   ```
   （不傳 `ws_guard` 也能跑，但就沒有 origin／listen key 檢查。）
4. **觸發點**：在 `on_event()`（約第 1083 行）`_fanout(snap)` 之後加一行：
   ```python
   visual_hub.feed(snap)
   ```
   `feed()` 只看 `type == "final"`、合法 room_id、有 zh 與 t0/t1 的事件，其餘直接回 False；它不做 I/O、不等待，可從任何執行緒呼叫。所以**不需要改 pipeline.py**——pipeline 的定稿（`Segment.public()` 的 `type: "final"`）本來就經過 `on_event` → `bus.publish`。若整合者偏好在 pipeline 端呼叫，位置是 pipeline 產生 `status in {"ready","silent"}` 的 Segment 並送出 `public()` 的地方，呼叫同一個 `visual_hub.feed(segment.public())`。
5. 房間結束：在 `bus.retire(room_id)` / `bus.drop(room_id)`（約第 1118、1127 行）旁 `await visual_hub.close_room(room_id)`（若該處不是 async，用 `asyncio.get_running_loop().create_task(...)`）。
6. 關機：在 lifespan 的 `shutdown()` 內 `await visual_hub.stop()`。

本機模型範例（只設環境變數，不改 Ollama 設定）：`BREEZE_VISUAL_LLM_BASE_URL=http://127.0.0.1:11434/v1`、`BREEZE_VISUAL_LLM_MODEL=<已安裝的本機模型名>`。

看得到的變化：掛上後開 `http://127.0.0.1:<port>/visual?room=class`，就能看到示意圖頻道的即時狀態與卡片。

## 參數與數字來源

- 觸發窗 30–60 s、冷卻 30 s、LLM 逾時 15 s、每房工作佇列 2、最多 12 行、術語 24 個：沿用 V1 `app/visual.py`（commit 721b2f0）預設值，未在筆電做效能量測。
- 去重時間窗 10 分鐘、每觀看者佇列 8 則、重播歷史 5 張、每房最多 64 位觀看者：本次設計選值，未量測。
- 測試耗時 1.23–1.29 s：筆電實測。

## 已知限制

- Mermaid 只顯示原始碼文字，沒有渲染成圖（不引入外部 JS 函式庫；要渲染需把 mermaid 放進 vendor 並另寫 CSP 安全的渲染）。
- 觀看頁沒有連到 host.html／room.html（那些檔案禁止修改）；需整合者另加連結或直接開 `/visual`。
- `VisualHub` 綁定第一個呼叫它的事件迴圈；在 uvicorn 單一迴圈下沒問題。若 `close_room` 沒接，房間的 generator 會留到關機。
- 預設 `host_guard` 只認 loopback 用戶端；若主持人從別台裝置操作，需傳入使用主持人 token 的 guard。
- 沒跑全套測試（依筆電記憶體規定）。`test_static_cache.py` 會掃 `static/*.js` 要求含 `export ` 且可重新驗證；`visual.js` 已是 ES module，應可通過，但需整合者跑全套確認。
- 停用狀態下 `/api/visual/...` 與 `/ws/visual` 仍可連，只是 `enabled: false`、不會有卡片。

## BLOCKED

無。
