# 本機翻譯後台：安裝與設定（Windows）

本文件說明 `feat/local-translation-backend` 分支新增的本機功能：Ollama 本機翻譯、字幕帳本（zen.sqlite3）、翻譯記憶（TM）、Silero VAD、embedding（只在閒置或課後）、後台（127.0.0.1:8791），以及 benchmark。

**所有新功能預設關閉。** 套用 patch 之後，不設任何環境變數，直播服務的行為和原本一樣（翻譯仍走原來的 OpenAI 路徑）。請先跑 benchmark，再依結果逐項開啟。

## 1. 前置需求

- Windows 10/11，PowerShell 5.1 以上
- Python 3.12（或 App 內建的 `.python`）
- [Ollama](https://ollama.com/download)（翻譯與 embedding 用；只監聽 127.0.0.1:11434）
- git

## 2. 一次性安裝

在 `sidecar\live-room` 目錄執行：

```powershell
.\scripts\setup_local.ps1
```

它會：建立 `.venv`、安裝 `requirements-lock.txt` 與 `requirements-local.txt`（onnxruntime）、`ollama pull qwen3:4b`（Q4_K_M）和 `qwen3-embedding:0.6b`、下載並以 sha256 驗證 Silero VAD 模型、建立資料庫。**不會開啟任何功能。**

可選參數：`-SkipOllama`、`-SkipVad`、`-TranslateModel qwen3:1.7b`。

## 3. 先跑 benchmark（開功能之前）

```powershell
.\scripts\bench_local.ps1 -Check          # 安全檢查 + 列出缺少的檔案
.\scripts\bench_local.ps1                 # ASR、翻譯、embedding；每組 3 次
.\scripts\bench_local.ps1 -Layers concurrent   # 第 3 層：同時跑，即時送片（每次 10 分鐘）
```

- 第一次執行會在 `%USERPROFILE%\zen-bench\matrix.json` 建立範本，請改成你的 whisper-cli／whisper-server／Breeze 模型／音檔路徑。音檔請用自己的課堂錄音，切成 6 秒、16 kHz 單聲道 WAV，同名 `.txt` 放人工校對字幕（用來算 CER）。
- 工具只讀系統資訊，只調整自己啟動的子程序優先權；不改登錄檔、電源計畫、BIOS、驅動、Defender；不下載模型；不上傳。
- 直播中（有觀眾或佇列不為 0）或狀態不明時會中止；使用電池時會中止（除非加 `-AllowBattery`）。
- 結果：`%USERPROFILE%\zen-bench\results\<時間>\results.json`、`runs.csv`、`segments.csv`、`samples.csv`。只看 `status=OK` 的列。

依結果決定：ASR 片段 RTF p95 > 0.9 時，翻譯改用 `qwen3:1.7b` 或降低翻譯執行緒。

## 4. 開啟功能（環境變數）

用 `start_backend.ps1` 啟動時，參數只設定給這次啟動的程序，不寫入系統環境。

```powershell
# 直播服務 + 本機翻譯 + 帳本 + TM
.\scripts\start_backend.ps1 -Live -Local -Ledger -TM
# 再加 VAD 與後台
.\scripts\start_backend.ps1 -Live -Local -Ledger -TM -Vad -Admin -OpenAdmin
```

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `BREEZE_TRANSLATE_ENGINE` | `openai` | 設 `local` 才用 Ollama |
| `BREEZE_TRANSLATE_BASE_URL` | `http://127.0.0.1:11434/v1` | 只接受本機位址；非本機需另設 `BREEZE_TRANSLATE_ALLOW_REMOTE=1` |
| `BREEZE_TRANSLATE_MODEL` | `qwen3:4b` | Ollama 的 `qwen3:4b` 即 Q4_K_M |
| `BREEZE_TRANSLATE_PROTOCOL` | `auto` | 11434 埠自動用 Ollama 原生 `/api/chat`（只有它吃得到執行緒設定）；`openai` 強制 `/v1/chat/completions` |
| `BREEZE_TRANSLATE_NUM_THREAD` | `4` | 翻譯生成執行緒（留核心給 ASR）；`0` = 交給 Ollama |
| `BREEZE_TRANSLATE_NUM_CTX` | `2048` | context 長度 |
| `BREEZE_TRANSLATE_KEEP_ALIVE` | `-1` | 翻譯模型常駐 |
| `BREEZE_TRANSLATE_QUEUE` | `4` | 翻譯佇列上限（滿了丟最舊的） |
| `BREEZE_TRANSLATE_STALE_S` | `8` | 最舊待翻句等超過幾秒，且後面還有新句時… |
| `BREEZE_TRANSLATE_STALE_POLICY` | `skip` | …`skip` 跳過（標 `skipped_backlog`，中文保留）、`merge` 併入下一句一起翻、`off` 不處理 |
| `BREEZE_TRANSLATE_PRIORITY` | `below_normal` | 翻譯工作執行緒優先權低於 ASR（`idle`／`below_normal`／`normal`） |
| `ZEN_LEDGER` | `0` | `1` = 把字幕寫進 zen.sqlite3（失敗不影響字幕） |
| `ZEN_DB_PATH` | `%LOCALAPPDATA%\ZenBridge\data\zen.sqlite3` | 主資料庫 |
| `ZEN_IDENTITY_DB_PATH` | `...\data\zen-identity.sqlite3` | 個資／權杖庫（與主庫分開） |
| `ZEN_BACKUP_DIR` | `%LOCALAPPDATA%\ZenBridge\backups` | 備份位置（建議另一顆磁碟） |
| `BREEZE_TM` | `0` | `1` = 翻譯記憶：完全相符直接用、相似句當範例、鎖定詞檢查 |
| `BREEZE_TM_FUZZY_MIN` | `0.86` | 相似度門檻 |
| `BREEZE_VAD` | `off` | `silero` = 用 Silero VAD 判斷無聲（模型缺少或驗證失敗會自動退回 RMS） |
| `BREEZE_VAD_THRESHOLD` / `BREEZE_VAD_MIN_SPEECH_RATIO` | `0.5` / `0.05` | |
| `ZEN_EMBED` | `1`（後台內） | `0` 關閉 embedding |
| `ZEN_EMBED_IDLE_S` | `10` | ASR 與翻譯佇列都空超過幾秒才跑 embedding |
| `ZEN_EMBED_BATCH` | `16` | 每批句數；每批之前重新檢查是否閒置 |
| `ZEN_EMBED_NUM_THREAD` | `2` | embedding 執行緒 |
| `ZEN_ADMIN_PORT` | `8791` | 後台埠；**8645 一律拒絕** |

注意：Ollama 的推論在 Ollama 自己的程序裡，本程式無法調它的優先權；所以用 `num_thread` 限制它吃的核心數，並讓 embedding 只在閒置時跑。

## 5. 後台（127.0.0.1:8791）

```powershell
.\scripts\start_backend.ps1 -Admin -OpenAdmin
```

- 只綁 127.0.0.1；Host 與 Origin 要含正確埠號；寫入需要同源與 CSRF。
- 第一次啟動會在主控台顯示一次 CLI 權杖（檔案只存雜湊），以及 5 分鐘內有效、只能用一次的登入網址。`-OpenAdmin` 會直接用瀏覽器開啟。
- API 前綴 `/admin/api/v1`；錯誤格式為 `application/problem+json`。

## 6. 疑難排解

- 翻譯一直 `network`：確認 `ollama serve` 在跑、`ollama list` 有 `qwen3:4b`。
- VAD 沒作用：看啟動訊息是否顯示退回 RMS；重跑 `.venv\Scripts\python.exe scripts\fetch_silero_vad.py`。
- 資料庫放在 OneDrive 或網路磁碟會被拒絕（WAL 不安全），請用 `ZEN_DB_PATH` 指到本機磁碟。
- 要完全回到原本行為：不加任何參數啟動，或用 `start.bat`。
