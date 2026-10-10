# Live Room 效能調校與量測指南（zh-TW）

本文件僅整理目前程式碼中真正存在的設定與環境變數，並附上定義來源。所有效能數字與建議都以「待實測」標示，不做未驗證的量化承諾。

適用對象：Windows 筆電、以 zh -> en 或 zh -> ja 單一目標語言跑 live captions，且以本倉庫 `sidecar/live-room` 的現有實作為準。

## 1. 開場前檢查

1. 插電與電源計畫
   - 這個 repo 沒有在程式中直接改寫 Windows 電源計畫；真正的安全檢查在 benchmark 工具裡會判斷 AC/DC 狀態，並在電池模式下保守失敗。
   - 相關定義：`scripts/bench/zbench.py` 的 `on_battery()` 與 `_win_ac_line_status()`，以及 `classify()` 會在電池狀態下不把結果判為 OK。
   - 也就是說，「插電 + Windows 電源計畫設為 最佳效能」屬於使用者手動操作，不是程式自動設定。

2. 背景同步、瀏覽器分頁與其他干擾
   - 本倉庫沒有自動關閉背景同步或瀏覽器分頁的設定；這類操作必須由使用者自行處理。
   - `scripts/bench/zbench.py` 明確要求：直播房間必須空閒，不能有即時串流；若電池狀態不明或是電池模式，結果不應被視為有效量測。

3. Defender 排除（使用者自行決定）
   - 目前程式碼沒有任何自動加入 Windows Defender 排除的邏輯，也沒有自動調整 Defender 設定的 env 變數。
   - 若要把模型、暫存資料夾、benchmark 目錄加入排除，必須由使用者本機自行決定與執行；這不屬於 repo 內的設定項目。

4. 程式實際會檢查什麼
   - ASR / 翻譯 / benchmark 會讀環境變數，而不是改 Windows 設定。
   - 真正存在的設定來源：`app/settings.py`、`app/asr_tuning.py`、`app/runtime_tuning.py`、`app/translate_config.py`、`app/vad.py`、`app/draft_asr.py`、`app/mt_backend.py`。

來源：`app/settings.py`、`scripts/bench/zbench.py`。

## 2. ASR 參數（依現有程式設定名）

### 2.1 主要設定（`Settings` / `.env`）

`app/settings.py` 內定義了 live-room 主要設定，並從環境變數讀取：

- `BREEZE_ASR`：`cli` / `resident` / `native`，定義在 `Settings.asr_mode` 與 `Settings.from_env()`。
- `BREEZE_ASR_THREADS`：對應 `Settings.asr_threads`，預設 `6`。
- `BREEZE_ASR_AUDIO_CONTEXT`：對應 `Settings.asr_audio_context`，預設 `0`，允許 `0` 或 `128..1500`。
- `BREEZE_ASR_BEAM_SIZE`：對應 `Settings.asr_beam_size`，預設 `0`，範圍 `0..8`。
- `BREEZE_ASR_BEST_OF`：對應 `Settings.asr_best_of`，預設 `0`，範圍 `0..8`。
- `BREEZE_ASR_TIMEOUT`：`Settings.asr_timeout_s`，預設 `120.0`。
- `BREEZE_RESIDENT_URL`、`BREEZE_RESIDENT_STARTUP_TIMEOUT`：resident / whisper-server 相關。

來源：`app/settings.py`（`Settings` 與 `Settings.from_env`）。

### 2.2 Native ASR（`native` 模式）

`app/asr_tuning.py` 定義了 native worker 的環境變數：

- `BREEZE_ASR_FLASH_ATTN`：預設 `0`，對應 `NativeTuning.flash_attn`。
- `BREEZE_ASR_TEMPERATURE_INC`：預設 `0.0`，對應 `temperature_inc`。
- `BREEZE_ASR_AUDIO_CONTEXT_MIN`：預設 `640`，對應 `ctx_min`。
- `BREEZE_ASR_MAX_TOKENS`：預設 `0`，對應 `max_tokens`。
- `BREEZE_ASR_REPEAT_GUARD`：預設 `1`，對應 `repeat_guard`。
- `BREEZE_ASR_TIMINGS`：預設 `1`，對應 `timings`。
- `BREEZE_ASR_PRIORITY`：預設 `above_normal`，只在 Windows 上調整 process priority。
- `BREEZE_ASR_ECOQOS_OFF`：預設 `1`，Windows 上關閉 EcoQoS。
- `BREEZE_ASR_LOG`：預設為 `logs/asr-worker.log`，或可關閉。

