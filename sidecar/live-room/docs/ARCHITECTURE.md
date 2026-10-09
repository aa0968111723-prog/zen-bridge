# 架構

日期：2026-10-05。對齊程式提交 `cb21186d35a50690b7f012f35b4423b88b7ff6d4`（`fix/round2-continue`）。這台 Linux 沒有實機驗證。

## 行程

`start.bat` 在 `BREEZE_PORT` 尚未設定時才從 `.env` 讀埠，否則預設 8780，然後執行 `python -m app.run`。`app.run` 呼叫 `fill_process_environ` 與 `Settings.from_env`：已在行程環境的鍵贏過 `.env`。Uvicorn 綁 `0.0.0.0` 與同一個 `settings.port`。單一 process，沒有多程序協調。

## 權杖

主持頁在本機瀏覽器要 `/api/host-token`。對端必須是 loopback。Host 必須帶與服務相同的埠。有 Origin 時，scheme、host、埠都要對上允許清單；非 loopback 的 Origin 不能省略埠。不信任 `X-Forwarded-*`。權杖只在記憶體，不進 QR、聽眾網址、日誌或 `/api/setup`。上傳與主持操作要 `Authorization: Bearer`。

## 房間與 RoomBus

`RoomBook` 只讓主持端開房。匿名 `/ws/listen` 找不到房就送 `room_unavailable` 並關閉，不建立房間。有進行中的會話或仍有聽眾時不因閒置被掃掉。

`RoomBus` 以 `room_id` 分歷史與遞增 cursor。發布不會寫進別的房間。同一段 id 以較新版本替換。聽眾佇列有上限，塞滿就丟掉該連線，不在 socket 上等待。

## 上傳與管線

`/api/push` 先查權杖與 Content-Length，再讀完並解析 request body（multipart），然後才佔佇列名額。非 multipart 不讀 body，回 415。讀 body 時不受佇列上限限制；只有主持權杖能上傳，單次最多約 2 MB 加 64 KB，讀取超過 20 秒回 408。同一 `(room_id, session_id, seq)` 在 await 之前只有一個 flight。相同內容的重送併入該 flight；不同內容拒絕。預設一個 ASR worker。

順序是：解碼（有時長上限）→ 辨識 → 先廣播中文 `zh_ready` → 另一條佇列英譯。英譯失敗只更新同一 segment。序號有洞時，gap 等待後標 `missing`，會話結束也會補洞。暫存目錄在 finally 刪除。

## 辨識

預設 `CliAsr`：每次呼叫都重開 whisper-cli，非零退出或逾時是失敗。`ResidentAsr` 只接受 loopback 網址。`ready` 旗標沒有 transport 或行程時不能辨識，`/api/setup` 看的是 `health()`。沒就緒就維持 cli，並標 `model_reloads_each_segment: true`。兩條路都不會改走雲端。

## 聽眾

頁面是 `/r/{id}`。WebSocket 帶最後的 cursor 重連，最多 8 次。窗口不夠就回 gap，客戶端不造字幕。沒有可分享位址時不產生 QR，也不用 localhost 充數。

## 儲存

預設只在記憶體。設了 `BREEZE_DATA_PATH` 才把字幕寫進 SQLite，不存音檔。舊檔用 `ALTER TABLE` 補上新欄位，不是整庫搬遷。每個房間的術語表在 `room_glossary`，不跟字幕的 24 小時期限一起刪。同一房間的新會話沿用這份詞表。關閉房間或閒置約 30 分鐘回收房間時也不清詞表；只有主持人整房刪除（`DELETE /api/captions?room_id=`）才清。舊的 `POST /api/glossary` 忽略 `session_id`，寫入的是整房詞表，不是某一堂。比對時才折全形、半形與英文大小寫；`zh_raw` 和 SRT 用的原文不改。

### 修改房間術語表

主持頁文字框最多 40 條，格式是 `標準詞|別名=英文`。備註、分類、未鎖定，或無法原樣寫回的詞，文字框只能看。要改那些欄位，用主持權杖：

`PUT /api/rooms/<房間代碼>/glossary`

`Authorization: Bearer <主持權杖>`，`Content-Type: application/json`。主體是 `{"if_version": <目前版本>, "terms": [...]}`。`if_version` 必填，必須等於先 `GET` 同一個網址讀到的 `version`；不合回 409，不寫入。`terms` 最多 200 條。每一條有 `zh`（繁體，1–20 字）、`en`（1–80 字）、可省略的 `aliases`（最多 8 個，每個最多 20 字）、`lock`（省略視為鎖定）、`category`（最多 20 字）、`note`（最多 80 字）。成功回 200，帶新的 `version`。同一段說明在 README「修改房間術語表」。重譯某一段中文（`POST /api/segment/retranslate` 的 `zh`）超過 500 字回 413，不送進詞表正規化。
