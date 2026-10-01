CREATE TABLE `memories` (
	`id` text PRIMARY KEY NOT NULL,
	`person_id` text,
	`role` text DEFAULT '通用' NOT NULL,
	`zh` text NOT NULL,
	`en` text NOT NULL,
	`meaning` text DEFAULT '' NOT NULL,
	`context` text DEFAULT '' NOT NULL,
	`status` text DEFAULT 'candidate' NOT NULL,
	`source_id` text,
	`version` integer DEFAULT 1 NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	FOREIGN KEY (`person_id`) REFERENCES `people`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`source_id`) REFERENCES `segments`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_memories_status_person` ON `memories` (`status`,`person_id`);--> statement-breakpoint
CREATE UNIQUE INDEX `idx_memories_source` ON `memories` (`source_id`);--> statement-breakpoint
CREATE TABLE `memory_revisions` (
	`id` text PRIMARY KEY NOT NULL,
	`memory_id` text NOT NULL,
	`payload` text NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`memory_id`) REFERENCES `memories`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `people` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`role` text NOT NULL,
	`notes` text DEFAULT '' NOT NULL,
	`reference_key` text,
	`reference_type` text,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `segments` (
	`id` text PRIMARY KEY NOT NULL,
	`session_id` text NOT NULL,
	`person_id` text,
	`label` text NOT NULL,
	`role` text NOT NULL,
	`zh` text NOT NULL,
	`en` text DEFAULT '' NOT NULL,
	`note` text DEFAULT '' NOT NULL,
	`audio_key` text,
	`offset` real DEFAULT 0 NOT NULL,
	`source` text NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`session_id`) REFERENCES `sessions`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`person_id`) REFERENCES `people`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_segments_session_created` ON `segments` (`session_id`,`created_at`);--> statement-breakpoint
CREATE TABLE `sessions` (
	`id` text PRIMARY KEY NOT NULL,
	`title` text NOT NULL,
	`topic` text DEFAULT '' NOT NULL,
	`notes` text DEFAULT '' NOT NULL,
	`roles` text DEFAULT '{}' NOT NULL,
	`status` text DEFAULT 'ready' NOT NULL,
	`created_at` text NOT NULL
);
