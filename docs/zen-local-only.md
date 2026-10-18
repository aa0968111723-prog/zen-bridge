# 給 Codex：禪譯（龜龜／小華主介面）完全本機化交接

> 柏能：「完全改為本機，並且同步給 codex」。Grok 在 box 完成；筆電 DESKTOP-P8RGA3A 目前離線，所以改以 GitHub draft PR＋patch 交接。
> 分支：`grok/zen-local-only`（= box/zen-local-only），基底 `origin/feat/local-translation-backend` e1c0151（內容與 main 1867a11 相同，PR #8 已合併）。
> 包含先前的 zen-ui-merge（Breeze 現場／管理後台整合進龜龜 UI，龜龜＋小華品牌，**沒有**禪釋蓮花 logo）。

## 1. 改了什麼
| 檔案 | 內容 |
|---|---|
| `lib/env.ts` | `DEPLOY_TARGET` 新增 `local`，而且是**預設**：有 `DATABASE_URL`→zeabur；Worker 有 `DB` binding→cloudflare；其餘→local。新 env `ZEN_UI_DATA_DIR`。 |
| `lib/platform/local.ts`（新） | 用 Node 內建 `node:sqlite`（**不需要原生模組、不需要 VC++**）。資料夾解析、WAL、foreign_keys、`__zen_migrations` 遷移紀錄、交易式 batch。遷移 SQL 直接用 repo 現有 `drizzle/*.sql`（D1 用的同一份 SQLite schema）。 |
| `lib/data.ts` | rows/one/write/batch 與 put/get/deleteAudio 增加 local 分支；音檔放 `<資料夾>\audio\`（`AUDIO_DIR` 仍可覆寫）。 |
| `scripts/start.mjs` | local 分支：先建立／遷移資料庫，再 `vinext start --hostname 127.0.0.1 --port 8785`。 |
| `scripts/local-config.mjs`（新） | 連接埠／位址檢查：預設 `127.0.0.1:8785`；拒絕 8645、8780、8791；`ZEN_UI_HOST` 只接受本機或私有網段。 |
| `scripts/start-local.ps1`（新，UTF-8 BOM） | Windows 一鍵啟動（檢查 Node ≥22.13、清掉雲端 env、必要時 build、開瀏覽器）。 |
| `scripts/migrate-to-local.mjs`（新） | 一次性把雲端資料搬回本機（見第 6 節）。 |
| `vite.config.ts`、`scripts/run-framework.mjs` | local 與 zeabur 一樣用純 vinext（不載 Cloudflare/Sites plugin、不跑 wrangler）。Sites managed-linux 預覽未指定時仍是 cloudflare。 |
| `lib/server.ts`、`lib/domain.ts`、`app/workspace.tsx` | `connection.dataDir`；「連線設定 → 社課資料庫」顯示「本機資料庫已就緒…資料夾：…」，執行平台顯示 `local（完全本機・只綁 127.0.0.1）`。 |
| `package.json` | 新 script `start:local`（`pnpm start` 在沒有雲端 env 時本來就會走 local）。 |
| 測試 | 新 `tests/local.test.ts`（加入 `test:runtime`）。`tests/runtime.test.ts`、`tests/startup.mjs` 的「無設定＝cloudflare」依需求改成「無設定＝local」，並**新增** `DB` binding→cloudflare、明確 `DEPLOY_TARGET=cloudflare`、`--zeabur` 配 `DEPLOY_TARGET=local` 要失敗等斷言；其他斷言未放寬。 |

沒動：`sidecar/`（live-room、Launcher.cs、setup.iss、build_setup.py、update_runtime.py、desktop_update.py）、詞典／詞彙表檔（`dictionary-data/`、`scripts/import-dictionaries.mjs`、glossary.py、host_glossary.js）、8645。雲端路徑（Cloudflare D1/R2、Zeabur Postgres/Volume、OpenAI/DashScope/Hermes）全部保留，只在設定對應 env 時才啟用。

## 2. 環境變數（本機）
| 變數 | 預設 | 說明 |
|---|---|---|
| `DEPLOY_TARGET` | （不設＝local） | 啟動腳本會明確設 `local` |
| `ZEN_UI_DATA_DIR` | `%LOCALAPPDATA%\ZenBridge` | 資料夾（Linux/mac：`$XDG_DATA_HOME/ZenBridge` 或 `~/.local/share/ZenBridge`） |
| `ZEN_UI_PORT` / `PORT` | `8785` | 不可 8645/8780/8791 |
| `ZEN_UI_HOST` | `127.0.0.1` | `0.0.0.0` 才會開放區域網路（會連主持介面一起開放，非必要不要開；聽眾頁本來就由 Breeze live-room 自己的 LAN 位址提供） |
| `ZEN_UI_MIGRATIONS_DIR` | `<app>\drizzle` | 打包時若搬動遷移檔才需要 |
| `AUDIO_DIR` | `<資料夾>\audio` | 覆寫音檔資料夾 |
| `BREEZE_LIVE_URL` | `http://127.0.0.1:8780` | Breeze 現場代理 |
| `BREEZE_ADMIN_URL` / `BREEZE_ADMIN_TOKEN` | `http://127.0.0.1:8791` / 無 | 管理後台分頁；沒 token 時頁面會提示 |
| **不要設** | — | `DATABASE_URL`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY`、`HERMES_*`（付費／雲端） |

## 3. 資料路徑
```
%LOCALAPPDATA%\ZenBridge\
  zen-ui.sqlite3 (+ -wal, -shm)   社課、講者、字幕、翻譯筆記、修訂、詞典表
  audio\recordings\<session>\<id>(.json)   錄音
  audio\references\<person>\<id>(.json)    講者聲音樣本
