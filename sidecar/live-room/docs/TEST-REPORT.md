# 本輪安裝驗收候選版測試紀錄（2026-10-05）

基於 fix/field-ready-core@1f4732e。以下結果是本輪親自執行，不是主持機的實測。

- Linux、CPython 3.12.14：原版 34 passed in 6.68s；本輪安裝及 Windows 修正後 46 項通過，完整匯出修正後 47 passed in 7.30s。node recorder machine 與 room client 均 exit 0。
- 實際下載並核對固定 Breeze q5 SHA256 與 whisper.cpp v1.9.2 Linux 套件。
- 使用 upstream 公開英文 JFK 音訊前 6 秒，常駐載入約 0.507 秒；兩次推論約 14.467／14.974 秒，均回傳非空文字、同一 PID、最後關閉。這不是中文準確度與 Windows 主持機結果。
- 此測試 RTF 約 2.41／2.50，未通過此環境的連續即時容量門檻。
- 實際核對 Windows whisper ZIP 的 SHA256，確認 whisper-cli、whisper-server 與 ggml／whisper DLL 都在套件中；PE 匯入顯示需要 Microsoft Visual C++ runtime。
- [Actions run 37296121195](https://github.com/aa0968111723-prog/breeze-live-room/actions/runs/37296121195) 在提交 `1d7cf08069054b1a18f8f439c201abd747cd80ce` 全部通過：Windows／Linux × Python 3.11／3.12 的 46 項回歸與兩組 Node，以及 Windows 中文／空白路徑安裝、SHA256、DLL、Unicode 桌面捷徑、常駐與 CLI 各兩次真實推論。Windows runner 是 Server 2025，不是使用者的 Windows 11 主持機。
- Windows 預設參數的 6 秒英文樣本：resident 33.766／33.641 秒，RTF 5.628／5.607；CLI 36.062／36.125 秒，RTF 6.010／6.021。推論可用，但未通過這台 CI 機的連續即時容量門檻。
- Linux 真實模型 API 流程：health、setup、上傳、辨識、相同段重送去重、SQLite、SRT 與程序關閉通過；同一段只推論一次、模型載入一次。HTTPX ASGI 全流程約 17.051 秒，不含真實瀏覽器收音。
- 完整匯出回歸：近期字幕窗口設成 2，送入 6 段、跨兩個會話；JSON／SRT 仍包含全部段落，重開服務後仍可匯出，其他房間不混入。新一輪四組矩陣測試以此提交的 Actions 為準。
- 完整匯出程式提交 `d5297f932c4933a7535ef454e0feb3dd9eac6be5` 的 [Actions run 37328332363](https://github.com/aa0968111723-prog/breeze-live-room/actions/runs/37328332363) 全部通過：四組 Windows／Linux × Python 3.11／3.12 各 47 項回歸、兩組 Node，以及 Windows 完整安裝與兩種模式的真實辨識。
- 該輪 Windows 6 秒英文樣本：resident 42.547／42.328 秒，RTF 7.091／7.055；CLI 45.015／44.969 秒，RTF 7.502／7.495。不同輪的速度有變動，仍不滿足此 CI 機的即時容量，必須測量實際主持機。
- 合成文字容量檢查：保存 1200 段、近期窗口 200，SRT 匯出仍有完整 1200 段及 2 小時時間軸，匯出約 0.0091 秒。這只驗證儲存／匯出容量，不是真實錄音或 2 小時連續運作測試。
- Chromium 整合環境受阻：下載回傳非有效 ZIP，未宣稱真實瀏覽器錄音整合通過。
- 尚無主持機真實麥克風、30 分鐘中文、2 小時場次、3 台手機、真金鑰英譯的通過證據。

- 實測速度選項：同一段英文樣本，greedy／audio context=0 為 14.050 秒，context=512 為 4.491 秒；文字不同。預設未變，中文品質與 Windows 速度仍需重新驗收。

- 透過交付的 verify_runtime.py 重測速度選項（context=512、beam=1、best_of=1）：5.218／6.039 秒，RTF 0.870／1.006；第二次仍未達即時容量門檻，不能只採用最快一次的數字。

下方是歷史查核紀錄，不代表本輪提交的現況。

---

# 測試紀錄

日期：2026-10-05。機器：Linux 查核環境，不是 Ryzen 5 5600H / 16GB / Windows 11 主持機。沒有麥克風、Breeze 權重、`whisper-cli.exe`、OpenAI 金鑰。

分支 `fix/round2-continue`。程式提交是 `cb21186d35a50690b7f012f35b4423b88b7ff6d4`，接在 `5c7784ca54d9c103529d85fbd1e2a111cc89c8bc` 之上。這份 SHA 紀錄是隨後的文件提交，不是 GitHub Actions 的結果。

## 已修

這些行為在提交 `cb21186`。本地自動測試有覆蓋對應失敗路徑。這不是實機通過，GitHub Actions 也還沒跑這個提交：

- 主持權杖只在 loopback，且 Host／Origin 要對上確切埠；權杖不進 setup／QR
- `/api/push` 在解析表單前准入；同一段 single-flight；重送相同內容不佔第二個名額
- 中文先廣播，英譯失敗保留同一段中文
- RoomBus 依 `room_id` 分開；缺段與過短的補償窗口會標 missing 或 gap
- 匿名 WebSocket 不開房
- `ResidentAsr` 的 ready 旗標不是推論
- `.env` 不覆蓋行程環境；`start.bat` 與 `app.run` 共用 `BREEZE_PORT`
- 房間 id 不截斷；過大音訊拒絕；whisper-cli 非零退出不當成功
- 錄音狀態機拒絕重入；中間切片不上傳；沒有可分享位址不產生 localhost QR

## 仍未完成

- 程式本體在 `cb21186`。推上後的 `97d0c77` 觸發 GitHub Actions：run `37261737041` 印出 `34 passed in 7.05s`，以及 `recorder machine ok`。工作流程沒有 `node tests/room_client.test.mjs`（這組權杖不能改 `.github/workflows/test.yml`）。該檔在這台 Node v26.3.1 印出 `room client ok`，CI 還沒跑它。Node 22 只跑了錄音狀態機那個測試。
- 模型雜湊未在此計算、未釘選。
- CLI 仍是每段重開行程。常駐「只載入一次」沒有實機證據。
- 捷徑腳本、Windows DLL、防火牆、埠、中文或空白路徑都沒在這裡跑。
- 沒有真金鑰，不能宣稱 401／429／計費已驗證。`translate_verified` 恆為 false。
- 沒有 RTF、p50、p95、主持機 RAM／CPU，也沒有「模型已在實機只載入一次」。6–15 秒不是實測。

## 自動測試已通過

2026-10-05，這台 Linux，CPython 3.11.17（`/root/breeze-live-room/.venv`），工作目錄 `/root/breeze-live-room`。沒有下載模型，沒有使用付費金鑰。rebase 之後的程式提交 `cb21186d35a50690b7f012f35b4423b88b7ff6d4` 上親見下列結果，不是 GitHub Actions 結果。

指令與結果：

- rebase 後的權威結果：`cd /root/breeze-live-room && .venv/bin/python -m pytest -q --tb=line` 印出 `34 passed in 14.78s`（exit 0）。同一輪 `node tests/recorder_machine.test.mjs` 印出 `recorder machine ok`，`node tests/room_client.test.mjs` 印出 `room client ok`（Node v26.3.1）。
- 同一批測試較早也通過：`34 passed in 11.49s`（rebase 前）、`32 passed`、`31 passed in 10.88s`、`31 passed in 10.24s`、`31 passed in 9.74s`。
- `node tests/recorder_machine.test.mjs` 印出 ok（`recorder machine ok`）；`node tests/room_client.test.mjs` 印出 ok（`room client ok`）。Node v26.3.1。CI 指定 Node 22，尚未在這個工作樹上跑。

socket `__aexit__` 與顯示順序（display-order）測試修正之前的乾淨複製是 `25 passed, 6 failed`，失敗都在 `tests/test_round2.py`，不是 `ModuleNotFoundError: No module named 'app'`。那次不能當成目前結果。`97d0c77` 的 Actions 已跑 pytest 與錄音狀態機測試，見上面的 run。那次成功不含 `room_client.test.mjs`，也不等於實機通過。Copilot review 成功也不等於測試通過。

## 實機驗證沒有通過

30 分鐘講話、2 小時連續跑、3 台手機，以及 Ryzen 5 5600H / 16GB / Windows 11 主持機上的檢查都沒有通過。具體阻塞：這台環境跑不了該測試。不要編 RTF 或延遲。

### 30 分鐘講話（阻塞）

需要主持機、真實麥克風、`tools\whisper-cli.exe` 或已就緒的本機 whisper-server、`models\ggml-breeze-asr-25-q5_0.bin`、ffmpeg 與 DLL。

1. 跑 `install.bat`、`start.bat`，打開 `http://127.0.0.1:<BREEZE_PORT>`。
2. 確認畫面不是「還缺 whisper／模型／ffmpeg」，且沒有改走雲端。
3. 連續講話 30 分鐘。
4. 記錄崩潰、缺段、英譯失敗時中文是否仍在、停止後軌道是否釋放。
5. 匯出 SRT，對一下 t0／t1。不要填沒量到的 RTF、p50、p95、RAM、CPU，也不要寫模型只載入一次。

這台做不到，所以沒有結果。

### 2 小時連續跑（阻塞）

1. 同一主持機連續跑 2 小時，會話保持 active。
2. 確認房間不被閒置掃描清掉，`tmp` 不殘留音檔目錄。
3. 沒有金鑰就只記中文模式，不要編 401、429 或金額。
4. 不要補延遲或記憶體曲線。

這台做不到，所以沒有結果。

### 3 台手機或平板（阻塞）

1. 三台與電腦同一 Wi-Fi，掃 QR，網址不得是 localhost。
2. 三台看到同一房間。沒金鑰只要求中文。
3. 一台斷線再連，依 cursor 補段；若 gap，畫面應說明補償窗口不足。
4. 連一個未開的房間，確認沒有新房間被匿名建立。
5. 從手機呼叫主持權杖與 `/api/push`，應被拒絕。

這台沒有手機，所以沒有結果。

## 阻塞條件

- 需要 Windows 主持機、麥克風、Breeze q5 權重、whisper.cpp Windows 套件、ffmpeg 與 DLL
- 英譯 401／429／計費需要使用者自己的金鑰，這次沒有代填
