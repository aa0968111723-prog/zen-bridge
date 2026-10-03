# 禪譯 Zen Bridge

> 同一份程式可部署到 Cloudflare Workers 或 Zeabur，以 DEPLOY_TARGET 選擇執行平台。

淡江禪學社的中英雙向翻譯工作台。以社團文宣的晴空、木色、奶油紙感與嫩芽龜龜客製介面。Sites 私人網站由平台管理登入與存取；Cloudflare 使用 D1／R2，Zeabur 使用 Postgres 與選用的 Volume。

## 已實作

- 活動、講義背景、人員與活動角色管理。
- 社課翻譯與雙向對話模式。雙向對話在同一裝置輪流按「我說中文」或「I speak English」，不是跨裝置通話。
- 同頁文字輸入與回覆切換；中文、英文草稿依社課分開保留。常用問句只帶入草稿，按保存後才加入紀錄。
- 「晴空／木緣」畫面風格、龜龜歡迎／聆聽／說話／筆記姿態。風格是裝置偏好；姿態跟隨收音與朗讀狀態。
- 已建立的社課可補充或修改名稱、主題與講義，保留既有角色及全部紀錄。
- 手動文字可保存中文或英文發言。每筆保留語言方向，英文提問及中文回答共用同一場活動與前後文。
- 字幕大字、投影、只看譯文、複製及裝置語音朗讀。開始收音前會停止朗讀，避免回音。
- 講者可直接錄製 5 秒聲音樣本、取消錄製、重錄及重聽，也保留檔案上傳方式。
- 手機底部導覽及字幕旁的講者選擇；活動名稱填好即可建立，課程背景可稍後補充。
- 收音與發言控制放在字幕之前；寬螢幕採側邊導覽，中型螢幕採上方導覽，手機維持底部導覽。英文發言區提供英文提示與保存按鈕。
- 字級、間距、按鈕與狀態統一，保留晴空／木緣風格及龜龜插畫。投影可按 Esc 返回，未建立活動也可預覽字幕，不保存範例。
- 文宣女孩加入共用頁首，與龜龜一起出現在字幕歡迎畫面；字幕顯示後保留完整閱讀區域。插畫使用依顯示尺寸產生的 WebP，頁首與女孩提供響應式尺寸，歡迎插畫延後載入，網站圖示另用小尺寸 PNG。
- 指定麥克風與 10 秒試音；收音、講者樣本與試音共用選定裝置。試音只顯示本機音量，不保存音檔、不送出音訊。裝置中斷時停止收音並提示，不自動改用另一個麥克風。
- 麥克風持續收取單聲道 PCM。Breeze 使用 6 秒滑動窗、每 0.75 秒前移與 VAD；Qwen Live 可維持伺服器端長連線，8 秒 chunk 保留為備援。
- OpenAI 自動辨識與手動接管。每場最多 4 位已上傳 2–10 秒聲音樣本的已知講者；無法配對的聲音標為待確認。
- 預設 Breeze ASR 25，僅透過本機 `BREEZE_ASR_URL` sidecar；模型權重不進網站、Worker 或 Zeabur 映像。Qwen3.8-LiveTranslate 與 OpenAI 路徑仍可明示選用，不會在缺少設定時偷偷切換。
- 每場活動有固定聽眾網址 `/r/{id}`；`/ws/listen` 優先 WebSocket，無法升級的 Node 路徑使用同端點 SSE。只廣播已落定並保存的中英譯文，重連補最近 40 句。
- 中英雙向語意翻譯支援 Hermes 文字 API；未設定 Hermes 時沿用 OpenAI gpt-4.1-mini，帶入本場講義、講者用語、前 6 段內容及最多 30 筆已確認例句。英文發言輸出臺灣繁體中文；問題只翻譯，不代為回答。
- 原始中英內容、音檔重聽、翻譯修正及版本保存。
- 候選、確認與封存的例句流程。編輯後重新確認。
- 全部例句、版本歷史及選定活動完整逐字稿 JSON 匯出。
- `/mcp` 提供記憶查詢與候選例句寫入；支援可用瀏覽器中的 WebMCP。

## 目前未連接

真實語音 API 與課堂麥克風辨識仍需使用平台 secret 完成實測。網站會顯示「尚未連接」，仍可管理人員、錄製講者聲音、保存雙向文字與人工例句。