```
（Breeze live-room 的資料仍在安裝目錄 `data\`，與此分開。）備份＝關閉 App 後複製整個 `ZenBridge` 資料夾。

## 4. 執行方式
- 開發／測試：`pnpm install`（只需一次、需網路）→ `pnpm build` → `pnpm start`（或 `pnpm start:local`）→ http://127.0.0.1:8785
- Windows：`powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1 [-Port 8785] [-DataDir D:\Zen] [-Rebuild] [-NoBrowser] [-Lan]`
- 需求：Node **22.13 以上**（`node:sqlite` 不需旗標；22.x 會印一行 ExperimentalWarning，啟動腳本已用 `--disable-warning=ExperimentalWarning` 隱藏）。

## 5. 桌面 launcher（Launcher.cs，屬安裝器範圍，請 Codex 改）
現況：`StartApp()` 把主視窗導到 `ZEN_BRIDGE_UI_URL`，預設 **`https://vexlark.co`（雲端）**，而且強制 HTTPS、要求貼 32 字元以上的「主持連線碼」（`ZEN_BRIDGE_AGENT_TOKEN`）。這是目前最大的雲端依賴。建議：
1. 啟動 Breeze（`python -m app.desktop_service`，8780）之後，再啟動禪譯 Node server：
   `"<app>\runtime\node\node.exe" --experimental-strip-types --disable-warning=ExperimentalWarning scripts\start.mjs --local`
   WorkingDirectory=`<app>\zen-ui`；env：`DEPLOY_TARGET=local`、`ZEN_UI_PORT=8785`、`ZEN_UI_HOST=127.0.0.1`、`BREEZE_LIVE_URL=http://127.0.0.1:<BREEZE_PORT>`、`BREEZE_ADMIN_URL=http://127.0.0.1:8791`、（後台 token 若有）`BREEZE_ADMIN_TOKEN`；移除 `DATABASE_URL`。同樣 `AssignProcessToJobObject(job, …)`，App 關閉時一起結束。
2. 輪詢 `http://127.0.0.1:8785/` 回 200 後 `browser.CoreWebView2.Navigate("http://127.0.0.1:8785/")`。
3. 本機模式時允許 `http://127.0.0.1` / `http://localhost`（目前只允許 https），且**不要求主持連線碼**（單機單一主持人；只綁 loopback 就是存取控制）。雲端模式（設了 `ZEN_BRIDGE_UI_URL=https://…`）維持原邏輯。
4. 托盤「管理後台」可改成主視窗切到 `?tab=admin`（龜龜 UI 內已有全部後台分頁）；原 8791 後台仍可用。

