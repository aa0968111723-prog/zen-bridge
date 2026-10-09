# 查核紀錄

日期：2026-10-05。

- 分支：`fix/round2-continue`
- 程式提交：`cb21186d35a50690b7f012f35b4423b88b7ff6d4`（`fix/round2-continue`，在 `5c7784ca54d9c103529d85fbd1e2a111cc89c8bc` 之上）
- 本紀錄描述提交 `cb21186`。`97d0c77` 的 GitHub Actions run `37261737041` 是 `34 passed in 7.05s` 與 `recorder machine ok`。`room_client.test.mjs` 不在該工作流程。
- 這台是 Linux，直譯器是倉庫 venv 的 CPython 3.11.17（`/root/breeze-live-room/.venv`），不是 Ryzen 5 5600H / 16GB / Windows 11 主持機。沒有麥克風、Breeze 權重、whisper-cli、`whisper-cli.exe`、OpenAI 金鑰。
- 倉庫沒有 `AGENTS.md`。

先前對較早程式的 P0 發現（未授權上傳、英譯失敗整段消失、段落交叉、房間 id 截斷碰撞、whisper-cli 非零退出仍可能當成功、錄音重入、切片不能獨立解碼、800 bytes 丟尾段、音訊無上限與暫存清理、金鑰與 QR localhost）在工作樹有對應程式。影響仍是那些：字幕被污染、中文消失、順序錯、房間併錯、失敗被當成字幕。下面只標工作樹狀態，不把未跑的測試寫成通過。

## 已修

- 主持權杖：`/api/host-token` 只接受真實 loopback（`127.0.0.1`／`::1`；測試才允許 `testclient`）。Host 必須帶與 `BREEZE_PORT` 相同的埠。有 Origin 時，scheme、host、埠都要完全符合；非 loopback 不能省埠。不看 `X-Forwarded-*`。上傳、開關房、設定、匯出、刪字幕都要 Bearer。`/api/setup` 的 `host_token` 是 null，QR 只含聽眾網址。
- 讀完並解析 request body 之後才准入（佔佇列名額）：`/api/push` 先查權杖與 Content-Length。非 multipart 回 415，超過上限回 413，讀取逾時回 408，格式不對回 400，然後才佔名額；滿了回 429。讀 body 的同時不佔名額（只有主持權杖、單次約 2 MB 加 64 KB、20 秒一定結束）。同一段若已在飛行中，重送不佔第二個名額。
- 管線 single-flight：同一 `(room_id, session_id, seq)` 在任何 await 之前登記一個 future。相同內容併入該 future；不同內容 409。預設 `BREEZE_ASR_WORKERS=1`。
- 中文先於英文：辨識成功先以 `zh_ready` 經 RoomBus 廣播，再排隊英譯。英譯例外、逾時、佇列滿或額度用完，只把同一 id 更新為 `translate_failed` 並加版本，中文留著。逾時不假設沒有計費。
- 缺段：序號有洞時，gap 等待（預設 3 秒）後標 `missing`，後面繼續。會話結束會補洞。堆積超過上限會先跳過缺的序號。聽眾帶 cursor 重連；日誌窗口不夠時 `gap: true`，客戶端只說補償窗口不足，不造段。
- RoomBus 以 `room_id` 分歷史與 cursor。事件不會寫進別的房間。
- 匿名 `/ws/listen` 只 `book.get`。房間不存在或已結束就送 `room_unavailable` 並關 4404，不開房。開房是主持端。閒置、沒有聽眾、沒有進行中的會話，才會被掃掉。
- `ResidentAsr.ready` 不是推論。只有測試旗標、沒有 transport 也沒有行程時，`transcribe` 直接失敗。`/api/setup` 用 `health()`。沒就緒就仍是 cli，且 `model_reloads_each_segment: true`。失敗不改走雲端。
- `.env` 經 `fill_process_environ`／`Settings.from_env` 只填尚未設定的鍵，行程環境贏。`start.bat` 與 `python -m app.run` 共用 `BREEZE_PORT`（未設定才 8780）。
- 房間與會話 id 不截斷，只接受 1–64 的英數、底線、減號。
- whisper-cli 非零退出或逾時是失敗，stdout 不當成功。
- 錄音狀態機：`idle / preparing / recording / draining / error`。每一段獨立 start/stop，只上傳 stop 後的完整 Blob。不再用 `requestData()` 切片或固定 800 bytes。
- 音訊有位元組與時長上限。每段一個暫存目錄，finally 整目錄刪除。沒有可分享位址不發 QR。

## 仍未完成

