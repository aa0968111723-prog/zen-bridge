# dbtest（資料庫實際測試與管理改善專家）— 筆電實測 NOTES

## 做了什麼
- 新增 `sidecar/live-room/tests/test_sqlite_live_load.py`、`tests/live_load_sim.py`（只新增檔案，未改任何 app 檔）。
- 模擬 2 小時講座：2400 段（每 2–4 秒一段，seed 固定、壓縮時間），每段寫 update＋final、翻譯、ledger、metrics（共 6810 筆）、60 筆 TM，量 WAL 成長、checkpoint、寫入延遲與鎖等待。
- 四個情境：baseline（沒有長讀者）、pinned（整場都有一個讀交易不放）、gapped（偶發讀者＋1.2 秒 admin 鎖）、always_on_truncate（讀者一直在，但每 300 段做一次 `wal_checkpoint(TRUNCATE)`）。重型變體要設 `ZEN_LOAD_HEAVY=1` 才會跑（預設 skip 2 個）。
- 結束時印出一行 `LIVE_LOAD_METRICS={json}`。

## base／分支
- 分支 `impl/dbtest`，基準 `grok/integrate`＠bea2e5c。commit sha 見下方「交件」。

## 測試指令與結果（筆電 DESKTOP-P8RGA3A，Ryzen 5 5600H，Windows 11 26200，Python 3.12.10，SQLite 3.49.1）
`cd sidecar\live-room; ..\..\..\zen-bridge-grok\sidecar\live-room\.venv\Scripts\python.exe -m pytest tests/test_sqlite_live_load.py -q -s`
→ **4 passed, 2 skipped in 86.1 s**。全套由整合者跑。

## 筆電量測（5600H＋Windows 磁碟，2026-10-10 23:4x）
| 情境 | WAL 最大 | 結束時 WAL | ledger 寫入 p50／p95／max | admin p95／max | locked | 遺失 |
|---|---|---|---|---|---|---|
| baseline | 6.0 MB | 6.0 MB（TRUNCATE 後 0） | 7.2／25.0／69 ms | 19／210 ms | 0 | 0 |
| pinned（整場長讀者） | **215.9 MB** | 215.9 MB（讀者放掉後 TRUNCATE 才歸 0） | 9.0／24.0／88 ms | 18／39 ms | 0 | 0 |
| gapped（1.2 s admin 鎖） | 10.6 MB | 10.6 MB | 8.1／22.2／**1314** ms | 19／1414 ms | 0 | 0 |
| always_on＋每 300 段 TRUNCATE | 28.3 MB | 0.15 MB | 7.5／26.4／208 ms | 22／72 ms | 0 | 0 |
- 每個情境都寫滿預期筆數：segments／transcripts／translations／events 各 2400、metrics 6810、TM 60，ledger_dropped 0、spooled 0。
- 週期 TRUNCATE 本身：p50 56 ms、p95／max 177 ms、busy 0。
- 對照 box（Xeon，非 5600H）：ledger p50 約 1 ms、p95 3–8 ms，pinned WAL 340–383 MB。**筆電單筆寫入慢約 5–7 倍**（Windows 磁碟＋fsync），但 2 小時講座每 2–4 秒一段的速率下，餘裕仍超過 100 倍。

## 瓶頸與建議
1. **P1 有長讀者時 WAL 無法回收**：pinned 情境中 PASSIVE checkpoint 寫回 52394 頁，但檔案仍是 216 MB；只有讀者放掉後跑 TRUNCATE 才會歸 0。app 只設了 autocheckpoint／journal_size_limit（`app/admin/db.py:149-150`），沒有排程 checkpoint。建議：每 5–10 分鐘或每 300 段跑一次 `PRAGMA wal_checkpoint(TRUNCATE)`（筆電實測 ≤177 ms），WAL 超過 64 MB 就在後台警告；讀端（後台頁面、匯出）不要長時間保持讀交易。
2. **P2 每次 commit 寫進 WAL 的量很大**：3.9 MB 的 DB 跑完 baseline，WAL 有 6 MB、checkpoint 重置 25 次。原因是 `secure_delete=ON`（`db.py:144`）加上 FTS 觸發器（`schema.sql:502-526`）。建議改用 `secure_delete=FAST`，並把 update 和 final 合成一筆寫入。
3. **P2 metrics 寫入沒有重試**：鎖住超過 5 秒 busy_timeout 時會失敗（`app/admin/observability.py:38`）。ledger 已有重試和 spool（`ledger.py:208-239`）。建議 metrics 也走同樣的重試，或允許丟失但要計數。
4. **P2（筆電新發現）`app/admin/migrate.py:20` 的 `main()` 用 print 輸出中文**：在 cp1252 的 Windows 主控台（或 stdout 被導向檔案）會丟出 `UnicodeEncodeError`。筆電實測第一次跑時 4 個測試全部因此失敗。測試已改成直接呼叫 `migrate()` 避開這個問題。建議 `main()` 開頭加 `sys.stdout.reconfigure(errors="replace")`，或改用 logging；安裝版若用 CLI 遷移也會遇到同樣問題。
5. 1.2 秒的 admin 鎖會讓單筆寫入等到 1.3 秒，但沒有任何錯誤（busy_timeout 5000 有效）。後台的長寫交易要拆小。

## 已知限制
- 時間是壓縮過的（實際 86 秒跑完 2 小時的量），量到的是吞吐和鎖，不是 2 小時內的熱、節流或防毒掃描效應。
- 沒測：真正斷電、磁碟滿、Defender 即時掃描 -wal 的影響。

## BLOCKED
- 無。
