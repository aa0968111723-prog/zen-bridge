# zen-bridge 桌面版威脅模型（THREAT_MODEL）

- 作者：資安長（ciso）；日期：2026-10-10（台北時間）
- 基準：`origin/feat/local-translation-backend` = `6ec5012`（PR #8 head）。所有行號都以此為準。
- 範圍：在 Windows 筆電（DESKTOP-P8RGA3A）上執行的 zen-bridge 桌面版，透過區域網路（LAN）用 QR 碼把字幕提供給觀眾。雲端版不在範圍內。
- 方法：依資產列出信任邊界，再對每個元件逐一走過 STRIDE（偽冒 Spoofing、竄改 Tampering、否認 Repudiation、資訊外洩 Information disclosure、阻斷服務 Denial of service、權限提升 Elevation of privilege）。每項都標示「既有控制」和「缺口」。
- 驗證等級標記：**[實測]** 表示在 box 上實際驗證過（Xeon/Linux，不是 5600H/Windows）；**[靜態]** 表示讀程式碼得出的結論；**[推估]** 表示沒有驗證。
- 本文件只是文件，沒有修改任何程式。

## 1. 資產

| # | 資產 | 位置（預設） | 敏感度 | 備註 |
|---|---|---|---|---|
| A1 | 現場音訊片段 | 主持端瀏覽器 → `POST` 上傳 → 暫存 `tmp/`；可選擇保存 `audio_path` | 高（講者聲音，屬生物特徵） | 私密暫停期間不應上傳 |
| A2 | 逐字稿／譯文 | `data/captions.sqlite3`（直播）、`%LOCALAPPDATA%\ZenBridge\data\zen.sqlite3`（帳本） | 中高（可能含學員發言或個人事例） | FTS 索引也是同一份內容的副本 |
| A3 | TM／詞彙表 | `zen.sqlite3` 的 `tm_units`、`glossary_*` | 中（屬智慧財產） | glossary 檔是 Codex 負責的，本文件只引用 |
| A4 | 個資庫 `identity_schema` | `zen-identity.sqlite3`（`user_accounts`、`api_tokens`、`speaker_identities`、`voiceprints`） | **最高**（真名、聲紋、權杖雜湊） | 和主庫分開存放，不使用 ATTACH |
| A5 | 後台權杖／session | `admin.token`（只存 sha256）、`admin_sessions`（sid 雜湊＋CSRF）、主控台只顯示一次的 CLI 權杖、一次性登入碼 | 高 | 環境變數 `ZEN_ADMIN_TOKEN` 是明文覆寫值（只供測試用） |
| A6 | 直播主持權杖 | 直播程序的記憶體；`GET /api/host-token`（只開放給 loopback） | 高（拿到就能刪除字幕、改詞彙、重新翻譯） | 每次重啟都會換 |
| A7 | 觀眾金鑰 `listen_key` | 房間設定；寫在 QR 碼和 OBS 網址的 `?k=` 裡 | 中 | 等同於「入場券」 |
| A8 | 備份 | `ZEN_BACKUP_DIR`（`zen-*.sqlite3`＋manifest，以及個資庫備份） | 等同 A2–A4 | 會保留多份 |
| A9 | 匯出／語料 | `corpus.zh-en.jsonl`、`corpus.zh-ja.jsonl`、`corpus_stats.json`、SRT／匯出檔 | 中高（是要拿去訓練的資料） | 必須先去識別化，並標明授權 |
| A10 | 模型與執行檔 | GGUF、whisper/llama-server、sherpa-onnx | 中（被換掉就等於可以在這台電腦執行任意程式碼） | 更新器負責驗證（Codex 的範圍） |
| A11 | 日誌 | 後台 stderr、`asr_tuning` 的 worker log、logfile | 中（可能夾帶權杖或內容） | |

## 2. 信任邊界與資料流

```
[觀眾手機/平板] --LAN HTTP/WS--> (B1) 直播服務 0.0.0.0:8780  <--loopback-- [主持端瀏覽器]
                                        |  \
                                        |   \--> (B4) Ollama 127.0.0.1:11434 / llama-server 127.0.0.1:8081
                                        |--> captions.sqlite3、ledger -> zen.sqlite3
[OBS 瀏覽器來源] --loopback/LAN WS--> (B5) /overlay + /ws/listen
[管理者瀏覽器/CLI] --loopback--> (B2) 後台 127.0.0.1:8791 --> zen.sqlite3、zen-identity.sqlite3（唯讀）、備份
                                        \--> (B3) 直播服務 8780（帶主持權杖）
(B6) 更新器/安裝程式（Codex）--> 網際網路（只引用，不在本文件範圍）
(X) 8645 = Hermes gateway：任何元件都不准 bind、proxy 或連線
```

