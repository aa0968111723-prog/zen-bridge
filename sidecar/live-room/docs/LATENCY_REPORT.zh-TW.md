# 延遲報告（latency_report.py）

課程結束後，把管線各段延遲整理成一個靜態 HTML 檔。只用 Python 標準函式庫，不連網、不啟動子程序、不碰任何連接埠；資料庫一律以唯讀方式開啟（`?mode=ro`），不會改動檔案。

```bat
cd sidecar\live-room
python tools\latency_report.py --db data\zen.sqlite3 --session <場次ID> --lang en --out report.html
python tools\latency_report.py --metrics metrics.jsonl --lang ja --title "週二課程" --out report.html
```

- `--db`：zen.sqlite3 讀後台的 `metrics` 表；captions.sqlite3 沒有延遲紀錄，會產生「No data」頁面。
- `--metrics`：JSONL 或 JSON（後台 `/metrics/export?format=json` 的 `columns`/`rows`、物件陣列，或 `{"records": [...]}`）。每筆可以是 `name`/`value` 長格式，或以指標名稱為欄位的寬格式。格式錯誤的行會略過並在頁面註明。
- `--session`、`--lang en|ja`：篩選。語言取自 `labels.lang`、紀錄的 `lang`，或 zen.sqlite3 的 `sessions.tgt_lang`；沒有語言標記的樣本（例如 ASR）視為共用，兩種語言都會納入。
- 未指定 `--out` 時輸出到標準輸出。成功回傳 0；參數錯誤、找不到檔案或不是 SQLite 檔回傳 2。

## 各段與指標名稱（毫秒）

| 段 | 說明 | 接受的指標名稱（依序取第一個有資料者） |
|---|---|---|
| A2 | 收音 → VAD 結束 | `a2_ms`、`a2_vad_end_ms`、`vad_end_ms` |
| A3 | ASR 草稿 | `a3_ms`、`a3_asr_draft_ms`、`asr_draft_ms`、`draft_asr_ms` |
| A4 | ASR 定稿 | `a4_ms`、`a4_asr_final_ms`、`asr_final_ms`、`asr_ms`（現有 RTF 計量） |
| A5 | 翻譯 | `a5_ms`、`a5_mt_ms`、`mt_ms`、`translate_ms` |
| A6 | 推送到房間 | `a6_ms`、`a6_publish_ms`、`publish_ms` |
| A7 | 聽眾端顯示 | `a7_ms`、`a7_display_ms`、`display_ms` |

每段列出筆數、p50、p95、p99、最大值；沒有資料的段顯示 `n/a`。端到端優先使用 `e2e_ms`，否則以同一段落（`segment_id`）的 A2、A4、A5、A6、A7 加總（A3 草稿與定稿並行，不計入），只計算各段都有資料的段落。若有 `rtf` 也會列出 RTF。

頁面只含內嵌 CSS 與內嵌 SVG 長條圖（藍色 p50、橘色 p95），沒有 `<script>`、外部字型或連結；資料中的文字一律經過 HTML 跳脫。
