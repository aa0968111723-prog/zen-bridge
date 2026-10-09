# Windows 安裝與操作

App 安裝：雙擊 `Breeze-Live-Room-Setup.exe`。不必先裝 Python。設定與字幕在更新時保留，關閉視窗會停止辨識服務。下面是原始碼 ZIP 的安裝方式。

這個候選版包含完整安裝、辨識自檢及長場次匯出修正。請從 [候選版分支](https://github.com/aa0968111723-prog/breeze-live-room/tree/codex/windows-readiness-20261005) 選 Code → Download ZIP，再解壓縮。修正在 [PR #3](https://github.com/aa0968111723-prog/breeze-live-room/pull/3)，目標為 main，目前仍是驗收候選版。

## 執行環境

- Python：`install.bat` 自動下載固定且核對 SHA256 的官方獨立 Python 到 `.python/`，不用手動安裝或修改 PATH。回歸 CI 同時測 3.11／3.12；使用者安裝採專用 3.12 環境。
- Microsoft Visual C++ x64 執行階段：缺少時安裝器會從 Microsoft 下載，核對官方數位簽章再安裝。也可使用 [Microsoft 官方說明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) 的 x64 下載。whisper 套件需要 MSVCP140／VCRUNTIME140。

## 安裝

1. 解壓縮專案，雙擊 `install.bat`。
2. 安裝器準備獨立 Python 並建立專案自己的 `.venv`，依鎖檔安裝套件，下載並校驗模型、whisper.cpp 與 ffmpeg，不需要手動挑選 CUDA 版本。
3. 模型固定來源為 shdennlin/breeze-asr-25-ggml 的 `36c726093efe1760d1dd39c3cfe8b6a7282437d1`，SHA256 固定為 `f51573bd6ef9b1fac0bd09a1652eda58a1a638d824a08771ccdee1102fe5990a`。
4. 程式工具固定為 whisper.cpp v1.9.2 CPU x64 與 Gyan FFmpeg 9.0.2 essentials。各下載的預期大小、SHA256 及來源在 `runtime-manifest.json`。FFmpeg 官方下載頁列有 Gyan 的 Windows 編譯版本。
5. 不完整下載保留為 .part，可重跑續傳；只有完整校驗通過的檔案會取代正式檔案。舊 tools 會備份到 `.downloads/previous-tools-*`，避免混用舊 DLL。
6. 首次複製 `.env.example` 為 `.env`；既有檔案保留。沒有金鑰就是中文模式。
7. 所有啟動前檢查通過才顯示完成；桌面捷徑建立失敗時仍可使用 `start.bat`。

安裝會在桌面建立三個捷徑：Breeze Live Room（`start.bat`）、Breeze Update（`update.bat`）、Breeze Doctor（`doctor.bat`）。`install-shortcut.ps1 -CheckOnly` 的結束代碼：0 表示正常；1 表示目標或目錄不正確（例如搬移安裝資料夾後），請再執行 `install.bat` 或 `install-shortcut.ps1`；2 表示捷徑不存在。`BREEZE_SKIP_SHORTCUT=1` 略過捷徑。`BREEZE_REQUIRE_SHORTCUT=1` 時捷徑失敗會讓安裝失敗；未設定時失敗不影響安裝。`install_runtime` 先讀行程環境變數，`.env` 只補尚未設定的鍵（`app/settings.fill_process_environ`）。第一次安裝時 `.env` 還不存在，請用環境變數設定。Breeze Update 捷徑執行的是沒有 `--check` 的 `update.bat`，雙擊會進行真正的更新。

## 主持機速度自檢

先關閉字幕服務，再雙擊 `verify.bat`。它會使用固定公開英文樣本完成兩次真實本機辨識，不會送到翻譯 API，結果存到 `data/runtime-check.json`。

- `inference_ok=true` 只代表模型可推論。
- `live_capacity_in_this_test=true` 代表此測試的兩次 RTF 都小於 1，不代表中文準確度或長時間服務已通過。
- RTF 大於或等於 1 時，先不要把它用於連續即時字幕；以真實中文樣本再量測，調整硬體／設定後重新驗收。
- 可在專案終端執行 `.venv\Scripts\python.exe scripts\verify_runtime.py --audio "你的中文音訊.wav"`。只擷取前 6 秒，可用 `--seconds` 調整至 0.5～30 秒；檔案不會外送雲端。

## 啟動與分享

雙擊 `start.bat`。程式自己讀取 `.env`，服務、瀏覽器及 QR 共用 `BREEZE_PORT`（預設 8780）。主持人用本機 127.0.0.1 開啟；聽眾用同 Wi-Fi 的區網位址。

Windows 防火牆需要允許這個服務在私人網路被其他裝置連線。若無可分享的位址，畫面不產生 localhost QR。

新安裝預設常駐模式與文字儲存。既有 `.env` 若仍為 `BREEZE_ASR=cli`，每段重新載入是該模式的行為。要測常駐可改為 `BREEZE_ASR=resident`，停止服務後重開。

## 長場次逐字稿

新安裝會把文字保存到 `data/captions.sqlite3`。匯出以資料庫中保存期限內的完整內容為準，超過近期字幕窗口後仍保留前段，重開服務後也能匯出原房間。錄音不會保留。

文字預設保留 24 小時；重要場次請在結束後匯出。若需延長，可在 `.env` 設 `BREEZE_CAPTION_TTL`（秒數）。舊 `.env` 未啟用儲存時，補上 `BREEZE_DATA_PATH=data/captions.sqlite3` 後重開服務；只會保存啟用後收到的文字。

畫面若顯示「逐字稿未啟用儲存」，服務重開會清空字幕，匯出只有近期內容。

## 排錯

雙擊 `doctor.bat`：檢查 Python 套件、模型 SHA256、執行檔／DLL、埠與目錄權限；不呼叫英譯 API，也不錄音。

- 缺少 Python：重新執行 install；檢查網路是否可連線 NuGet。
- whisper 無法啟動、代碼 3221225781／-1073741515：檢查 Microsoft Visual C++ x64 執行階段與完整 tools 套件。
- 埠被占用：先關閉舊字幕服務，或修改 `BREEZE_PORT`。
- 常駐模型載入失敗：查看畫面原因，確認模型完整、whisper-server 可執行及 `BREEZE_RESIDENT_URL` 沒有與其他服務撞埠。
- 處理積壓：畫面會暫停產生新錄音，請暫停說話。必須解決辨識速度不足才適合正式使用。
- 上傳未確認完成：網路逾時不能代表伺服器沒收到；先檢查字幕／匯出內容。

正式使用前依 [驗收清單](RELEASE-CHECKLIST.md) 執行，不只看安裝器的完成訊息。

## 可選速度設定，需另驗中文品質

本輪在同一台 Linux、同一段 6 秒英文樣本，比較 greedy 解碼的完整 context 與 512 context：14.050 秒／4.491 秒（RTF 2.342／0.748）。兩者文字不相同，不能宣稱品質等價，也不是 Windows 主持機結果。

手動速度試驗需同時設定 BREEZE_ASR_AUDIO_CONTEXT=512、BREEZE_ASR_BEAM_SIZE=1、BREEZE_ASR_BEST_OF=1。三個選項預設均為 0，各自保留模型原本參數。修改後先關閉服務，再用自己的中文音訊執行 verify，對照逐字稿及術語準確度；通過速度及品質驗收後才使用。

這個選項會改變模型的音訊上下文及解碼方式，不是品質無代價的加速。