Hermes 文字翻譯接入程式已備妥；實際啟用需要平台連線設定及 API 存取授權。「已設定」不代表連線成功；設定頁的檢查按鈕只驗證文字 API 的認證與模型清單。長期記憶依 Hermes profile 的設定運作，未新增自動寫入或模型微調。

## 服務設定

DASHSCOPE_API_KEY 與 OPENAI_API_KEY 只存於 Cloudflare secret 或 Zeabur Variables。不要放在原始碼、瀏覽器、公開環境變數、zbpack.json 或 Git。`SPEECH_PROVIDER` 接受 `breeze`（預設）、`qwen-live`、`openai`；`SPEECH_MODE` 接受 `stream` 或 `chunk`，只有 Qwen 可用 stream。`SHARE` 接受 `room`（預設）或 `off`。

分享房必須設定外部 `PUBLIC_BASE_URL`；未設定時設定頁明示停用，且不生成 localhost QR。Breeze sidecar 必須只聽本機，設定例如 `BREEZE_ASR_URL=http://127.0.0.1:8770/transcribe`。可在獨立 GPU/本機環境執行：

```sh
python -m pip install -r sidecar/requirements.txt
uvicorn sidecar.breeze_asr:app --host 127.0.0.1 --port 8770
```

sidecar 啟動時只載入一次 `MediaTek-Research/Breeze-ASR-25`（Apache-2.0），接收 16-bit 單聲道 PCM WAV；模型快取與音檔都留在 sidecar 主機，不納入網站映像或 Git。

伺服器可設定 `OPENAI_TRANSCRIPTION_MODEL` 與 `OPENAI_TRANSLATION_MODEL`。兩條 API 路徑分開實作，日後依你們的社課資料比較再替換。

聲音樣本只供已知講者辨識，不能保證辨識正確。每 8 秒片段加上 API 處理時間是第一版延遲，尚未做逐字串流、跨片段聲音追蹤或整場音檔合併。辨識失敗會停止收音並提示；已保存的紀錄不會消失。

API 超時、額度、音量、口音與音檔格式仍需在真實課堂錄音驗證。OpenAI 自動模式的講者標記模型沒有使用術語 prompt；課程及記憶會在第二階段翻譯加入。

## 部署

### Cloudflare

設定 DEPLOY_TARGET=cloudflare；未設定 DEPLOY_TARGET 且沒有 DATABASE_URL 時也會選 Cloudflare。保留現有 vinext／Wrangler 建置與 D1／R2 bindings：DB、BUCKET。`npm run build` 建置 Worker，`npm start` 啟動既有本地 D1／R2 預覽；正式發布使用現有 Cloudflare／Sites 流程。D1 migrations 位於 `drizzle/`。

語音金鑰只使用 Cloudflare secret：DASHSCOPE_API_KEY、OPENAI_API_KEY。其餘服務設定欄位為 DASHSCOPE_REGION、QWEN_LIVE_MODEL、SPEECH_PROVIDER、HOTWORD_LIMIT、OPENAI_TRANSCRIPTION_MODEL、OPENAI_TRANSLATION_MODEL。

### Zeabur

從同一個 GitHub main 部署，使用 Node 22。`zbpack.json` 建置命令是 `DEPLOY_TARGET=zeabur pnpm build`，start command 是 `pnpm start:zeabur`。Node 聽平台提供的 PORT，綁定 0.0.0.0。

建置命令中的 DEPLOY_TARGET 不會自動保留到執行階段。`start:zeabur` 是明示的 Node 啟動入口；未設定 DEPLOY_TARGET 時會在該進程選用 zeabur，即使尚未設定 DATABASE_URL，頁面也能啟動，資料 API 回「資料庫尚未就緒」。明示 cloudflare 或不合法的 DEPLOY_TARGET 不會被這個入口覆蓋。一般 `npm start` 仍沿用未設定時的推斷規則。

在 Zeabur Variables 設定 DEPLOY_TARGET=zeabur、DATABASE_URL（Postgres 連線），及需要的 DASHSCOPE_API_KEY／OPENAI_API_KEY。未明示 DEPLOY_TARGET 時，有 DATABASE_URL 就選 Zeabur。DEPLOY_TARGET 只接受 cloudflare 或 zeabur；其他值在啟動時報錯，訊息不回顯值。

部署 GitHub 程式不會建立 PostgreSQL 服務。請先在禪譯所在的同一個 Zeabur 專案新增 PostgreSQL，再於禪譯 Variables 將 DATABASE_URL 引用 POSTGRES_CONNECTION_STRING（Zeabur 的內部連線參考變數），重新啟動並在「連線設定」按「檢查連線」。連線成功後，程式才會自動建立資料表。設定頁會區分缺少 DATABASE_URL 與資料庫目前無法讀取，API 診斷不回傳連線字串或金鑰。

