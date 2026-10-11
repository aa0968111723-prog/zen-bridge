# 微調資料準備系統（講者錄音、社課收錄、資料集匯出）

> 目標：之後在 NVIDIA GPU 上微調，讓 Breeze-ASR-25 聽懂淡江禪學社的用語與固定講者的口音，讓本機翻譯模型學到社團的說法。
> **這一版只做資料準備與訓練腳本，不在筆電上訓練。** 全部只在本機，不呼叫雲端、不上傳。

程式位置：`sidecar/live-room/app/training/`（後台頁面 `/admin/training`、命令列 `python -m app.training`），訓練腳本在 repo 根目錄 `training/`。

## 1. 資料流

```
           ┌───────────── 講者名冊＋同意（training.sqlite3） ─────────────┐
           │  每個動作都先查：有沒有有效同意？用途（ASR／MT／現場收錄）？      │
           ▼                                                             │
 錄音室（提示句朗讀）  社課現場收錄（主持人逐場開啟）  匯入既有錄音＋逐字稿   │
   16k WAV＋提示句     Breeze 定稿段＋該段 WAV          ffmpeg 轉 16k＋解析時間  │
        │                     │                              │            │
        └──── 每次錄音 = recording：clips/*.wav ＋ live.jsonl（邊錄邊寫） ─┘
                              │ 結束 → recording.wav ＋ transcript.{json,srt,vtt,txt}
                              ▼
                     審核佇列（未校對 → 核准／修正／退回）
                     ├─ 修正後逐字稿 → ASR 配對
                     └─ 修正後譯文   → MT 配對（zh→en／zh→ja）→ 送「待審修正」進翻譯記憶
                              ▼
       資料集建置：品質篩選 → 去重 → 依「講者×場次」切 train/validation/test（無洩漏）
                              ▼
     exports\ds_…\asr\（HF audiofolder＋manifest）  mt\（chat JSONL）  README.md（資料集卡）
                              ▼
            training\scripts：Breeze LoRA、MT LoRA、轉回 ggml／gguf 給 App 用
```

## 2. 本機儲存位置

根目錄：`%LOCALAPPDATA%\ZenBridge\training\`（`ZEN_TRAINING_DIR` 可覆寫；沒有設定時跟著 `ZEN_DATA_DIR`）。與字幕主庫 `data\zen.sqlite3` 分開，撤回同意時可以乾淨刪除。

```
training\
  training.sqlite3                     講者、同意、錄音、片段、MT 配對、收錄開關、匯出、稽核
  recordings\
    studio\<speaker_id>\<rs_…>\        錄音室一次坐下錄
    harvest\<speaker_id>\<rh_…>\       社課一場收錄
    import\mixed\<ri_…>\               匯入（source.wav 是隔離區，可能有沒同意的人的聲音）
      clips\<clip_id>.wav              每段 16 kHz 單聲道 PCM16
      live.jsonl                       錄音中逐行寫入的字幕事件（draft／final／rename／end，含時間）
      recording.wav                    結束後依序串接的整段音檔（匯入則用 source.wav）
      transcript.json / .srt / .vtt / .txt   完整逐字稿，時間軸對齊 recording.wav，含講者標籤
  import\inbox\                        放「同檔名」音檔＋逐字稿，`python -m app.training import --scan`（匯入後刪除收件匣複本）
  import\uploads\                      後台上傳暫存（匯入後刪除）
  exports\ds_YYYYMMDD_HHMMSS_xxxx\     資料集匯出
