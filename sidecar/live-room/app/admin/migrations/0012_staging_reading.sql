-- v11 -> v12：staging 的日文詞條可帶讀音（round4 日文 ruby）。只對 kind='term' 且 tgt_lang='ja' 有意義；
-- 程式驗證只允許假名（app/admin/staging.py validate），核准時寫進 admin_term_meta.reading
-- （0003 的 trigger 會同步到 glossary_term_targets.reading）。
ALTER TABLE staging_items ADD COLUMN reading TEXT
  CHECK (reading IS NULL OR (length(reading) BETWEEN 1 AND 80 AND kind = 'term' AND tgt_lang = 'ja'));
