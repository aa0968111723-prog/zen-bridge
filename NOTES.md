# backend — 後台 staging 核准流程（round3 §5-1）NOTES

- 工作區：`C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\backend`，分支 `impl/backend`
- base：`grok/integrate` @ `bea2e5c3cac2ab7d3f35bdaa25cf2702c1b8894d`（含 6ec5012）
- 程式 commit：`be273bdfe7812a2a857883a48328035f9ba69e92`（這份 NOTES 另一個 commit 在其後）
- 合併：`git merge impl/backend`

## 做了什麼
人工修正（`POST /admin/api/v1/corrections`、審稿頁 `PATCH /segments/{seg}/text`）**不再直接寫 TM／詞表**：
字幕修正本身照舊成為新的人工版本（這是字幕，不是 TM），但 TM 與 `propose_term` 只會建立 `staging_items`（pending）。
管理員核准時，在**同一個 SQLite 交易（BEGIN IMMEDIATE）**裡寫入 `tm_units`／`glossary_terms`、更新項目狀態、寫 `staging_audit`（誰、何時、前後內容）。連管理員自己的修正也要走核准（刻意，符合 VocBench 流程）。

狀態機：`pending → approved | rejected(必填原因) | withdrawn(提案者或管理員) | superseded(同目標出現新提案)`；`pending` 可編輯（rev+1）。其他轉換一律 409 `invalid_transition`。

## 檔案
- 新：`app/admin/staging.py`（APIRouter＋頁面 router＋函式庫）、`app/admin/migrations/0010_admin_staging.sql`、`app/admin/static/staging.html|staging.js|staging.css`、`tests/test_admin_staging.py`
- 改（都在 app/admin，非禁改檔）：`server.py`（掛 router 4 行、corrections 改走 staging）、`info.py`（審稿頁存檔改走 staging；`/review/queue` 多 `staging_pending`）、`db.py`（SCHEMA_VERSION=10；遷移器允許保留空號）、`static/index.html`（導覽加「待審修正」）、`static/app.js`（審稿頁加待審區塊與核准/退回）
- 改舊測試（行為改變後的新預期，非放寬）：`test_admin_api.py`、`test_admin_info.py`、`test_ai_agent_qa.py`、`test_migrations_v3.py`

## 端點（全部在 /admin/api/v1，沿用既有 LoopbackOnly、Host/Origin、Sec-Fetch-Site、session CSRF、problem+json）
| 方法 路徑 | 角色 | 說明 |
|---|---|---|
| GET `/staging?state=&kind=&tgt_lang=&room_id=&segment_id=&cursor=&limit=` | editor | keyset（created_at DESC, id DESC）＋HMAC 簽章 cursor；limit 1–200；pending 項目帶 `conflict` |
| GET `/staging/{id}` | editor | ETag `"s{id}-r{rev}"`；含 base（送審時）、current（目前）、diff、audit |
| POST `/staging` | editor | 建立（kind tm/term）；Idempotency-Key；201＋Location＋ETag；同內容重送回 200 同一筆 |
| PATCH `/staging/{id}` | 提案者/admin | If-Match；改 tgt_text/aliases/note；`rebase:true` 重抓目標快照 |
| POST `/staging/{id}/approve` | admin | If-Match（缺→428；`*`→428；不符/弱 ETag→412）；目標變動→409 `target_changed`（可帶 `override_conflict:true`，記入稽核）；鎖定詞→409 `term_locked`（override 也不行） |
| POST `/staging/{id}/reject` | admin | If-Match＋`reason`（1–500 字） |
| POST `/staging/{id}/withdraw` | 提案者/admin | If-Match |
| POST `/staging/bulk-approve` | admin | `{items:[{id,etag}] ≤100, override_conflict}`；每筆一個 SAVEPOINT，回傳逐筆結果；Idempotency-Key |
| 頁面 GET `/admin/staging`、`/admin/staging/staging.js|.css` | 公開靜態檔（資料仍需登入） | |

輸入驗證：room_id 走既有 `validate_room_id`；詞條 zh 1–20 字且必須是繁體（用 glossary.py 的 FOLD＋tm.py 的補充表，簡體詞 422 `not_traditional`）；英文譯詞不得含中日文；aliases ≤8；英譯 TM 走 `validate_caption_en`；控制字元/`<think>` 拒絕；未知欄位 422。

