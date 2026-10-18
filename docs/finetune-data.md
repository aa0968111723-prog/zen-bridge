# 微調資料準備系統（社課收錄為主、錄音室選用、資料集匯出）

> 目標：之後在 NVIDIA GPU 上微調，讓 Breeze-ASR-25 聽懂淡江禪學社的用語與固定講者的口音，讓本機翻譯模型學到社團的說法。
> **這一版只做資料準備與訓練腳本，不在筆電上訓練。** 全部只在本機，不呼叫雲端、不上傳。
> **主要資料來源是「真的上課」**：主持頁一個開關，整堂課的音檔＋字幕自動存下來，下課後機器先切段、比對聲紋，人只看不確定的段落。錄音室（提示句朗讀）是選用的額外功能。

程式位置：`sidecar/live-room/app/training/`（後台頁面 `/admin/training`、命令列 `python -m app.training`），訓練腳本在 repo 根目錄 `training/`。

## 0. 上課日流程（一步一步）

**第一次（每位固定講者只做一次，不到一分鐘）**

1. 後台 `/admin/training` →「社課收錄」→「快速登記主講者」：填暱稱（例如「法師」，不要真名／學號／電話）與紀錄人，用途（語音辨識、翻譯、允許上課時收錄）預設已勾，勾「本人已聽過／讀過上面的說明並同意」→「登記並記錄同意」。一次建好講者＋同意。命令列：`python -m app.training quick-speaker 法師 --by 社長 --confirm`。
2. （選用）把聲紋模型放進 `training\models\`（見 §5.2）；沒放也能用，只是聲紋比對改用較弱的簡易版，需要人看的段落會多一些。

**每堂課**

1. 主持頁照常「開始聽」。右下角「訓練資料」小面板：選「主講者」→ 勾「收錄訓練資料」。**同一個房間會記住**：下次上課自動是同一位主講者、已勾選（主持人開過一次的已同意講者，預設就開）。
2. 收錄中面板顯示紅點「● 收錄中」，以及已存幾分鐘、幾句、多少 MB；沒勾時顯示灰色「未收錄」。
3. 有學生提問或討論時，按 **Q**（或面板上的「提問中」）→ 面板變「提問中（預設排除）」；提問結束再按一次 Q。這段時間的句子課後會自動排除；忘了按也沒關係，聲紋檢查會再抓一次。
4. 下課照常「停止」。系統自動寫出完整逐字稿（JSON／SRT／VTT／TXT），並在**所有收錄都停了 60 秒後**，以較低優先權做課後整理：從整堂音檔切出每一句、算音質、比對聲紋、給每句一個「信心分數」。

**課後（幾分鐘）**

1. 後台「課後審核」→ 選這堂課。預設「不確定的先」排序，只列待審（主講者）。
2. 先按「核准門檻以上全部」（預設門檻 0.85）：聲紋像主講者、音質好、沒有警示的句子一次核准。
3. 剩下的用鍵盤：`J`/`↓` 下一段、`K`/`↑` 上一段、`空白` 播放、`A`/`Enter` 核准並下一段、`E` 修改文字（`Enter` 存並核准、`Esc` 取消）、`X` 排除（不是主講者）、`I` 收回（其實是主講者）、`R` 退回（刪音檔）。
4. 「時數統計」看每位講者每週／每月收了幾小時、核准幾小時。「保留與磁碟」看用量、調整保留天數。

## 1. 資料流

```
 講者名冊＋同意（training.sqlite3）── 每個動作都先查有效同意
          │
 主持頁「收錄訓練資料」（每個房間記住主講者）
          │   live-room pipeline 只「丟進佇列」就返回（< 0.1 ms，不等寫檔）
          ▼
 收錄背景執行緒（capture.py）：每 0.5 秒批次寫
   audio\chunk_0000.wav …（16 kHz 單聲道，每 5 分鐘一檔，標頭隨時有效）
   live.jsonl（final／draft／question 標記，含時間；每 10 秒 fsync）
          │ 停止 → transcript.{json,srt,vtt,txt}
          ▼
 課後整理（derive.py，下課後背景、低優先權）
   依 live.jsonl 定稿時間切段 → 音質 → 聲紋比對主講者（前幾分鐘自動建檔）→ 信心分數
   不像主講者／提問中 → 排除（預設不進資料集）
          ▼
 課後批次審核（核准門檻以上全部＋鍵盤快捷鍵）→ 修正逐字稿 → ASR 配對；修正譯文 → MT 配對
          ▼
 資料集建置：品質篩選 → 去重 → 依「講者×場次」切 train/validation/test（無洩漏）
          ▼
 exports\ds_…\asr\（HF audiofolder＋manifest）  mt\（chat JSONL）  README.md（資料集卡）
          ▼
 training\scripts：Breeze LoRA、MT LoRA、轉回 ggml／gguf 給 App 用

 （選用）錄音室提示句朗讀、匯入既有錄音＋逐字稿 → 也是 recording ＋ clips，走同一個審核與匯出
