# perf（效能長）交件說明 — app/hw_tune.py

- 分支：`impl/perf`（worktree `C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\perf`）
- 基準：`grok/integrate` @ `bea2e5c`
- Commit：`15441e2896372f06b294345a027b608e02922534`（`15441e2`），只新增 4 個檔，沒有改動任何既有檔案：
  - `sidecar/live-room/app/hw_tune.py`（新模組）
  - `sidecar/live-room/app/hw_routes.py`（唯讀 APIRouter：`GET /api/hw/status`）
  - `sidecar/live-room/tests/test_hw_tune.py`、`tests/test_hw_routes.py`
- 沒有 push，沒有開 PR。

## 做了什麼
1. **行程優先權**：ASR（whisper.cpp worker／native worker）設 ABOVE_NORMAL，MT（llama-server、ollama runner）設 BELOW_NORMAL，embedding 設 IDLE。程式裡寫死永遠不設 REALTIME；目標本來就是 HIGH 或 REALTIME 的行程會跳過，不去降它。每個目標回傳結構化結果 `Result(role, action, status, pid, name, detail)`，status 是 `applied / denied / not_found / unsupported / skipped / failed`。Ollama 以服務或其他使用者身分執行時，OpenProcess 拿到 AccessDenied 會回 `denied` 並寫 log，不會默默吞掉。
2. **EcoQoS／power throttling**：ASR 用 `SetProcessInformation(ProcessPowerThrottling)` 關掉節流（ControlMask=EXECUTION_SPEED，StateMask=0）；`ZEN_HW_TIMER_RES=1` 時另外把 IGNORE_TIMER_RESOLUTION 也關掉。embedding 預設打開 EcoQoS。舊版 Windows 沒有這個 API 時回 `unsupported`。目前執行緒的 background mode 用 `enter_background_thread()`（PROCESS_MODE_BACKGROUND 只對自己有效，所以給 embedding worker 執行緒在自己身上呼叫）。
3. **CPU 親和**：用 `GetLogicalProcessorInformationEx(RelationProcessorCore)` 讀實際的核心對應，不假設 SMT 兄弟是相鄰編號（測試裡有不相鄰的對應）；API 失敗時退回 psutil，再退回 `os.cpu_count`。親和**預設關閉**（`ZEN_HW_AFFINITY=1` 才開），開了也只在 policy=split、有真實核心對應、沒有超額配置時才套用，ASR 和 MT 各拿完整的實體核心（兩個 SMT 兄弟一起）。
4. **插電／電源計畫偵測（唯讀）**：`GetSystemPowerStatus`（ACLineStatus 0/1/255；BatteryFlag 128＝沒有電池，桌機視為 AC）、`PowerGetActiveScheme`（GUID 對應到 Balanced / High performance / Power saver / Ultimate），Win11 電源模式用 `PowerGetEffectiveOverlayScheme`，沒有的話改用 `PowerGetActualOverlayScheme`，兩個都沒有就標 UNKNOWN。絕對不改電源計畫（測試會檢查沒有呼叫任何 PowerSet*）。電源模式是「最佳電源效率」或計畫是 Power saver 時，在 plan.warnings 加上提醒。
5. **5600H 執行緒建議** `recommend_threads()`（純函式）：預設 policy `split` 在 6C/12T 上給 ASR 4 + MT 2（draft ASR 佔核時 MT 降到 1，讓 Breeze 至少保留 4）；`asr_first` 給 ASR 6 + MT 2，靠優先權而不靠親和。重度執行緒合計超過實體核心時標 `oversubscribed` 並警告；embedding 只有在不在直播中而且插電時才給執行緒。`ThreadPlan.env()` 輸出 `BREEZE_ASR_THREADS / BREEZE_TRANSLATE_NUM_THREAD / BREEZE_DRAFT_THREADS / ZEN_EMBED_NUM_THREAD`，每個建議都附 rationale 字串。
6. **非 Windows**：全部 no-op，回 `unsupported`；psutil 是選用的，import 有包起來。
7. **入口**：`apply_profile(profile=None, pids=None)`（冪等，可以重複呼叫，例如 Ollama runner 重啟換了 PID 時再呼叫一次）、`revert()`（還原第一次記下的原始優先權／親和／節流狀態）、`status()`（唯讀快照）。`python -m app.hw_tune` 會把 status 印成 JSON。
8. **看得到的變化**：`app/hw_routes.py` 的 `make_router(guard)` 提供 `GET /api/hw/status`，回傳核心拓撲、插電/電源模式、建議執行緒和警告，給主控頁或後台顯示（例如「電源模式是最佳電源效率，ASR 會變慢」）。