| 邊界 | 說明 | 誰在邊界外 |
|---|---|---|
| B1 | LAN ↔ 直播服務（`app/run.py:66` 綁 `0.0.0.0`） | 同一個 Wi-Fi 上的任何裝置 |
| B2 | 本機其他程序或網頁 ↔ 後台 8791（只綁 loopback） | 本機的其他程式；主持人瀏覽器裡其他分頁上的網頁 |
| B3 | 後台 → 直播（loopback 加主持權杖） | — |
| B4 | 直播或後台 → 本機推論服務（Ollama 11434、llama-server） | 冒充 11434 的本機程式 |
| B5 | OBS（CEF）↔ 直播 | 被分享出去的 OBS 場景檔 |
| B6 | 更新器 ↔ 網際網路 | 供應鏈（Codex 負責，只做參考） |
| B7 | 直播服務 → 示意圖 LLM（`BREEZE_VISUAL_LLM_BASE_URL`，aitest V2，第 2 輪新增） | 設定錯誤時的遠端主機、proxy、8645 |

## 3. 各元件的 STRIDE 分析

### 3.1 後台 127.0.0.1:8791（`app/admin/`）
| STRIDE | 威脅 | 既有控制 | 缺口／殘餘風險 |
|---|---|---|---|
| S | DNS rebinding、別的埠的網頁冒充後台 | `LoopbackOnly`：對端必須是 loopback；Host 必須完全相符（包含埠號）；不接受 absolute-form；寫入時 Origin 必須完全相符加上 `Sec-Fetch-Site: same-origin`；GET `/admin/api` 會拒絕 same-site／cross-site 的請求（security.py:146-151）**[實測 d87096f＋靜態 6ec5012]** | 本機有惡意程式時，它可以直接帶 Bearer 權杖來呼叫（後台本來就把本機使用者當成可信任的） |
| S | 登入碼或權杖外洩 | 一次性 `#code`（只用一次、5 分鐘過期、放在 fragment）；權杖只存 sha256，比對用 `compare_digest`；查詢字串帶權杖一律回 400 **[實測]** | CLI 權杖和登入網址會印在 stderr（run.py），使用者如果把輸出轉存到檔案就會落地 **[靜態]** |
| T | CSRF、改寫請求方法、text/plain 表單 | 同步型 CSRF token 加上 Origin 和 SFS 檢查；method override 無效 **[實測]** | — |
| R | 管理操作沒有紀錄 | `events` 表會記下修正（admin.correction）**[靜態]** | 登入、登出、產生登入碼、匯出、備份**沒有稽核紀錄** |
| I | 日誌外洩權杖或內容 | `RedactingFilter`；基準版已經改成在 uvicorn 設定完之後掛到所有 handler 上（`install_redaction_on_handlers`，server.py:181）；遮罩規則已補上 `'token'`、`code=`、`sid=` **[靜態＋單元實測 6ec5012]**；`access_log=False` | 舊的缺陷「子 logger 沒有遮罩」（資安長.md #2）：基準版已修，但還要在 Windows 實機確認一次 **[推估]** |
| I | 個資庫被一般 API 讀到 | 個資庫是獨立檔案，沒有 ATTACH，只有 `_identity_lookup` 用唯讀連線讀 `api_tokens`；FTS、ledger、匯出都不會碰 **[實測 d87096f]** | 主庫和個資庫在 POSIX 上預設仍是 **0644**（基準版只限縮了備份和還原檔）**[實測 6ec5012]**；在 Windows 上靠 `%LOCALAPPDATA%` 繼承來的 ACL **[推估]** |
| D | SSE 連線耗盡、登入被封鎖 | SSE 每一輪都重新驗證身分（登出後送出 `logout` 事件並結束），而且有每個身分的連線數上限（server.py:890-915）**[靜態]** | 登入和寫入的速率限制仍然是**全域 key**（server.py:169-170,325,443）：本機任一程式送 10 次錯誤登入，就能讓正確的登入碼也被擋 60 秒 **[實測 d87096f；6ec5012 靜態未變]** |
| D | FTS 查詢導致 500 | phrase 引號跳脫；`q` 含控制字元時回 422（server.py:608）**[單元實測 6ec5012]** | — |
| E | 低權限權杖拿來寫入 | `need(role)`；`read` scope 一律降成 viewer **[實測]** | session 只有滑動式期限，沒有絕對壽命上限 **[靜態]** |

