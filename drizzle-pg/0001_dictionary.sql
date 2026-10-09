CREATE TABLE dictionary_sources (
 id text PRIMARY KEY, title text NOT NULL, license text NOT NULL,
 license_url text NOT NULL DEFAULT '', source_url text NOT NULL DEFAULT '',
 version text NOT NULL DEFAULT '', sha256 text NOT NULL DEFAULT '',
 entry_count integer NOT NULL DEFAULT 0, public integer NOT NULL DEFAULT 0,
 updated_at text NOT NULL
);
--> statement-breakpoint
CREATE TABLE dictionary_entries (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES dictionary_sources(id),
 snapshot text NOT NULL, word text NOT NULL, alternative text NOT NULL DEFAULT '',
 pronunciation text NOT NULL DEFAULT '', zh text NOT NULL DEFAULT '', en text NOT NULL DEFAULT ''
);
--> statement-breakpoint
CREATE INDEX idx_dictionary_snapshot ON dictionary_entries(source_id,snapshot);
--> statement-breakpoint
CREATE TABLE dictionary_keys (
 entry_id text NOT NULL REFERENCES dictionary_entries(id), key text COLLATE "C" NOT NULL,
 PRIMARY KEY(entry_id,key)
);
--> statement-breakpoint
CREATE INDEX idx_dictionary_key ON dictionary_keys(key);
