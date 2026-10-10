# 筆電桌面版即時字幕（中→英／中→日）最終驗收清單

- 版本：v1.1（2026-10-11 00:05，Asia/Taipei；v1 為 2026-10-10 23:15），技術總監（代號 `cto`）撰寫
- 基準：v1 寫於 `6ec5012`（VERSION 0.4.3）；v1.1 依筆電 `grok/integrate` @ `9743ce8` 更新路徑、測試名稱與行號。路徑都相對於 `sidecar/live-room/`。
- 「（交件新增）」標記：v1.1 已確認在 `grok/integrate` @ 9743ce8 存在的檔案改標「（9743ce8 已合入）」；仍未合入的保留「（交件新增）」。
- 目標機：DESKTOP-P8RGA3A，Ryzen 5 5600H（6C12T）、15.4 GB RAM、Radeon 內顯，無 NVIDIA，Windows，全部本機執行。每場只選一種目標語（en **或** ja）。
- 前一版：`/workspace/zen-qa/技術總監.md`（七節驗收標準、CTO-01～22）。缺陷現況見 `/workspace/zen-sandbox/out/grok-local-v2/DEFECTS.md` §C。

## 0. 使用方式與規則

**欄位定義**
- **編號**：`節-序號`。
- **門檻**：可以量測的通過條件。
- **對應檔案**：要檢查的檔案。標「（交件新增）」的檔案目前基準還沒有，要等該代號交件。
- **測試／指令**：怎麼量。
- **量測環境**：
  - `box 假元件`：box 上用 fake ASR／MT 跑。
  - `box 靜態`：讀程式或文件。
  - `box 量測（Xeon，非 5600H）`：box 上用真模型量，數字不能套用到筆電。
  - `柏能實機`：只能在筆電量，由柏能統一執行，其他代號不得對筆電操作。
- **負責**：代號。`exec`＝柏能代理執行手（pipeline／server／draft_asr／mt_backend／rtf_check 的擁有者），`codex`＝Codex（安裝器／更新器）。
- **優先度**：P0＝不過就不能交給使用者；P1＝正式長期使用前要過；P2＝排進維護。

**判定**：每項只有 PASS／FAIL／UNKNOWN／BLOCKED。UNKNOWN 和 BLOCKED 都不算通過。**P0 全部 PASS** 才能宣告「桌面版可交付」。

**數字來源標記**
- 〔研究〕：`/workspace/zen-research/*.md`，附章節。
- 〔repo〕：repo 內檔案加行號（v1.1 起以 grok/integrate @ 9743ce8 為準）。
- 〔box〕：box 量測（Xeon，非 5600H）。
- 〔建議〕：本文件提出的門檻，沒有外部來源，要柏能確認。
- 沒有來源的數字一律寫 UNKNOWN。

**共通規則**：所有交件必須通過 `/workspace/zen-impl/cto/GATE.md` 的閘門程序。不得碰禁碰檔案，不得新增網路呼叫或付費模型，embedding 維持關閉。

---