### 3.2 直播服務 0.0.0.0:8780（LAN）
| STRIDE | 威脅 | 既有控制 | 缺口／殘餘風險 |
|---|---|---|---|
| S | LAN 上的裝置冒充主持端 | `/api/host-token` 只發給 loopback，而且 Host 必須在白名單內（auth.py:137-150）；主持 API 需要 Bearer 加 Origin **[靜態]** | 本機任何程序都能 GET `/api/host-token` 拿到主持權杖 **[靜態]** |
| I | Wi-Fi 被竊聽 | 只在 LAN 上用 | **HTTP／WS 沒有加密**：同一個網段的人可以收看字幕，也能看到 `listen_key`。要保密的場次應該改用隔離網段或熱點，或加上 TLS **[推估]** |
| I | 觀眾端拿到不該看的欄位 | 觀眾廣播只送白名單欄位（`dispatch._LISTENER_PASS`）**[靜態]** | — |
| D | 觀眾連線把服務塞爆 | `max_listeners` 預設 40，上傳大小有上限 **[靜態]** | OBS 疊加層也佔一個觀眾席位（fullstack README） |
| T | 竄改字幕 | 刪除和修正都需要主持權杖 **[靜態]** | — |

### 3.3 推論服務（Ollama 11434、llama-server、X-ASR）與 8645 封鎖
| STRIDE | 威脅 | 既有控制 | 缺口／殘餘風險 |
|---|---|---|---|
| S/T | 冒充 11434 的程式回 302/307 把請求導到 8645，或回傳惡意譯文 | `app/net.py`：`safe_opener()` 不跟轉址、loopback 不用 proxy；`translate_config.validate_base_url` 會檢查 `RESERVED_PORTS`（含沒寫埠時的 80／443）；後台的 Ollama 探測、embed、translate、mt_backend 全都改用 safe_opener **[靜態 6ec5012]**；`validate_caption` 會過濾輸出 **[靜態]** | 舊缺陷（資安長.md #1）基準版已修。剩下 `scripts/bench/zbench.http_json` 預設還是用 `urlopen`（tools/t4.py 用它來做健康檢查），屬低風險 |
| I | 字幕被送到雲端 | 只允許 loopback；要連遠端得另外設 `BREEZE_TRANSLATE_ALLOW_REMOTE=1` | 誤設環境變數的風險，靠部署檢查清單控管 |
| E | 原生 worker 被換成惡意版本 | 執行檔、模型路徑固定；subprocess 用串列參數 | 檔案完整性由更新器負責（B6，Codex） |

### 3.4 資料庫、備份、匯出
| STRIDE | 威脅 | 既有控制 | 缺口／殘餘風險 |
|---|---|---|---|
| I | 備份或還原檔被其他帳號讀走 | 基準版的 `backup_set`、`restore_from` 會呼叫 `_restrict`（db.py:432-436,605）**[靜態]** | 主庫和個資庫本體仍是 0644（見 3.1）；restore_drill 的演練副本留在工作資料夾，沒有清掉（REVIEW 第 1 輪 backup P2-3） |
| I | 語料匯出裡出現 PII | corpus_export 會拒絕讀個資庫、不 JOIN speakers、遮蔽 email／電話／身分證字號，輸出 0600（dba patch）| 發言裡提到的人名，只有在提供 `--names-file` 時才會遮蔽 **[靜態]** |
| T | 用惡意備份做還原 | 會跑 `integrity_check`、FTS 檢查和 manifest 的 SHA256 | restore_drill 拼接表名時沒有跳脫，manifest 檔名也沒有限制在備份資料夾內（只是唯讀，屬 P2） |
| D | WAL 一直長大 | `wal_autocheckpoint`、`journal_size_limit` | 看 dbtest 的量測結果（尚未交件） |

