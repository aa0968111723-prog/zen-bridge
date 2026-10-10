# dba 交件 NOTES— corpus_export：zh-en／zh-ja 平行語料匯出


## 筆電交件（2026-10-10 23:4x Asia/Taipei）
- 分支：`impl/dba`（worktree `Documents\zen-bridge-impl\dba`，從 `grok/integrate` @ bea2e5c 開出）
- commit：`312faf713b678928b063d841d4b5b31da4ef1bb9`（只新增兩個檔：`sidecar/live-room/app/admin/corpus_export.py`、`sidecar/live-room/tests/test_corpus_export.py`；沒有改任何既有檔）
- 筆電測試（5600H，共用 venv `zen-bridge-grok\sidecar\live-room\.venv`）：`python -m pytest tests/test_corpus_export.py -q` → **37 passed in 2.59s**
- 筆電上第一次跑時 1 failed：測試建了一個名稱含 `?` 的資料夾，Windows 不允許（WinError 123）。已改成 Windows 上用 `資料 #1 x`，POSIX 照舊保留 `?`。程式本體沒改。
- 全套測試照 BRIEF 交給整合者跑；本職只跑自己的測試檔。
- 整合方式：`git merge impl/dba`。目前還沒接到 admin jobs，掛接點見下方「設計重點」。

## 交件內容（兩個新檔，不改任何既有檔）
| box 路徑 | 放進 repo 的位置 | 行數 | SHA256 |
|---|---|---|---|
| `/workspace/zen-impl/dba/sidecar/live-room/app/admin/corpus_export.py` | `sidecar/live-room/app/admin/corpus_export.py` | 556 | `07711da1891483ac995969ae73bb396cbb8706deb5a78b2d6a71469104868ccf` |
| `/workspace/zen-impl/dba/sidecar/live-room/tests/test_corpus_export.py` | `sidecar/live-room/tests/test_corpus_export.py` | 418 | `a97f7c9eca762f73c576a89986fce8c9a1cd513db90dfd1b31b6a9804c2a64e8` |
| `/workspace/zen-impl/dba/dba.patch`（參考用） | `git format-patch`，base 6ec5012 | — | `617a28d1b03a74f589c1f94b2aaf799033ad627bd3499de96b33ed9b4b8a05c1` |

- 只用標準函式庫＋pytest，不加相依套件、不加網路呼叫、不讀個資庫。
- **base**：box 上沒有 `grok/integrate @ bea2e5c`（`git cat-file` 找不到這個物件），所以是在 `feat/local-translation-backend @ 6ec5012` 的唯讀 clone（`/tmp/impl-dba`，分支 `impl/dba`，本機 commit 9d83b2b，沒有 push）上開發和測試。兩個檔都是新增檔，`git apply --check` 在 6ec5012 上可乾淨套用。整合者在筆電 worktree（bea2e5c）上請重跑 `python -m pytest sidecar/live-room/tests/test_corpus_export.py -q`。
- 接線：目前是獨立 CLI（`python -m app.admin.corpus_export`）。若要掛到後台 jobs（`jobs.kind` 已允許 `'export'`），由整合者在 `app/admin/jobs.py` 加一個 handler，呼叫 `corpus_export.export(db_path, out_dir, ExportConfig(...))`；本次沒有改動它。

## 用法
```
cd sidecar/live-room
python -m app.admin.corpus_export --db %LOCALAPPDATA%\ZenBridge\data\zen.sqlite3 --out D:\corpus ^
    --license-map license.json [--pairs zh-en,zh-ja] [--sources ledger,tm,glossary] ^
    [--min-quality 0.0] [--min-tm-quality 3] [--include-unknown-license] [--allow-license owner,consented] ^
    [--near-dup] [--pii-mode mask|drop] [--names-file names.txt] [--mask-all-urls] [--include-live] ^
    [--hash-salt ...（或環境變數 ZEN_CORPUS_HASH_SALT）] [--dry-run]
```
輸出：`corpus.zh-en.jsonl`、`corpus.zh-ja.jsonl`、`corpus_stats.json`。每行一筆：
`{"id","src","tgt","src_lang":"zh","tgt_lang":"en|ja","source":"ledger|tm|glossary","origin","license","quality"(0..1),"session"(雜湊或 null)}`。
結束代碼：0 成功；2 是 DB 不存在、拒讀個資庫、或 SQLite 錯誤；2（argparse）是參數錯誤。