## 1. 測試門檻（T）

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| T-01 | 全套 0 failed；skipped 數不得比基準多；每個 skip 都有分類 | `tests/` | 筆電：`cd sidecar\live-room; C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests -q -rs`（全套由整合者跑；請在 `sidecar/live-room` 下執行，基準有 cwd 相依測試） | box 假元件 | 整合者 | P0 |
| T-02 | 每件交件**新增**至少 1 個測試檔或測試函式，而且套用後全套 0 failed（BRIEF 完成定義） | 各交件 | GATE.md 第 4、5 步 | box 假元件 | 各代號 | P0 |
| T-03 | Windows＋Python 3.11 與 3.12 全套 0 failed | `.github/workflows/live-room-windows-tests.yml`（交件新增） | GitHub Actions `windows-latest` matrix；筆電 `.\.venv\Scripts\python.exe -m pytest -q tests` | CI＋柏能實機 | cicd | P0 |
| T-04 | Node ≥ 22，前端 `.mjs` 套件實際執行，不得因版本不符而 skip（CTO-11） | `tests/test_node_suites.py` | CI 設 Node 22，`pytest tests/test_node_suites.py -rs` 的 skip 數 = 0 | CI | cicd | P1 |
| T-05 | 測試環境和 `requirements-lock.txt` 版本差異為 0（CTO-05） | `requirements-lock.txt`、`requirements-dev.txt` | CI 用全新 venv 安裝鎖檔後再跑；加一步比對 `importlib.metadata` 和鎖檔 | CI | cicd | P1 |
| T-06 | Actions 全部用 40 位 commit SHA 釘選；新 workflow 不改既有 workflow | `.github/workflows/live-room-windows-tests.yml`（交件新增） | `rg -n "uses: .*@" <yml>`，每一行都要是 40 位十六進位；`git diff --name-only` 不含其他 workflow | box 靜態 | cicd | P1 |
| T-07 | 桌面流程煙霧測試：啟動後端→建場次→送假音訊→收字幕→翻譯→暫停→恢復→匯出，全部步驟成功，box 上 ≤ 60 s〔建議〕 | `tools/smoke_desktop.py`、`tests/test_smoke_desktop.py`（9743ce8 已合入） | `python tools/smoke_desktop.py --fake --lang en`；`pytest tests/test_smoke_desktop.py`；實機 `--real --lang en --audio <wav>` | box 假元件；Windows 由柏能實機 | testlead | P0 |
| T-08 | 煙霧測試不連外網、不用 8645、結束後子程序全部清掉（測試後沒有殘留 python／whisper 程序） | 同上 | 測試裡檢查 `psutil` 子程序數＝0、監聽埠清單 | box 假元件 | testlead | P1 |

## 2. 真實收音（R）——全部柏能實機

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| R-01 | 使用者課堂錄音 6 s／16 kHz 單聲道 ≥ 50 段，附人工校對稿；Breeze 定稿 CER 不比現況差超過 1 個百分點〔研究 round4 §3.3 選擇規則〕；絕對 CER 門檻 UNKNOWN（要等第一次量測後再訂） | `tools/rtf_check.py`、`tools/t4.py`、`tools/t4.example.json`（9743ce8 已合入）、`scripts/bench/zbench.py` | `python tools/rtf_check.py t4 --plan tools/t4.example.json --audio <dir> --ref <dir> --approved`；`pytest tests/test_t4.py`（假元件） | 柏能實機 | exec | P0 |
| R-02 | 草稿 ASR（X-ASR 480 ms int8，`num_threads=1`）同一段跑 3 次輸出完全相同〔研究 round4 §2.3，D1〕；不穩定就改 fp32 | `app/draft_asr.py`（XAsrDraft） | `python tools/rtf_check.py det --dir <x-asr 資料夾> --threads 1,2 --audio <dir>`；`pytest tests/test_draft_xasr.py`（假元件） | 柏能實機（box 結果不能套用：box 有 AMX/VNNI，5600H 只有 AVX2） | exec／arch | P0 |
| R-03 | 100 分鐘連續場次：沒有積壓暫停；`pending` ≤ 2、`oldest_wait_ms` ≤ 15000、`rejected` = 0、`missing` ≤ 5〔repo `docs/DEVICE-ACCEPTANCE.md:43`〕 | `scripts/device_check.py` | `python scripts/device_check.py watch`，加錄影 | 柏能實機 | testlead | P0 |
| R-04 | 私密暫停：按下後到恢復＋1 s 之間收的音一律不進字幕、也不進資料庫；觀眾端顯示暫停狀態 | `app/server.py:1504-1548`（`/api/rooms/{room_id}/pause`）、`tests/audience_private_pause.test.mjs` | `pytest tests/test_private_pause.py`（box）＋實機錄影 | box 假元件＋柏能實機 | exec | P0 |
| R-05 | ja 場次：20 句 zh→ja 由日本同學依評分表評分，平均分數門檻 UNKNOWN（要柏能和評分者先訂）；照抄原文或沒有假名的句數 = 0〔研究 round3 §4.3、round4 D8〕 | `app/mt_backend.py:162-189`（`ja_copy_of_source`／ja 驗證） | 評分表加 `pytest tests/test_mt_round3.py` | 柏能實機（人工） | aitest／exec | P1 |
| R-06 | Silero VAD：人工標成語音的段落被判 silent ≤ 1%〔建議〕 | `app/vad.py` | 從後台 metrics 取 `segments_silent`，和人工標註比對 | 柏能實機 | exec | P1 |
| R-07 | MT 掛掉時中文照常，恢復後 ≤ 1 分鐘新句子有譯文〔repo `docs/DEVICE-ACCEPTANCE.md:145`〕 | `app/mt_backend.py` | 手動停掉 llama-server，再啟動 | 柏能實機 | exec | P1 |