### 3.5 OBS 疊加層與 SSE
- 疊加層（fullstack，尚未接線）：字幕只用 `textContent` 寫入，參數都有夾限，CSP 是 `default-src 'none'`。缺口：`connect-src ws: wss:` 太寬；`?k=` 會留在 OBS 的場景檔裡。
- 後台的 SSE：只推全域指標，不帶個人資料；跨源請求沒有 CORS，讀不到內容 **[實測 d87096f]**。

### 3.7 第 2 輪新增元件（基準 `grok/integrate` = `9743ce8`，2026-10-11 補充）
- **示意圖 V2（`app/visual.py`、`app/visual_routes.py`，aitest）**：會把定稿字幕送到 OpenAI 相容的 LLM，再把產生的示意圖廣播出去。預設是關閉的，要設定 `BREEZE_VISUAL_LLM_*` 才會開。
  - I（資訊外洩）：`GET /api/visual/{room_id}` **沒有驗證**就回傳示意圖歷史（內容來自字幕），但 `/ws/visual` 需要 Origin 和聽眾金鑰。8780 綁在 LAN 上，預設房名又是 `class`，同網段的人不用金鑰就能讀到 **[實測，box probe]**。
  - I／S（B7 出口）：`is_loopback_url()` 用 `host.startswith("127.")` 判斷，所以 `127.evil.example` 這種網域名稱也會通過；沒有擋 8645；用的是一般的 `urllib.request.urlopen`，會讀 proxy 環境變數或登錄檔、也會跟轉址，沒有沿用 `app.net.safe_opener()` **[實測：判斷函式；靜態：opener]**。設定錯誤時，字幕可能離開這台筆電，或被送到 8645。
  - D（阻斷服務）：`POST /api/visual/{room}/trigger` 只檢查來源是不是 loopback，沒有 CSRF 或主持權杖。本機瀏覽器裡任何網頁都能用跨站 POST 反覆觸發 LLM（`force=1` 可以略過冷卻）**[靜態]**。
  - 既有控制：模型輸出只用 `textContent` 顯示；每位觀看者的佇列有上限；`validate_room_id`；`strip_think`／`is_presentable`。
- **草稿字幕 `/ws/draft`（round4 #5）**：第一個 frame 必須帶主持權杖（`same_secret`），會檢查 Origin，每個封包最多 32000 bytes，私密暫停時不會發布。缺口：先 `accept` 才驗證，不過有 5 秒逾時（P2）**[靜態]**。
- **`/api/hw/status`（perf）**：用 `require_host` 保護，而且是唯讀的；`apply_profile` 目前沒有接線。如果之後接上，會用 ctypes 呼叫 OpenProcess/SetPriorityClass，而且程式寫死拒絕 HIGH/REALTIME、絕不改電源計畫、有 `revert()` **[靜態]**。
- **後台 staging（backend）**：所有端點都透過既有的 `need(role)`（session、Origin、Sec-Fetch-Site、CSRF、寫入速率限制）；核准需要 admin 加上 If-Match；`staging_audit` 會記錄改前和改後。這一塊**部分**補上了 §5 第 6 點的稽核缺口（只涵蓋 TM 和詞彙的核准，登入和匯出仍然沒有稽核） **[靜態＋測試]**。
- **`tools/smoke_desktop.py`（testlead）**：只在自己找的 loopback 空埠啟動子程序，指令用串列參數，拒絕 8645，會清掉 `*_API_KEY` 環境變數，不印出權杖 **[靜態]**。

### 3.6 更新器、安裝程式（Codex 負責，只做參考）
- 下載必須驗證 SHA256 或簽章、不跟到其他網域的轉址、不能用來降級。這部分不在本文件範圍，請參考 Codex 的文件。

## 4. 舊審查缺陷的現況（`/workspace/zen-qa/資安長.md`，d87096f → 6ec5012）

| # | 缺陷 | 6ec5012 現況 | 怎麼確認的 |
|---|---|---|---|
| 1 | 8645 封鎖沒有檢查埠號、會跟著轉址 | **已修**（app/net.py、translate_config 加上 RESERVED_PORTS 檢查） | 靜態 |
| 2 | 日誌遮罩沒有涵蓋子 logger | **已修**（install_redaction_on_handlers） | 靜態 |
| 3 | redact 規則有漏洞 | **已修** | 單元實測 |
| 4 | NUL 造成 500 | **已修**（422） | 單元實測 |
| 5 | room_id 沒有跳脫 | **已修**（validate_room_id 加 quote） | 靜態 |
| 6 | 登出後 SSE 還在 | **已修**（每一輪重新驗證，有連線上限） | 靜態 |
| 7 | 速率限制用全域 key | **未修** | 靜態 |
| 8 | DB 權限 0644 | **部分修**（備份和還原檔已限縮，主庫和個資庫沒有） | 實測（umask 022） |
| 9 | GET 沒有 Fetch Metadata 檢查 | **已修** | 靜態 |
| 10 | session 沒有絕對壽命上限 | **未修** | 靜態 |

