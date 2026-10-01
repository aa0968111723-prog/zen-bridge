CREATE TABLE "memories" (
	"id" text PRIMARY KEY NOT NULL,
	"person_id" text,
	"role" text DEFAULT '通用' NOT NULL,
	"zh" text NOT NULL,
	"en" text NOT NULL,
	"meaning" text DEFAULT '' NOT NULL,
	"context" text DEFAULT '' NOT NULL,
	"status" text DEFAULT 'candidate' NOT NULL,
	"source_id" text,
	"version" integer DEFAULT 1 NOT NULL,
	"created_at" text NOT NULL,
	"updated_at" text NOT NULL
);
--> statement-breakpoint
CREATE TABLE "memory_revisions" (
	"id" text PRIMARY KEY NOT NULL,
	"memory_id" text NOT NULL,
	"payload" text NOT NULL,
	"created_at" text NOT NULL
);
--> statement-breakpoint
CREATE TABLE "people" (
	"id" text PRIMARY KEY NOT NULL,
	"name" text NOT NULL,
	"role" text NOT NULL,
	"notes" text DEFAULT '' NOT NULL,
	"reference_key" text,
	"reference_type" text,
	"created_at" text NOT NULL
);
--> statement-breakpoint
CREATE TABLE "segment_revisions" (
	"id" text PRIMARY KEY NOT NULL,
	"segment_id" text NOT NULL,
	"payload" text NOT NULL,
	"created_at" text NOT NULL
);
--> statement-breakpoint
CREATE TABLE "segments" (
	"id" text PRIMARY KEY NOT NULL,
	"session_id" text NOT NULL,
	"person_id" text,
	"label" text NOT NULL,
	"role" text NOT NULL,
	"direction" text DEFAULT 'zh-en' NOT NULL,
	"zh" text NOT NULL,
	"en" text DEFAULT '' NOT NULL,
	"original_zh" text DEFAULT '' NOT NULL,
	"original_en" text DEFAULT '' NOT NULL,
	"note" text DEFAULT '' NOT NULL,
	"audio_key" text,
	"offset" double precision DEFAULT 0 NOT NULL,
	"source" text NOT NULL,
	"created_at" text NOT NULL
);
--> statement-breakpoint
CREATE TABLE "sessions" (
	"id" text PRIMARY KEY NOT NULL,
	"title" text NOT NULL,
	"topic" text DEFAULT '' NOT NULL,
	"notes" text DEFAULT '' NOT NULL,
	"roles" text DEFAULT '{}' NOT NULL,
	"speaker_ids" text DEFAULT '[]' NOT NULL,
	"status" text DEFAULT 'ready' NOT NULL,
	"created_at" text NOT NULL
);
--> statement-breakpoint
ALTER TABLE "memories" ADD CONSTRAINT "memories_person_id_people_id_fk" FOREIGN KEY ("person_id") REFERENCES "public"."people"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "memories" ADD CONSTRAINT "memories_source_id_segments_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."segments"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "memory_revisions" ADD CONSTRAINT "memory_revisions_memory_id_memories_id_fk" FOREIGN KEY ("memory_id") REFERENCES "public"."memories"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "segment_revisions" ADD CONSTRAINT "segment_revisions_segment_id_segments_id_fk" FOREIGN KEY ("segment_id") REFERENCES "public"."segments"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "segments" ADD CONSTRAINT "segments_session_id_sessions_id_fk" FOREIGN KEY ("session_id") REFERENCES "public"."sessions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "segments" ADD CONSTRAINT "segments_person_id_people_id_fk" FOREIGN KEY ("person_id") REFERENCES "public"."people"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "idx_memories_status_person" ON "memories" USING btree ("status","person_id");--> statement-breakpoint
CREATE UNIQUE INDEX "idx_memories_source" ON "memories" USING btree ("source_id");--> statement-breakpoint
CREATE INDEX "idx_segment_revisions_segment" ON "segment_revisions" USING btree ("segment_id");--> statement-breakpoint
CREATE INDEX "idx_segments_session_created" ON "segments" USING btree ("session_id","created_at");