## 3. 效能 SLO（P）

延遲分段命名依〔研究 round4 §3.2〕：A1 等片結束、A2 上傳、A3 排隊、A4 解碼＋VAD、A5 Breeze ASR、A6 MT、A7 推送＋繪製、B1 草稿第一字、B2 草稿 RTF。

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| P-01 | **Breeze 定稿 RTF p95 < 0.9**〔repo `tools/rtf_check.py:34` `P95_LIMIT = 0.9`〕。現況 RTF ≈ 6.7–8.0〔研究 round4 D5，引 README〕；同級 Ryzen 4750U 公開數字 CPU RTFx 0.96〔研究 optimization-round2 §0-1〕 | `app/asr_tuning.py`、`app/native_worker.py` | `python tools/rtf_check.py pace --audio <課堂錄音>.wav`；`t4` 子命令 | 柏能實機 | exec／perf | P0 |
| P-02 | **中文定稿字幕延遲**（段尾到手機）**p50 ≤ 10 s、p95 ≤ 15 s、max ≤ 20 s**；首末 10 分鐘的 p50 差 ≤ 3 s〔repo `docs/DEVICE-ACCEPTANCE.md:52`〕。預算估計平均 ≈ 6.5 s（RTF 0.5 時）〔研究 round4 §3.2 加總，估計〕 | `app/pipeline.py`、`scripts/device_check.py` | `device_check.py listen`＋錄影對時 | 柏能實機 | exec／testlead | P0 |
| P-03 | **譯文延遲**（中文出現到譯文補上）**p95 ≤ 10 s**〔repo `docs/DEVICE-ACCEPTANCE.md:109`〕 | `app/pipeline.py`、`app/mt_backend.py` | 同上；zbench 第 3 層 `en_p95_ms` | 柏能實機 | exec | P0 |
| P-04 | **草稿第一字 B1 p95 < 2.5 s**。box 中位數 1.2–2.1 s〔box；研究 round4 §2.1，含音檔開頭靜音〕 | `app/draft_asr.py` | t4 草稿層 `first_partial_p50/p95` | 柏能實機 | exec／arch | P1 |
| P-05 | **草稿 RTF B2**：Breeze 同時在跑時 p95 < 0.5〔研究 round4 §3.2〕。box int8 threads=1 為 0.087〔box〕 | `app/draft_asr.py` | t4 同時跑層 | 柏能實機 | exec | P1 |
| P-06 | **MT 單句 A6**：en p95 < 1.5 s、ja p95 < 2 s〔研究 round4 §3.2〕。box Hy-MT2 Q4_K_M en p50 0.93／p95 1.26 s，ja 1.31／1.50 s〔box，round4 §2.4〕 | `app/mt_backend.py` | t4 MT 層；`round4/mt_bench.py` | 柏能實機 | exec | P0 |
| P-07 | 排隊 A3 p95 < 1 s，任何時候 < 6 s（超過代表 RTF > 1，會越積越多）〔研究 round4 §3.2〕 | `app/pipeline.py`（`oldest_wait_ms`） | 每 5 s 讀 `/api/metrics` | 柏能實機 | exec | P0 |
| P-08 | A2 上傳 p95 < 0.3 s、A4 解碼＋VAD p95 < 0.15 s、A7 推送＋繪製 p95 < 0.5 s〔研究 round4 §3.2〕 | `app/latency.py`（9743ce8 已合入）、`app/pipeline.py`、`app/server.py` | `/api/metrics` 已有 A2–A6 p50／p95（`app/latency.py` STAGES）；**A7 推送＋繪製仍無量測點**（見 M-03）；`pytest tests/test_stage_latency.py` | 柏能實機 | exec | P1 |
| P-09 | 記憶體：Breeze＋草稿＋MT＋瀏覽器同時跑時系統最低可用記憶體 ≥ 2048 MB〔建議〕；live-room RSS 第 50→100 分鐘增加 < 100 MB〔repo `docs/DEVICE-ACCEPTANCE.md:87`〕 | `scripts/bench/zbench.py`（`min_avail_mb`、`peak_wset_mb`） | zbench 第 3 層＋`device_check.py watch` | 柏能實機 | perf | P1 |
| P-10 | CPU：直播中系統 CPU 30 s 平均 ≤ 85%〔repo `README.md:91` 已有 85% 條，標尚未驗證〕；ASR 執行緒數加 MT 執行緒數加草稿執行緒數 ≤ 12 邏輯核心 | `app/hw_tune.py`（9743ce8 已合入）、`docs/ARCHITECTURE_LIVE.zh-TW.md`（交件新增） | zbench `samples.csv` | 柏能實機 | perf／arch | P1 |
| P-11 | hw_tune：ASR 程序 ABOVE_NORMAL、MT BELOW_NORMAL、EcoQoS 關閉、親和遮罩讓 ASR 和 MT 不重疊；沒插電時降級並寫入 log；**只改本程式自己啟動的子程序**，不改系統電源計畫（hardware.md §6.1 安全規則） | `app/hw_tune.py`、`app/hw_routes.py`（`GET /api/hw/status`，9743ce8 已合入） | `pytest tests/test_hw_tune.py tests/test_hw_routes.py`（mock ctypes／psutil）；筆電上用 `Get-Process` 確認優先權 | box 假元件＋柏能實機 | perf | P1 |
| P-12 | 降頻偵測：長時間負載時出現事件 37 或 RTF 惡化 > 15%，log 要有記錄〔repo `scripts/bench/zbench.py:53` `throttle_drop: 0.15`〕 | `scripts/bench/zbench.py` | zbench 第 3 層 10 分鐘 | 柏能實機 | perf | P2 |
| P-13 | 架構文件寫出 5600H 的核心配置表（ASR／草稿／MT／UI 各用哪幾個邏輯核心）和至少 3 級降級策略（例如 MT Q8→Q4→只出草稿），每級有觸發條件和可量測的恢復條件 | `docs/ARCHITECTURE_LIVE.zh-TW.md`（交件新增） | 文件審查；觸發條件要對得到 `/api/metrics` 欄位 | box 靜態 | arch | P1 |

