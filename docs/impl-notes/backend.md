# backend：後台 staging 核准流程（round3 §5-1）NOTES

- 工作區：`C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\backend`，分支 `impl/backend`
- base：`grok/integrate` @ `bea2e5c3cac2ab7d3f35bdaa25cf2702c1b8894d`（含 6ec5012）
- commits（由舊到新）：
  1. `be273bdfe7812a2a857883a48328035f9ba69e92`：staging 核准流程、0010、審核頁
  2. `e03c06d`：第一版 NOTES
  3. `d9acf265e6ed99859bbb83e955fec4aba6179c9f`：管理員自我核准開關（0011）
  4. `8f50dc8d1d272f819c9abc1803bc2eb054433049`：日文詞條讀音 reading（0012）
  5. 本 NOTES 更新（在 8f50dc8 之後）
- 合併方式：`git merge impl/backend`

## 做了什麼
人工修正（`POST /admin/api/v1/corrections`、審稿頁 `PATCH /segments/{seg}/text`）**不再直接寫 TM 或詞表**。字幕修正本身照舊會成為新的人工版本（那是字幕，不是 TM）；TM 和 `propose_term` 只會各建立一筆 `staging_items`（狀態 pending）。管理員核准時，在**同一個 SQLite 交易（BEGIN IMMEDIATE）**裡寫入 `tm_units` 或 `glossary_terms`、更新項目狀態，並寫一筆 `staging_audit`（誰、何時、改前、改後、模式）。

狀態機：`pending → approved | rejected（必填原因） | withdrawn（提案者或管理員） | superseded（同一目標出現新提案）`。pending 狀態下可以編輯（rev+1）。其他轉換一律回 409 `invalid_transition`。

## 端點（都在 /admin/api/v1；沿用既有 LoopbackOnly、Host/Origin、Sec-Fetch-Site、session CSRF、problem+json）
| 方法與路徑 | 角色 | 說明 |
|---|---|---|
| GET `/staging?state=&kind=&tgt_lang=&room_id=&segment_id=&cursor=&limit=` | editor | keyset 分頁（created_at DESC, id DESC），cursor 有 HMAC 簽章；limit 1–200；pending 項目會帶 `conflict` |
| GET `/staging/{id}` | editor | ETag 格式 `"s{id}-r{rev}"`；內容含 base（送審時）、current（目前）、diff、audit（含 mode） |
| POST `/staging` | editor | 建立項目（kind 為 tm 或 term；ja 詞條可帶 `reading`）；支援 Idempotency-Key；回 201＋Location＋ETag；同內容重送回 200 與原本那筆 |
| PATCH `/staging/{id}` | 提案者或 admin | 需要 If-Match；可改 tgt_text、aliases、note、reading；`rebase:true` 會重抓目標快照 |
| POST `/staging/{id}/approve` | admin | 沒帶 If-Match 或用 `*` → 428；ETag 不符或用弱 ETag → 412；目標已變動 → 409 `target_changed`（可帶 `override_conflict:true`，會記成 mode=override）；鎖定詞 → 409 `term_locked`（override 也不能繞過） |
| POST `/staging/{id}/reject` | admin | 需要 If-Match 和 `reason`（1–500 字） |
| POST `/staging/{id}/withdraw` | 提案者或 admin | 需要 If-Match |
| POST `/staging/bulk-approve` | admin | 最多 100 筆；每筆在自己的 SAVEPOINT 裡處理，逐筆回結果；支援 Idempotency-Key |
| 頁面 GET `/admin/staging`、`/admin/staging/staging.js`、`/admin/staging/staging.css` | 靜態檔公開，資料仍需登入 | |