`app/asr_tuning.py` 也定義：

- `audio_ctx_for(seconds, fixed=0, ctx_min=640)`：`fixed > 0` 會優先；否則依片長計算。
- `max_tokens_for(seconds, fixed=0)`：`fixed > 0` 優先；否則依片長計算。
- `FULL_CTX = 1500`，`CTX_STEP = 64`，表示 30 秒窗長的上限與步進。

`app/native_worker.py` 進一步確認了 native worker 啟動引數：

- `--threads`, `--context`, `--beam`, `--best`, `--flash-attn`, `--temperature-inc`, `--context-min`, `--max-tokens`, `--repeat-guard`, `--timings`, `--priority`, `--ecoqos-off`。
- 其中 `context_params={'use_gpu': False, ...}` 被硬編碼為 `False`，也就是現有 native worker 預設是 CPU 路徑。

來源：`app/asr_tuning.py`、`app/native_worker.py`、`app/native_asr.py`。

### 2.3 draft ASR（實驗性／未接線）

`app/draft_asr.py` 只定義草稿模型的介面與環境開關，不是正式 live pipeline：

- `BREEZE_DRAFT_ASR`：只有 `off` 或 `sherpa`。
- `BREEZE_DRAFT_MODEL_DIR`：模型目錄。
- `BREEZE_DRAFT_THREADS`：draft recognizer 的執行緒。

這個模組說明它是「草稿（draft）ASR」設計，未接線到主流程；任何數字都要以「待實測」視之。

來源：`app/draft_asr.py`。

## 3. MT 參數與量化建議

### 3.1 真正存在的 local MT 設定

`app/translate_config.py` 與 `app/runtime_tuning.py` 讀取的環境變數有：

- `BREEZE_TRANSLATE_ENGINE`：預設 `openai`；只有 `local` 與 `openai` 兩種值。
- `BREEZE_TRANSLATE_BASE_URL`：預設 `http://127.0.0.1:11434/v1`（local）
- `BREEZE_TRANSLATE_ALLOW_REMOTE`：僅在明確允許外部主機時使用。
- `BREEZE_TRANSLATE_MODEL`：預設 `qwen3:4b`
- `BREEZE_TRANSLATE_PROTOCOL`：`auto` / `openai` / `ollama`
- `BREEZE_TRANSLATE_NO_THINK`：預設 `1`
- `BREEZE_TRANSLATE_EXTRA_BODY`
- `BREEZE_TRANSLATE_API_KEY`
- `BREEZE_TRANSLATE_NUM_THREAD`：預設 `4`
- `BREEZE_TRANSLATE_NUM_CTX`：預設 `2048`
- `BREEZE_TRANSLATE_KEEP_ALIVE`：預設 `-1`
- `BREEZE_TRANSLATE_QUEUE`：預設 `4`
- `BREEZE_TRANSLATE_STALE_S`：預設 `8.0`
- `BREEZE_TRANSLATE_STALE_POLICY`：`skip` / `merge` / `off`
- `BREEZE_TRANSLATE_LATE_POLICY`：`drop` / `publish`
- `BREEZE_TRANSLATE_LATE_S`
- `BREEZE_LOCKED_TERM_POLICY`：`flag` / `withhold`
- `BREEZE_TRANSLATE_PRIORITY`：`idle` / `below_normal` / `normal`

來源：`app/translate_config.py`、`app/runtime_tuning.py`。

### 3.2 `ja` 的量化建議

目前 repo 沒有任何 `BREEZE_MT_*_QUANT`、`BREEZE_TRANSLATE_QUANT`、`BREEZE_..._Q4`、`BREEZE_..._Q8` 這類明確量化開關。