## 6. 安裝器需求（setup.iss / build_setup.py，請 Codex 決定）
- **Node runtime**：建議隨附 Node 22 LTS（≥22.13）可攜版 `node.exe` 到 `<app>\runtime\node\`（`start-local.ps1` 也會找這個位置）。約 80 MB，無需系統安裝。
- **App 檔案**：vinext 目前沒有 Next 的 standalone 輸出；打包內容＝`dist\`、`drizzle\`、`scripts\`（start.mjs、local-config.mjs、migrate-to-local.mjs）、`lib\`（start.mjs 會 import `lib/env.ts`、`lib/platform/local.ts`）、`package.json`、`node_modules\`（`pnpm install --prod --frozen-lockfile` 後；vinext、react 等 runtime 需要）、`public\`、`.env` 範本。可在 build 機器上 `pnpm build` 後再 `pnpm prune --prod`。
- **VC++**：禪譯 Node 端**不需要** VC++（`node:sqlite` 內建、沒有 better-sqlite3 之類原生模組）。Breeze／GPU ASR 那邊仍依你們既有需求（VC++ ≥14.40）。
- 防火牆：預設只綁 127.0.0.1，不需要防火牆規則。
- 解除安裝：不要刪 `%LOCALAPPDATA%\ZenBridge`（使用者資料），或詢問後再刪。

## 7. 從雲端資料遷移（一次性、手動，需要網路與原本的雲端權限）
- Zeabur Postgres：`set DATABASE_URL=postgres://…` → `node --experimental-strip-types scripts\migrate-to-local.mjs --from-postgres`；Volume 音檔：把 `AUDIO_DIR` 內容複製到 `%LOCALAPPDATA%\ZenBridge\audio\`。
- Cloudflare D1：`npx wrangler d1 export <資料庫> --remote --output dump.sql` → `… migrate-to-local.mjs --from-d1-sql dump.sql`；R2 音檔：依 `people.reference_key`／`segments.audio_key` 用 `wrangler r2 object get` 下載到 `audio\<key>`，並寫同名 `.json`（`{"mime":"audio/wav"}`）。
- 舊本機 SQLite（例如 `.wrangler/state` 的 D1 檔）：`--from-sqlite <檔案>`。
- 以 id 去重（INSERT OR IGNORE），可重跑；遷移完 `DATABASE_URL` 就不必再設。

## 8. 驗證（box，2026-10-11）
- `pnpm build` 成功（純 vinext／Node）；`tsc --noEmit` 0 錯誤；`pnpm test:runtime` 5/5 套件通過（runtime、hermes、dictionary、breeze-live、local）；`node tests/startup.mjs` 通過。
- ESLint：新改檔案 0 問題；全 repo 2 errors／30 warnings，2 個 error 在上游 PR #8 的 `components/agent-console.tsx`、`components/dictionary-panel.tsx`（react-hooks set-state-in-effect），不是本分支造成，未修改。
- 斷網等價 E2E：`unshare -rn`（只有 loopback 的網路 namespace，確認連不到 github.com）內跑 `pnpm start`＋假 Breeze（**FAKE ASR/MT，只用於 box 截圖**）→ 建立講者、社課、字幕、修正（含修訂 batch）、翻譯筆記、上傳／讀回聲音樣本（位元組一致），Breeze 現場經本機代理收到字幕。截圖：`zen-local-home.png`、`zen-local-connections-db.png`、`zen-local-breeze-live.png`、`zen-local-admin.png`。

## 9. 未完成／請 Codex 決定
1. Launcher.cs／setup.iss／build_setup.py 的上述修改（我方禁區）。
2. 詞典匯入（`scripts/import-dictionaries.mjs`）目前只支援 Postgres；本機模式公開詞典表是空的（自有詞彙與翻譯筆記正常）。要不要加 SQLite 匯入由你們決定（屬詞典檔範圍，我沒改）。
3. 龜龜 UI 收音的草稿串流（/ws/draft）仍未接；目前 6 秒切段 push。
4. `BREEZE_ADMIN_TOKEN` 的取得方式（launcher 產生並注入最順）。