Postgres 使用 `db/schema.pg.ts` 對應既有 Drizzle schema，以 `pnpm db:generate:zeabur` 產生 `drizzle-pg/` migrations；首次資料存取會在交易鎖內套用待執行的 Postgres migrations，不執行 D1 SQL。沒有資料庫或連線尚未就緒時，API 回「資料庫尚未就緒」，頁面仍可啟動。

需要重聽錄音／講者樣本時，掛載 Volume 並將 AUDIO_DIR 設為掛載目錄。沒有 AUDIO_DIR 時，字幕仍保存，audio_key 留空。不要把音檔保存到未掛載的容器目錄。

兩個平台預設連新加坡 dashscope-intl；只有 DASHSCOPE_REGION=cn 才使用北京。Qwen3.8-LiveTranslate 只呼叫外部 API，不能微調或把權重放進部署平台。HOTWORD_LIMIT 預設 200，小於 1 回到 200，大於 1000 取 1000；只取已確認且符合長度的例句。

語音未回傳譯文時，已設定文字翻譯服務才補譯，補譯失敗只記在 note。手動文字「翻譯並保存」仍只呼叫 translateText，不開同傳 WebSocket。

### Hermes 文字翻譯

在平台 secret／Variables 設定 HERMES_API_URL、HERMES_API_KEY；HERMES_API_KEY 必須對應目標 Hermes 服務的 API_SERVER_KEY。不要將其他模型、GitHub、Telegram 或 MCP 金鑰複製到禪譯。HERMES_TRANSLATION_MODEL 缺省為 hermes-agent；TRANSLATION_PROVIDER 只接受 hermes 或 openai。未明示時，完整設定 Hermes 就用 Hermes，否則沿用 OpenAI；明示的服務未設定時不會偷偷切換。

Zeabur 同專案可使用 Hermes 的私有位址與 API_SERVER_PORT；Cloudflare 要使用可存取的 HTTPS API 位址。HERMES_API_URL 可帶 /v1，禁止嵌入帳號、密碼、查詢參數或片段。所有金鑰只存平台 secret，不輸出到前端、錯誤訊息或 Git。

目前指定的 Hermes API 有文字接口，沒有 audio API。單獨設定 Hermes 不會啟用收音；錄音辨識仍用 Qwen 或 OpenAI。文字翻譯傳入本場背景、最近 6 段及最多 30 筆已確認例句，並要求只翻譯當前發言。未改動社課資料庫或插畫。

Hermes 的 API 會在伺服器執行已設定的工具；並非每個版本都遵守 tool_choice。因此公開網站應使用獨立的翻譯 profile，限制工具與資料存取，不能只靠提示詞阻止工具操作。不要為連線開放未授權的代理能力。

## 開發與驗證

`pnpm test:runtime` 包含 Hermes 的模擬 API 測試，涵蓋文字服務切換、認證失敗、回傳驗證、確認例句與前後文、原文保存及金鑰不回傳前端。此測試不代表正式 Hermes 連線或翻譯品質已驗證。

平台初始化、依賴與建置命令見 [runtime notes](docs/RUNTIME.md)。SQL migrations 位於 `drizzle/`，本地及正式資料庫各自追蹤，不能重複執行已套用的 migration。

完成 TypeScript 檢查、Worker 建置與既有本地 D1/R2 端點整合驗證。介面升級另外檢查桌面、390 px 手機與 768 px 平板的版面、字幕預覽、投影與頁籤操作。

Zeabur 建置後可執行 `node tests/startup.mjs`，驗證未設定 DEPLOY_TARGET／DATABASE_URL 時的命名啟動入口、PORT、頁面可開啟、API 資料庫未就緒回應，以及明示部署目標的驗證。

執行 `node tests/capture.mjs` 驗證麥克風選擇、試音不產生音檔、錄音分段及中斷／失敗後釋放裝置。這是模擬裝置測試；DJI 實機、正式語音 API 與 WebMCP 行為仍需實測。訊飛接入尚未實作。

後續 API 評測可用相同錄音與人工核定的逐字稿／英譯，依中文錯字、術語誤辨、語意／否定／比喻、漏翻、延遲及成本比較。保留原始與修正資料，可供評測或日後整理訓練樣本。