`app/translate_config.py` 只把 `num_thread`、`num_ctx`、`keep_alive` 與 model 名稱當作本機翻譯參數；它沒有把「量化格式」作為設定項。

`app/mt_backend.py` 的 Hy-MT2 backend 可以設定：

- `BREEZE_MT_BACKEND`
- `BREEZE_MT_BASE_URL`
- `BREEZE_MT_MODEL`
- `BREEZE_MT_PROMPT`
- `BREEZE_MT_TEMPERATURE`
- `BREEZE_MT_MAX_TOKENS`

但它沒有 `ja` 專用精度量化開關。實際每 session 的目標語言由 `SessionTargets` 維護，預設為 `"en"`，且可切到 `"ja"`；範圍是 `TARGET_LANGS = ("en", "ja")`。這是 session-level 配置，不是量化層級配置。

因此，若你想討論「ja 可能需要較高精度量化」，這個 repo 中目前沒有正式設計好的 env knob；任何這類調整都只能標示為「待實測」。

來源：`app/mt_backend.py`、`app/translate_config.py`。

## 4. iGPU / Vulkan 是否值得開、怎麼開、怎麼退回 CPU

1. 目前程式碼中「沒有現成的 Vulkan / iGPU 開關」
   - `app/native_worker.py` 中 `context_params={'use_gpu': False, ...}` 已經明確把 native worker 設成 `False`。
   - 這表示：在本 repo 現行實作中，ASR native worker 預設走 CPU，沒有 `BREEZE_ASR_USE_GPU`、`BREEZE_ASR_VULKAN`、`BREEZE_VULKAN_*` 等實際存在的 env 變數。

2. 這代表什麼
   - 若你使用的是目前 `sidecar/live-room` 的現成設定，沒有可用的「開 iGPU / Vulkan」開關。
   - 若是自行採用另外的 native binding 或外部模型堆疊，才可能有 GPU 路徑；但那不屬於這份 repo 目前可見的設定檔。

3. 怎麼退回 CPU
   - 保持現在預設：`context_params={'use_gpu': False}`，或確保任何自訂的 native binding 不覆寫成 `True`。
   - 這是現有程式能可靠退回 CPU 的做法；沒有對應的 env 變數可以直接打開/關閉 GPU 路徑。

4. 「是否值得開」
   - 這個 repo 中沒有任何 CPU/GPU 比較數據，可直接宣稱 iGPU / Vulkan 「值得開」或「不值得開」。
   - 所有關於 AMD iGPU / Vulkan 的效能結論，都只能寫成「待實測」。

來源：`app/native_worker.py`。

## 5. 記憶體預算表（ASR + MT + 系統）

以下表格中的數字都以「待實測」為主，因為 repo 內沒有可直接硬編碼的 benchmark 數值。這裡只列出實際會影響佔用的因素：

| 項目 | 影響因素 | 來源 | 備註 |
|---|---|---|---|
| ASR | `BREEZE_ASR_THREADS`、`BREEZE_ASR_AUDIO_CONTEXT`、`BREEZE_ASR_BEAM_SIZE`、`BREEZE_ASR_BEST_OF`、model 大小 | `app/settings.py`, `app/asr_tuning.py`, `app/native_worker.py` | ASR 記憶體「待實測」 |
| MT / local LLM | `BREEZE_TRANSLATE_NUM_THREAD`、`BREEZE_TRANSLATE_NUM_CTX`、`BREEZE_TRANSLATE_MODEL`、`BREEZE_TRANSLATE_PROTOCOL` | `app/translate_config.py`, `app/runtime_tuning.py` | 翻譯佔用「待實測」 |
| 系統 / 背景應用 | Windows、瀏覽器、同步、Defender 工作負載 | `scripts/bench/zbench.py` | 系統預留「待實測」 |
| draft ASR（實驗） | `BREEZE_DRAFT_MODEL_DIR`、`BREEZE_DRAFT_THREADS` | `app/draft_asr.py` | 草稿模式佔用「待實測」 |
| VAD | `BREEZE_VAD`、`BREEZE_VAD_MODEL`、`BREEZE_VAD_THRESHOLD`、`BREEZE_VAD_MIN_SPEECH_RATIO` | `app/vad.py` | VAD 記憶體「待實測」 |

