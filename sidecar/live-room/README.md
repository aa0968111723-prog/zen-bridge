# 禪譯 Zen Bridge 本機引擎與 Windows App

此目錄是 `breeze-live-room` 匯入的本機 runtime。主要 UI 與維護倉庫為 [Zen Bridge](https://github.com/aa0968111723-prog/zen-bridge)，線上工作台為 https://vexlark.co 。UI 修改位置在主倉庫 `app/workspace.tsx`；此目錄負責本機模型、配對通道與桌面安裝。

## 安裝與使用

從 [Zen Bridge 正式 Releases](https://github.com/aa0968111723-prog/zen-bridge/releases/latest) 下載 `Zen-Bridge-Setup.exe` 並安裝。App 包含獨立 Python、CPU 辨識模型與必要工具，完成後開啟「禪譯 Zen Bridge」。首次主持配對需要管理員提供連線碼；公開安裝包不包含連線碼或 API 金鑰。

App 顯示 Zen Bridge 主工作台。建立或選擇社課，選擇麥克風與語言，再開始收音。聽眾沿用 Zen 的 `/r/{id}` 與手機 QR。選單「檢查更新」讀取 Zen Bridge 的正式安裝檔並核對 SHA256，保留 `.env` 與既有資料。關閉 App 會結束本機模型與服務。

設定為 `ZEN_BRIDGE_UI_URL`、`ZEN_BRIDGE_AGENT_URL`、`ZEN_BRIDGE_AGENT_TOKEN`。配對使用 WSS、Bearer 認證及隨 App 更新的 certifi CA，並驗證憑證和網域。

## 執行限制與維護

辨識使用同一個常駐本機模型。收音轉為 PCM16／mono／16kHz，片段約 6 秒且不重疊；積壓會暫停收音並顯示提示，暫停發言不加入字幕。Ryzen 5 5600H 主持機實測 6 秒公開音訊約需 40–48 秒，尚不符合無缺段持續即時字幕。真實中文、麥克風與長時間課堂仍需驗收。

雲端 relay 會在記憶體轉送音訊給主持機，本機音訊暫存於辨識後移除；Breeze 模式沒有自動雲端 ASR fallback。文字翻譯沿用 Zen 的 Hermes／既有 provider 與資料庫。

安裝器：`desktop/setup.iss`；App：`desktop/Launcher.cs`；配對：`app/zen_agent.py`；模型：`app/native_asr.py` 和 `app/native_worker.py`。CI、部署與正式發布由主倉庫根目錄 `.github/workflows` 維護；此 subtree 內的舊 workflow 只供來源追蹤。

本機診斷仍可使用 `doctor.bat`、`verify.bat` 與以下原引擎文件。下面涉及 ZIP／區網主持頁的操作是保留的獨立引擎診斷流程；日常使用採上面的 Zen Bridge App 流程。

## 安裝包與更新

正式安裝包有兩種，不要裝進同一個資料夾。App 用 `Breeze-Live-Room-Setup.exe`。原始碼包在 [GitHub Releases](https://github.com/aa0968111723-prog/breeze-live-room/releases)：下載 ZIP、解壓縮後雙擊 `install.bat`；第一次安裝自動準備 Python、套件、模型與工具，並建立啟動／更新／診斷桌面捷徑。

桌面捷徑的檢查、結束代碼與 `BREEZE_SKIP_SHORTCUT`／`BREEZE_REQUIRE_SHORTCUT` 見 [安裝說明](docs/INSTALL.md#安裝)。

關閉字幕程式後雙擊 `update.bat` 更新；`rollback.bat` 回復前版。更新保留 `.env` 與字幕資料，先核對發布包 SHA256、準備新環境並備份。維護者推送版本標籤後，GitHub 自動測試 Windows 一鍵安裝與真實辨識，通過才發布。尚未建立正式 Release 時更新器會明確提示。完整流程見 [長期維護說明](docs/MAINTENANCE.md)。

## 中文與英譯

沒有 API 金鑰即可使用本機中文字幕。英譯需自行在 `.env` 填 `OPENAI_API_KEY`，中文辨識不改走雲端。安裝器不覆蓋既有 `.env` 或逐字稿。

新安裝預設 `BREEZE_ASR=native`，模型留在同一個本機程序。App 視窗也會強制使用這個模式。失敗會提示，不會偷偷改成 CLI 或雲端。`BREEZE_ASR=resident` 與 `BREEZE_ASR=cli` 仍可使用；cli 每段會重新載入模型。

新安裝以 SQLite 保存文字（`BREEZE_DATA_PATH=data/captions.sqlite3`），不保存錄音。啟用儲存後，匯出讀取資料庫中保存期限內的完整內容，重開服務後仍可匯出，不只最近 200 段。預設保留 24 小時，重要場次請及時匯出；可用 `BREEZE_CAPTION_TTL` 調整秒數。既有配置保留原值，未啟用儲存時只有近期字幕。

## 修改房間術語表

主持頁的文字框一行寫一條，格式是 `標準詞=英文`，別名寫成 `標準詞|別名=英文`。這裡最多 40 條，而且只能改文字框可以原樣寫回的詞。詞表裡如果有備註、分類、未鎖定的詞，或文字框無法原樣表示的內容，文字框只能看、不能改。

要改那些欄位，請找負責詞表的人。這是技術操作，不是主持頁上的按鈕。在主持機用主持權杖呼叫：

`PUT /api/rooms/<房間代碼>/glossary`

請求要帶 `Authorization: Bearer <主持權杖>` 和 `Content-Type: application/json`。先 `GET` 同一個網址，讀出現在的 `version`。`if_version` 必填，必須是整數，而且要等於那個版本；不合就回 409，詞不會被蓋掉。`terms` 是陣列，最多 200 條。每一條：

| 欄位 | 說明 |
| --- | --- |
| zh | 繁體標準詞，1 到 20 字 |
| en | 英文，1 到 80 字 |
| aliases | 別名陣列，可省略。最多 8 個，每個最多 20 字 |
| lock | 是否鎖定。省略視為鎖定 |
| category | 分類，可空，最多 20 字 |
| note | 備註，可空，最多 80 字 |

```json
{
  "if_version": 1,
  "terms": [
    {"zh": "禪學社", "en": "Zen Club", "aliases": ["柴學社"], "lock": true, "category": "", "note": ""}
  ]
}
```

成功回 200，內容含新的 `version` 與 `terms`。

## 速度門檻

`verify.bat` 使用公開英文樣本執行兩次本機辨識，顯示即時率 RTF：辨識時間 ÷ 音訊時間。RTF 小於 1 才有可能承受持續語音，仍須以自己的中文語音及完整流程驗收。

主持頁在收音狀態下面單獨一行顯示辨識速度。狀態詞在前面（跟得上、接近上限、跟不上），一般與最慢是這一場最近最多 200 段的中位數與第 95 百分位，句子裡的段數是實際樣本數，不是上限。低於 0.9 才跟得上。這一場還沒有樣本時顯示「開始聽之後才有數字」，不會沿用上一場或整台服務的數字。讀不到 `/api/metrics` 時，原本的狀態詞和顏色留著，並加上「（數字暫停更新）」。

`/api/metrics`（與其他主持操作相同，需要主持權杖）仍提供原本的佇列欄位。`last_process_ms` 是最後一段從開始處理到辨識回來的時間，**含**等待辨識空位，也不是 RTF 的分子。`process_ms` 只算解碼加辨識，不含等待，也不含靜音掃描。主持頁「上次辨識」顯示 `last_process_ms`，「純辨識」顯示 `process_ms`。另外提供最近 200 段的 `asr_rtf_p50`、`asr_rtf_p95`、`decode_ms`、`asr_wait_ms`（等空位），以及每個房間本場的辨識毫秒、音訊毫秒、RTF、解碼、等待的 p50、p95、最大值。百分位有快取，只有新樣本才重算；主持頁仍每 2 秒讀一次。

計數單位是**每一次嘗試**，不是每一段音訊。同一段若先逾時、重送之後拿到空白，會同時算進 `asr_timeouts` 和 `asr_empty`。同一次嘗試裡，逾時、辨識錯誤、空白、靜音門檻不會互相重複計。`asr_timeouts` 與 `asr_errors` 不進入速度樣本。太安靜而沒送辨識的是 `silent_skipped`。有跑辨識但沒有字的是 `asr_empty`：命令列模式仍留下速度樣本（辨識確實跑完）；常駐模式回傳空白只算 `asr_empty`，不當錯誤，也不寫速度樣本。兩個房間交替送段不會互相清掉本場統計；同一房間換新的一場才重算該房。全靜音的新場次也會換掉該房上一場，避免舊的速度繼續被拿來判定。

`backlog_audio_s` 維持原本的定義：解碼後用真實 WAVE 時長計入，正在辨識的那一段也算，辨識結束才清掉。沒有這類音訊時這個值是 0。`backlog_s` 是另一個欄位：上傳當下先用段長估計，解出 WAVE 後改為真實時長，該段一開始辨識就清掉（不含正在辨識）。各房分開，`backlog_by_room` 跟著 `backlog_s`。`asr_active_s` 是目前正在辨識的最久一段已經跑了幾秒，沒有正在辨識時是 0，一開始辨識就是正值（不足一毫秒也不會顯示成 0）；`asr_active_by_room` 各房分開。音訊長度用解碼後的 WAVE 時長，不用檔案大小去猜。統計有上限，100 分鐘場次不會讓這份記憶體跟著段數一直長。實機「積壓尖峰 < 12 秒」看 `backlog_audio_s`，不看 `backlog_s`。

在主持機上看這台電腦的 p95 是否小於 0.9：

```text
python tools/rtf_check.py metrics --base http://127.0.0.1:8780
python tools/rtf_check.py metrics --base http://127.0.0.1:8780 --room class
python tools/rtf_check.py run --n 4 --audio 你的樣本.wav
python tools/rtf_check.py pace --audio 你的樣本.wav
python tools/rtf_check.py matrix --audio 你的樣本.wav
```

`metrics` 讀已經在跑的服務。判定用所有房間本場 RTF p95 的最大值；`--room` 只看那一間。`run` 是整檔重複的冒煙，不切段、不按真實節奏。`pace` 切成 6 秒段再依真實節奏送。`matrix` 用同一套切段比較 threads 4、6、8 與 workers 1、2，挑同時滿足 RTF p95 < 0.9 且積壓 < 12 秒的最小組合。這裡的積壓是這個工具自己算的排隊音訊，不是 `/api/metrics` 的 `backlog_audio_s`。這台沒有 psutil，CPU 顯示 UNKNOWN，85% 那一條仍是尚未驗證。這些指令用目前的本機辨識設定，不上傳雲端，也不改辨識預設。門檻不過時結束碼是 1。輸出會註明實機結果仍是尚未驗證：在真正的主持機跑過之前，不要把數字當成驗收通過。

若兩段上傳／辨識都未完成，畫面會顯示「處理積壓，已暫停產生新錄音」。這段等待沒有新的錄音，請暫停說話；不要把這個降級行為當成無缺段的正式即時服務。上傳失敗也會顯示紀錄。

## 測試與限制

本輪 Linux／Python 3.12 已通過 44 項 Python 回歸及兩組 Node 測試；實際載入固定 Breeze q5 權重，以同一個常駐程序完成兩次辨識並關閉。

這台 Linux 環境辨識 6 秒英文樣本約需 14.5～15 秒，不能承受該樣本的連續音訊。這不是 Ryzen 5 5600H／Windows 主持機的測量，也不是中文準確度測試。

Windows workflow 增加 Python 3.11／3.12、錄音與觀眾端測試，以及含中文／空白路徑的安裝與真實推論檢查。執行結果以該提交的 GitHub Actions 為準；不先宣稱通過。

目前未完成真實麥克風、30 分鐘中文、2 小時場次、3 台手機及真金鑰英譯驗收。瀏覽器整合在本輪環境受阻，Chromium 下載回傳無效檔案。正式使用門檻見 [驗收清單](docs/RELEASE-CHECKLIST.md)。實機項目與門檻見 [實機驗收清單](docs/DEVICE-ACCEPTANCE.md)，每一項都還是尚未驗證。

## 實機輔助檢查

在主持機本機（才能拿到 loopback 權杖）對已啟動的服務執行 `scripts/device_check.py`。它不會印出權杖。`watch` 每隔數秒把佇列深度、拒絕、缺段與記憶體寫進 CSV／JSON，其中 `backlog_audio_s` 是積壓尖峰（已解碼、含正在辨識，門檻仍是 12 秒），`backlog_s` 只是尚未開始辨識的秒數，`asr_active_s` 是正在辨識已經跑了幾秒（沒有正在辨識時是 0）；`export` 下載該房的 JSON／SRT，並對編號、時間格式、單調、重疊、結束晚於開始、seq 是否重複、缺號各印 PASS／FAIL；`listen` 以聽眾身份連上 `/ws/listen`，記錄每則字幕的到達時間。沒有安裝 `websockets` 時，`listen` 會說明並略過，不影響另外兩個指令。

本版未包含前次提出的全部 10 個追加面向；那些是後續功能工作包。

## 小型程式更新

從 v0.4.3 起，App 的「檢查更新」先比對執行環境指紋，下載前再核對實際模型、工具／DLL 的 SHA256、Python 套件版本與原生 binding。相容時使用 `Zen-Bridge-Update.exe`，保留現有 Python、tools、model、設定與資料；不相容時使用完整 Setup。初次安裝使用 `Zen-Bridge-Setup.exe`。已裝 0.4.0–0.4.2 的主持機可直接從正式 Release 下載小型更新包；安裝器仍會先核對相容性。

維護更新相容性 gate 時，若判定契約改变，請提升 `scripts/update_runtime.py` 的 GATE_VERSION；發行時保留完整 Setup、小型 Update、各自 SHA256 和 runtime descriptor 五個資產。