## 4. 安全（S）

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| S-01 | 後台只綁 127.0.0.1／::1，拒絕 8645；不信任 proxy 標頭 | `app/admin/run.py`、`app/admin/security.py` | `pytest tests/test_admin_api.py -k "bind or 8645"`；筆電 `netstat -ano \| findstr 8791` | box 假元件＋柏能實機 | ciso | P0 |
| S-02 | 所有出站設定（MT、草稿、示意圖 LLM、overlay）都只能連 loopback，而且拒絕 8645（CTO-14） | `app/translate_config.py`、`app/mt_backend.py`、`app/visual_routes.py`（9743ce8 已合入） | 單元測試：`validate_base_url('http://127.0.0.1:8645/v1')` 要丟出例外 | box 假元件 | ciso／exec | P1 |
| S-03 | 交件**沒有新增網路呼叫**：新程式碼不得出現 `requests`、`httpx`、`urllib.request`、`socket.create_connection`、`aiohttp`、`websockets.connect` 指向非 loopback 的位址 | 各交件 | GATE.md 第 6 步 `rg` 規則 | box 靜態 | cto | P0 |
| S-04 | 交件**沒有付費模型**：不得出現 `api.openai.com`、`anthropic`、`generativelanguage`、`OPENAI_API_KEY` 的新用法 | 各交件 | GATE.md 第 6 步 | box 靜態 | cto | P0 |
| S-05 | 金鑰、權杖、cookie、登入碼不出現在任何 log（含子 logger、uvicorn.error、traceback）（CTO-04） | `app/logfile.py`、`app/admin/security.py` | `pytest tests/test_security_ciso.py tests/test_logfile.py` | box 假元件 | ciso | P0 |
| S-06 | git 裡沒有秘密：`git grep -E 'sk-[A-Za-z0-9]{20,}\|ghp_\|BEGIN .*PRIVATE KEY'` 0 筆；`.env` 被 ignore | 全 repo | GATE.md 第 6 步 | box 靜態 | ciso | P0 |
| S-07 | 威脅模型涵蓋：LAN 上的觀眾頁（8780 綁 0.0.0.0）、後台、OBS overlay、示意圖頻道、語料匯出；每個威脅都有對應的控制和測試 | `docs/THREAT_MODEL.zh-TW.md`（交件新增） | 文件審查 | box 靜態 | ciso | P1 |
| S-08 | ciso 審查：`/workspace/zen-impl/*/` 每件交件都有 PASS／FAIL 和理由 | `/workspace/zen-impl/ciso/REVIEW.md` | 和 GATE.md 交叉比對代號清單 | box 靜態 | ciso | P1 |
| S-09 | 語料匯出不含說話者姓名、帳號、identity DB 欄位；有授權和來源旗標；沒有授權的列不輸出 | `app/admin/corpus_export.py`（9743ce8 已合入） | `pytest tests/test_corpus_export.py`：放入假姓名，輸出要 grep 不到 | box 假元件 | dba／ciso | P0 |
| S-10 | staging 核准：人工修正只寫 staging；只有 admin（或以上）核准才寫入 TM／詞彙；editor 核准回 403；核准和退回都有稽核紀錄 | `app/admin/staging.py`、`app/admin/migrations/0010_admin_staging.sql`、`0011_staging_self_approve.sql`、`0012_staging_reading.sql`（9743ce8 已合入） | `pytest tests/test_admin_staging.py`；自核開關 `ZEN_ADMIN_SELF_APPROVE` 預設關閉也要驗 | box 假元件 | backend | P0 |
| S-11 | 新的 APIRouter（overlay、visual、staging、perf 頁）沿用 loopback／Host／CSRF 防護；觀眾可讀端點不得洩漏 host token 或 listen key | `app/overlay_routes.py`、`app/visual_routes.py`、`app/admin/staging.py`、`app/admin/perf_routes.py`（9743ce8 已合入） | 各自的測試要有「錯誤 Host → 403」「寫入沒有 CSRF → 403」 | box 假元件 | fullstack／aitest／backend | P1 |
| S-12 | 相依完整性：鎖檔帶 hash；新交件不得新增未鎖定的相依（CTO-06） | `requirements-*.txt` | `grep -c hash=sha256`；GATE.md 檢查 diff 有沒有改 requirements | box 靜態 | cicd／codex | P1 |
| S-13 | 前端新頁（overlay、perf 頁）不載入外部 JS 或字型 CDN；符合 CSP `script-src 'self'` | `app/static/overlay.html`、`overlay.js`、`app/static/visual.html`、`visual.js`、`app/admin/static/perf*.*`、`staging.*`（9743ce8 已合入） | `rg -n "https?://" <新 html/js/css>` 0 筆（註解除外） | box 靜態 | fullstack／uiux-a | P1 |