## 遷移（遷移器允許跳號、不可重號；SCHEMA_VERSION = 12）
- `0010_admin_staging.sql`：建立 `staging_items`（部分唯一索引保證同一目標同時只有一筆 pending）與 `staging_audit`。
- `0011_staging_self_approve.sql`：新增 `staging_items.approval_mode`、`staging_audit.mode`，值域為 `manual`、`override`、`self_auto`。
- `0012_staging_reading.sql`：新增 `staging_items.reading`。DB 層 CHECK 規定只有 kind='term' 且 tgt_lang='ja' 才能有值，長度 1–80。
- 決策 1 是「保留跳號規則、SCHEMA_VERSION 10」。規則照舊沒改；但 SCHEMA_VERSION 依遷移器的定義必須等於最高的遷移號，所以加了 0011 和 0012 之後必然變成 12（否則遷移器不會套用它們）。這是新遷移帶來的結果，不是改規則。
- 遷移器的行為：第一個檔必須是 0003，之後可以跳號，但不可重號。如果出現**編號比資料庫目前版本小、卻從沒套用過**的檔，會丟 SchemaError，不會默默跳過。

## 管理員自我核准開關（決策 2）
- 名稱：環境變數 `ZEN_ADMIN_SELF_APPROVE`。**預設關**，只有值剛好是 `"1"` 才開（`true`、`yes` 都算關）。測試或嵌入時可用 `create_admin_app(..., staging_self_approve=True/False)` 覆寫；目前狀態可從 `app.state.staging_self_approve` 看到。
- 開啟時：只有 **admin 或 owner 本人新建立**的修正項目（corrections 端點與審稿頁存檔）會在**同一個交易**裡用 SAVEPOINT 自動核准。檢查和人工核准完全相同：狀態、ETag、目標衝突、鎖定詞、輸入驗證。自我核准**不會**用 override 繞過衝突。
- 稽核：`staging_audit` 照樣寫 create 和 approve 兩筆（含改前與改後全文），approve 那筆 `mode='self_auto'`；`staging_items.approval_mode='self_auto'`，`decision_note` 會註明是開關觸發；`events` 的 `audit.staging.approve` payload 也帶 `mode:"self_auto"`（只放 id，不放全文）。頁面明細會顯示「本人自動核准」。
- 不會自動核准的情況：
  - editor 的修正；
  - 與他人既有提案內容相同（沒有新建項目）；
  - 合併句（全站 D4，一律要人看）。
- 自動核准失敗時（例如鎖定詞）：只回滾這次核准，項目維持 pending 等人工審，修正本身照常存檔。回應 `auto_approved.{tm|term}` 會標 `ok:false` 和錯誤碼。
- 範圍：只套用在修正端點。`POST /staging` 手動建立的項目不會自動核准。

## 日文讀音 reading（決策 4）
- 欄位 `reading`，選填，只對 **kind=term 且 tgt_lang=ja** 有意義。
- 驗證：先做 NFKC 正規化（半形片假名轉全形）並把連續空白壓成一個，再要求 1–80 字，而且只能用平假名、片假名、長音ー、中點・、疊字符ゝゞヽヾ和空白。
- 我的選擇：en 詞條和 TM 帶了 reading 一律回 **422 `invalid_reading`，不默默忽略**，避免呼叫端以為讀音已經存了。
- 寫入：詞表 schema 已有讀音欄位（`admin_term_meta.reading`，0003 的 trigger 會同步到 `glossary_term_targets.reading`），所以核准時直接寫進去。項目沒帶 reading 時，保留詞條原本的讀音。鎖定詞如果帶了不同的讀音 → 409 `term_locked`。
- TM 沒有 ruby 欄位，句子層級的讀音也沒有意義，所以 TM 不收 reading，不需要整合者另外接線。
- 頁面：日文詞條會顯示讀音差異（舊讀音紅色刪除線、新讀音綠色）；「編輯」按鈕可以改譯詞和讀音（PATCH 加 If-Match）。修正端點的 `propose_term` 也可以帶 `tgt_lang:"ja"`、`target`、`reading`。

## 檔案
- 新增：
  - `app/admin/staging.py`
  - `app/admin/migrations/0010_admin_staging.sql`、`0011_staging_self_approve.sql`、`0012_staging_reading.sql`
  - `app/admin/static/staging.html`、`staging.js`、`staging.css`
  - `tests/test_admin_staging.py`
- 修改（都在 app/admin，不是禁改檔）：
  - `server.py`：掛 router；corrections 端點改走 staging；加開關參數
  - `info.py`：審稿頁存檔改走 staging；`/review/queue` 多回傳 `staging_pending`
  - `db.py`：SCHEMA_VERSION 與遷移器
  - `static/index.html`：導覽列加「待審修正」
  - `static/app.js`：審稿頁加待審區塊
