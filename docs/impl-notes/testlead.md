# testlead（全站測試長）交件：桌面流程煙霧測試 `tools/smoke_desktop.py`

- 分支：`impl/testlead`（worktree `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\testlead`）
- 基準：`grok/integrate` @ `bea2e5c`（round4 #2: T4 prompt 'current' = live app prompt）
- Commit：`0faa4cd34dc9538ff0148e81b2c1aa497ca188cb`（`0faa4cd`），只新增 2 個檔，未改任何既有檔：
  - `sidecar/live-room/tools/smoke_desktop.py`（838 行）
  - `sidecar/live-room/tests/test_smoke_desktop.py`（192 行）
- 合併：`git merge impl/testlead`。注意 `sidecar/live-room/.gitignore` 第 3 行忽略 `tools/`，所以工具是用 `git add -f` 加入（跟 `rtf_check.py` 一樣）；之後修改也要 `-f`。
- 本 NOTES.md 沒有 commit（放在 worktree 根目錄、未追蹤），避免和其他代理的 NOTES.md 合併衝突。

## 做了什麼
端到端煙霧測試，每步印 `[PASS]/[FAIL]/[SKIP]`，第一個 FAIL 會被點名（`RESULT: FAIL at step '<名>'`），結束碼 0＝全過、1＝某步失敗、2＝參數錯：

1. `start_backend`：在空的 loopback 埠啟動後端（`sys.executable` 子程序），等 `/api/health` 就緒。永不使用 8645（指定 8645 直接以結束碼 2 拒絕）。
2. `host_token`：`GET /api/host-token`（token 不印出）。
3. `open_room`：`/api/rooms/open` + `/api/session/active`（每次唯一 session id）。
4. `captions_zh`：`/api/push` 假音訊，每段要回中文。
5. `translation`：每段譯文要通過 en / ja 字幕檢查（每場只一種語言）。
6. `pause_resume`：暫停中上傳必須被拒（409）；恢復＋寬限期後新段落要有中文與譯文；暫停段之後不得出現。
7. `export`：`/api/export` srt / vtt / txt / json；SRT cue 含中文＋譯文、不含暫停段。
8. `end_session`、9. `shutdown`：graceful 結束（Windows 用 `CTRL_BREAK_EVENT`／fake 用 stdin `quit`），逾時才 terminate/kill，不留殘留程序。

假元件模式（`--fake`，預設）：子程序跑「真的」`create_app`，只注入假 ASR（中文文字放在真 WAV 的 `zhtx` chunk）、假 decoder，以及 loopback 假 Ollama（en，走真 `app.translate.Translator`）/ 假 llama-server（ja，走真 `app.mt_backend.HyMtLlamaServer`）。無網路、無模型、ledger/TM/VAD/embedding 關閉、資料在暫存資料夾（工具自己建的暫存資料夾結束後移除，可用 `--keep-work` 保留）。沒有改任何禁改檔（pipeline.py、server.py、mt_backend.py、rtf_check.py 等）。

### 筆電上找到並修掉的 Windows 問題
第一次在筆電跑 `tests/test_smoke_desktop.py`：7 failed / 7 passed / 1 skipped。原因：輸出被 pipe 時 Windows stdout 是 cp1252，第一行中文就 `UnicodeEncodeError`，報告 JSON 也沒寫出。修法：`__main__` 時，非 console 的 stdout/stderr 改 UTF-8（`errors="replace"`），真正的 console 不動。新增回歸測試 `test_piped_ansi_codepage_stdout_does_not_crash`（以 `PYTHONIOENCODING=cp1252` 重現）。

## 測試結果（筆電 DESKTOP-P8RGA3A 量測）
指令（只跑自己的測試檔，未跑全套）：
```powershell
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\testlead\sidecar\live-room
C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_smoke_desktop.py -q -p no:cacheprovider -rs
```
- 結果：**15 passed, 1 skipped, 0 failed**，pytest 39.30 秒（wall 40.2 秒），23:33:42–23:34:24（台北時間）。
- 當時負載：CPU 使用率 64% → 35%（`Win32_Processor.LoadPercentage`），可用記憶體 4408 → 4609 MB／15724 MB。
- 唯一 skip：`test_real_mode_on_windows_host`——需要真元件（模型、ffmpeg）與 `ZEN_SMOKE_REAL_AUDIO=<中文語音.wav>`，這次沒有提供語音檔，也依指示不下載模型。
- Python：共用 venv，Python 3.12.10，pytest 9.1.1，uvicorn 0.54.0。`*_API_KEY`：環境中沒有（執行前仍逐一清除）。

## 煙霧測試實跑輸出（筆電 DESKTOP-P8RGA3A 量測，Windows、假音訊、自動空埠）
跑前確認 8645 / 8791 都沒有在監聽；沒有碰已安裝的 app。開始 23:34:42（CPU 60%），結束 23:34:54（CPU 31%）。

