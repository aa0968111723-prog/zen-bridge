-- =====================================================================
-- zen-bridge 本機翻譯資料庫 schema v2（修正版，SQLite）＋後台 jobs/admin_sessions/idempotency_keys、glossary_terms.rev
-- 來源：/workspace/zen-translation-schema.sql (v1) 的 DBA 審查修正；審查報告見 database-review.md
-- 需求：SQLite ≥ 3.42（unixepoch('subsec')、FTS5 'integrity-check' rank=1）；FTS5 trigram ≥ 3.34。
-- 套用方式：由 migrate runner 在「新的空檔案」上執行（v1 既有庫請改用 migrations/0002_*.sql，見報告 §6）。
-- 重要：
--   * 每條連線都要自己設定的 PRAGMA（foreign_keys / synchronous / busy_timeout / wal_autocheckpoint /
--     journal_size_limit / recursive_triggers）不能只寫在 schema 檔，必須由 connect() 每次設定（見報告 §5）。
--   * 個資（帳號、顯示名稱、密碼雜湊、token、講者真名、聲紋）不在本檔，
--     放在另一個檔 zen-identity.sqlite3（見 zen-identity-schema.fixed.sql，報告 §8）。
--   * transcripts.text_uni 由應用程式填入：在每個 CJK 字前後插入空白（只插入、不刪改任何字元），
--     CHECK 會驗證 replace(text_uni,' ','') = replace(text,' ','')，保證和 text 一致。
-- =====================================================================
PRAGMA auto_vacuum = INCREMENTAL;    -- 必須在建立任何表之前；既有庫需 VACUUM 一次才生效
PRAGMA journal_mode = WAL;           -- 持久屬性（寫入檔頭），只需設一次
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 版本管理
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,               -- 與 PRAGMA user_version 一致
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL,                     -- sha256(migration 檔內容)，防止已套用的檔案被改
  applied_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  duration_ms INTEGER
);
-- v1 相容：保留 schema_meta，但版本以 PRAGMA user_version 為準
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------- 使用者（主庫只存假名化角色資料）
CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY,               -- 與 ident.user_accounts.user_id 相同
  role        TEXT NOT NULL DEFAULT 'viewer'
              CHECK (role IN ('owner','admin','editor','host','viewer')),
  disabled    INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0,1)),
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  deleted_at  REAL                                -- 軟刪除（個資已從 identity 庫清除）
);

