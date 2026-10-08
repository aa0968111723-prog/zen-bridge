# Windows 發布與長期維護

## 使用者

下載 GitHub Releases 的 `Breeze-Live-Room-Windows.zip`，解壓縮到固定位置後雙擊 `install.bat`。不需要先安裝 Python 或 Git；安裝器使用獨立 Python 3.12.10，核對官方 NuGet 套件 SHA256，再建立專用環境。首次下載約 1.3GB，預留至少 5GB。

雙擊桌面 `Breeze Live Room` 啟動；關閉服務後雙擊 `Breeze Update` 更新到最新正式版本。`update.bat -Check` 只檢查版本；`rollback.bat` 回復最近一次更新前的程式與環境。尚未有正式 Release 時，更新器會顯示沒有版本可用。

`.env`、`data/` 字幕資料與其他私人檔案不會包進發布 ZIP，更新與回復不更動它們。預設字幕保留 24 小時，請及時匯出。更新前一定要關閉字幕服務與辨識程序。備份放在 `.updates/`，確認新版本正常後，可自行整理舊備份；更新器不會自動刪掉備份。

安裝檔為可檢查的批次／PowerShell／Python 原始碼套件，不是經數位簽章的 EXE。VC++ DLL 缺少時，自動下載 Microsoft 官方安裝器並驗證 Microsoft 數位簽章；Windows 可能要求管理員權限或重新啟動。麥克風權限、手機 Wi-Fi、Windows 防火牆仍需依實際使用情況設定。

## 維護者

1. 修改程式，更新 `VERSION`（例如 `0.2.1`）、發布說明與驗收紀錄。
2. 套件改動須同步 `requirements.txt` 與 `requirements-lock.txt`，在 Windows/Linux、Python 3.11/3.12 執行回歸。鎖檔包含間接依賴；勿只更動頂層版本。
3. 模型或原生工具改動，更新 `runtime-manifest.json` 的固定 URL、版本、大小與 SHA256。確認 CPU 相容性、授權及中文辨識品質。
4. Python 更新須檢查官方 Windows 支援和安全維護狀態，更新 `bootstrap-manifest.json` 的版本與 SHA256、安裝器版本檢查、CI；並在已安裝舊版的電腦驗證升級。涉及 Python 大版本的更新，先重新執行新安裝包的 `install.bat` 準備新 runtime。
5. 將通過驗證的提交標記 `v<版本>` 並 push。`Windows release` workflow 先跑回歸、全新原始碼安裝及兩次真實推論，再編譯離線 Setup.exe，檢查 App 視窗啟動／關閉、重複安裝保留設定與字幕。成功才建立 GitHub Release，附 Setup.exe、原始碼 ZIP 與各自的 SHA256。
6. `workflow_dispatch` 可驗證並產生 Actions 安裝包，但不會發布正式 Release。Workflow 失敗不會刊出正式安裝包。

Dependabot 每週提出 Python 依賴更新，每月檢查 Actions；仍需維護者審查、更新鎖檔與實機驗收後發布。Python/模型/ffmpeg 的更新不會由 Dependabot 自動完成。不能把自動化設定視為永遠不需人維護。

`python scripts/build_setup.py --assets <已有模型與工具的目錄> --sdk <WebView2 SDK 目錄>` 產生 `dist/Breeze-Live-Room-Setup.exe` 與同名 `.sha256`。正式發布時兩個檔案都要放上 Release，檔名不可改。安裝檔不含 `.env`、字幕與日誌。App 內的檢查更新只認這兩個檔案。

`python scripts/package_release.py` 可離線建原始碼包；同一份檔案會產生相同 ZIP。更新器只接受本儲存庫正式 Release 的固定檔名，核對 ZIP 及逐檔 SHA256，拒絕路徑穿越、重複檔名與私人資料路徑。HTTPS 與 GitHub 帳號是信任邊界，SHA256 不等同獨立數位簽章。

更新先建立新虛擬環境，再備份來源、模型和工具。失敗恢復前版；回復不修改字幕資料或設定。遇到斷電／強制關機，保留 `.updates/backup-*`，修復後再試 `rollback.bat`；目前不保證突然斷電時的完整交易恢復，也不支援資料庫格式的跨版本逆向遷移。