實務上，這些是你在測試時需要記錄的欄位：

- ASR：`BREEZE_ASR`、`BREEZE_ASR_THREADS`、`BREEZE_ASR_AUDIO_CONTEXT`、`BREEZE_ASR_BEAM_SIZE`、`BREEZE_ASR_BEST_OF`
- MT：`BREEZE_TRANSLATE_ENGINE`、`BREEZE_TRANSLATE_NUM_THREAD`、`BREEZE_TRANSLATE_NUM_CTX`、`BREEZE_TRANSLATE_MODEL`
- 系統：Windows 版本、RAM 狀態、背景程序狀態、是否插電
- 性能：RTF p95、CPU、記憶體、風扇狀態、是否有 backlog

所有數值都記為「待實測」；不要把任何單一量測結果當成正式硬性上限。

## 6. 如何用 `tools/bench_pipeline.py` 與 `tools/rtf_check.py` 量測並記錄結果

### 6.1 `tools/bench_pipeline.py`

此腳本會模擬 pipeline 的 ASR / translation 量測，並由 `PRESETS` 定義不同情境（`A` / `B` / `soak`）。

實際命令：

```powershell
python tools/bench_pipeline.py --scenario A --json .\bench_A.json
python tools/bench_pipeline.py --scenario B --json .\bench_B.json
python tools/bench_pipeline.py --scenario soak --json .\bench_soak.json
```

它會依據 `app/server.py` 的 `create_app`、`app/settings.py` 的 `Settings`、以及 `app.asr` / `app.translate` 的行為來模擬真實排程；不會發送任何外部 API 呼叫。

輸出重點：

- `rtf`：即時率（real-time factor）
- `backlog`：等待辨識或翻譯的累積時間
- `asr`、`translate` 的每段延遲
- 結果可保存為 JSON 或終端報告

來源：`tools/bench_pipeline.py`。

### 6.2 `tools/rtf_check.py`

此腳本用來量測這台機器上辨識的 RTF。它支援：

```powershell
python tools/rtf_check.py metrics --base http://127.0.0.1:8780
python tools/rtf_check.py run --n 4 --audio sample.wav
```

它會讀 `Settings.from_env()` 與 `build_asr()`，並把 `threads` 參數只覆蓋當次實驗，不改寫保存的設定檔。

`tools/rtf_check.py` 也明確寫了：

- PASS 定義為「本次 session RTF p95 < 0.9」
- 但實機結果仍須在真正主持機上驗證，否則要以「尚未驗證」表示

這代表：你可以在本機跑量測，但「該台機器的正式認證」需要在正式直播主機上重跑。

來源：`tools/rtf_check.py`。

### 6.3 空白量測表（可直接複製）

| 日期 | 會議/場景 | `BREEZE_ASR` | `BREEZE_ASR_THREADS` | `BREEZE_ASR_AUDIO_CONTEXT` | `BREEZE_ASR_BEAM_SIZE` | `BREEZE_ASR_BEST_OF` | `BREEZE_TRANSLATE_NUM_THREAD` | `BREEZE_TRANSLATE_NUM_CTX` | `BREEZE_TRANSLATE_MODEL` | RTF p95 | backlog | 記憶體 | 風扇 | 結論 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 | 待實測 |

也可以用以下簡短格式記錄：

- 設定：`BREEZE_ASR=native` / `BREEZE_ASR_THREADS=待實測` / `BREEZE_ASR_AUDIO_CONTEXT=待實測`
- MT：`BREEZE_TRANSLATE_ENGINE=待實測` / `BREEZE_TRANSLATE_NUM_THREAD=待實測` / `BREEZE_TRANSLATE_NUM_CTX=待實測`
- 結果：RTF p95 = 待實測，latency = 待實測，backlog = 待實測，記憶體 = 待實測