- 掛載：router 在 `create_admin_app` 裡以 `zstaging.register(app, ctx)` 掛上（app/admin 內部完成），**整合者不用另外接線**。

## 4 個舊測試檔改了哪些斷言（都是預定的行為改變，不是放寬條件）
以下行號以 commit 8f50dc8 為準。原因代號：**S** = 修正改走 staging（round3 §5-1）；**V** = 遷移號跳到 0010～0012，所以 SCHEMA_VERSION 變成 12；**G** = 遷移器允許跳號。

### tests/test_admin_api.py
| 行 | 舊 | 新 | 原因與嚴格度 |
|---|---|---|---|
| 309 | 測試名 `..._feeds_tm` | `..._stages_tm`，並加 docstring | S，只是改名 |
| 320 後（322–328 新增） | 斷言 `SELECT tgt_text FROM tm_units` = 修正文 | 先斷言 `tm_units` 有 **0** 筆、`glossary_terms` 裡「因緣」有 **0** 筆，接著用 ETag 核准 staging 必須回 200，最後**原本的 tgt_text 斷言原樣保留** | S。原斷言還在，前面多了「核准前不得寫入」和「核准成功」兩道檢查，條件比原來多 |
| 334–337 | 用 corrections 的 `propose_term` 產生舊式 glossary proposal | 改用 `feedback.propose_term()` 直接產生同一種 proposal | S。只有測試前置資料的來源改了（corrections 已不會產生舊式 proposal）；後面的 428、412、200、412 斷言**一行都沒改** |
其他斷言沒改（Idempotency 的 201、重放、422、corrections 筆數 = 1 都照舊）。

### tests/test_admin_info.py
| 行 | 舊 | 新 | 原因與嚴格度 |
|---|---|---|---|
| 164–167 | `review/queue` 的 `tm_pending` 長度 = 1，內容是 "The Dharma" | `tm_pending == []`（核准前不得進 TM），而且 `staging_pending` 長度 = 1、內容是 "The Dharma"、kind 為 tm | S。項目數與內容比對一樣，還多一條「TM 裡不得有東西」 |
| 188 → 190–195 | admin 存檔後 `tm_pending is False`（直接上線） | `tm_pending is True`、`tm_id is None`、live TM **為空**；再用 ETag 核准必須回 200；**原本的 live TM 斷言（下一行）原樣保留** | S（決策：admin 也要經過核准；開關預設關）。上線結果還是要驗，多了「核准前不上線」和「核准成功」 |
| 261–262 → 268–273 | editor 存檔後查 `status=pending` 有 1 筆 | 查 `status=pending` 必須是 **0** 筆；核准 staging 必須回 200；再查 `status=live`（後續 provenance、PATCH 428/422/200、disable 403/200、404、400 等斷言**全部沒改**） | S。舊流程是存檔先寫成 quality 2 的待審 TM，新流程是核准後才寫成 quality 5 的 live TM，後面所有檢查照舊 |

### tests/test_ai_agent_qa.py
| 行 | 舊 | 新 | 原因與嚴格度 |
|---|---|---|---|
| 138 → 138–141 | `tm_id = r.json()["tm_id"]`（修正後立刻有 TM） | 斷言 `tm_id is None`；用 ETag 核准 staging 後，從 `target_id` 取得 tm_id；後面 `/tm/{id}/reject → served False`、`approve → served True`、`<think>` 回 400/422 **都沒改** | S。多了「修正當下不寫 TM」的斷言，角色和審查的檢查照舊 |