## 5. 備份還原（B）

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| B-01 | 自動備份 `zen.sqlite3`、`zen-identity.sqlite3`、`captions.sqlite3`；預設每 24 h〔repo `app/desktop_maint.py:53` `ZEN_BACKUP_INTERVAL_H` 預設 24.0〕；RPO ≤ 24 h〔建議〕 | `app/desktop_maint.py`、`app/admin/db.py` | `pytest tests/test_backup_restore.py tests/test_desktop_maint.py` | box 假元件 | backup | P0 |
| B-02 | 還原演練工具：VACUUM INTO 備份→integrity 與 FTS 驗證→還原到暫存→每個表筆數相同→輸出報告；失敗回非 0 結束碼 | `tools/restore_drill.py`（交件新增） | `pytest tests/test_restore_drill.py`；`python tools/restore_drill.py --data-dir <tmp>` | box 假元件 | backup | P0 |
| B-03 | RTO ≤ 30 分鐘（從決定還原到後台 health 正常）〔建議〕；有繁中操作說明 | `tools/restore_drill.py`、操作說明（交件新增） | 筆電實際計時一次 | 柏能實機 | backup | P1 |
| B-04 | 至少一份在不同磁碟：`ZEN_BACKUP_DIR` 和主庫在同一顆磁碟時，health 要顯示 `same_disk`（DEFECTS CTO-09 ff1647c） | `app/admin/server.py`（health） | `pytest tests/test_backup_restore.py -k health` | box 假元件；實際磁碟要柏能實機 | backup | P1 |
| B-05 | rollback 和清理程序不得刪除備份（CTO-13） | Codex 交接文件 | 文件審查 | box 靜態 | codex | P1 |
| B-06 | 2 小時講座寫入負載（每 2–4 s 一段＋翻譯＋metrics）：0 次 `database is locked`；寫入鎖等待 p95 < 50 ms〔建議〕；checkpoint 後 WAL 回到 < 4 MB〔建議；DBA 審查實測 checkpoint 可把 4,132,392 bytes 清到 0（`database-review.md` §5）〕 | `tests/test_sqlite_live_load.py`、`tests/live_load_sim.py`（9743ce8 已合入） | `pytest tests/test_sqlite_live_load.py`（時間壓縮）；NOTES 附 WAL 曲線 | box 量測（Xeon，非 5600H） | dbtest | P1 |
| B-07 | 每月一次還原演練，有紀錄 | 演練紀錄檔 | 檔案存在，日期在 31 天內 | 柏能實機 | backup | P2 |

