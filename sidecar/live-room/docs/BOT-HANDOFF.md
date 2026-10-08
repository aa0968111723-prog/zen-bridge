# 柏能與 BOT 的 Breeze App 任務交接

更新時間：2026-10-07，Asia/Taipei。

使用者要求延續 Windows App／一鍵安裝工作，配合柏能調度，指正執行方向和檔案位置。這份文件是此專案的交接事實，不代表其他專案也應改用相同路徑。

## 路徑與版本

- 整合開發目錄：`%USERPROFILE%\Documents\Codex\2026-10-05\c-users-macmiryzen5-downloads-breeze-live\work\integration`。
- 整合分支：`codex/windows-app-integration-20261007`。
- 原始 App 開發成果：同一工作區的 `work\repo`，已保留為快照；不要與整合目錄同時改同一份功能。
- 使用者實際 App 安裝位置：`%USERPROFILE%\AppData\Local\Programs\Breeze Live Room`，執行檔 `Breeze.exe`，桌面名稱「禪譯聽眾房」。這是部署目錄，不是 Git 開發來源。
- 舊下載目錄：`%USERPROFILE%\Downloads\breeze-live-room-main\breeze-live-room-main`。檢查時 VERSION 為 0.2.0，不能把它當成最新開發版本。
- 交付檔案目錄：`%USERPROFILE%\Documents\Codex\2026-10-05\c-users-macmiryzen5-downloads-breeze-live\outputs`。
- GitHub：`https://github.com/aa0968111723-prog/breeze-live-room`。

## 必須配合的新更新

`main` 已合併 Grok PR #12，以及 #17、#18、#19、#20 等後續修正；整合副本再次同步至 `db46a1c5beccaf05526fa3d546a2a7679337cb46`。除了字幕期限、重啟還原、取消／停止、翻譯佇列、捷徑處理，還包括聽眾連線金鑰、避免 zh_raw 外送、重送處理、靜態檔重新驗證與單次 multipart 解析。不要用舊分支或舊下載檔直接覆蓋 app/server.py、pipeline.py、store.py、dispatch.py 和前端錄音／聽眾程式。

之前簡單的 TEMP 改寫與 main 的完整捷徑修復重疊。整合版保留 main 的可驗證、可還原 TEMP 處理，移除舊的前置改寫。

## 工作方向

1. 以桌面 App 能啟動、關閉能停掉自有服務與辨識子程序、重複安裝保留設定及字幕為交付門檻。只有 source ZIP 或 HTTP 頁面，不等於完成 Setup.exe 交付。
2. 新 App 使用 native 本機常駐 worker，模型只載入一次。whisper-server.exe 曾在此主機退出 0xC0000005；不要假設換回舊 resident 就已修復。
3. RTF 尚未達到持續即時字幕門檻。曾測 6 秒英文音訊需約 14～15 秒；它不是中文準確度或麥克風驗收。不可把通過單元測試寫成正式即時字幕通過。
4. App 的檢查更新必須核對本儲存庫、本版本、固定檔名及 SHA256；重複安裝保留 .env/data，不將金鑰、字幕或私人日誌包入安裝檔。
5. BOT 若要改程式，從最新 main／已發布整合分支建立自己的 checkout。先宣告要改的檔案，交付 commit／diff 與驗證結果，不在安裝目錄原地改碼。桌面外殼、安裝器與 release workflow 由本次整合工作負責，避免雙寫。
6. QA 類任務可在獨立副本補中文音訊、手機連線、停止／重連／資料保存與效能驗收；不得捏造麥克風、2 小時場次或多人實測。
7. 發布前重新 fetch main，確認 BOT 新提交是否已合併，再跑所需回歸與 App smoke test。合併新變更後重建 Setup.exe，不重寫已存在的正式 tag。

## 協作可見性

可看到 Grok Bot 與 Hermes gateway 正在執行；本機 Hermes kanban.db 沒有任務，bot_relay 名單為空，尚未取得「柏能」可用的聊天／Bot 代號。此文件已準備供轉交；未取得通道與送達證據前，不宣稱已直接指正或調整它的全部任務。