詞表核准：glossary `version+1`、term `rev+1`，**不自動推送直播房間**；回應帶 `live_push.pushed=false` 與提示（用既有 `POST /rooms/{id}/glossary/push` 的 dry_run 差異＋if_room_version）。
TM 核准：`origin='approved'`、quality 5（同句其他 quality 5 降為 4，確保核准的那句被 `exact()` 先用）；`corrections.promoted_tm_id/term_id` 回填。
`events` 只記 id（schema 規定 payload 不放全文）；前後全文只在 `staging_audit`，刪段落時隨 FK CASCADE 一起清（個資）。

## 遷移
- `0010_admin_staging.sql`：`staging_items`（含部分唯一索引：同目標同時只一筆 pending）、`staging_audit`。可重複執行。
- **偏離既有遷移器**：原本要求編號連續，0010 會被拒。改為「第一個必須是 0003、之後可跳號、不可重號」，且若出現**比資料庫目前版本小卻未套用**的檔（例如之後才合進 0004）會丟 SchemaError，不會默默跳過。若整合者寧願保持連續，可把檔名改成 `0004_admin_staging.sql`、SCHEMA_VERSION=4，並還原 db.py 的 runner 改動（test_admin_staging 的 gap 測試要一起拿掉）。
- 既有 v3 DB 升級會先做 `pre-migrate-v3.sqlite3` 快照（測試有驗）。

## 頁面
後台首頁導覽多「待審修正」→ `http://127.0.0.1:8791/admin/staging`。篩選（狀態/類型/語言/每頁）、cursor 分頁（下一頁/第一頁）、差異（送審時刪除線紅、提議綠斜體）、衝突標記、明細（base/current/提議/稽核軌跡）、核准/退回(原因)/撤回、勾選批次核准（隨機 Idempotency-Key）。寫入都帶 `X-Zen-CSRF`＋`If-Match`，無 inline script（CSP）。審稿頁（#review）也加了待審區塊。

## 測試（筆電 DESKTOP-P8RGA3A，共用 venv Python 3.12）
- `cd sidecar\live-room; ..\..\..\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_admin_staging.py -q` → **19 passed, 1 skipped**（skip = 筆電 PATH 沒有 node，`node --check staging.js`；box 上有 node 時通過）
- 我改過的舊測試：`tests/test_migrations_v3.py tests/test_admin_api.py tests/test_admin_info.py tests/test_ai_agent_qa.py tests/test_backend_p2.py` → **113 passed**
- 頁面實測：臨時 uvicorn 127.0.0.1:18797（非 8645）＋暫存 DB：`/admin/staging` 200 text/html、`staging.js` 200 text/javascript、`/admin` 含連結、匿名 API 401 problem+json；已停止。
- 涵蓋：遷移（v3→v10、跳號、晚到小號拒絕、重號拒絕）、修正端點不直寫、TM/詞表 happy path＋稽核、ja、auth/角色/CSRF/Origin、428/412/*、狀態機、驗證、TM/詞表衝突 409＋override＋rebase、鎖定詞、兩執行緒/四個 HTTP 同時核准只有一個成功、Idempotency（create＋bulk）、bulk 逐筆結果、分頁同時間戳＋中途插入無漏無重、竄改 cursor 400、刪段落 cascade、頁面。
- 依 BRIEF 沒跑全套（記憶體限制）；box 先前（base 6ec5012）跑過受影響的 6 個測試檔全過。全套由整合者跑。

## 已知限制／待決
1. 管理員自己的修正也進 staging（不再「admin 直接上線」）；若柏能要 admin 一鍵自動核准，可在 corrections 加 `auto_approve` 旗標（未做）。
2. 既有後台直接編輯詞表／TM（`/glossary/terms`、`/tm/{id}` PATCH、CSV 匯入）視為管理員明確操作，**沒有**改走 staging；舊的 `glossary/proposals` 與 `tm/{id}/approve`（quality 2 舊資料）保留。
3. staging 的 ja 詞條不收讀音（reading）；核准後可在詞表頁補。
4. 遷移器改為允許跳號（見上）；要不要保留由整合者決定。
5. 在共用 repo 設定了 `extensions.worktreeConfig=true`（為了只在本 worktree 設 git user `grok-backend`）；這是 `zen-bridge-grok\.git\config` 的一行設定，不影響工作樹內容。

## BLOCKED
無。