## 6. 文件（D）

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| D-01 | 每件交件都有 NOTES.md，內容含：做了什麼、base commit、patch SHA256、測試指令與 passed／failed、已知限制、BLOCKED 段（若有）；需要接線的要寫掛在哪裡 | `/workspace/zen-impl/<代號>/NOTES.md` | GATE.md 第 8 步 | box 靜態 | 各代號 | P0 |
| D-02 | 安裝文件第三人可以照做：乾淨的 Windows PS 5.1 一次完成 | `docs/INSTALL.md`、`docs/LOCAL_SETUP.md` | 筆電照做並逐步記錄 | 柏能實機 | codex／cto | P0 |
| D-03 | 觀眾端無障礙規格：一般字幕文字對比 ≥ 4.5:1、大字 ≥ 3:1（WCAG 2.x SC 1.4.3）；ja 斷行規則（禁則）與 ruby；深色模式；手機橫式和直式都不溢出 | `docs/AUDIENCE_UX.zh-TW.md`、`app/static/room_a11y.css`（9743ce8 已合入） | `pytest tests/test_room_a11y_css.py`；文件審查；對比用工具計算 CSS 色碼；`node` 測試或 Playwright 截圖（若有） | box 靜態 | uiux-b | P1 |
| D-04 | 架構文件：兩段式 ASR（草稿＋定稿）、MT、示意圖、metrics 的時序圖；資源分配；和 perf、draft_asr 的審查意見 | `docs/ARCHITECTURE_LIVE.zh-TW.md`（交件新增） | 文件審查（同 P-13） | box 靜態 | arch | P1 |
| D-05 | 故障排除至少 8 類：MT 斷線、VAD 退回 RMS、DB 位置被拒、RTF 積壓、8791 被占用、登入碼過期或 CLI 權杖遺失、SQLite 版本、記憶體不足（CTO-19） | `docs/LOCAL_SETUP.md` | 文件逐項勾選 | box 靜態 | cto／exec | P2 |
| D-06 | 文件和程式一致：抽查 10 個環境變數的預設值，0 處矛盾 | `docs/*.md`、`app/*.py` | 抽查 | box 靜態 | cto | P2 |
| D-07 | 本驗收清單每次交件後更新判定欄，寫在 GATE.md | 本文件、GATE.md | — | box 靜態 | cto | P1 |

