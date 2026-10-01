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
- 麥克風錄音，每 8 秒送出完整 WAV 片段，依序辨識與翻譯。
- OpenAI 自動辨識與手動接管。每場最多 4 位已上傳 2–10 秒聲音樣本的已知講者；無法配對的聲音標為待確認。
- 有 DASHSCOPE_API_KEY 時預設 Qwen3.8-LiveTranslate 外部同傳 API；模型 ID 為 qwen3.8-livetranslate-flash-realtime。已確認例句提供熱詞，不是模型微調；Qwen 自動模式不套用講者聲紋。只有 OPENAI_API_KEY 時使用原本 OpenAI 路徑；SPEECH_PROVIDER=openai 可明示切換。
- 中英雙向語意翻譯預設 gpt-4.1-mini，帶入本場講義、講者用語、前 6 段內容及最多 30 筆已確認例句。英文發言輸出臺灣繁體中文；問題只翻譯，不代為回答。
- 原始中英內容、音檔重聽、翻譯修正及版本保存。
- 候選、確認與封存的例句流程。編輯後重新確認。
- 全部例句、版本歷史及選定活動完整逐字稿 JSON 匯出。
- `/mcp` 提供記憶查詢與候選例句寫入；支援可用瀏覽器中的 WebMCP。

## 目前未連接

真實語音 API 與課堂麥克風辨識仍需使用平台 secret 完成實測。網站會顯示「尚未連接」，仍可管理人員、錄製講者聲音、保存雙向文字與人工例句。

尚未操作使用者的 Hermes Agent 環境。MCP 工具及匯出格式已備妥，不代表 Hermes 已授權、連接或自動整理。這裡的改善是確認例句提供後續翻譯參考，不是重新訓練模型權重。

## 服務設定

DASHSCOPE_API_KEY 與 OPENAI_API_KEY 只存於 Cloudflare secret 或 Zeabur Variables。不要放在原始碼、瀏覽器、公開環境變數、zbpack.json 或 Git。

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

Postgres 使用 `db/schema.pg.ts` 對應既有 Drizzle schema，以 `pnpm db:generate:zeabur` 產生 `drizzle-pg/` migrations；首次資料存取會在交易鎖內套用待執行的 Postgres migrations，不執行 D1 SQL。沒有資料庫或連線尚未就緒時，API 回「資料庫尚未就緒」，頁面仍可啟動。

需要重聽錄音／講者樣本時，掛載 Volume 並將 AUDIO_DIR 設為掛載目錄。沒有 AUDIO_DIR 時，字幕仍保存，audio_key 留空。不要把音檔保存到未掛載的容器目錄。

兩個平台預設連新加坡 dashscope-intl；只有 DASHSCOPE_REGION=cn 才使用北京。Qwen3.8-LiveTranslate 只呼叫外部 API，不能微調或把權重放進部署平台。HOTWORD_LIMIT 預設 200，小於 1 回到 200，大於 1000 取 1000；只取已確認且符合長度的例句。

語音未回傳譯文時，有 OPENAI_API_KEY 才補譯，補譯失敗只記在 note。手動文字「翻譯並保存」仍只呼叫 translateText，需要 OPENAI_API_KEY。

## 開發與驗證

平台初始化、依賴與建置命令見 [runtime notes](docs/RUNTIME.md)。SQL migrations 位於 `drizzle/`，本地及正式資料庫各自追蹤，不能重複執行已套用的 migration。

完成 TypeScript 檢查、Worker 建置與既有本地 D1/R2 端點整合驗證。介面升級另外檢查桌面、390 px 手機與 768 px 平板的版面、字幕預覽、投影與頁籤操作。

Zeabur 建置後可執行 `node tests/startup.mjs`，驗證未設定 DEPLOY_TARGET／DATABASE_URL 時的命名啟動入口、PORT、頁面可開啟、API 資料庫未就緒回應，以及明示部署目標的驗證。

執行 `node tests/capture.mjs` 驗證麥克風選擇、試音不產生音檔、錄音分段及中斷／失敗後釋放裝置。這是模擬裝置測試；DJI 實機、正式語音 API 與 WebMCP 行為仍需實測。訊飛接入尚未實作。

後續 API 評測可用相同錄音與人工核定的逐字稿／英譯，依中文錯字、術語誤辨、語意／否定／比喻、漏翻、延遲及成本比較。保留原始與修正資料，可供評測或日後整理訓練樣本。
