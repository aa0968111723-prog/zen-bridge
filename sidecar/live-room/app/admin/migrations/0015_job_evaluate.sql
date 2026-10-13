-- v14 -> v15：課後評分可排進 jobs。不改即時字幕，也不註冊 embed_backfill。
CREATE TABLE jobs_v15 (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('summarize','export','glossary_import','embed_backfill','backup','retranslate_batch','enhance','diarize','evaluate')),
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
INSERT INTO jobs_v15 SELECT * FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v15 RENAME TO jobs;
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs(kind, params_hash) WHERE state IN ('queued','running','cancel_requested');
CREATE INDEX jobs_claim ON jobs(state, run_after, priority DESC);