## 7. 監控（M）

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| M-01 | 直播服務 `/api/health`：就緒回 200，未就緒回 503 | `app/server.py:1411-1416` | `pytest tests/test_readiness.py` | box 假元件 | exec | P1 |
| M-02 | 後台 health：DB 錯誤或必要依賴掛掉時 HTTP 回 503 或 `ok=false`（CTO-09，現況部分修） | `app/admin/server.py:477-478` | 新測試：probe 回 down → `ok=false` | box 假元件 | backend | P1 |
| M-03 | `/api/metrics` 提供 A2–A7、B1 的 p50／p95（round4 §3.2）；欄位命名寫進文件。9743ce8 現況：A2–A6 已有（`app/latency.py`），A7、B1 尚無 | `app/latency.py`、`app/pipeline.py`、`app/rtf.py` | `pytest tests/test_stage_latency.py`；A7／B1 仍需新測試 | box 假元件 | exec | P0 |
| M-04 | 後台「即時效能」頁：顯示 A2–A6 的 p50／p95 條形圖和 RTF 卡；沒有外部 JS 函式庫；端點還沒有時用假資料 schema，並在 NOTES 註明欄位對照 | `app/admin/static/perf.html`、`perf.js`、`perf_logic.js`、`perf.css`、`perf_fixture.json`、`app/admin/perf_routes.py`（`/admin/perf`，9743ce8 已合入） | `pytest tests/test_admin_perf_page.py`；`node --test tests/admin_perf.test.mjs`；`rg "https?://"` 0 筆 | box 假元件 | uiux-a | P1 |
| M-05 | 指標歷史保留 30 天，rollup 正確（DEFECTS CTO-08 未修項目） | `app/admin/observability.py`、`app/admin/retention.py` | `pytest tests/test_admin_info.py tests/test_retention_purge.py` | box 假元件 | backend | P1 |
| M-06 | 持久化 log：`%LOCALAPPDATA%\ZenBridge\logs\*.log`，5 MB × 5 輪替，已遮罩〔repo `app/logfile.py:3`〕 | `app/logfile.py` | `pytest tests/test_logfile.py` | box 假元件；路徑要柏能實機確認 | exec | P1 |
| M-07 | 告警：RTF p95 ≥ 0.9 持續 60 s、MT 掛掉、備份超過 26 h、磁碟剩不到 5 GB、`rejected` > 0，任一發生時主持頁或後台要顯示，並留紀錄〔建議〕 | 主持頁、後台 | 故障注入測試 | box 假元件 | uiux-a／backend | P1 |

## 8. 交件功能驗收（F）——依 BRIEF 分工