```text
> python tools\smoke_desktop.py --fake --lang en --json %TEMP%\zen-smoke-en.json
[PASS] start_backend (1328 ms): port 58562，fake 已就緒
[PASS] host_token (14 ms): 已取得（不印出）
[PASS] open_room (32 ms): room=smoke session=smoke-20261010233444-31360
[PASS] captions_zh (358 ms): 2 段中文已回來
[PASS] translation (391 ms): 2 段 en 譯文通過字幕檢查
[PASS] pause_resume (2187 ms): 暫停中上傳被拒 409；恢復等 1.8 秒後 seq 4 有中文與譯文
[PASS] export (46 ms): SRT 3 cues；vtt/txt/json 200；暫停段未出現
[PASS] end_session (0 ms): ok
[PASS] shutdown (483 ms): graceful 結束（exit 0，0.5 秒）
RESULT: PASS（fake, en, 4842 ms）
exit=0

> python tools\smoke_desktop.py --fake --lang ja --json %TEMP%\zen-smoke-ja.json
[PASS] start_backend (1375 ms): port 58685，fake 已就緒
[PASS] host_token (0 ms): 已取得（不印出）
[PASS] open_room (32 ms): room=smoke session=smoke-20261010233450-13984
[PASS] captions_zh (264 ms): 2 段中文已回來
[PASS] translation (297 ms): 2 段 ja 譯文通過字幕檢查
[PASS] pause_resume (2187 ms): 暫停中上傳被拒 409；恢復等 1.8 秒後 seq 4 有中文與譯文
[PASS] export (30 ms): SRT 3 cues；vtt/txt/json 200；暫停段未出現
[PASS] end_session (0 ms): ok
[PASS] shutdown (500 ms): graceful 結束（exit 0，0.5 秒）
RESULT: PASS（fake, ja, 4687 ms）
exit=0
```
跑前、跑後的 python 程序 PID 相同（2944, 11112, 17112, 20348），沒有殘留程序。

## Windows 怎麼跑（PowerShell）
```powershell
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\testlead\sidecar\live-room
$py = "C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe"
& $py tools\smoke_desktop.py --fake --lang en
& $py tools\smoke_desktop.py --fake --lang ja --json $env:TEMP\smoke.json --save-srt $env:TEMP\smoke.srt
# 真元件（需要已備好的模型／ffmpeg 與一段中文語音 WAV；本次未跑）
& $py tools\smoke_desktop.py --real --lang en --audio C:\path\speech.wav --timeout 180
# 測試
& $py -m pytest tests/test_smoke_desktop.py -q
```
其他參數：`--port N`（0＝自動）、`--timeout`、`--start-timeout`、`--asr-timeout`、`--mt-timeout`、`--room`、`--work-dir`、`--keep-work`、`--quiet`。

## 已知限制
- `--real` 沒有在這次實跑（沒有語音檔、不下載模型）；`--real` 啟動 `python -m app.run`，用設定好的 ASR/MT 與資料夾，會寫入該設定的資料位置，跑之前請確認不是正式資料。
- ja：pipeline 目前不會把 `tgt_lang` 傳給 MT（`app/pipeline.py` 搜不到 `tgt_lang`）。假模式用工具內的 `JaPipelineAdapter` 把 `HyMtLlamaServer(tgt_lang="ja")` 接到 pipeline 的翻譯呼叫，只是測試用轉接器。
- 假模式驗證的是 HTTP 流程、暫停/恢復語意、匯出內容與關機；不量 ASR/MT 品質或延遲。上面的毫秒數是假元件的流程時間，不代表真實效能。
- 測試會開子程序與 loopback 埠；同機有很多重程序時，`test_overall_timeout_is_honored` 依賴時間上限（5 秒＋關機預算），屬時間敏感測試。
- box 參考（box 量測（Xeon，非 5600H），舊基準 6ec5012、整套）：980 passed / 39 skipped / 2 failed，338.97 秒，負載 load average 4.76 / 6.55 / 5.09（23:28）。2 個失敗是既有的 `test_sim_backlog.py::test_100min_session_never_waits`、`test_sim_srt.py::test_100min_srt_valid_and_monotonic`，跟本交件無關，在高負載下標為 UNKNOWN（時間/模擬相關），沒有用重跑通過當作修好。筆電依指示沒有跑全套，由整合者跑。

## BLOCKED
- 真元件 ja 端到端：要等 pipeline 原生支援 `tgt_lang`（禁改檔 `app/pipeline.py`，整合者負責）。
- `--real` 實跑：需要整合者提供中文語音 WAV 與已就緒的模型（X-ASR 由整合者處理）。
