CREATE TABLE `segment_revisions` (
	`id` text PRIMARY KEY NOT NULL,
	`segment_id` text NOT NULL,
	`payload` text NOT NULL,
	`created_at` text NOT NULL,
	FOREIGN KEY (`segment_id`) REFERENCES `segments`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_segment_revisions_segment` ON `segment_revisions` (`segment_id`);--> statement-breakpoint
ALTER TABLE `segments` ADD `original_zh` text DEFAULT '' NOT NULL;--> statement-breakpoint
ALTER TABLE `segments` ADD `original_en` text DEFAULT '' NOT NULL;--> statement-breakpoint
ALTER TABLE `sessions` ADD `speaker_ids` text DEFAULT '[]' NOT NULL;
--> statement-breakpoint
UPDATE segments SET original_zh=zh,original_en=en;