## 7. 常見問題

### Q1：延遲越來越高

可能原因：

- ASR 處理速度跟不上音訊進度；`BREEZE_ASR_THREADS`、`BREEZE_ASR_AUDIO_CONTEXT`、`BREEZE_ASR_BEAM_SIZE`、`BREEZE_ASR_BEST_OF` 都會影響這一點。
- 翻譯 queue 與 stale policy 滯留；`BREEZE_TRANSLATE_QUEUE`、`BREEZE_TRANSLATE_STALE_S`、`BREEZE_TRANSLATE_STALE_POLICY`、`BREEZE_TRANSLATE_LATE_POLICY` 會直接影響 backlog。
- 這些設定都存在於 `app/settings.py`、`app/asr_tuning.py`、`app/runtime_tuning.py` 和 `app/translate_config.py`；沒有更高層的全域自動調校。

建議順序：

1. 先固定 ASR 參數（`BREEZE_ASR_*`）。
2. 再固定 MT（`BREEZE_TRANSLATE_*`）。
3. 用 `tools/bench_pipeline.py` 與 `tools/rtf_check.py` 量測 backlog 與 RTF。
4. 如果 backlog 持續增加，先檢查 queue / stale policy，不要直接假設為 GPU 或電源問題。

來源：`app/settings.py`、`app/runtime_tuning.py`、`app/translate_config.py`。

### Q2：風扇狂轉

- 程式碼中「以 Windows 給予較高 priority」的設定只有 `BREEZE_ASR_PRIORITY`，且它只是 worker process 的 priority；沒有解釋為「關閉散熱」或「降低風扇」。
- `BREEZE_ASR_ECOQOS_OFF` 會在 Windows 上關掉 EcoQoS；這是為了避免電源調節/節流邏輯影響即時性，不是散熱管理工具。
- 任何風扇噪音與散熱狀態，仍應以實機量測視之；在 repo 中沒有關於風扇策略的設定。

來源：`app/asr_tuning.py`。

### Q3：電池模式下數字不可信

- 這是 repo 的明確要求：benchmark 會在電池模式下保守失敗，不把結果判定為 OK。
- 具體實作見 `scripts/bench/zbench.py` 的 `on_battery()` 與 `classify()`；它會在 AC 狀態不明時保守判定。
- 所以只要你在電池模式或未確認電源狀態下跑 benchmark，數字都不應該被當成正式有效結果，應標示為「待實測」或直接停用測試。

來源：`scripts/bench/zbench.py`。

### Q4：我想知道「這台電腦是否值得開 iGPU/Vulkan」

- 這個 repo 內沒有支援的 iGPU/Vulkan 開關；現有 native ASR worker 直接固定 `use_gpu=False`。
- 因此，對本 repo 而言，最正確的結論是：目前沒有可用的環境變數來開啟 iGPU/Vulkan；任何這類建議都屬於「待實測」外部實驗，而不是 repo 內的設定。

來源：`app/native_worker.py`。

## 總結

- 這份指南中提到的所有設定，只有以下幾類是本倉庫中可見且實際存在的：
  - `Settings` / `.env`（`app/settings.py`）
  - ASR tuning（`app/asr_tuning.py`、`app/native_worker.py`、`app/native_asr.py`）
  - MT / local translation（`app/translate_config.py`、`app/runtime_tuning.py`）
  - VAD / draft ASR（`app/vad.py`、`app/draft_asr.py`）
  - benchmark / RTF 驗證工具（`tools/bench_pipeline.py`、`tools/rtf_check.py`）
- 任何關於「GPU 效能」、「記憶體上限」、「ja 更高精度量化」和「實機定量 benchmark」的數字，均以「待實測」表示，且不應在未實際量測前當成正式設定值。

這份文件只記錄當前代碼中已定義的內容；未來若要加入新的 GPU/OpenVINO/Vulkan 或更精細的 ASR/MT 量化選項，應先在程式碼中新增實際 env 變數與定義，並再補回正式的測試與文件說明。
