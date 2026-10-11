-- 訓練資料庫 training.sqlite3（與 zen.sqlite3 分開；撤回同意時可整批刪除，不碰字幕主庫）。
-- 位置：%LOCALAPPDATA%\ZenBridge\training\training.sqlite3（ZEN_TRAINING_DIR 可覆寫）。
-- 不存學號、電話、Email、真名；speakers.label 只放暱稱。
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS speakers (
  id            TEXT PRIMARY KEY CHECK (id GLOB 'spk_[a-z0-9]*' AND length(id) BETWEEN 6 AND 40),
  label         TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 24),
  accent_note   TEXT CHECK (accent_note IS NULL OR length(accent_note) <= 40),   -- 例：「台語腔」，不得寫個資
  created_at    REAL NOT NULL,
  withdrawn_at  REAL                                                          -- 撤回後 label 改成「已撤回」
);

CREATE TABLE IF NOT EXISTS consents (
  id               INTEGER PRIMARY KEY,
  speaker_id       TEXT NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
  scope_asr        INTEGER NOT NULL CHECK (scope_asr IN (0,1)),
  scope_mt         INTEGER NOT NULL CHECK (scope_mt IN (0,1)),
  local_only       INTEGER NOT NULL DEFAULT 1 CHECK (local_only = 1),         -- 只能是本機
  harvest_live     INTEGER NOT NULL DEFAULT 0 CHECK (harvest_live IN (0,1)),  -- 是否允許從社課現場收錄
  consent_version  TEXT NOT NULL,
  consent_date     TEXT NOT NULL CHECK (consent_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  recorded_by      TEXT NOT NULL CHECK (length(recorded_by) BETWEEN 1 AND 40),
  granted_at       REAL NOT NULL,
  revoked_at       REAL,
  CHECK (scope_asr = 1 OR scope_mt = 1)
);
CREATE INDEX IF NOT EXISTS consents_speaker ON consents(speaker_id, revoked_at);

-- 一次錄音：錄音室一次坐下錄、社課一場收錄、或匯入的一個既有錄音
CREATE TABLE IF NOT EXISTS recordings (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('studio','harvest','import')),
  speaker_id    TEXT REFERENCES speakers(id) ON DELETE SET NULL,
  room_id       TEXT,
  session_id    TEXT,
  title         TEXT CHECK (title IS NULL OR length(title) <= 80),
  status        TEXT NOT NULL DEFAULT 'recording' CHECK (status IN ('recording','ended')),
  dir           TEXT NOT NULL,                     -- 相對 training 目錄
  source_audio  TEXT,                              -- 匯入：轉好的 16k 原始檔（含他人聲音，隔離區）
  started_at    REAL NOT NULL,
  ended_at      REAL,
  transcript_at REAL                               -- 最後一次寫出完整逐字稿的時間
);
CREATE INDEX IF NOT EXISTS recordings_speaker ON recordings(speaker_id);

CREATE TABLE IF NOT EXISTS clips (
  id            TEXT PRIMARY KEY,
  recording_id  TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  speaker_id    TEXT REFERENCES speakers(id) ON DELETE CASCADE,
  speaker_tag   TEXT,                              -- 匯入檔裡原本的講者標籤（對應前）
  origin        TEXT NOT NULL CHECK (origin IN ('studio','harvest','import')),
  prompt_id     INTEGER REFERENCES prompts(id) ON DELETE SET NULL,
  segment_id    TEXT,                              -- 社課：pipeline 的 "{room}:{session}:{seq}"
  ord           INTEGER NOT NULL DEFAULT 0,        -- 在這次錄音裡的順序
  t0_ms         INTEGER,                           -- 錄音時間軸（社課 = 直播時間軸）
  t1_ms         INTEGER,
  audio_path    TEXT,                              -- 相對 training 目錄；NULL = 還沒切或已刪
  sha256        TEXT,
  duration_s    REAL,
  rms_dbfs      REAL,
  peak_dbfs     REAL,
  clip_ratio    REAL,
  silence_ratio REAL,
  snr_db        REAL,
  draft_text    TEXT,                              -- 草稿字幕（PR #49 草稿 ASR 接上後填）
  asr_text      TEXT,                              -- Breeze 原始輸出
  text          TEXT,                              -- 參考文字（提示句／人工修正後）
  tgt_lang      TEXT CHECK (tgt_lang IS NULL OR tgt_lang IN ('en','ja')),
  mt_text       TEXT,                              -- 當時的譯文
  status        TEXT NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('unverified','approved','rejected','needs_review')),
  align         TEXT NOT NULL DEFAULT 'native' CHECK (align IN ('native','file','forced','none')),
  reviewed_by   TEXT,
  reviewed_at   REAL,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status, created_at);
CREATE INDEX IF NOT EXISTS clips_speaker ON clips(speaker_id);
CREATE INDEX IF NOT EXISTS clips_recording ON clips(recording_id, ord);
CREATE UNIQUE INDEX IF NOT EXISTS clips_segment ON clips(segment_id) WHERE segment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS prompts (
  id         INTEGER PRIMARY KEY,
  source     TEXT NOT NULL CHECK (source IN ('club','glossary','dharma','mixed','custom')),
  text       TEXT NOT NULL CHECK (length(text) BETWEEN 2 AND 200),
  term       TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  UNIQUE (source, text)
);

CREATE TABLE IF NOT EXISTS mt_pairs (
  id          INTEGER PRIMARY KEY,
  src_text    TEXT NOT NULL,
  tgt_lang    TEXT NOT NULL CHECK (tgt_lang IN ('en','ja')),
  tgt_text    TEXT NOT NULL,
  origin      TEXT NOT NULL CHECK (origin IN ('review','tm','ledger')),
  clip_id     TEXT REFERENCES clips(id) ON DELETE CASCADE,
  speaker_id  TEXT REFERENCES speakers(id) ON DELETE CASCADE,
  session_id  TEXT,
  segment_id  TEXT,
  tm_unit_id  INTEGER,                             -- zen.sqlite3 tm_units.id（核准後回填）
  staging_id  INTEGER,                             -- zen.sqlite3 staging_items.id（送審 TM）
  created_at  REAL NOT NULL,
  UNIQUE (src_text, tgt_lang, tgt_text)
);

CREATE TABLE IF NOT EXISTS harvest_optins (
  id          INTEGER PRIMARY KEY,
  room_id     TEXT NOT NULL,
  session_id  TEXT,                                -- NULL = 綁定這個房間下一個出現的場次
  speaker_id  TEXT NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
  recording_id TEXT REFERENCES recordings(id) ON DELETE SET NULL,
  enabled_by  TEXT NOT NULL,
  enabled_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  stopped_at  REAL
);
CREATE INDEX IF NOT EXISTS harvest_optins_room ON harvest_optins(room_id, stopped_at);

CREATE TABLE IF NOT EXISTS exports (
  id          TEXT PRIMARY KEY,
  dir         TEXT NOT NULL,
  speakers    TEXT NOT NULL DEFAULT '[]',
  stats       TEXT NOT NULL DEFAULT '{}',
  created_at  REAL NOT NULL,
  deleted_at  REAL,
  deleted_reason TEXT
);

-- 稽核：不放字幕全文、不放暱稱
CREATE TABLE IF NOT EXISTS audit (
  id         INTEGER PRIMARY KEY,
  at         REAL NOT NULL,
  action     TEXT NOT NULL,
  speaker_id TEXT,
  actor      TEXT,
  detail     TEXT NOT NULL DEFAULT '{}'
);
