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

`/api/push` 先查權杖、Content-Length、佇列與在途位元組，然後才解析 multipart。同一 `(room_id, session_id, seq)` 在 await 之前只有一個 flight。相同內容的重送併入該 flight；不同內容拒絕。預設一個 ASR worker。

順序是：解碼（有時長上限）→ 辨識 → 先廣播中文 `zh_ready` → 另一條佇列英譯。英譯失敗只更新同一 segment。序號有洞時，gap 等待後標 `missing`，會話結束也會補洞。暫存目錄在 finally 刪除。

## 辨識

預設 `CliAsr`：每次呼叫都重開 whisper-cli，非零退出或逾時是失敗。`ResidentAsr` 只接受 loopback 網址。`ready` 旗標沒有 transport 或行程時不能辨識，`/api/setup` 看的是 `health()`。沒就緒就維持 cli，並標 `model_reloads_each_segment: true`。兩條路都不會改走雲端。

## 聽眾

頁面是 `/r/{id}`。WebSocket 帶最後的 cursor 重連，最多 8 次。窗口不夠就回 gap，客戶端不造字幕。沒有可分享位址時不產生 QR，也不用 localhost 充數。

## 儲存

預設只在記憶體。設了 `BREEZE_DATA_PATH` 才把字幕寫進 SQLite，不存音檔，也沒有完整 migration。
