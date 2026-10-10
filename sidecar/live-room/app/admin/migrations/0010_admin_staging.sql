-- v3 -> v10（round3 §5-1 後台 staging 核准流程）：人工修正先進 staging_items，
-- 管理員核准後才在同一個交易內寫入 tm_units／glossary_terms，並留下 staging_audit。
-- 0004–0009 保留給其他遷移（BRIEF：backend 從 0010 起編號避免撞號）。
-- 由 app.admin.db 的增量遷移器在單一 BEGIN IMMEDIATE 交易內執行；可重複執行（IF NOT EXISTS）。

CREATE TABLE IF NOT EXISTS staging_items (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('tm','term')),
  state         TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','approved','rejected','superseded','withdrawn')),
  rev           INTEGER NOT NULL DEFAULT 1,                 -- ETag "s{id}-r{rev}"
  tgt_lang      TEXT NOT NULL DEFAULT 'en' CHECK (tgt_lang IN ('en','ja')),
  -- tm: src_text = 中文原句、tgt_text = 譯文；term: src_text = 中文詞、tgt_text = 譯詞
  src_text      TEXT NOT NULL CHECK (length(src_text) BETWEEN 1 AND 2000),
  tgt_text      TEXT NOT NULL CHECK (length(tgt_text) BETWEEN 1 AND 2000),
  aliases       TEXT NOT NULL DEFAULT '[]'
                CHECK (json_valid(aliases) AND json_type(aliases) = 'array' AND json_array_length(aliases) <= 8),
  note          TEXT CHECK (note IS NULL OR length(note) <= 500),
  target_key    TEXT NOT NULL,                              -- tm: src_hash；term: "g{glossary_id}:{zh}"
  base_json     TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(base_json)),   -- 送審當下目標條目的快照
  base_sig      TEXT NOT NULL CHECK (length(base_sig) = 64),                 -- sha256(base_json)：核准時比對
  correction_id INTEGER REFERENCES corrections(id) ON DELETE CASCADE,
  segment_id    TEXT REFERENCES segments(id) ON DELETE CASCADE,   -- 刪場次時連同 staging 一起清（個資）
  room_id       TEXT REFERENCES rooms(id) ON DELETE SET NULL,
  author_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  author_via    TEXT,
  decided_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  decided_via   TEXT,
  decided_at    REAL,
  decision_note TEXT CHECK (decision_note IS NULL OR length(decision_note) <= 500),   -- 退回原因／強制核准說明
  target_id     INTEGER,                                    -- 核准後寫入的 tm_units.id 或 glossary_terms.id
  superseded_by INTEGER REFERENCES staging_items(id) ON DELETE SET NULL,
  created_at    REAL NOT NULL DEFAULT (unixepoch('subsec')),
  updated_at    REAL NOT NULL DEFAULT (unixepoch('subsec'))
);
CREATE INDEX IF NOT EXISTS staging_items_list ON staging_items(state, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS staging_items_correction ON staging_items(correction_id);
CREATE INDEX IF NOT EXISTS staging_items_segment ON staging_items(segment_id);
-- 同一個目標（同句同語言、同詞表同詞）同時只會有一筆待審；新提案會把舊的標成 superseded
CREATE UNIQUE INDEX IF NOT EXISTS staging_items_one_pending
  ON staging_items(kind, tgt_lang, target_key) WHERE state = 'pending';

-- 稽核軌跡：誰、何時、動作、前後內容（events 表不放全文，所以前後內容記在這裡，跟著 staging_items 一起清）
CREATE TABLE IF NOT EXISTS staging_audit (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES staging_items(id) ON DELETE CASCADE,
  action      TEXT NOT NULL CHECK (action IN ('create','edit','approve','reject','withdraw','supersede')),
  actor_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  actor_via   TEXT,
  at          REAL NOT NULL DEFAULT (unixepoch('subsec')),
  before_json TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
  after_json  TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
  note        TEXT CHECK (note IS NULL OR length(note) <= 500)
);
CREATE INDEX IF NOT EXISTS staging_audit_item ON staging_audit(item_id, id);
