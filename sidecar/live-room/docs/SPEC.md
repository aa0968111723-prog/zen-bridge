# 規格

日期：2026-10-05。程式提交：`cb21186d35a50690b7f012f35b4423b88b7ff6d4`（`fix/round2-continue`）。這台 Linux 不是主持機，下面沒有實測數字。

## 目標

長時間持續字幕。聽寫預設本機 Breeze ASR 25。英譯可關。聽眾房必做。聽寫、翻譯、分享分開。

## 四個旋鈕

1. 聽寫：現在實作 breeze。預設 `cli`（每段重開 whisper-cli）。`resident` 只連本機 loopback 的 whisper-server；ready 旗標不是推論，沒就緒不改走雲端。
2. 送法：chunk。主持頁約每 6 秒用獨立的 start/stop 送一整段，不是 Qwen 串流，也不把 `requestData()` 切片當獨立檔。
3. 翻譯：OpenAI（`OPENAI_API_KEY`，預設模型 gpt-4.1-mini）。沒金鑰、關閉、逾時或失敗都只留中文。問題只翻譯，不代答。
4. 分享：room。預設開。沒有可分享位址就沒有 QR。

## 資料流

1. 本機瀏覽器先拿主持權杖，再開房。匿名聽眾不開房。
2. 麥克風每段帶 `room_id`、`session_id`、`seq`。伺服器在解析表單前准入。同一段 single-flight。
3. ffmpeg 轉 16 kHz 單聲道 wav，超過時長或大小就拒絕。暫存目錄用完即刪。
4. whisper 語言 zh，`initial_prompt` 只是偏置，不保證鎖詞。
5. 中文先廣播。有金鑰才英譯，結果寫回同一 segment。
6. 缺序號會在等待後標 `missing`。聽眾用 cursor 補段，窗口不夠就回 gap。
7. 聽眾頁大字偏向英文，下方留中文。沒有英文時中文仍在。

## 配備參考（不是這次量測）

- q5 約 1.1GB，q8 約 1.7GB，fp16 約 2.9GB。雜湊未在此釘選。
- 文件紀錄的暫時主持機是 Ryzen 5 5600H、16GB、無 NVIDIA、Windows 11。規格上走 CPU，不走 CUDA。
- 磁碟安裝前要留 5GB。這台沒有量到 RTF、p50、p95、RAM 或 CPU，也不能把 6–15 秒當成實測。

## 禁止

- 不把權重打進 Git、Worker 或映像。
- 不讓 Breeze 出英譯，辨識失敗不改走雲端。
- 不生成 localhost QR，不把金鑰寫進前端、QR 或儲存庫。
- 不把 `.env` 蓋過已經設定的行程環境。
- 不把未在主持機跑過的延遲或「模型只載入一次」寫成事實。

## 相關專案

- zen-bridge：禪譯工作台，目前沒有聽眾房。
- tku-live-translate：已有 QR 房間形狀。這個專案是桌面可安裝版，執行期不依賴它。