-- ---------------------------------------------------------------- 模型設定（先建，供 sessions 參照）
CREATE TABLE IF NOT EXISTS model_profiles (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  tier        TEXT NOT NULL CHECK (tier IN ('cpu','igpu','npu','gpu8','gpu12_16','gpu24')),
  config      TEXT NOT NULL CHECK (json_valid(config)),
  is_active   INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
  updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE UNIQUE INDEX IF NOT EXISTS model_profiles_one_active ON model_profiles(is_active) WHERE is_active = 1;

-- ---------------------------------------------------------------- 房間 / 場次 / 講者
CREATE TABLE IF NOT EXISTS rooms (
  id          TEXT PRIMARY KEY,
  title       TEXT,
  topic       TEXT,
  default_glossary_id INTEGER REFERENCES glossaries(id) ON DELETE SET NULL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  archived_at REAL
);
CREATE INDEX IF NOT EXISTS rooms_default_glossary ON rooms(default_glossary_id) WHERE default_glossary_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  room_id       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  session_ord   INTEGER,
  title         TEXT,
  started_at    REAL NOT NULL,
  ended_at      REAL CHECK (ended_at IS NULL OR ended_at >= started_at),
  src_lang      TEXT NOT NULL DEFAULT 'zh-TW',
  tgt_lang      TEXT NOT NULL DEFAULT 'en',
  model_profile_id INTEGER REFERENCES model_profiles(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'live'
                CHECK (status IN ('live','ended','reviewed','archived')),
  notes         TEXT,                             -- 不得寫入個資（UI 提示）
  purge_after   REAL,                             -- 保留期限（epoch 秒）；NULL = 依全域政策
  legal_hold    INTEGER NOT NULL DEFAULT 0 CHECK (legal_hold IN (0,1)),
  UNIQUE (id, room_id)                            -- 供 segments 複合外鍵
);
CREATE INDEX IF NOT EXISTS sessions_room ON sessions(room_id, started_at);
CREATE INDEX IF NOT EXISTS sessions_status_time ON sessions(status, started_at);
CREATE INDEX IF NOT EXISTS sessions_model_profile ON sessions(model_profile_id) WHERE model_profile_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sessions_purge ON sessions(purge_after) WHERE purge_after IS NOT NULL AND legal_hold = 0;

CREATE TABLE IF NOT EXISTS speakers (               -- 只存假名 label；真名在 ident.speaker_identities
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  label       TEXT NOT NULL CHECK (label GLOB 'SPEAKER_[0-9]*' OR label IN ('host','guest','unknown')),
  UNIQUE (session_id, label)
);

-- ---------------------------------------------------------------- 段落
CREATE TABLE IF NOT EXISTS segments (
  id          TEXT PRIMARY KEY,                   -- "{room}:{session}:{seq}"（沿用 pipeline）
  session_id  TEXT NOT NULL,
  room_id     TEXT NOT NULL,                      -- 反正規化（熱查詢用），由複合外鍵保證與 sessions 一致
  seq         INTEGER NOT NULL,
  t0_ms       INTEGER NOT NULL,
  t1_ms       INTEGER NOT NULL CHECK (t1_ms >= t0_ms),
  speaker_id  INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
  audio_path  TEXT,
  audio_sha256 TEXT,
  rms         REAL,
  speech_ratio REAL CHECK (speech_ratio IS NULL OR speech_ratio BETWEEN 0 AND 1),
  status      TEXT NOT NULL DEFAULT 'received'
              CHECK (status IN ('received','silent','asr_done','translated','missing','error','timeout','cancelled','deleted')),
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (session_id, seq),
  FOREIGN KEY (session_id, room_id) REFERENCES sessions(id, room_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS segments_session_t0 ON segments(session_id, t0_ms);       -- 取代 segments_room_time
CREATE INDEX IF NOT EXISTS segments_room_created ON segments(room_id, created_at);
CREATE INDEX IF NOT EXISTS segments_speaker ON segments(speaker_id) WHERE speaker_id IS NOT NULL;

-- ---------------------------------------------------------------- 辨識稿
CREATE TABLE IF NOT EXISTS transcripts (
  id          INTEGER PRIMARY KEY,
  segment_id  TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  is_current  INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  text_raw    TEXT,                               -- 可依保留政策清空（NULL）
  text        TEXT NOT NULL,
  text_uni    TEXT CHECK (text_uni IS NULL OR replace(text_uni, ' ', '') = replace(text, ' ', '')),
  lang        TEXT NOT NULL DEFAULT 'zh-TW',
  origin      TEXT NOT NULL DEFAULT 'asr' CHECK (origin IN ('asr','human','import','redacted')),
  asr_engine  TEXT,
  asr_model   TEXT,
  asr_ms      INTEGER,
  audio_ms    INTEGER,
  rtf         REAL GENERATED ALWAYS AS (CASE WHEN audio_ms > 0 THEN CAST(asr_ms AS REAL) / audio_ms END) VIRTUAL,
  avg_logprob REAL,
  no_speech_prob REAL,
  redacted_at REAL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (segment_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS transcripts_one_current ON transcripts(segment_id) WHERE is_current = 1;
-- 短查詢後援：只涵蓋「尚未產生 text_uni 的 current 列」，可 covering scan，不讀整列
CREATE INDEX IF NOT EXISTS transcripts_cur_nouni ON transcripts(segment_id, text) WHERE is_current = 1 AND text_uni IS NULL;

-- ---------------------------------------------------------------- 譯文
CREATE TABLE IF NOT EXISTS translations (
  id            INTEGER PRIMARY KEY,
  segment_id    TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
  transcript_id INTEGER REFERENCES transcripts(id) ON DELETE SET NULL,
  tgt_lang      TEXT NOT NULL DEFAULT 'en',
  version       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  is_current    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
  text          TEXT NOT NULL,
  origin        TEXT NOT NULL DEFAULT 'mt'
                CHECK (origin IN ('mt','post_edit','human','tm_exact','import','redacted')),
  engine        TEXT,
  model         TEXT,
  glossary_version INTEGER,
  term_flags    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(term_flags) AND json_type(term_flags) = 'array'),
  status        TEXT NOT NULL DEFAULT 'ok',
  latency_ms    INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  qe_score      REAL,
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (segment_id, tgt_lang, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS translations_one_current ON translations(segment_id, tgt_lang) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS translations_transcript ON translations(transcript_id) WHERE transcript_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS translations_flagged ON translations(segment_id) WHERE is_current = 1 AND term_flags <> '[]';

-- transcript_id 必須屬於同一個 segment（複合 FK 無法搭配 SET NULL，故用觸發器）
CREATE TRIGGER IF NOT EXISTS translations_transcript_same_segment_i BEFORE INSERT ON translations
WHEN new.transcript_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM transcripts WHERE id = new.transcript_id AND segment_id = new.segment_id)
BEGIN SELECT RAISE(ABORT, 'translations.transcript_id belongs to another segment'); END;
CREATE TRIGGER IF NOT EXISTS translations_transcript_same_segment_u BEFORE UPDATE OF transcript_id, segment_id ON translations
WHEN new.transcript_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM transcripts WHERE id = new.transcript_id AND segment_id = new.segment_id)
BEGIN SELECT RAISE(ABORT, 'translations.transcript_id belongs to another segment'); END;

-- ---------------------------------------------------------------- 術語表
CREATE TABLE IF NOT EXISTS glossaries (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  scope       TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global','room','session')),
  room_id     TEXT REFERENCES rooms(id) ON DELETE CASCADE,
  session_id  TEXT REFERENCES sessions(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL DEFAULT 1,
  updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  CHECK ( (scope = 'global'  AND room_id IS NULL     AND session_id IS NULL)
       OR (scope = 'room'    AND room_id IS NOT NULL AND session_id IS NULL)
       OR (scope = 'session' AND session_id IS NOT NULL) )
);
CREATE UNIQUE INDEX IF NOT EXISTS glossaries_scoped_name
  ON glossaries(scope, COALESCE(room_id, ''), COALESCE(session_id, ''), name);
CREATE INDEX IF NOT EXISTS glossaries_room ON glossaries(room_id) WHERE room_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS glossaries_session ON glossaries(session_id) WHERE session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS glossary_terms (
  id          INTEGER PRIMARY KEY,
  glossary_id INTEGER NOT NULL REFERENCES glossaries(id) ON DELETE CASCADE,
  zh          TEXT NOT NULL CHECK (length(zh) BETWEEN 1 AND 20),
  en          TEXT NOT NULL CHECK (length(en) <= 80),
  aliases     TEXT NOT NULL DEFAULT '[]'
              CHECK (json_valid(aliases) AND json_type(aliases) = 'array' AND json_array_length(aliases) <= 8),
  locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  category    TEXT,
  note        TEXT,
  source      TEXT NOT NULL DEFAULT 'manual'
              CHECK (source IN ('manual','csv','correction','dictionary','suggested')),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','proposed','rejected','retired')),
  hit_count   INTEGER NOT NULL DEFAULT 0,
  miss_count  INTEGER NOT NULL DEFAULT 0,
  rev         INTEGER NOT NULL DEFAULT 1,       -- [zen-bridge admin] 詞條樂觀鎖（ETag "t{id}-r{rev}"）
  created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (glossary_id, zh)
);
CREATE INDEX IF NOT EXISTS glossary_terms_status ON glossary_terms(glossary_id, status);
CREATE INDEX IF NOT EXISTS glossary_terms_created_by ON glossary_terms(created_by) WHERE created_by IS NOT NULL;

-- ---------------------------------------------------------------- 翻譯記憶
CREATE TABLE IF NOT EXISTS tm_units (
  id          INTEGER PRIMARY KEY,
  src_lang    TEXT NOT NULL DEFAULT 'zh-TW',
  tgt_lang    TEXT NOT NULL DEFAULT 'en',
  src_text    TEXT NOT NULL,
  src_norm    TEXT NOT NULL,
  tgt_text    TEXT NOT NULL,
  src_hash    TEXT NOT NULL CHECK (length(src_hash) = 40),   -- sha1(src_lang|tgt_lang|src_norm)：v2 起把 src_lang 也納入
  origin      TEXT NOT NULL DEFAULT 'correction'
              CHECK (origin IN ('correction','approved','import','post_edit')),
  quality     INTEGER NOT NULL DEFAULT 3 CHECK (quality BETWEEN 1 AND 5),
  room_id     TEXT REFERENCES rooms(id) ON DELETE SET NULL,
  segment_id  TEXT REFERENCES segments(id) ON DELETE SET NULL,
  use_count   INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  updated_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (src_hash, tgt_text)
);
-- 移除 v1 的 tm_units_hash(src_hash)：已被 UNIQUE(src_hash, tgt_text) 的自動索引前綴涵蓋
-- 精確命中熱路徑：WHERE src_hash=? AND quality>=3 ORDER BY quality DESC, use_count DESC LIMIT 1 → 不需 temp B-tree
CREATE INDEX IF NOT EXISTS tm_units_exact ON tm_units(src_hash, quality DESC, use_count DESC) WHERE quality >= 3;
CREATE INDEX IF NOT EXISTS tm_units_segment ON tm_units(segment_id) WHERE segment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tm_units_room ON tm_units(room_id) WHERE room_id IS NOT NULL;

-- ---------------------------------------------------------------- 人工修正
CREATE TABLE IF NOT EXISTS corrections (
  id            INTEGER PRIMARY KEY,
  target_type   TEXT NOT NULL CHECK (target_type IN ('transcript','translation','diagram','term')),
  target_id     TEXT NOT NULL,
  segment_id    TEXT REFERENCES segments(id) ON DELETE CASCADE,   -- v1 為 SET NULL：會留下含原文的孤兒修正（個資殘留）
  before_text   TEXT,
  after_text    TEXT NOT NULL,
  reason        TEXT CHECK (reason IS NULL OR reason IN ('asr_misheard','term','fluency','meaning','other')),
  author_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','applied','rejected')),
  promoted_tm_id   INTEGER REFERENCES tm_units(id) ON DELETE SET NULL,
  promoted_term_id INTEGER REFERENCES glossary_terms(id) ON DELETE SET NULL,
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  applied_at    REAL
);
CREATE INDEX IF NOT EXISTS corrections_status ON corrections(status, created_at);
CREATE INDEX IF NOT EXISTS corrections_target ON corrections(target_type, target_id);
CREATE INDEX IF NOT EXISTS corrections_segment ON corrections(segment_id) WHERE segment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS corrections_author ON corrections(author_id) WHERE author_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS corrections_tm ON corrections(promoted_tm_id) WHERE promoted_tm_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS corrections_term ON corrections(promoted_term_id) WHERE promoted_term_id IS NOT NULL;

-- ---------------------------------------------------------------- 示意圖與素材
CREATE TABLE IF NOT EXISTS diagrams (
  id          TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  session_id  TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  version     INTEGER NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('concept_card','mermaid_flow','mermaid_mindmap','compare_table','image')),
  state       TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','approved','rejected','retracted')),
  origin      TEXT NOT NULL DEFAULT 'llm' CHECK (origin IN ('llm','library','fallback','human')),
  title_zh    TEXT, title_en TEXT,
  body_zh     TEXT, body_en  TEXT,
  mermaid     TEXT CHECK (mermaid IS NULL OR length(mermaid) <= 2000),
  payload     TEXT NOT NULL CHECK (json_valid(payload)),
  concept_key TEXT,
  t0_ms       INTEGER, t1_ms INTEGER,
  pinned      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
  llm_model   TEXT,
  latency_ms  INTEGER,
  approved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  approved_at REAL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (room_id, version),
  CHECK (id = 'visual:' || room_id || ':' || version)       -- id 為衍生值，強制一致
);
CREATE INDEX IF NOT EXISTS diagrams_room_state ON diagrams(room_id, state, created_at);
CREATE INDEX IF NOT EXISTS diagrams_concept ON diagrams(concept_key) WHERE state = 'approved';
CREATE INDEX IF NOT EXISTS diagrams_session ON diagrams(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS diagrams_approved_by ON diagrams(approved_by) WHERE approved_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS diagram_sources (
  diagram_id  TEXT NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
  segment_id  TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
  PRIMARY KEY (diagram_id, segment_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS diagram_sources_segment ON diagram_sources(segment_id);

CREATE TABLE IF NOT EXISTS assets (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('concept_card','svg','png','audio','export','other')),
  concept_key TEXT,
  title       TEXT,
  rel_path    TEXT NOT NULL UNIQUE,
  mime        TEXT NOT NULL,
  bytes       INTEGER NOT NULL CHECK (bytes >= 0),
  sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
  license     TEXT,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS assets_concept ON assets(concept_key) WHERE concept_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS diagram_assets (
  diagram_id  TEXT NOT NULL REFERENCES diagrams(id) ON DELETE CASCADE,
  asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  PRIMARY KEY (diagram_id, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS diagram_assets_asset ON diagram_assets(asset_id);

-- ---------------------------------------------------------------- 摘要 / 匯出
CREATE TABLE IF NOT EXISTS summaries (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('minutes','summary_zh','summary_en','outline','qa')),
  content_md  TEXT NOT NULL,
  model       TEXT,
  source_from_ms INTEGER, source_to_ms INTEGER,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS summaries_session ON summaries(session_id, kind, created_at);
CREATE TABLE IF NOT EXISTS exports (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  format      TEXT NOT NULL CHECK (format IN ('srt','vtt','docx','md','txt','json')),
  variant     TEXT NOT NULL DEFAULT 'bilingual' CHECK (variant IN ('zh','en','bilingual')),
  asset_id    INTEGER REFERENCES assets(id) ON DELETE SET NULL,
  created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS exports_session ON exports(session_id, created_at);
CREATE INDEX IF NOT EXISTS exports_asset ON exports(asset_id) WHERE asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS exports_created_by ON exports(created_by) WHERE created_by IS NOT NULL;

-- ---------------------------------------------------------------- 事件 / 指標
CREATE TABLE IF NOT EXISTS events (                 -- append-only；payload 不得含字幕全文或個資
  id          INTEGER PRIMARY KEY,
  ts          REAL NOT NULL DEFAULT (unixepoch('subsec')),
  room_id     TEXT, session_id TEXT, segment_id TEXT, -- 刻意不設 FK（稽核需在刪除後仍存在）；刪場次時由 purge 程序清理
  kind        TEXT NOT NULL,
  level       TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('debug','info','warn','error')),
  actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  payload     TEXT CHECK (payload IS NULL OR json_valid(payload))
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);                       -- 保留期清理 + 依時間列表
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_level ON events(level, ts);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_actor ON events(actor_id) WHERE actor_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS metrics (
  ts          REAL NOT NULL,
  name        TEXT NOT NULL,
  value       REAL NOT NULL,
  room_id     TEXT, session_id TEXT,
  labels      TEXT CHECK (labels IS NULL OR json_valid(labels))
);
CREATE INDEX IF NOT EXISTS metrics_name_ts ON metrics(name, ts);
CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts);                     -- 30 天保留 DELETE WHERE ts < ? 需要

CREATE TABLE IF NOT EXISTS metrics_rollup (         -- 30 天後的日彙總
  day         TEXT NOT NULL,                        -- 'YYYY-MM-DD'（台北時間）
  name        TEXT NOT NULL,
  room_id     TEXT NOT NULL DEFAULT '',
  n           INTEGER NOT NULL,
  p50         REAL, p95 REAL, max REAL, avg REAL,
  PRIMARY KEY (day, name, room_id)
) WITHOUT ROWID;

-- ---------------------------------------------------------------- 保留政策（由 jobs.py 讀取）
CREATE TABLE IF NOT EXISTS retention_policy (
  item        TEXT PRIMARY KEY,
  keep_days   INTEGER,                                -- NULL = 永久
  note        TEXT
);
INSERT OR IGNORE INTO retention_policy(item, keep_days, note) VALUES
  ('metrics_raw',            30,  'rollup 到 metrics_rollup 後刪除'),
  ('events_debug_info',      30,  NULL),
  ('events_warn_error',     180,  NULL),
  ('transcripts_noncurrent', 30,  '非 current 版本'),
  ('text_raw',               30,  '場次結束後清空 transcripts.text_raw'),
  ('session_content',       365,  '整場字幕；sessions.purge_after 可覆寫；legal_hold=1 不刪'),
  ('glossary_tm',          NULL,  '人工整理資產，永久；僅隨使用者刪除'),
  ('backups_daily',          14,  NULL),
  ('backups_weekly',         56,  '8 週，至少一份在異地/外接碟');

-- [zen-bridge admin] 以下三張表為後台新增（backend-review §1.3 / §2.2），非 DBA v2 原檔內容
-- ---------------------------------------------------------------- 後台：長工作（jobs）
-- 依 backend-review §3c：admin 行程內單一 asyncio worker；lease + heartbeat；
-- 啟動時把 lease 過期的 running 重新入列（attempt < max）或標 failed。
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,                 -- 'job_' + 時間排序 hex
  kind          TEXT NOT NULL CHECK (kind IN ('summarize','export','glossary_import','embed_backfill','backup','retranslate_batch')),
  target_json   TEXT NOT NULL DEFAULT '{}',
  params_json   TEXT NOT NULL DEFAULT '{}',
  params_hash   TEXT NOT NULL,
  state         TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued','running','cancel_requested','succeeded','failed','cancelled')),
  priority      INTEGER NOT NULL DEFAULT 0,
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  run_after     REAL NOT NULL DEFAULT (unixepoch('subsec')),
  lease_owner   TEXT, lease_until REAL,
  progress_done INTEGER, progress_total INTEGER, progress_stage TEXT, progress_msg TEXT,
  result_json   TEXT, error_json TEXT, idempotency_key TEXT,
  created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  started_at    REAL, finished_at REAL, heartbeat_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe ON jobs(kind, params_hash) WHERE state IN ('queued','running','cancel_requested');
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(state, run_after, priority DESC);

-- ---------------------------------------------------------------- 後台：瀏覽器 session（只存 hash）
CREATE TABLE IF NOT EXISTS admin_sessions (
  sid_hash      TEXT PRIMARY KEY,                 -- sha256(cookie 值)
  user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
  role          TEXT NOT NULL DEFAULT 'owner',
  csrf_token    TEXT NOT NULL,                    -- synchronizer token（綁定此 session）
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  last_seen_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  expires_at    REAL NOT NULL
);

-- ---------------------------------------------------------------- 後台：Idempotency-Key（保留 24 小時）
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key           TEXT NOT NULL,
  route         TEXT NOT NULL,
  body_sha256   TEXT NOT NULL,
  status        INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  headers_json  TEXT NOT NULL DEFAULT '{}',
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  PRIMARY KEY (key, route)
);

-- ---------------------------------------------------------------- 向量（不含聲紋；聲紋屬生物特徵，放 identity 庫）
CREATE TABLE IF NOT EXISTS embeddings (
  id          INTEGER PRIMARY KEY,
  owner_type  TEXT NOT NULL CHECK (owner_type IN ('transcript','translation','tm_unit','glossary_term','diagram','summary')),
  owner_id    TEXT NOT NULL,
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL CHECK (dim > 0),
  vector      BLOB NOT NULL CHECK (length(vector) = 4 * dim),
  text_sha1   TEXT NOT NULL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  UNIQUE (owner_type, owner_id, model)
);
-- 多型關聯無法用 FK：以觸發器在 owner 刪除時連帶清除（避免含個資的衍生資料殘留）
CREATE TRIGGER IF NOT EXISTS transcripts_emb_ad AFTER DELETE ON transcripts BEGIN
  DELETE FROM embeddings WHERE owner_type = 'transcript' AND owner_id = CAST(old.id AS TEXT);
END;
CREATE TRIGGER IF NOT EXISTS translations_emb_ad AFTER DELETE ON translations BEGIN
  DELETE FROM embeddings WHERE owner_type = 'translation' AND owner_id = CAST(old.id AS TEXT);
END;
CREATE TRIGGER IF NOT EXISTS tm_units_emb_ad AFTER DELETE ON tm_units BEGIN
  DELETE FROM embeddings WHERE owner_type = 'tm_unit' AND owner_id = CAST(old.id AS TEXT);
END;
CREATE TRIGGER IF NOT EXISTS glossary_terms_emb_ad AFTER DELETE ON glossary_terms BEGIN
  DELETE FROM embeddings WHERE owner_type = 'glossary_term' AND owner_id = CAST(old.id AS TEXT);
END;
CREATE TRIGGER IF NOT EXISTS diagrams_emb_ad AFTER DELETE ON diagrams BEGIN
  DELETE FROM embeddings WHERE owner_type = 'diagram' AND owner_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS summaries_emb_ad AFTER DELETE ON summaries BEGIN
  DELETE FROM embeddings WHERE owner_type = 'summary' AND owner_id = CAST(old.id AS TEXT);
END;
-- 內容改變時讓向量失效（下次重算）
CREATE TRIGGER IF NOT EXISTS transcripts_emb_au AFTER UPDATE OF text ON transcripts BEGIN
  DELETE FROM embeddings WHERE owner_type = 'transcript' AND owner_id = CAST(old.id AS TEXT);
END;

-- =====================================================================
-- FTS5（external content）。規則：
--   * 只在「被索引欄位」改變時觸發 UPDATE 觸發器（v1 glossary_terms_au 會被 hit_count 更新觸發）。
--   * 一律用 INSERT INTO fts(fts, rowid, ...) VALUES('delete', old...) 形式刪除。
--   * 禁止對有 FTS 的表使用 INSERT OR REPLACE / REPLACE（recursive_triggers=OFF 時不會觸發 delete 觸發器）；
--     請改用 INSERT ... ON CONFLICT DO UPDATE。connect() 另設 PRAGMA recursive_triggers=ON 作保險。
--   * 搜尋時一律 JOIN 基表並加 is_current = 1（FTS 索引所有版本，才能用 'rebuild' 與 integrity-check rank=1 驗證）。
-- =====================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
  text, content='transcripts', content_rowid='id', tokenize='trigram'
);
-- 1–2 字查詢用：unicode61 對「已在每個 CJK 字前後插入空白」的 text_uni 建單字索引；2 字用 phrase 查詢保證相鄰
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts_uni USING fts5(
  text_uni, content='transcripts', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS transcripts_ai AFTER INSERT ON transcripts BEGIN
  INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
  INSERT INTO transcripts_fts_uni(rowid, text_uni) VALUES (new.id, new.text_uni);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_ad AFTER DELETE ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO transcripts_fts_uni(transcripts_fts_uni, rowid, text_uni) VALUES ('delete', old.id, old.text_uni);
END;
CREATE TRIGGER IF NOT EXISTS transcripts_au AFTER UPDATE OF text, text_uni ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
  INSERT INTO transcripts_fts_uni(transcripts_fts_uni, rowid, text_uni) VALUES ('delete', old.id, old.text_uni);
  INSERT INTO transcripts_fts_uni(rowid, text_uni) VALUES (new.id, new.text_uni);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS translations_fts USING fts5(
  text, content='translations', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS translations_ai AFTER INSERT ON translations BEGIN
  INSERT INTO translations_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS translations_ad AFTER DELETE ON translations BEGIN
  INSERT INTO translations_fts(translations_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS translations_au AFTER UPDATE OF text ON translations BEGIN
  INSERT INTO translations_fts(translations_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO translations_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS glossary_fts USING fts5(
  zh, en, aliases, note, content='glossary_terms', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS glossary_terms_ai AFTER INSERT ON glossary_terms BEGIN
  INSERT INTO glossary_fts(rowid, zh, en, aliases, note) VALUES (new.id, new.zh, new.en, new.aliases, new.note);
END;
CREATE TRIGGER IF NOT EXISTS glossary_terms_ad AFTER DELETE ON glossary_terms BEGIN
  INSERT INTO glossary_fts(glossary_fts, rowid, zh, en, aliases, note) VALUES ('delete', old.id, old.zh, old.en, old.aliases, old.note);
END;
CREATE TRIGGER IF NOT EXISTS glossary_terms_au AFTER UPDATE OF zh, en, aliases, note ON glossary_terms BEGIN
  INSERT INTO glossary_fts(glossary_fts, rowid, zh, en, aliases, note) VALUES ('delete', old.id, old.zh, old.en, old.aliases, old.note);
  INSERT INTO glossary_fts(rowid, zh, en, aliases, note) VALUES (new.id, new.zh, new.en, new.aliases, new.note);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS tm_fts USING fts5(
  src_text, tgt_text, content='tm_units', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS tm_units_ai AFTER INSERT ON tm_units BEGIN
  INSERT INTO tm_fts(rowid, src_text, tgt_text) VALUES (new.id, new.src_text, new.tgt_text);
END;
CREATE TRIGGER IF NOT EXISTS tm_units_ad AFTER DELETE ON tm_units BEGIN
  INSERT INTO tm_fts(tm_fts, rowid, src_text, tgt_text) VALUES ('delete', old.id, old.src_text, old.tgt_text);
END;
CREATE TRIGGER IF NOT EXISTS tm_units_au AFTER UPDATE OF src_text, tgt_text ON tm_units BEGIN
  INSERT INTO tm_fts(tm_fts, rowid, src_text, tgt_text) VALUES ('delete', old.id, old.src_text, old.tgt_text);
  INSERT INTO tm_fts(rowid, src_text, tgt_text) VALUES (new.id, new.src_text, new.tgt_text);
END;

-- 版本切換（保險；應用層也做）
CREATE TRIGGER IF NOT EXISTS transcripts_bump BEFORE INSERT ON transcripts
WHEN new.is_current = 1 BEGIN
  UPDATE transcripts SET is_current = 0 WHERE segment_id = new.segment_id AND is_current = 1;
END;
CREATE TRIGGER IF NOT EXISTS translations_bump BEFORE INSERT ON translations
WHEN new.is_current = 1 BEGIN
  UPDATE translations SET is_current = 0 WHERE segment_id = new.segment_id AND tgt_lang = new.tgt_lang AND is_current = 1;
END;

-- 後台列表 view（不再含講者真名；需要時由後台 JOIN ident.speaker_identities）
CREATE VIEW IF NOT EXISTS v_segment_current AS
SELECT s.id AS segment_id, s.room_id, s.session_id, s.seq, s.t0_ms, s.t1_ms, s.status,
       sp.label AS speaker_label,
       t.text AS zh, t.text_raw AS zh_raw, t.asr_ms, t.audio_ms,
       tr.text AS en, tr.origin AS en_origin, tr.model AS mt_model, tr.latency_ms AS mt_ms, tr.term_flags
FROM segments s
LEFT JOIN speakers sp ON sp.id = s.speaker_id
LEFT JOIN transcripts t  ON t.segment_id = s.id AND t.is_current = 1
LEFT JOIN translations tr ON tr.segment_id = s.id AND tr.tgt_lang = 'en' AND tr.is_current = 1;

-- OPTIONAL: sqlite-vec（載入 extension 後才執行；維度以實際 embedding 長度為準）
-- CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(embedding_id INTEGER PRIMARY KEY, embedding float[1024]);

INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '2');   -- schema_meta 無 FTS，REPLACE 安全
INSERT OR IGNORE INTO schema_migrations(version, name, checksum) VALUES (2, 'baseline_v2', 'see-runner');
PRAGMA user_version = 2;