### tests/test_migrations_v3.py
| 行 | 舊 | 新 | 原因與嚴格度 |
|---|---|---|---|
| 40 | `migrate(p) == 3 == SCHEMA_VERSION` | `migrate(p) == SCHEMA_VERSION == 12` | V。仍是**精確等於**（寫死 12）。v3 的回填、快照、checksum 斷言都沒改 |
| 64 | `migrate(p) == 3` | `== db.SCHEMA_VERSION` | V。第 40 行已把它釘死在 12，所以這行等於精確 12 |
| 73 | `migrate(p) == 3` | `== db.SCHEMA_VERSION` | V，同上 |
| 147 | `user_version = 9`（當時 9 > 3，代表「比程式新」） | `user_version = SCHEMA_VERSION + 1`（= 13） | V/G。原本的 9 現在比 12 小，已經不算「比程式新」，這個測試會失去意義；改成 +1 才能維持原意，仍然要求丟 SchemaError 並提到 pre-migrate |
| 178–179、183 | `"v3" in out`、`db_version == 3` | `f"v{SCHEMA_VERSION}"`、`== SCHEMA_VERSION`（等於 12） | V。仍是精確版本比對 |
- `test_gap_in_step_numbers_and_edited_step_refused` **沒改**，而且仍然通過：只有 0004 的資料夾仍被拒絕（第一個必須是 0003）；改 checksum 仍被拒絕。跳號、晚到的小號、重號另有新測試 `test_runner_allows_reserved_gap_but_refuses_late_lower_number`。
- 結論：沒有任何一條舊斷言被刪掉或改成較寬的比較（全部仍是 `==`、`is`、精確計數）。凡是因行為改變而必須改的地方，都**另外加了「核准前不得寫入」的反向斷言**。

## 測試（筆電 DESKTOP-P8RGA3A，共用 venv，Python 3.12）
- 指令：`cd sidecar\live-room` 後執行 `<共用 venv>\Scripts\python.exe -m pytest tests/test_admin_staging.py tests/test_admin_api.py tests/test_admin_info.py tests/test_ai_agent_qa.py tests/test_migrations_v3.py -q`
- 各 commit 的結果：
  - d9acf26：133 passed、1 skipped
  - 8f50dc8：**136 passed、1 skipped**；其中只跑 `test_admin_staging.py` 是 26 passed、1 skipped
- 那 1 個 skip：筆電 PATH 沒有 node，所以跳過 `node --check staging.js`。box 有 node，那邊 27 個全過。
- 頁面實測（be273bd 時）：臨時 uvicorn 開在 127.0.0.1:18797（不是 8645）並用暫存 DB。`/admin/staging` 回 200 text/html，`staging.js` 回 200，`/admin` 頁面有連結，匿名呼叫 API 回 401 problem+json；測完已停止。
- 依 BRIEF，全套測試沒有跑，交給整合者。
- `test_admin_staging.py` 涵蓋：
  - 遷移：v3→v12、跳號、晚到小號、重號
  - 修正端點不直接寫 TM 或詞表
  - TM、詞表、ja 的 happy path 與稽核
  - auth、CSRF、Origin
  - 428、412、`*`
  - 狀態機與輸入驗證
  - 衝突 409、override、rebase、鎖定詞
  - 同時核准只有一個成功（兩個執行緒、四個 HTTP 請求）
  - Idempotency 與 bulk 逐筆結果
  - 分頁：同時間戳、中途插入、竄改 cursor
  - 刪除段落時 cascade
  - 頁面
  - 自我核准：預設關、開啟後的稽核、editor 不自動核准、鎖定詞回滾、只限本人
  - reading：驗證規則、DB CHECK、核准後寫入 admin_term_meta 並同步 targets、PATCH 編輯、鎖定詞

## 頁面
後台導覽列的「待審修正」連到 `http://127.0.0.1:8791/admin/staging`。功能：
- 篩選與 cursor 分頁
- 差異顯示（舊內容紅色刪除線、新內容綠色斜體），ja 詞條另顯示讀音差異
- 衝突標記
- 明細：base、current、提議、稽核（含模式）
- 核准、退回、撤回、編輯
- 勾選後批次核准

所有寫入都帶 `X-Zen-CSRF` 和 `If-Match`；頁面沒有 inline script（符合 CSP）。

## 已知限制
1. 既有的後台直接編輯詞表或 TM（`/glossary/terms`、`/tm/{id}` PATCH、CSV 匯入）維持在 staging 之外（決策 3）。
2. 共用 repo 加了一行設定 `extensions.worktreeConfig=true`，用途是只在本 worktree 設定 git user `grok-backend`。
3. 筆電 `%TEMP%` 留下 `grok-backend-*.patch` 和 `grok-backend-pagecheck.py`，依「不刪檔」規則沒有刪。

## BLOCKED
無。