## 5. 殘餘風險（接受或待處理）
1. LAN 上走明文 HTTP：字幕和 `listen_key` 可以被同一網段的人看到。目前以「場地網路可信，或用隔離熱點」為前提接受。
2. 本機惡意程式：後台和直播都把 loopback 上的使用者視為可信任，惡意程式可以拿到主持權杖，或直接讀取資料庫。只能靠 Windows 帳號隔離和防毒軟體。
3. 速率限制可以被本機程式拿來阻斷服務（P2）。
4. 主庫和個資庫的檔案 ACL 只靠目錄繼承（P2）；資料目錄如果放到 `C:\` 底下會變成所有使用者都能讀。
5. 語料裡可能殘留人名（P2）。
6. 沒有管理稽核紀錄（P2）。

## 6. 建議處理順序
| 優先 | 項目 | 負責 | 驗收方式 |
|---|---|---|---|
| P1 | 主庫和個資庫建立或遷移後一律呼叫 `restrict_file`（含 `-wal`、`-shm`），資料目錄設成 0700 或只給本人的 ACL | backend／dba | 在 umask 022 下建立後，權限是 0600；Windows 上 `icacls` 只列出目前使用者 |
| P1 | corpus_export 預設強制要有 `--names-file`，或在輸出前由人工審閱，並在 stats 標明「人名未遮蔽」 | dba | 測試：沒有 names-file 時拒絕執行或明確警告 |
| P2 | 速率限制改成依 sid 雜湊或權杖雜湊計數，只計算失敗次數 | backend | 10 次錯誤嘗試之後，另一組正確的登入碼仍然能登入 |
| P2 | session 加上絕對壽命上限（例如 24 小時），Cookie Max-Age 保持一致 | backend | 用 fake clock 的測試 |
| P2 | 登入、登出、login-code、匯出、備份、還原都寫入 `events` 稽核紀錄（不含權杖） | backend | 測試確認有寫入，而且內容不含機密 |
| P2 | 疊加層的 CSP 改成 `connect-src 'self'`；說明文件提醒不要分享 OBS 場景檔 | fullstack | 檢查標頭的測試 |
| P2 | restore_drill 的表名要跳脫、manifest 檔名要限制在備份資料夾內、提供 `--cleanup` | backup | 用含 `"` 的表名、`../` 的 manifest 測試 |
| P2 | cicd 的 checkout 加上 `persist-credentials: false` | cicd | 檢查 yml |
| P2 | `zbench.http_json` 改用 `safe_opener` | 執行手 | 轉址測試 |
| P1 | `GET /api/visual/{room_id}` 套用和 `/ws/visual` 相同的 Origin 與聽眾金鑰檢查（或拿掉 history） | aitest／整合者 | 沒帶 `k` 回 403；帶正確的 `k` 回 200 |
| P1 | 示意圖 LLM 的 URL 改用 `ipaddress` 判斷 loopback（不接受網域名稱，只接受 `localhost`），拒絕 `RESERVED_PORTS`，請求改走 `app.net.safe_opener()` | aitest | 測試：`127.evil.example`、`127.0.0.1:8645`、302 轉址、`HTTP_PROXY` 都要被拒絕或不生效 |
| P2 | `POST /api/visual/{room}/trigger` 改用主持權杖（`require_host`）當 guard | 整合者 | 沒帶權杖回 401 |
| 規劃 | LAN 加密方案（自簽 TLS 或隔離熱點）評估 | arch | 文件 |

## 7. 已知限制
- 所有實測都在 box（Linux、Xeon）上做，沒有在 Windows 5600H 上驗證過 ACL、CEF 或 Defender 的行為。
- perf、backend、aitest、testlead 已在第 2 輪補進 §3.7（基準 `9743ce8`）；§1–§6 的其他行號仍以 6ec5012 為準。
- 更新器和安裝程式屬於 Codex 的範圍，本文件只做參考。
