-- =====================================================================
-- zen-bridge 個資庫 zen-identity.sqlite3（與主庫分檔；檔案 ACL 只給執行服務的 Windows 帳號）
-- 由後台以 ATTACH DATABASE '<path>\zen-identity.sqlite3' AS ident 掛上；live-room ledger 不掛載。
-- 跨檔不能有 FK：user_id / speaker_id 與主庫同值，刪除由應用層 purge 程序處理（見報告 §8）。
-- 不得對本庫任何欄位建立 FTS；不得把本庫資料寫進 events.payload。
-- =====================================================================
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS user_accounts (
  user_id       INTEGER PRIMARY KEY,               -- = main.users.id
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT,
  password_hash TEXT,                              -- argon2id；本機單人可 NULL
  last_login_at REAL
);
CREATE TABLE IF NOT EXISTS api_tokens (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES user_accounts(user_id) ON DELETE CASCADE,
  token_hash  TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),   -- sha256；明文不落地
  label       TEXT,
  scopes      TEXT NOT NULL DEFAULT 'read' CHECK (scopes IN ('read','read,write','read,write,admin')),
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec')),
  expires_at  REAL,
  revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id);
CREATE TABLE IF NOT EXISTS speaker_identities (
  speaker_id   INTEGER PRIMARY KEY,                -- = main.speakers.id
  session_id   TEXT NOT NULL,                      -- = main.sessions.id（供整場刪除）
  display_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS speaker_identities_session ON speaker_identities(session_id);
CREATE TABLE IF NOT EXISTS voiceprints (             -- 生物特徵：預設不建立資料；需明示同意
  speaker_id  INTEGER PRIMARY KEY REFERENCES speaker_identities(speaker_id) ON DELETE CASCADE,
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL CHECK (dim > 0),
  vector      BLOB NOT NULL CHECK (length(vector) = 4 * dim),
  consent_at  REAL NOT NULL,
  created_at  REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
PRAGMA user_version = 1;