### 授權對照檔（license.json）
schema 裡沒有任何授權或同意欄位（`rg -i "consent|license"` 只找到 identity 的 `consent_at` 和不相干的 `exports.license`），所以授權**只能由外部對照檔提供**，沒對到的一律是 `unknown`，預設排除：
```json
{"default": "unknown",
 "source_defaults": {"glossary": "owner"},
 "rooms": {"class": "owner"},
 "sessions": {"<session id>": "no-train"},
 "tm_origins": {"approved": "consented", "correction": "owner"},
 "glossary": "owner"}
```
優先順序：ledger 是 session > room；TM 是 tm_origin > room；glossary 是 `glossary`；以上都沒有時再看 `source_defaults[來源]`，最後是 `default`，再沒有就是 `unknown`。
預設允許訓練的授權值：`train-ok, owner, consented, public-domain, cc0, cc-by, cc-by-4.0, cc-by-sa, cc-by-sa-4.0`（可用 `--allow-license` 改）。其他值（例如 `no-train`、`cc-by-nc`）會以 `license_disallowed` 丟棄。

## 設計重點
1. **唯讀**：用 `Path.resolve().as_uri() + "?mode=ro"` 開啟（Windows 會得到 `file:///C:/...`，空白、`#`、`?`、中文都會 percent-encode，有測試），並設 `PRAGMA query_only=ON`；所有來源包在**同一個讀取交易**裡，拿到一致的快照。因為是 WAL 模式，live 寫入端持有 `BEGIN IMMEDIATE` 時也能讀，而且只看得到已提交的資料。
2. **不碰個資**：檔名含 `identity` 就拒絕；DB 裡有 `user_accounts／speaker_identities／voiceprints／api_tokens` 任一表也拒絕；查詢完全不 join `speakers`。`speakers.label` 本來就被 CHECK 限制為 `SPEAKER_n／host／guest／unknown`。真名只能透過 `--names-file` 清單遮蔽，本工具不從任何地方讀真名。
3. **來源**：
   - ledger：`translations(is_current=1, tgt_lang∈{en,ja})` JOIN 現行 `transcripts`／`segments`／`sessions`。以下各有丟棄原因：`redacted`、segment 狀態異常（`segment_status`）、`status≠'ok'`（`translation_not_ok`）、`translations.transcript_id` 不是現行逐字稿（`stale_pair`，也就是譯文是用舊版逐字稿翻的）、live 場次（`session_live`，可用 `--include-live` 納入）。
   - TM：`tm_units`（`src_lang` 必須是 zh*；`quality < --min-tm-quality` 記為 `low_tm_quality`）。
   - glossary：v3 讀 `glossary_term_targets`（en/ja），v2 退回讀 `glossary_terms.en`；只取 `status='active'`。
   - 品質分數 0..1：ledger 的 human=1.0、post_edit=0.9、tm_exact=0.8、import=0.6，mt 用 `qe_score`（沒有就 0.5）；TM 是 quality/5；glossary 的 locked=1.0，其餘 0.9。這些權重是**本工具的假設**，可再調。
4. **處理順序**：選取 → 授權 → PII → 品質 → 精確去重 →（選用）近似去重。每筆被丟的資料只算在第一個命中的原因下，所以 `input = dropped + dedup + output`（有測試）。
5. **去重鍵**：NFKC → 全／半形標點統一（。、，「」（）～… 等）→ 空白壓縮 → 去掉 CJK／標點旁的空白 → casefold。同鍵只留一筆：人工編修 > 品質 > 較新。`--near-dup` 是同一正規化原文只留一筆，用同樣的優先順序。輸出文字只壓縮空白，保留原本的全形標點。
6. **PII（可重現的固定標籤）**：`[EMAIL]`、`[URL]`（只遮帶 token／key／sig／password／auth／code 等參數或 `user:pw@` 的網址；`--mask-all-urls` 會遮全部）、`[PHONE]`（台灣手機與市話、+886、日本手機與市話、+81、一般國際 +號，至少 8 位數字；全形數字也算）、`[ID]`（台灣身分證／居留證 `[A-Z][1289]\d{8}`，不驗檢查碼，寧可多遮）、`[NAME]`（名單）。`--pii-mode drop` 會整筆丟棄。統計包含 `rows_redacted`、`rows_dropped` 和各類命中次數。
7. **品質過濾**：`empty`（去掉遮蔽標籤後為空）、`too_long`、`src_equals_tgt`、`src_not_zh`、`wrong_script`（ja 譯文沒有假名也沒有漢字；en 譯文沒有拉丁字母或混入大量 CJK）、`ja_no_kana`（ja 譯文有 6 個以上漢字但沒有假名，很可能是中文沒翻）、`length_ratio`（原文至少 4 字才檢查；zh-en 的長度比要在 0.3–10，zh-ja 要在 0.3–4；glossary 不檢查）、`low_quality`（`--min-quality`）。
8. **輸出原子性**：在同一資料夾用 `tempfile.mkstemp` 寫入 → fsync → `os.replace`；失敗時刪掉暫存檔、保留舊檔（有測試）。輸出依來源和正規化文字排序，同樣輸入會產生逐位元組相同的檔案（有測試）。
9. **session 雜湊**：`sha256(salt|session_id)[:16]`。預設鹽是公開常數 `zen-corpus-v1`，只要知道 session id 就猜得出雜湊；正式匯出請設 `ZEN_CORPUS_HASH_SALT`。stats 只記 `default` 或 `custom`，不寫入鹽值。