```

## 2. 本機儲存位置

根目錄：`%LOCALAPPDATA%\ZenBridge\training\`（`ZEN_TRAINING_DIR` 可覆寫；沒有設定時跟著 `ZEN_DATA_DIR`）。與字幕主庫 `data\zen.sqlite3` 分開，撤回同意時可以乾淨刪除。

```
training\
  training.sqlite3                     講者、同意、錄音、片段、MT 配對、收錄開關、匯出、稽核
  recordings\
    studio\<speaker_id>\<rs_…>\        錄音室一次坐下錄
    harvest\<speaker_id>\<rh_…>\       社課一堂課
      audio\chunk_0000.wav …          整堂課原始音檔（每 5 分鐘一檔；依保留設定自動刪）
    import\mixed\<ri_…>\               匯入（source.wav 是隔離區，可能有沒同意的人的聲音）
      clips\<clip_id>.wav              每段 16 kHz 單聲道 PCM16
      live.jsonl                       錄音中逐行寫入的字幕事件（draft／final／rename／end，含時間）
      recording.wav                    錄音室：結束後依序串接的整段音檔（匯入則用 source.wav；社課用 audio\）
      transcript.json / .srt / .vtt / .txt   完整逐字稿，時間軸對齊 recording.wav，含講者標籤
  import\inbox\                        放「同檔名」音檔＋逐字稿，`python -m app.training import --scan`（匯入後刪除收件匣複本）
  import\uploads\                      後台上傳暫存（匯入後刪除）
  models\                             （選用）聲紋模型 .onnx
  exports\ds_YYYYMMDD_HHMMSS_xxxx\     資料集匯出