| 編號 | 門檻 | 對應檔案 | 測試／指令 | 量測環境 | 負責 | 優先度 |
|---|---|---|---|---|---|---|
| F-perf | 第 3 節 P-10／P-11 全過；給出 5600H 6C12T 的建議 thread 分配表，每個數字都標來源；非 Windows 時所有函式安全地什麼都不做 | `app/hw_tune.py`、`tests/test_hw_tune.py`、`tests/test_hw_routes.py`（9743ce8 已合入） | `pytest tests/test_hw_tune.py tests/test_hw_routes.py` | box 假元件 | perf | P1 |
| F-backend | S-10 全過；migration 編號 ≥ 0010，用既有遷移器格式，checksum 通過；升級和重跑都是冪等的 | `app/admin/staging.py`、`app/admin/migrations/0010_admin_staging.sql`～`0012_staging_reading.sql`（9743ce8 已合入） | `pytest tests/test_admin_staging.py tests/test_migrations_v3.py` | box 假元件 | backend | P0 |
| F-dba | S-09 全過；zh-en 和 zh-ja 分開輸出；完全相同的句對去重；JSONL 每列有 `src,tgt,lang,origin,license,session_id`；另外輸出統計檔（句數、去重數、去個資數） | `app/admin/corpus_export.py`（9743ce8 已合入） | `pytest tests/test_corpus_export.py` | box 假元件 | dba | P1 |
| F-dbtest | B-06 全過；NOTES 寫出瓶頸和建議（數字標 box 量測） | `tests/test_sqlite_live_load.py`（9743ce8 已合入） | 同 B-06 | box 量測（Xeon，非 5600H） | dbtest | P1 |
| F-backup | B-02、B-03 全過 | `tools/restore_drill.py`（交件新增） | 同 B-02 | box 假元件 | backup | P0 |
| F-fullstack | OBS overlay：背景透明；只顯示最新 2 行；en／ja 字型分開；URL 參數可以控制字級、行數、語言，不合法的參數回預設值；斷線自動重連；不需要 host token | `app/static/overlay.html`、`overlay.js`、`app/overlay_routes.py`（9743ce8 已合入，整合者已接線） | `pytest tests/test_overlay_routes.py`；`node --test tests/overlay.test.mjs` | box 假元件；OBS 實際顯示要柏能實機 | fullstack | P1 |
| F-uiux-a | M-04 全過；鍵盤可以操作、有 aria-label | `app/admin/static/perf*.*`（9743ce8 已合入） | 同 M-04 | box 假元件 | uiux-a | P2 |
| F-uiux-b | D-03 全過；CSS 只新增檔案，不改 `room.html`（禁碰），在 NOTES 說明要怎麼掛上 | `app/static/room_a11y.css`、`docs/AUDIENCE_UX.zh-TW.md`（9743ce8 已合入） | `pytest tests/test_room_a11y_css.py`；文件審查＋GATE 禁碰檢查 | box 靜態 | uiux-b | P2 |
| F-aitest | 示意圖 V2：獨立廣播頻道（不混進字幕事件）；觸發要有冷卻（同一主題 N 秒內不重送）和去重；用假 LLM 測試；LLM 只能連 loopback；預設關閉 | `app/visual_routes.py`、`app/visual.py`、`app/static/visual.*`（9743ce8 已合入） | `pytest tests/test_visual_routes.py` | box 假元件 | aitest | P2 |
| F-testlead | T-07、T-08 全過 | `tools/smoke_desktop.py`、`tests/test_smoke_desktop.py`（9743ce8 已合入） | 同 T-07 | box 假元件＋柏能實機 | testlead | P0 |
| F-ciso | S-07、S-08 全過 | `REVIEW.md`、`docs/THREAT_MODEL.zh-TW.md` | 文件審查 | box 靜態 | ciso | P1 |
| F-cicd | T-03～T-06 全過；不改既有 workflow（`.github/workflows/desktop.yml` 禁碰） | `.github/workflows/live-room-windows-tests.yml`（交件新增） | `actionlint`（若有）；GATE 禁碰檢查 | box 靜態＋CI | cicd | P1 |
| F-arch | P-13、D-04 全過；附 perf 和 draft_asr 的審查意見（每條有檔案和行號） | `docs/ARCHITECTURE_LIVE.zh-TW.md`（交件新增） | 文件審查 | box 靜態 | arch | P1 |
| F-cto | 本清單和 GATE.md 每批都更新；閘門結果要可重現（寫出指令） | 本文件、`/workspace/zen-impl/cto/GATE.md` | — | box 靜態 | cto | P1 |

## 9. 項目統計

| 節 | P0 | P1 | P2 | 小計 |
|---|---|---|---|---|
| 1 測試 T | 4 | 4 | 0 | 8 |
| 2 真實收音 R | 4 | 3 | 0 | 7 |
| 3 效能 P | 5 | 7 | 1 | 13 |
| 4 安全 S | 7 | 6 | 0 | 13 |
| 5 備份 B | 2 | 4 | 1 | 7 |
| 6 文件 D | 2 | 3 | 2 | 7 |
| 7 監控 M | 1 | 6 | 0 | 7 |
| 8 交件 F | 3 | 8 | 3 | 14 |
| **合計** | **28** | **41** | **7** | **76** |

> 統計核對方式：`grep -cE '^\| [TRPSBDMF]-' docs/ACCEPTANCE.zh-TW.md`，再依最後一欄計數。
