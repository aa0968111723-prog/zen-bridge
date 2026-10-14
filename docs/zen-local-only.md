# 禪譯 0.6.0 Windows 本機版

安裝包包含主介面、Node 22.23.3、Python、語音模型與字幕服務，App 負責服務啟動與停止。採用 Grok PR #43 的本機儲存設計，保留 main 較新的字幕、錄音與錯誤處理，沒有整批套用舊版介面。

| 項目 | 位置 |
| --- | --- |
| 主介面 | http://127.0.0.1:8785，從 App 開啟以取得主持授權 |
| 原生辨識 | 127.0.0.1:8780 |
| 代理 | 127.0.0.1:8776 |
| 本機後台 | App 選單「本機後台」，127.0.0.1:8791 |
| Ollama | 127.0.0.1:11434，預設 qwen3:1.7b |
| 主介面 SQLite | %LOCALAPPDATA%\ZenBridge\zen-ui.sqlite3 |
| 主介面音檔 | %LOCALAPPDATA%\ZenBridge\audio |
| 原生後台帳本 | %LOCALAPPDATA%\ZenBridge\data\zen.sqlite3，與主介面分開 |

本機 App 不需要雲端網站、Postgres 或 API key。電腦需要既有 Ollama 及其模型；此安裝包不含 Ollama 本體和它的權重。GitHub 更新檢查與首次模型下載仍需要網路。App 服務只綁 loopback，關閉區域網路分享。

安裝目錄 .env 可設定 ZEN_BRIDGE_LOCAL=1、ZEN_LOCAL_LLM_MODEL=qwen3:1.7b。本機模式強制本機翻譯，停用雲端與混合路由，不啟動 App 自己的 Hermes 代理；另外執行的 Grok/Hermes 不受影響。

啟動會驗證並匯入包內詞典快照；既有 SHA256 相符的版本不重複匯入。失敗保留既有資料與版本。ZEN_UI_DATA_DIR 可覆寫主介面資料夾。

## 維護與重建

GitHub integration workflow 僅檢查及產出本機介面，已移除自動部署雲端主機步驟。

Windows 建置需要官方 Node 22.23.3 x64（含 LICENSE）、pnpm 11.25.0、Python 與 Inno Setup。於專案根目錄執行：

```
pnpm install --frozen-lockfile
python sidecar/live-room/scripts/build_setup.py --payload build/desktop --output dist
```

桌面 workflow 執行同一流程。desktop-runtime/package.json 與 lockfile 固定桌面依賴；build_local_ui.py 驗證 Node 與 lockfile SHA256，建置純 Node 介面，hoisted/copy 安裝避免依賴開發機 pnpm store。變更 Node 或依賴時須同步 desktop/desktop-manifest.json；環境指紋不同會要求完整安裝。

## 備份與搬移

關閉 App 後備份整個 %LOCALAPPDATA%\ZenBridge，另備份安裝目錄 .env、data（包括 WebView2）與 logs。解除安裝保留使用者資料。

scripts/import-local-snapshot.mjs 可匯入授權取得的快照，INSERT OR IGNORE 保留本機編輯。JSON 匯入不會下載錄音。雲端 export 不可用而使用 workspace 快照時，不代表所有修訂歷史已搬移。

舊網站來源的 WebView IndexedDB 原始錄音不會自動跨到 localhost；保留原 WebView profile，勿清除後聲稱錄音已搬移。

## 驗收限制

驗收使用獨立資料庫、公開語音樣本與真實本機模型。通過安裝、辨識、翻譯與音檔位元組一致測試，仍不等於真實課堂驗收。實驗性快速草稿、快速定稿、講者辨識及評估模型應另量測 CPU、記憶體、誤辨與延遲。