```

錄音室的 `live.jsonl` 每段 `fsync` 一次；社課的音檔分段與 `live.jsonl` 由背景執行緒每 10 秒 `fsync`、每個分段 WAV 的標頭隨時更新，當機或斷電最多少最後約 10 秒，重開後自動接續或結束該堂課。按停止（或自動停止）才寫出完整逐字稿，之後每次人工修正都會重寫。

## 3. 資料表（schema）

完整定義：`sidecar/live-room/app/training/schema.sql`。

| 表 | 重點欄位 | 說明 |
| --- | --- | --- |
| `speakers` | `id`（`spk_xxxxxxxx`）、`label`（暱稱，≤24 字）、`accent_note`、`collect_default`、`withdrawn_at` | **不存學號、電話、Email、真名**；暱稱含 6 位以上數字、`@`、電話格式一律拒絕 |
| `consents` | `scope_asr`、`scope_mt`、`harvest_live`、`local_only`（CHECK = 1）、`consent_version`、`consent_date`、`recorded_by`、`revoked_at` | 每次更新是新的一列，舊的標 `revoked_at`；至少一個用途 |
| `recordings` | `kind`（studio／harvest／import）、`speaker_id`、`room_id`、`session_id`、`dir`、`source_audio`、`status` | 一次錄音＝音檔＋逐字稿 |
| `clips` | `audio_path`、`sha256`、`duration_s`、`rms_dbfs`、`peak_dbfs`、`clip_ratio`、`silence_ratio`、`snr_db`、`draft_text`、`asr_text`（Breeze 原始）、`text`（參考／修正）、`speaker_tag`、`status`（unverified／needs_review／approved／rejected）、`align`（native／file／forced）、`confidence`、`spk_score`、`flags`、`excluded`、`excluded_reason`（question／speaker_mismatch／reviewer） | 一段話；`segment_id` = live 的 `{room}:{session}:{seq}` |
| `prompts` | `source`（club／glossary／dharma／mixed／custom）、`text`、`term` | 錄音室提示句 |
| `mt_pairs` | `src_text`、`tgt_lang`、`tgt_text`、`clip_id`、`speaker_id`、`segment_id`、`staging_id`、`tm_unit_id` | 審核產生的翻譯配對，連到 zen.sqlite3 的 staging／翻譯記憶 |
| `room_collect` | `room_id`、`speaker_id`、`collect` | 主持頁開關：每個房間記住主講者與要不要收錄 |
| `class_sessions` | `room_id`、`session_id`、`speaker_id`、`recording_id`、`audio_s`、`bytes`、`finals`、`dropped`、`stop_reason`、`derived_at`、`derive_note`、`raw_deleted_at` | 一堂課的收錄紀錄與課後整理結果 |
| `speaker_profiles` | `speaker_id`、`backend`、`emb`（float32 平均聲紋） | 只存有同意的主講者；撤回時刪除；學生的聲紋不保存 |
| `settings` | `raw_retention_days`、`excluded_audio_days`、`auto_approve_min` | 保留與一鍵核准門檻 |
| `exports` | `dir`、`speakers`（JSON）、`stats`、`deleted_at`、`deleted_reason` | 記錄每份匯出含哪些講者，撤回時找得到 |
| `audit` | `action`、`speaker_id`、`actor`、`detail` | 不放字幕全文、不放暱稱 |

## 4. 講者名冊與同意（必要條件）

* 每位講者：ID＋顯示名稱（暱稱即可）＋同意紀錄（用途 ASR／MT、只限本機、日期、紀錄人、可撤回）。同意書文字固定在 `registry.CONSENT_TEXT`（版本 `zen-train-consent-v1`），後台記錄時必須勾「講者本人已閱讀並同意」。
* **錄音室**需要 ASR 同意；**社課現場收錄**另外需要「允許現場收錄」；**MT 配對**需要 MT 同意。
* 每次匯出都**重新檢查同意**，不是只看當初收錄時。
* **撤回**（後台「撤回並刪除」或 `python -m app.training withdraw spk_… --yes`）會在同一個交易內：撤銷同意、停止收錄、刪除他所有片段音檔與資料列、他的 MT 配對、他的錄音資料夾（含完整逐字稿）、含他聲音的匯入原始檔，以及**所有包含他的本機匯出資料夾**（包括 `--out` 指到訓練資料夾外的匯出，靠 `manifest.json` 的 export_id 核對後才刪）。暱稱改成「已撤回」，只留下沒有個資的稽核列。已經拿去訓練出來的模型無法「忘記」，所以文件與同意書都說明：撤回後要用新的匯出重新訓練。
* 雲端 GPU：同意書 v1 寫的是「只存放在社團本機電腦」，所以**不能**把資料傳到雲端 GPU。若之後要用雲端，必須先改同意書版本並重新取得同意。

## 5. 社課收錄（主要流程）

### 5.1 收錄（不影響即時字幕）

* 開關在主持頁（`/api/training/class`，角色 host 以上）；講者必須有「允許現場收錄」同意，撤回同意會立刻停掉收錄。
* live-room `pipeline._process` 解出 16 kHz WAV 後呼叫 `capture.tap_wav(...)`、Breeze 定稿後呼叫 `capture.final(...)`、場次結束呼叫 `capture.end(...)`。這三個只是 `queue.put_nowait`（佇列滿就丟掉該片並記在 `dropped`，**絕不等待**），寫檔、SQLite、fsync 全在獨立的背景執行緒，每 0.5 秒批次處理一次，避免和字幕路徑搶 GIL。關掉收錄時只是一次字典查詢。環境變數 `ZEN_TRAINING_CAPTURE=0` 可整個停用。
* 音檔依直播時間軸寫在 `audio\chunk_NNNN.wav`（每 5 分鐘一檔），中間沒聲音的空檔不佔空間；`live.jsonl` 存每句定稿（Breeze 原文＋套詞表後字幕＋時間）、提問標記。PR #49 合併後在草稿 hub 呼叫 `capture.draft(...)` 即可把草稿也寫進去（本 PR 沒改 #49 的檔案）。
* 4 分鐘沒有辨識結果：主持頁面板提醒（可能在靜坐）；5 分鐘：先寫出目前的逐字稿、暫停寫原始音檔（保留最近 60 秒，有人說話就補寫回去），不會把靜坐的 40 分鐘都存成音檔。5 分鐘完全沒收到音訊或場次結束 → 這堂課結束。
* 段落配對**不依賴**每句當下切檔成功：全部從整堂音檔＋`live.jsonl` 課後重新切，所以中途當機也能整理。

### 5.2 課後整理與聲紋檢查（`derive.py`、`spkcheck.py`）

* 下課後背景、低優先權執行；也可在後台「重新整理（切段＋聲紋）」或 `python -m app.training derive`。
* 聲紋：預設用 sherpa-onnx 的 3D-Speaker CAM++ 中文聲紋模型（`3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`，約 28 MB，CPU）。放在 `ZEN_SPEAKER_MODEL`、`training\models\` 或 App 的 `models\speaker\`。沒有模型或沒裝 sherpa-onnx 時改用簡易 MFCC 聲紋（區分力弱，所以只標「需要看」，很少自動排除）。
* 主講者建檔：用**這堂課前幾分鐘、非提問時段**的句子算平均聲紋，再以所有高分句重新取中心；如果之前的課已存過這位講者的聲紋，就一起參考。只存有同意的主講者聲紋，學生的不保存。
* 分數（cosine）：< 0.50 → 排除（`speaker_mismatch`）；0.50–0.70 → 待審（「聲紋不太像」）；≥ 0.70 視為主講者。在瀏覽器實測（Opus 壓縮、同一支麥克風）：同一人 0.66–0.93、不同人 0.32–0.56。收幾堂真的課之後可用 `ZEN_SPK_EXCLUDE`／`ZEN_SPK_CONFIDENT` 調整。
* 提問中（Q）時段內的句子直接排除；前後 3 秒的句子標「接近提問時段」。
* 信心分數 0–1 綜合：聲紋、破音、雜音、靜音比例、字數、語速、重複字（疑似幻覺）、草稿與定稿差異。
* 每句一律先是「未校對／待審」，**人按核准才會進資料集**；排除的句子預設不進資料集，審核時可按 `I` 收回。
* 如果之後覺得模型太重：拿掉模型就自動退回「提問中（Q）＋課後審核」的做法，流程不變。

### 5.3 課後批次審核

見 §0。後台 API：`GET /admin/api/v1/training/classes/{rid}/batch?sort=confidence|time&show=todo|all|excluded`、`POST …/approve-above`、`POST /admin/api/v1/training/clips/{cid}/exclude`。核准門檻預設 0.85（「保留與磁碟」可改 0.5–1.0）。

### 5.4 磁碟與保留

* 原始整堂音檔：16 kHz × 16 bit 單聲道 = **約 115 MB／每小時課**（實際只算有說話的時間；長時間靜坐不存）。課後切出的片段另外約 80–90 MB／小時（核准的會保留到匯出）。
* 「保留與磁碟」設定（`settings` 表；也可 `python -m app.training settings`）：
  * `raw_retention_days`（預設 14）：課後整理完幾天後刪掉整堂原始音檔（片段與逐字稿保留）。設 0 = 整理完就刪。
  * `excluded_audio_days`（預設 3）：被排除的片段（學生提問、不像主講者）幾天後刪音檔，只留文字與時間。設 0 = 立即刪。
* 清理在每次課後整理完自動跑，也可在後台按或手動 `python -m app.training cleanup`。一學期（每週 2 小時 × 16 週）若都保留原始檔約 3.7 GB；照預設保留只會留最近兩週約 0.5 GB＋片段。

## 6. 錄音室（選用，`/admin/training` →「錄音室（選用）」）

1. 選一位已同意的講者 →「開始這次錄音」。提示句混合四種來源：社團用語、詞表（唯讀讀取 zen.sqlite3 的 `glossary_terms`，套模板成句子）、開示常用句、中英夾雜句；可加自訂句。
2. 按「錄音」→ 讀 → 按「停止」。停止後會**多收 1.2 秒尾音**才結束，避免切掉最後一個字。錄音時有即時音量條與削波（破音）警告，超過 30 秒自動停止。
3. 瀏覽器把麥克風音訊重新取樣成 16 kHz 單聲道 PCM16 WAV 上傳；伺服器再檢查一次：長度 1–30 秒、削波比例、音量、靜音比例、SNR。太短／太長／幾乎靜音的不存並請重錄；有削波、太小聲、SNR 偏低的會存成「未校對」並建議重錄。
4. 播放器從後台讀回剛存的檔案（符合 CSP，不用 blob）；「重錄這句」會用新的一次取代舊的（舊檔刪除）。
5. 進度：每位講者已錄分鐘數，以及每個來源已錄／總數。
6. 4 分鐘沒有新錄音會提醒，5 分鐘自動結束並寫出完整逐字稿；按「結束並產生逐字稿」也一樣。結束後可一鍵下載 TXT／SRT／VTT／JSON／音檔或整包 ZIP。

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
* 權限：名冊／同意／撤回／匯出 = admin；快速登記主講者 = admin；錄音室、審核、匯入 = editor；主持頁收錄開關與提問標記 = host。
* 讀 zen.sqlite3 一律 `mode=ro`＋`query_only`；唯一寫入是把修正譯文送進既有 staging 佇列（和人工修正同一條路）。
