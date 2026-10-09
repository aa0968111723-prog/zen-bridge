# breeze-live-room

本機 Breeze 中文辨識、可選英文翻譯，以及區網手機／平板字幕。

目前是安裝驗收候選版。完整安裝、辨識自檢及長場次匯出修正在 [PR #3](https://github.com/aa0968111723-prog/breeze-live-room/pull/3)，目標為 main；正式使用仍須完成主持機的速度、中文準確度、麥克風與長時間驗收。通過單元測試不等於通過實機驗收。

## 第一次使用

1. 準備 Windows 11 x64（Intel／AMD）。安裝器會下載並核對獨立 Python，無需先手動安裝 Python 或 Git。
2. 解壓縮專案，雙擊 `install.bat`。會安裝 Python 套件，下載固定版本 Breeze 模型、whisper.cpp CPU 套件與 ffmpeg，並核對大小與 SHA256。第一次約下載 1.2GB，需至少 5GB 可用空間。
3. 關閉字幕服務後，雙擊 `verify.bat`，確認真實模型可辨識及速度足以承受。結果在 `data/runtime-check.json`，不包含逐字稿或 API 金鑰。
4. 雙擊 `start.bat`。啟動前會檢查工具／DLL、模型、埠及目錄權限；瀏覽器等服務啟動後才開啟。
5. 在主持機的 `http://127.0.0.1:<BREEZE_PORT>` 允許麥克風，開始聽。聽眾裝置與電腦連同一個 Wi-Fi，掃 QR 看字幕。

錯誤時雙擊 `doctor.bat`。詳細步驟與 Microsoft 官方下載入口在 [安裝說明](docs/INSTALL.md)。

## 安裝成 App

雙擊 `Breeze-Live-Room-Setup.exe`。安裝精靈是中文，不用選資料夾，也不用另外裝 Python。完成後桌面會出現「禪譯聽眾房」，按完成就會開啟。打開後畫面上有三步：開始聽、允許麥克風、手機掃 QR。

打開後先看到啟動狀態，模型就緒才進入字幕頁。選單「檢查更新」只接受這個儲存庫正式 Release 的同名安裝檔，核對 SHA256 後才安裝；`.env` 與字幕資料會留著。關閉視窗會一併停止字幕服務與辨識程序。

還沒有附安裝檔的正式 Release 時，檢查更新會直接說明，不會改去下載其他檔案。

## 安裝包與更新

正式安裝包有兩種，不要裝進同一個資料夾。App 用 `Breeze-Live-Room-Setup.exe`。原始碼包在 [GitHub Releases](https://github.com/aa0968111723-prog/breeze-live-room/releases)：下載 ZIP、解壓縮後雙擊 `install.bat`；第一次安裝自動準備 Python、套件、模型與工具，並建立啟動／更新／診斷桌面捷徑。

桌面捷徑的檢查、結束代碼與 `BREEZE_SKIP_SHORTCUT`／`BREEZE_REQUIRE_SHORTCUT` 見 [安裝說明](docs/INSTALL.md#安裝)。

關閉字幕程式後雙擊 `update.bat` 更新；`rollback.bat` 回復前版。更新保留 `.env` 與字幕資料，先核對發布包 SHA256、準備新環境並備份。維護者推送版本標籤後，GitHub 自動測試 Windows 一鍵安裝與真實辨識，通過才發布。尚未建立正式 Release 時更新器會明確提示。完整流程見 [長期維護說明](docs/MAINTENANCE.md)。

## 中文與英譯

沒有 API 金鑰即可使用本機中文字幕。英譯需自行在 `.env` 填 `OPENAI_API_KEY`，中文辨識不改走雲端。安裝器不覆蓋既有 `.env` 或逐字稿。

新安裝預設 `BREEZE_ASR=native`，模型留在同一個本機程序。App 視窗也會強制使用這個模式。失敗會提示，不會偷偷改成 CLI 或雲端。`BREEZE_ASR=resident` 與 `BREEZE_ASR=cli` 仍可使用；cli 每段會重新載入模型。

新安裝以 SQLite 保存文字（`BREEZE_DATA_PATH=data/captions.sqlite3`），不保存錄音。啟用儲存後，匯出讀取資料庫中保存期限內的完整內容，重開服務後仍可匯出，不只最近 200 段。預設保留 24 小時，重要場次請及時匯出；可用 `BREEZE_CAPTION_TTL` 調整秒數。既有配置保留原值，未啟用儲存時只有近期字幕。

## 速度門檻

`verify.bat` 使用公開英文樣本執行兩次本機辨識，顯示即時率 RTF：辨識時間 ÷ 音訊時間。RTF 小於 1 才有可能承受持續語音，仍須以自己的中文語音及完整流程驗收。

若兩段上傳／辨識都未完成，畫面會顯示「處理積壓，已暫停產生新錄音」。這段等待沒有新的錄音，請暫停說話；不要把這個降級行為當成無缺段的正式即時服務。上傳失敗也會顯示紀錄。

## 測試與限制

本輪 Linux／Python 3.12 已通過 44 項 Python 回歸及兩組 Node 測試；實際載入固定 Breeze q5 權重，以同一個常駐程序完成兩次辨識並關閉。

這台 Linux 環境辨識 6 秒英文樣本約需 14.5～15 秒，不能承受該樣本的連續音訊。這不是 Ryzen 5 5600H／Windows 主持機的測量，也不是中文準確度測試。

Windows workflow 增加 Python 3.11／3.12、錄音與觀眾端測試，以及含中文／空白路徑的安裝與真實推論檢查。執行結果以該提交的 GitHub Actions 為準；不先宣稱通過。

目前未完成真實麥克風、30 分鐘中文、2 小時場次、3 台手機及真金鑰英譯驗收。瀏覽器整合在本輪環境受阻，Chromium 下載回傳無效檔案。正式使用門檻見 [驗收清單](docs/RELEASE-CHECKLIST.md)。實機項目與門檻見 [實機驗收清單](docs/DEVICE-ACCEPTANCE.md)，每一項都還是尚未驗證。

## 實機輔助檢查

在主持機本機（才能拿到 loopback 權杖）對已啟動的服務執行 `scripts/device_check.py`。它不會印出權杖。`watch` 每隔數秒把佇列深度、拒絕、缺段與記憶體寫進 CSV／JSON；`export` 下載該房的 JSON／SRT，並對編號、時間格式、單調、重疊、結束晚於開始、seq 是否重複、缺號各印 PASS／FAIL；`listen` 以聽眾身份連上 `/ws/listen`，記錄每則字幕的到達時間。沒有安裝 `websockets` 時，`listen` 會說明並略過，不影響另外兩個指令。

本版未包含前次提出的全部 10 個追加面向；那些是後續功能工作包。
