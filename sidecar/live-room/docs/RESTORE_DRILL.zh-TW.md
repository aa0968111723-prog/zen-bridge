# 備份還原演練操作說明

這份工具只做「演練」：檢查一份備份能不能完整還原。它不會覆蓋正式資料。真的要還原，請用 `python -m app.admin.restore`，並先關閉直播服務與後台。

## 什麼時候跑
- 每月一次。
- 每次升版前一次。
- 改過 `ZEN_BACKUP_DIR` 或換備份磁碟之後一次。

超過 35 天沒有 PASS 的演練紀錄，就當作備份沒有保障。

## 怎麼跑
在 `sidecar/live-room` 資料夾裡執行，Windows 用 venv 裡的 `python.exe`。

| 目的 | 指令 |
|---|---|
| 現做一份備份再演練（最常用） | `python tools/restore_drill.py` |
| 演練備份資料夾裡最新的一份 | `python tools/restore_drill.py --latest` |
| 演練指定的備份 | `python tools/restore_drill.py --from-backup D:\ZenBackup\zen-20261010-230000.sqlite3` |
| 指定演練資料夾 | 加上 `--work-dir D:\ZenDrill\2026-10` |
| 給程式讀的結果 | 加上 `--json` |

主資料庫、個資庫和備份資料夾，預設分別取 `ZEN_DB_PATH`、`ZEN_IDENTITY_DB_PATH`、`ZEN_BACKUP_DIR`。要換路徑，可以用 `--db`、`--identity`、`--backup-dir` 指定。

直播中也可以跑。它只用 `VACUUM INTO` 讀一份快照，不會鎖住字幕寫入。但它會讀整個資料庫，建議在課後跑。

## 它做了哪四步
1. **備份**：預設現做一份 `backup_set`，主庫、個資庫和 manifest 一起做，放在演練資料夾的 `backups\`。如果用了 `--latest` 或 `--from-backup`，就直接用既有的備份；有 manifest 的話，會自動找到配對的個資庫備份。
2. **驗證**：檢查 `integrity_check` 與 FTS 索引，並把 SHA256 和大小跟 manifest 比對。只要有一項不過，就停在這裡判 FAIL，不會進行還原。
3. **還原到暫存**：用正式的 `restore_from` 還原到演練資料夾的 `restore\zen.sqlite3`（以及 `zen-identity.sqlite3`）。如果還原目標剛好就是正式資料庫的路徑，工具會拒絕執行。
4. **比對**：逐表比對筆數（含 FTS 表），以及 `user_version`。備份和還原結果必須完全一致。如果是現做的備份，還會另外列出「備份後正式庫又多了幾筆」，這只是資訊，不算失敗，因為直播中本來就會一直寫入。

## 看結果
- 畫面第一行會顯示 `PASS` 或 `FAIL`。結束碼 0 代表 PASS，1 代表 FAIL，2 代表無法執行（找不到檔案、參數錯誤）。
- 完整報告存在演練資料夾裡的 `drill-report.json`。報告只有表名、筆數、檔名和耗時，不含字幕內容或個資，可以放心存檔。
- 演練產物不會自動刪除。確認 PASS 之後，你可以自行處理演練資料夾。它裡面有一份完整資料（含個資庫），請不要放在公開或雲端同步的位置。

## FAIL 時怎麼辦
| 問題訊息 | 意思 | 處理方式 |
|---|---|---|
| SHA256 與 manifest 不符 | 備份檔在做完之後被改過或損壞 | 這份不能用。改演練前一份，並檢查備份磁碟 |
| 備份驗證失敗 | 檔案壞了，或根本不是資料庫 | 同上，並馬上現做一份新備份 |
| manifest 列了個資庫備份，但檔案不存在 | 個資庫備份遺失 | 找回檔案，或現做一份新備份 |
| 還原後筆數不符 | 還原過程出錯 | 保留演練資料夾和報告，回報維護者 |
| 有表無法計數 | 例如 sqlite-vec 的 `vec0` 表沒有載入 extension | 回報維護者，目前 schema 沒有啟用 vec0 |

## 演練紀錄建議
每次演練請記下：日期、使用的備份檔名、PASS 或 FAIL、總筆數、耗時。最簡單的做法，是把 `drill-report.json` 改名成 `drill-YYYYMMDD.json`，放在備份磁碟上。