```

`live.jsonl` 每收到一段就 `fsync` 一次，所以當機或斷電時，已經錄到的音檔和字幕都還在；按停止（或自動停止）才寫出完整逐字稿，之後每次人工修正都會重寫。

## 3. 資料表（schema）

完整定義：`sidecar/live-room/app/training/schema.sql`。

| 表 | 重點欄位 | 說明 |
| --- | --- | --- |
| `speakers` | `id`（`spk_xxxxxxxx`）、`label`（暱稱，≤24 字）、`accent_note`、`withdrawn_at` | **不存學號、電話、Email、真名**；暱稱含 6 位以上數字、`@`、電話格式一律拒絕 |
| `consents` | `scope_asr`、`scope_mt`、`harvest_live`、`local_only`（CHECK = 1）、`consent_version`、`consent_date`、`recorded_by`、`revoked_at` | 每次更新是新的一列，舊的標 `revoked_at`；至少一個用途 |
| `recordings` | `kind`（studio／harvest／import）、`speaker_id`、`room_id`、`session_id`、`dir`、`source_audio`、`status` | 一次錄音＝音檔＋逐字稿 |
| `clips` | `audio_path`、`sha256`、`duration_s`、`rms_dbfs`、`peak_dbfs`、`clip_ratio`、`silence_ratio`、`snr_db`、`draft_text`、`asr_text`（Breeze 原始）、`text`（參考／修正）、`speaker_tag`、`status`（unverified／needs_review／approved／rejected）、`align`（native／file／forced） | 一段話；`segment_id` = live 的 `{room}:{session}:{seq}` |
| `prompts` | `source`（club／glossary／dharma／mixed／custom）、`text`、`term` | 錄音室提示句 |
| `mt_pairs` | `src_text`、`tgt_lang`、`tgt_text`、`clip_id`、`speaker_id`、`segment_id`、`staging_id`、`tm_unit_id` | 審核產生的翻譯配對，連到 zen.sqlite3 的 staging／翻譯記憶 |
| `harvest_optins` | `room_id`、`session_id`（NULL = 綁定下一場）、`speaker_id`、`expires_at`、`stopped_at` | 主持人逐場開啟的現場收錄 |
| `exports` | `dir`、`speakers`（JSON）、`stats`、`deleted_at`、`deleted_reason` | 記錄每份匯出含哪些講者，撤回時找得到 |
| `audit` | `action`、`speaker_id`、`actor`、`detail` | 不放字幕全文、不放暱稱 |

## 4. 講者名冊與同意（必要條件）

* 每位講者：ID＋顯示名稱（暱稱即可）＋同意紀錄（用途 ASR／MT、只限本機、日期、紀錄人、可撤回）。同意書文字固定在 `registry.CONSENT_TEXT`（版本 `zen-train-consent-v1`），後台記錄時必須勾「講者本人已閱讀並同意」。
* **錄音室**需要 ASR 同意；**社課現場收錄**另外需要「允許現場收錄」；**MT 配對**需要 MT 同意。
* 每次匯出都**重新檢查同意**，不是只看當初收錄時。
* **撤回**（後台「撤回並刪除」或 `python -m app.training withdraw spk_… --yes`）會在同一個交易內：撤銷同意、停止收錄、刪除他所有片段音檔與資料列、他的 MT 配對、他的錄音資料夾（含完整逐字稿）、含他聲音的匯入原始檔，以及**所有包含他的本機匯出資料夾**（包括 `--out` 指到訓練資料夾外的匯出，靠 `manifest.json` 的 export_id 核對後才刪）。暱稱改成「已撤回」，只留下沒有個資的稽核列。已經拿去訓練出來的模型無法「忘記」，所以文件與同意書都說明：撤回後要用新的匯出重新訓練。
* 雲端 GPU：同意書 v1 寫的是「只存放在社團本機電腦」，所以**不能**把資料傳到雲端 GPU。若之後要用雲端，必須先改同意書版本並重新取得同意。

## 5. 錄音室（`/admin/training` →「錄音室」）

1. 選一位已同意的講者 →「開始這次錄音」。提示句混合四種來源：社團用語、詞表（唯讀讀取 zen.sqlite3 的 `glossary_terms`，套模板成句子）、開示常用句、中英夾雜句；可加自訂句。
2. 按「錄音」→ 讀 → 按「停止」。停止後會**多收 1.2 秒尾音**才結束，避免切掉最後一個字。錄音時有即時音量條與削波（破音）警告，超過 30 秒自動停止。
3. 瀏覽器把麥克風音訊重新取樣成 16 kHz 單聲道 PCM16 WAV 上傳；伺服器再檢查一次：長度 1–30 秒、削波比例、音量、靜音比例、SNR。太短／太長／幾乎靜音的不存並請重錄；有削波、太小聲、SNR 偏低的會存成「未校對」並建議重錄。
4. 播放器從後台讀回剛存的檔案（符合 CSP，不用 blob）；「重錄這句」會用新的一次取代舊的（舊檔刪除）。
5. 進度：每位講者已錄分鐘數，以及每個來源已錄／總數。
6. 4 分鐘沒有新錄音會提醒，5 分鐘自動結束並寫出完整逐字稿；按「結束並產生逐字稿」也一樣。結束後可一鍵下載 TXT／SRT／VTT／JSON／音檔或整包 ZIP。

## 6. 社課現場收錄

* 主持人（後台角色 host 以上）在「社課收錄」填房間代碼、選講者（需「允許現場收錄」同意）→「本場開始收錄」。場次 ID 可空：會綁定這個房間**下一個出現的場次**；房間換到新場次時自動停止（逐場同意）。最長預設 4 小時。
* live-room 的 `pipeline._process` 在 Breeze 定稿後呼叫 `app.training.harvest.capture`（在 worker thread，失敗不影響字幕）。沒有 training.sqlite3 或沒開收錄時只是一次快取檢查（3 秒一次）。收下的每段：WAV＋Breeze 原始字（`asr_text`）＋套詞表後的字幕（`text`）＋時間 `t0_ms/t1_ms`，狀態一律「未校對」。
* 草稿字幕（PR #49 的串流草稿 ASR）預留 `harvest.note_draft(...)`，合併 #49 後在草稿 hub 呼叫即可寫進 `live.jsonl`；本 PR 沒有改 #49 的檔案。
* 4 分鐘沒有辨識結果在後台顯示警告，5 分鐘自動停止並寫出完整逐字稿（後台輪詢與 live-room 背景檢查都會觸發）。
* 一場裡可能有其他人發言（提問）：在審核或「錄音與逐字稿」把那幾段標成 `Speaker 1`，再把 `Speaker 1`「排除」→ 音檔立刻刪除、該段不會進資料集。

## 7. 審核佇列與 MT 配對

* 沿用 staging 的做法：所有收錄、匯入的片段先是 `unverified`／`needs_review`，人工核准才進資料集（錄音室品質良好的句子直接 `approved`，因為文字就是提示句）。
* 審核卡片：播放音檔、Breeze 原文、可編輯逐字稿（若 zen.sqlite3 帳本裡已有人工修正會自動帶入）、譯文語言與譯文（帶入帳本當下的譯文）、講者標籤。
* 「核准」：寫入修正後的文字；有填譯文且講者有 MT 同意時建立 `mt_pairs`，並自動送到既有的「待審修正」（`staging_items`，kind=tm），管理員在 `/admin/staging` 核准後才寫進翻譯記憶（`mt_pairs.staging_id` 記下連結）。
* 「退回」：音檔立即刪除。
* 講者標籤：預設 `Speaker 0`；可單段改，也可整份錄音改名（例如 `Speaker 1` → `法師`）或對應到名冊／排除，套用到整份逐字稿。

## 8. 匯入既有錄音＋逐字稿（低優先，通用格式）

* 音檔：MP3／M4A／MP4／WAV／AAC／FLAC／OGG／OPUS／WEBM／MOV／MKV／WMA，用本機 ffmpeg（安裝版 `tools\ffmpeg.exe`）轉 16 kHz 單聲道。
* 逐字稿：SRT、VTT（含 `<v 講者>`）、JSON（`segments` 陣列：`start/end` 秒或 `start_ms/end_ms`、`text`、`speaker`；也吃 whisper.cpp `-oj`）、TXT（`[00:01:02] 講者：文字`、`00:01:02 講者` 標題行＋下一行文字，或沒有時間的段落）。
* 沒有時間戳：若設定 `ZEN_TRAINING_ALIGNER`（例如 `whisper-cli -m <breeze ggml> -l zh -oj -of {out} -f {wav}`），用 Breeze 的時間戳對齊；否則依字數比例估計。兩者都標 `align=forced`、狀態 `needs_review`。
* **所有講者預設排除**：要把標籤對應到名冊裡有 ASR 同意的講者，才會切出片段（`unverified`）。沒對應或沒同意的段落沒有音檔、不會進資料集。處理完可「刪除原始整段音檔」。

## 9. 資料集建置與匯出

後台「匯出資料集」（admin）或：

```bat
cd "%LOCALAPPDATA%\Programs\Breeze Live Room"
python -m app.training build                       :: 輸出到 training\exports\ds_…
python -m app.training build --out D:\zen-ds1 --max-cer 0.4 --min-snr 12
```

篩選（每個被篩掉的片段只算一個原因，寫在資料集卡）：沒有有效同意 → 沒音檔 → 退回 → 未校對（`--include-unverified` 才收）→ 沒文字 → 長度不在 1–30 秒 → 削波比例 > 0.1% → SNR < 10 dB → 靜音 > 80% → **Breeze 輸出與修正稿 CER > 0.5（可能對錯段，列在 `flagged_high_cer`）**；接著去重：相同音檔 sha256、同一講者重錄同一提示句只留 SNR 最好的。

切分：以「講者 × 場次」（錄音室與匯入是「講者 × 錄音」）為單位整組分到 train／validation／test（約 80／10／10，依時長貪婪分配，雜湊排序所以結果固定；≥3 組時 validation 與 test 至少各一組）。再做文字防洩漏：同一句正規化後的文字只留在一個切分（test 優先）。因為目標是學「固定講者」的口音，test 裡是同一批講者的**不同場次**。

輸出：

```
ds_…\
  asr\train\*.wav + metadata.jsonl      ← datasets.load_dataset("audiofolder", data_dir="asr")
  asr\validation\…  asr\test\…           欄位：file_name, transcription, speaker_id, group, origin, duration, snr_db, asr_text, cer_breeze
  asr\train.jsonl / validation.jsonl / test.jsonl   同內容 manifest，audio 為相對路徑
  mt\zh-en.{train,validation,test}.jsonl  {"messages":[system,user(中文),assistant(譯文)], "meta":{origin, group, tm_unit_id}}
  mt\zh-ja.{…}.jsonl
  manifest.json                          統計、選項、講者 ID 清單
  README.md                              資料集卡：各切分時數、每位講者時數、篩選數、術語涵蓋（詞表＋提示詞，列出還沒錄到的）