## 測試（box 量測，Xeon，非 5600H；Python 為 /workspace/zen-sandbox/venv）
- `python -m pytest tests/test_corpus_export.py -q` → **37 passed**（0.73–1.03 s）。fixture DB 用實作自己的 `db.migrate()`（schema.sql＋0003 遷移，v3）建立。
  涵蓋：授權過濾（unknown／disallowed／優先順序／`--allow-license`）、session 雜湊、精確去重（全／半形、空白、大小寫）與人工優先、近似去重、PII 各樣式（11 個參數化案例＋5 個不該遮的案例＋名單／全 URL）、mask 與 drop 兩種模式、不讀 speakers、拒讀個資庫（含改名後的副本）、各品質過濾、live 場次、zh-ja 文字檢查、glossary 狀態、stats 一致性、輸出可重現、原子寫入失敗回復、空 DB、唯讀不寫入（主檔和 WAL 內容不變）、寫入端持有 `BEGIN IMMEDIATE` 時仍可匯出、Windows 路徑與特殊字元 URI、不存在的 DB 不會被建立、CLI（dry-run 不寫檔、`--pairs`、參數錯誤、DB 不存在回傳 2）。
  `ruff check --select F,E9` 通過。
- 全套 `python -m pytest sidecar/live-room/tests -q`（6ec5012＋本次兩檔）→ **12 failed, 992 passed, 38 skipped**（333.69 s）。這 12 個失敗都不是本次造成的：
  - 10 個在**沒有本次檔案的 base（6ec5012）上同樣失敗**：`test_dba_p2::test_ledger_to_uni_fallback_is_null`（子行程 `ModuleNotFoundError: app.ledger`，是測試環境的 cwd／路徑問題）、`test_pipeline_repair` 4 個、`test_rtf_metrics` 5 個（host.html／前端字串相關）。
  - 2 個是時間敏感的模擬測試：`test_sim_translation::test_push_returns_before_slow_translation` 單獨重跑時通過；`test_sim_backlog::test_100min_session_never_waits` 等待 1.32 s，超過門檻 1 s；**在 base 6ec5012（沒有本次檔案）上同樣失敗**（等待 3.06 s）。當時 box 上有其他代理同時在跑測試，負載很高。
  - 本次兩檔都是新增檔，既有模組不會 import 它們。
- 額外量測（box）：2,000 段 ledger 加上另一條執行緒持續 `BEGIN IMMEDIATE` 寫入（匯出期間提交 293 次），匯出耗時 0.184 s，沒有鎖等待錯誤。`EXPLAIN QUERY PLAN`：`SCAN tr`（rowid 順序），其他表都走索引（`transcripts_one_current` 是 covering），沒有 TEMP B-TREE。

## 已知限制／BLOCKED
- **BLOCKED（輕）**：box 上沒有 bea2e5c，所以沒有在 integrate head 上測過。如果 integrate 改了 `tm_units`、`translations`、`glossary_term_targets` 的欄位，需要調整 `select_records`。只是多了新表（例如 backend 的 staging）不受影響。
- 資料夾唯讀、而且 `-shm` 不存在時，SQLite 的 WAL 讀取端無法開啟。這時會給出清楚的錯誤訊息，請改用可寫入的資料夾或備份副本。資料夾可寫時，讀取端可能會建立空的 `-wal`／`-shm`（這是 SQLite 正常行為，內容不變，有測試）。
- 遮蔽時整句會先做 NFKC（例如「：」會變成「:」）；沒有命中就保留原文。中文數字寫的電話（「零九一二…」）、日本個人番號、信用卡號都**沒有**處理。
- 姓名只能靠名單遮蔽，不做 NER。
- 長度比門檻和品質權重是經驗值，沒有用真實語料校正過（box 上沒有真實 DB，也沒有自己編造資料）。
- 未測：真正的 Windows 執行（只用 `PureWindowsPath` 驗證 URI 格式）、大型真實 DB 的效能。