- Actions 沒跑聽眾客戶端測試。實機驗證沒有通過。
- q5 網址仍是 Hugging Face 的 `ggml-breeze-asr-25-q5_0.bin`。雜湊沒有在這台計算，儲存庫沒有釘選。`install.bat` 只有在旁路 sha256 檔存在時才比對。
- CLI 每次重新啟動 whisper-cli。常駐路徑未在主持機證明「模型只載入一次」。ready 旗標不能拿來宣稱已載入。
- 不是單一 exe／MSI。`install-shortcut.ps1` 存在，這裡沒執行。Windows DLL、防火牆、埠佔用、含中文或空白的安裝路徑未驗證。
- 標點：Breeze 訓練時多半拿掉標點。程式只對「嗎／呢」補全形問號，不是完整後處理。
- 預設房間與字幕在記憶體，重開就沒了。`BREEZE_DATA_PATH` 才寫 SQLite 單表，沒有完整 migrations，不存音檔。
- 仍是單一 Uvicorn process，綁 `0.0.0.0`。`silence_rms` 預設 0，沒有一般意義的 VAD。
- 多網卡、VPN、熱點會列進可選位址，選錯就掃不到。沒有區網位址就沒有 QR。
- 聽眾自動重連最多 8 次，之後要手動。英文大字在譯文還沒到時是空的，中文在下方。
- `translate_verified` 固定是 false。沒有真金鑰，不能宣稱 401、429、計費或額度行為已在線上通過。價格沒有來源與日期時不顯示金額。
- 6–15 秒只是舊文件裡的估計。這次沒有量端到端延遲，沒有 RTF、p50、p95、主持機 RAM／CPU。畫面上的 RSS 只是這台 Linux 讀 `/proc`，不是主持機實測。

## 自動測試已通過

2026-10-05 在這台 Linux 的工作樹上親見，直譯器是倉庫 venv 的 CPython 3.11.17（`/root/breeze-live-room/.venv`）。沒有模型、沒有付費金鑰。這不是 GitHub Actions 的結果。

- rebase 後的權威結果：`cd /root/breeze-live-room && .venv/bin/python -m pytest -q --tb=line` 印出 `34 passed in 14.78s`（exit 0）。
- 同一批測試較早也通過：`34 passed in 11.49s`（rebase 前）、`32 passed`、`31 passed in 10.88s`、`31 passed in 10.24s`、`31 passed in 9.74s`。
- socket `__aexit__` 與顯示順序（display-order）測試修正之前的乾淨複製是 `25 passed, 6 failed`，不能當成目前結果。
- `node tests/recorder_machine.test.mjs` 與 `node tests/room_client.test.mjs` 皆印出 ok（Node v26.3.1）。CI 仍指定 Node 22，尚未在這個工作樹上跑。

Copilot review 成功不等於測試通過。

## 實機驗證沒有通過

30 分鐘講話、2 小時連續跑、3 台手機，以及 Ryzen 5 5600H / 16GB / Windows 11 主持機上的檢查都沒有通過。具體阻塞：這台環境跑不了該測試。不要編 RTF 或延遲。下列三項都阻塞。這台沒有主持機、麥克風、權重、whisper-cli、whisper.cpp Windows 套件、ffmpeg DLL、金鑰與手機。不要事後補上沒量到的數字。

### 30 分鐘講話（阻塞）

1. 在 Windows 主持機跑 `install.bat` 與 `start.bat`，確認頁面是 `http://127.0.0.1:<實際 BREEZE_PORT>`。
2. 主持頁不得顯示還缺 whisper-cli、Breeze 模型或 ffmpeg，也不得改走雲端。
3. 對真實麥克風連續講話 30 分鐘（約 6 秒一段）。
4. 只記有沒有崩潰、缺段、英譯失敗時中文是否還在、佇列是否卡住、停止後麥克風是否釋放。
5. 匯出 SRT，時間軸應來自錄音的 t0／t1。沒量到就不填 RTF、p50、p95、RAM、CPU，也不寫模型已在實機只載入一次。

### 2 小時連續跑（阻塞）

1. 同一主持機、同一套權重，會話保持 active，連續 2 小時。
2. 確認進行中的房間不會因閒置掃描消失；`tmp` 不殘留該段目錄。
3. 若沒有金鑰，只記中文模式。不要假造 401、429 或計費。
4. 同樣禁止填沒量到的延遲與記憶體曲線。

### 3 台手機或平板（阻塞）

1. 三台與主持機同一 Wi-Fi，掃畫面上的 QR（不是 localhost）。
2. 三台都應看到同一 `room_id`。有金鑰才要求英文；沒金鑰只要求中文。
3. 其中一台斷線再回來，應依 cursor 補段；窗口不夠時畫面寫補償窗口不足。
4. 對一個沒開過的房間連 `/ws/listen`，不應因此出現新房間。
5. 從手機要 `/api/host-token` 與 `/api/push`，應被拒絕。

## 仍不做

- 單一 exe／MSI。核心流程在實機穩定後再做。
- 多程序協調。現在維持單一 Uvicorn process。