```

MT 來源：審核產生的 `mt_pairs`（需 MT 同意）＋ zen.sqlite3 翻譯記憶 `tm_units`（品質 ≥ 4，唯讀）＋ 帳本裡人工修正的譯文（`origin` = human／post_edit，唯讀）。同樣依場次分組切分、來源句不跨切分。

## 10. 怎麼接到微調（`training/`）

* ASR：`training/scripts/train_asr_lora.py` 讀 `asr/`，HF transformers＋peft 對 Breeze-ASR-25 做 LoRA（q/k/v/out_proj），訓練前後在 test 算 CER（`report.json`）。`eval_asr_cer.py` 也能直接用 whisper.cpp（App 實際用的引擎）量 CER。
* MT：`training/scripts/train_mt_lora.py` 讀 `mt/`，Qwen3-4B（可換 Qwen3-1.7B 等）LoRA；有 unsloth 就用 4-bit QLoRA。
* 回到 App：`convert_asr_to_ggml.py`（whisper.cpp `convert-h5-to-ggml.py`＋量化 → `.env` 設 `BREEZE_MODEL`）、`convert_mt_to_gguf.py`（llama.cpp `convert_hf_to_gguf.py`＋`llama-quantize` → `ollama create` 後設 `BREEZE_TRANSLATE_MODEL`，或 llama-server 用 `BREEZE_MT_GGUF_EN`／`_JA`）。
* GPU 需求與 dry-run：見 `training/README.md`。

## 11. 龜龜 UI 入口（掛勾，尚未接）

主 UI（`app/workspace.tsx`、`components/breeze/*`）目前由 PR #43（龜龜 UI 合併／完全本機化）在改，本 PR 不碰這些檔案。等 #43 合併後，建議在後台面板（`components/breeze/admin-panel.tsx`）加一個「訓練資料（錄音室）」按鈕，開啟 `http://127.0.0.1:8791/admin/training`（若 #43 的 `/api/breeze-admin/[...path]` 代理要在主 UI 內嵌，需讓它也轉送 `/admin/training*` 與 `/admin/api/v1/training*`；未驗證）。圖示只用龜龜／小華，不用蓮花標誌；後台訓練頁本身也沒有放任何標誌。

## 12. 安全與隱私摘要

* 只綁本機：沿用後台 `LoopbackOnly`、Cookie＋CSRF、CSP（`script-src 'self'`，錄音用 AudioWorklet 檔案載入，沒有 inline script）。沒有任何對外連線；不碰 8645。
* 權限：名冊／同意／撤回／匯出 = admin；錄音室、審核、匯入 = editor；開關現場收錄 = host。
* 讀 zen.sqlite3 一律 `mode=ro`＋`query_only`；唯一寫入是把修正譯文送進既有 staging 佇列（和人工修正同一條路）。
