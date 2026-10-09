# Zen Bridge 主介面與 Breeze 本機引擎

Zen Bridge 是唯一主要 UI。活動、人員、講義、例句與中英譯文繼續使用既有資料庫，聽眾沿用 `/r/{id}` 與 `/ws/listen`。Breeze 主持頁保留為本機診斷入口，桌面 App 正常使用時載入 Zen Bridge 網站。

`sidecar/live-room` 是從 breeze-live-room 匯入的可追蹤 subtree，起始引擎提交 `0c98e03a3699186b86b2bdc42232be52236f1559`。後续更新先比較 subtree 變更與原儲存庫，再合併；不要用舊 ZIP 蓋掉整個介面。

資料流：Zen Bridge 收取 PCM WAV → 雲端 loopback `/transcribe` gateway → 已配對主持 App 的 outbound WSS → 本機常駐 Breeze → 原 Zen Bridge 的文字翻譯、保存及聽眾房。音訊會經過雲端 relay 的記憶體，不送給其他雲端 ASR；網站 image 不含模型。App 暫存音訊在辨識後移除。

雲端設定：`BREEZE_ASR_URL=http://127.0.0.1:8770/transcribe`、`BREEZE_AGENT_TOKEN`、`PUBLIC_BASE_URL`。Token 至少 32 字元，只保存在服務 secret 與已授權主持機設定，不進 Git／安裝包／前端 JS。WS 位址為外部 HTTPS 同網域的 `/asr-agent`。內部 HTTP 需要 token 且只接受 loopback；外部 WS 需要 Bearer token。每次只接受一台主持機，1 個執行中的工作與最多 1 個等待工作。

主持 App 設定：`ZEN_BRIDGE_UI_URL`、`ZEN_BRIDGE_AGENT_URL`、`ZEN_BRIDGE_AGENT_TOKEN`。App 在 UI 自身的 API 請求加入主持證明，普通聽眾頁不持有 token。沒有配對、worker 離線或過載時會明確停止該次辨識，不會自動改走 OpenAI／Qwen。其他既有 provider 仍可由使用者明示選用。

Tencent 的公開入口在已設定配對 token 時，會檢查工作台 API、寫入、資料匯出、錄音／聲音樣本、Hermes 偵測及 MCP 的 `X-Zen-Host`，未配對回覆 403。UI backend 僅聽 loopback。普通瀏覽器可開啟 UI 外殼、開放詞典與聽眾房，但不讀取全部課程、人物與私人筆記；這些功能從已配對桌面 App 使用。外部 MCP 客戶端需透過管理員提供的主持授權設定相同標頭，不能把 token 放進前端 JavaScript 或公開 URL。

採納 Hermes 審查：Breeze 改用不重疊約 6 秒片段，輸出 16kHz／mono／PCM16；不再每 0.75 秒重跑一段完整 ASR。最多保留兩筆普通發言並為停止時的最後片段留一個位置；積壓期間顯示「此時說話不會加入字幕」，維持有界暫停後恢復。這不是無缺段的持續即時服務。CPU 實測仍可能慢於語音，須依主持機實測驗收。

部署限制：復用既有 Tencent／Zeabur 服務 image 的相同 Node 22 runtime 及鎖定套件，只掛載新的 source/dist release。不重新安裝整套依賴或下載模型到空間不足的主機；部署前保存服務模板，使用 readiness 與回復腳本。資料庫、其他服務及既有 API 金鑰不改動。


部署管理以根目錄 GitHub Actions 為主。已在 Zeabur 服務設定保存 BREEZE_AGENT_TOKEN、BREEZE_AGENT_PORT、BREEZE_ASR_URL、SPEECH_PROVIDER 和 PUBLIC_BASE_URL，並停用此環境的重複 Git trigger，避免 Zeabur 重建覆蓋 CI 掛載／配對設定。調整平台設定前保存 private backup；以 CI／受限 deploy 命令發布，不另啟兩條自動部署流程。若要變更 Node 鎖定依賴，先建立相符 image，再通過部署脚本的 lock 檢查。


Breeze 上游同步至已合併 main `36fe197fefc9242e44cf2c30b704ba47c66c00f2`（PR22–31）。以匯入原生 App 分支與 main 的共同祖先 `db46a1c5beccaf05526fa3d546a2a7679337cb46` 取得上游差異，再於 `sidecar/live-room` 做三方套用，保留 Zen 原生 App、配對與小型更新改動。PR32 仍在複審，不包含在此同步中。後續上游同步請從這個 main SHA 比較，並保留 Zen 主 UI。