## 設定名稱（沿用既有命名）
`BREEZE_ASR_PRIORITY`（normal|above_normal，與 asr_tuning.py 一致）、`BREEZE_ASR_ECOQOS_OFF`、`ZEN_HW_POLICY`（split|asr_first）、`ZEN_HW_AFFINITY`（預設關）、`ZEN_HW_MT_PRIORITY`（idle|below_normal|normal）、`ZEN_HW_TIMER_RES`、`ZEN_HW_EMBED_ECOQOS`、`ZEN_HW_EMBED_MATCH`、`ZEN_HW_MT_EXCLUDE`（MT 比對時排除 embedding 用的 runner）。

## 測試
筆電（DESKTOP-P8RGA3A，共用 venv）：
```
cd C:\Users\MacMiRyzen5\Documents\zen-bridge-impl\perf\sidecar\live-room
C:\Users\MacMiRyzen5\Documents\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_hw_tune.py tests/test_hw_routes.py -q -p no:cacheprovider
```
結果：**53 passed, 0 failed**（1 個 warning 是 starlette TestClient 的 httpx 棄用提示，跟這個模組無關）。其中 2 個是真實 Win32 測試（只在 Windows 跑）：一個是 status() 的唯讀檢查；另一個只對測試自己開的 python 子行程套用 BELOW_NORMAL、親和、ABOVE_NORMAL＋EcoQoS 關閉，再 revert 並確認還原，最後結束這個子行程。
box（Linux，mock）：51 passed, 2 skipped。依 BRIEF，全套由整合者跑；稍早在 box 跑全套時只有 `test_sim_backlog.py` / `test_sim_srt.py` 這類計時測試在高負載下失敗，這兩個檔不會 import hw_tune。

## 真實 API 煙霧值（筆電，唯讀，2026-10-10 約 23:45 台北時間）
- 核心拓撲（GLPI_EX）：12 邏輯 / 6 實體，SMT 兄弟是相鄰編號 [0,1] [2,3] [4,5] [6,7] [8,9] [10,11]
- 電源：AC 插電（ACLineStatus=1），BatteryFlag=9，電量 91%，有電池
- 電源計畫：Balanced（381b4222-f694-41f0-9685-ff5bb260df2e）
- **Win11 電源模式：Best power efficiency（961cc777-2547-4f9d-8174-7d86181b8a7a）**（來源 PowerGetEffectiveOverlayScheme）← 插電中卻是最省電模式，會拖慢 ASR／RTF。建議柏能在場次前切到「平衡」或「最佳效能」（程式不會自己改）。
- 建議執行緒：ASR 4 / MT 2 / draft 0 / embed 0，沒有超額配置
- 測試用 python 行程本身的優先權類別：0x20（NORMAL）

## 整合者接線位置（pipeline.py / server.py / native_asr.py / mt_backend.py 我沒碰）
1. **server.py 啟動時**：`app.include_router(hw_routes.make_router(lambda r: require_host(r, ...)))`，guard 用既有的主控驗證；主控頁可以顯示 plan.warnings。
2. **native_asr.py 開好 whisper worker 之後**：`hw_tune.apply_profile(pids={"asr": [proc.pid]})`。asr_tuning.py 已經有 BREEZE_ASR_PRIORITY/ECOQOS 的處理，二選一就好，避免重複設定（重複呼叫是無害的）。
3. **mt_backend.py 開好 llama-server 之後**：`apply_profile(pids={"mt": [proc.pid]})`。用 Ollama 時，在第一次翻譯回應之後（runner 才會出現）呼叫 `apply_profile()` 不帶 pids，讓它依名稱找 runner；之後每隔一段時間或偵測到 runner PID 改變時再呼叫一次。
4. **embedding worker 啟動時**：在 worker 執行緒內呼叫 `enter_background_thread(True)`，結束時 `enter_background_thread(False)`。embedding 目前依 BRIEF 維持關閉。
5. **關閉場次時**：`hw_tune.revert()`。
6. 執行緒數：用 `recommend_threads(...).env()` 當成 BREEZE_ASR_THREADS 等設定的預設值（使用者已經設定的值優先）。

## 只能在筆電上實際驗證的事
- Ollama 以服務身分執行時，runner 會不會回 `denied`（這次照規則沒有碰 Ollama 行程）。
- 親和打開（4+2 分核）和預設關閉相比，字幕延遲 p95 的差別（需要 layer 3 實測）。
- EcoQoS 關閉、電源模式切到「最佳效能」之後的 RTF 改善幅度。

## BLOCKED
無。
