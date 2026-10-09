# 中英詞典

Zen 主 UI 的「中英詞典」收錄 CC-CEDICT 125,244 條與 ECDICT 770,585 條，共 895,829 條。不同來源的同詞條會保留，不宣稱去重後有同樣多個單字。繁簡詞頭、拼音、英文詞形及原始釋義保留；ECDICT 中文主要是簡體。來源、授權、固定版本與 SHA256 見 `dictionary-data/catalog.json`，完整授權通知保留於同目錄。

CC-CEDICT 的格式轉換資料依 CC BY-SA 4.0 分享；ECDICT 保留 MIT 授權。這些資料集的授權各自適用，不改變其他軟體檔案的授權。沒有收錄未授權的商業字典。

## 使用與私人資料

公開來源可搜尋；自有字典需已配對的主持 App，透過 UTF-8 JSON 匯入。格式為 `{ "title": "我的詞庫", "entries": [{ "word": "meditation", "zh": "靜坐", "en": "" }] }`，或在 UI 上傳詞條陣列。每次最多 5,000 條、2 MB。`alternative`、`pronunciation`、`zh`、`en` 可省略。

自有資料不提供普通聽眾查詢，也不提供未授權 MCP 呼叫的翻譯參考。主持機使用翻譯服務時，會把相關自有釋義和已確認筆記作為有限文字上下文傳給所選翻譯服務；詞庫本身不送到語音辨識服務。字典提供詞義參考，句子翻譯仍依語境核對。

每次自有字典匯入都新增一份詞庫，不依名稱覆蓋既有詞條；目前沒有刪除介面。

## 可重現更新

1. 從來源的官方下載位址取得 UTF-8 原檔，記錄版本、原檔 SHA256，確認新版本的授權通知。
2. `python scripts/dictionary_dataset.py --format cedict --input cedict.gz --source-id cc-cedict --output dictionary-data/cc-cedict.ndjson.gz`；ECDICT 使用 `--format ecdict --input ecdict.csv --source-id ecdict`。
3. 保存轉換報告的詞條數、拒收數及輸出 SHA256，更新 catalog。此次 ECDICT 有 26 行因格式、空詞头、控制字元或長度限制未匯入；以轉換器實際驗證規則為準。
4. 執行轉換器單元測試、`node tests/runtime.mjs`、TypeScript 檢查與 production build，透過正常 PR 審查、CI 與 Tencent 發布流程更新。不要直接覆蓋正式資料庫。

Tencent 啟動會在背景執行串流匯入，壓縮檔先驗 SHA256；每批為有界交易，跨程序使用 PostgreSQL advisory lock，避免兩個啟動程序同時重建。新來源只有完整條數核對成功後才切换可查詢版本，中途失敗保留舊版本。重啟會繼續同一快照，既有課程、人物、筆記及自有詞庫不會刪除。舊詞典快照暫時保留供回復，沒有自動清理使用者資料。

新部署完成後需確認 `/api/dictionaries` 的來源和條數，再測試 `meditation`、`禪`、主持機匯入及未授權拒絕。尚在匯入時 UI 會顯示準備中。
