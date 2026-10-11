-- v2 -> v3（round2 T7／round3 C3）：詞條的目標語譯名改放子表 glossary_term_targets。
-- glossary_terms.en 這一版保留（現有程式仍以它為準），由 trigger 同步到子表；下一版再移除。
-- 由 app.admin.db 的增量遷移器在單一 BEGIN IMMEDIATE 交易內執行；可重複執行（IF NOT EXISTS／OR IGNORE）。

-- 後台（admin-info）用來標記 ja 詞表與讀音的表；這裡先建立，回填時才分得出 en／ja
CREATE TABLE IF NOT EXISTS admin_glossary_lang (
  glossary_id INTEGER PRIMARY KEY REFERENCES glossaries(id) ON DELETE CASCADE,
  tgt_lang    TEXT NOT NULL CHECK (tgt_lang IN ('en','ja')));
CREATE TABLE IF NOT EXISTS admin_term_meta (
  term_id     INTEGER PRIMARY KEY REFERENCES glossary_terms(id) ON DELETE CASCADE,
  reading     TEXT CHECK (reading IS NULL OR length(reading) <= 80));

CREATE TABLE IF NOT EXISTS glossary_term_targets (
  id         INTEGER PRIMARY KEY,
  term_id    INTEGER NOT NULL REFERENCES glossary_terms(id) ON DELETE CASCADE,
  tgt_lang   TEXT NOT NULL CHECK (tgt_lang IN ('en','ja')),
  text       TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 80),
  reading    TEXT CHECK (reading IS NULL OR length(reading) <= 80),
  locked     INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  source     TEXT NOT NULL DEFAULT 'manual'
             CHECK (source IN ('manual','csv','correction','dictionary','suggested')),
  status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','proposed','rejected','retired')),
  hit_count  INTEGER NOT NULL DEFAULT 0,
  miss_count INTEGER NOT NULL DEFAULT 0,
  rev        INTEGER NOT NULL DEFAULT 1,
  updated_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (term_id, tgt_lang)
);
CREATE INDEX IF NOT EXISTS glossary_term_targets_lang ON glossary_term_targets(tgt_lang, term_id);

-- 回填：每個舊詞條一筆；目標語看它所在的詞表（admin_glossary_lang），沒標記的是 en
INSERT OR IGNORE INTO glossary_term_targets(term_id, tgt_lang, text, reading, locked, source, status, hit_count, miss_count, rev, updated_at)
SELECT t.id, COALESCE(l.tgt_lang, 'en'), t.en, m.reading, t.locked, t.source, t.status, t.hit_count, t.miss_count, t.rev, t.updated_at
FROM glossary_terms t
LEFT JOIN admin_glossary_lang l ON l.glossary_id = t.glossary_id
LEFT JOIN admin_term_meta m ON m.term_id = t.id
WHERE length(t.en) BETWEEN 1 AND 80;

-- 同步：現有程式只寫 glossary_terms，trigger 讓子表跟著變
CREATE TRIGGER IF NOT EXISTS glossary_terms_targets_ai AFTER INSERT ON glossary_terms
WHEN length(NEW.en) BETWEEN 1 AND 80
BEGIN
  INSERT OR REPLACE INTO glossary_term_targets(term_id, tgt_lang, text, locked, source, status, hit_count, miss_count, rev, updated_at)
  VALUES (NEW.id, COALESCE((SELECT tgt_lang FROM admin_glossary_lang WHERE glossary_id = NEW.glossary_id), 'en'),
          NEW.en, NEW.locked, NEW.source, NEW.status, NEW.hit_count, NEW.miss_count, NEW.rev, NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS glossary_terms_targets_au AFTER UPDATE ON glossary_terms
BEGIN
  DELETE FROM glossary_term_targets WHERE term_id = NEW.id AND NOT (length(NEW.en) BETWEEN 1 AND 80);
  INSERT INTO glossary_term_targets(term_id, tgt_lang, text, locked, source, status, hit_count, miss_count, rev, updated_at)
  SELECT NEW.id, COALESCE((SELECT tgt_lang FROM admin_glossary_lang WHERE glossary_id = NEW.glossary_id), 'en'),
         NEW.en, NEW.locked, NEW.source, NEW.status, NEW.hit_count, NEW.miss_count, NEW.rev, NEW.updated_at
  WHERE length(NEW.en) BETWEEN 1 AND 80
  ON CONFLICT(term_id, tgt_lang) DO UPDATE SET text = excluded.text, locked = excluded.locked, source = excluded.source,
     status = excluded.status, hit_count = excluded.hit_count, miss_count = excluded.miss_count,
     rev = excluded.rev, updated_at = excluded.updated_at;
END;
CREATE TRIGGER IF NOT EXISTS admin_term_meta_targets_ai AFTER INSERT ON admin_term_meta
BEGIN
  UPDATE glossary_term_targets SET reading = NEW.reading WHERE term_id = NEW.term_id;
END;
CREATE TRIGGER IF NOT EXISTS admin_term_meta_targets_au AFTER UPDATE ON admin_term_meta
BEGIN
  UPDATE glossary_term_targets SET reading = NEW.reading WHERE term_id = NEW.term_id;
END;

-- 目前場次檢視：保留舊欄位 en／en_origin（相容），新增依 sessions.tgt_lang 的 tgt_lang／tgt／tgt_origin
DROP VIEW IF EXISTS v_segment_current;
CREATE VIEW v_segment_current AS
SELECT s.id AS segment_id, s.room_id, s.session_id, s.seq, s.t0_ms, s.t1_ms, s.status,
       sp.label AS speaker_label,
       t.text AS zh, t.text_raw AS zh_raw, t.asr_ms, t.audio_ms,
       tr.text AS en, tr.origin AS en_origin, tr.model AS mt_model, tr.latency_ms AS mt_ms, tr.term_flags,
       COALESCE(se.tgt_lang, 'en') AS tgt_lang, tt.text AS tgt, tt.origin AS tgt_origin
FROM segments s
LEFT JOIN sessions se ON se.id = s.session_id
LEFT JOIN speakers sp ON sp.id = s.speaker_id
LEFT JOIN transcripts t  ON t.segment_id = s.id AND t.is_current = 1
LEFT JOIN translations tr ON tr.segment_id = s.id AND tr.tgt_lang = 'en' AND tr.is_current = 1
LEFT JOIN translations tt ON tt.segment_id = s.id AND tt.tgt_lang = COALESCE(se.tgt_lang, 'en') AND tt.is_current = 1;

INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '3');
