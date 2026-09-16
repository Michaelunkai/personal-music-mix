CREATE TABLE `music_feedback` (
	`event_id` text PRIMARY KEY NOT NULL,
	`track_key` text NOT NULL,
	`recording_key` text NOT NULL,
	`provider` text NOT NULL,
	`event` text NOT NULL,
	`listened_seconds` integer DEFAULT 0 NOT NULL,
	`duration_seconds` integer,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_music_feedback_track_created` ON `music_feedback` (`track_key`,`created_at`);--> statement-breakpoint
CREATE INDEX `idx_music_feedback_recording_created` ON `music_feedback` (`recording_key`,`created_at`);--> statement-breakpoint
CREATE TABLE `music_candidates` (
	`candidate_key` text PRIMARY KEY NOT NULL,
	`recording_key` text NOT NULL,
	`provider` text NOT NULL,
	`provider_track_id` text NOT NULL,
	`title` text NOT NULL,
	`artist` text NOT NULL,
	`album` text DEFAULT '' NOT NULL,
	`genre` text DEFAULT '' NOT NULL,
	`mood` text DEFAULT '' NOT NULL,
	`duration_seconds` real,
	`audio_url` text NOT NULL,
	`provider_url` text NOT NULL,
	`artwork_url` text,
	`seed_keys` text DEFAULT '[]' NOT NULL,
	`discovered_at` text NOT NULL,
	`last_verified_at` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `music_candidates_recording_key_unique` ON `music_candidates` (`recording_key`);--> statement-breakpoint
CREATE INDEX `idx_music_candidates_discovered_at` ON `music_candidates` (`discovered_at`);--> statement-breakpoint
ALTER TABLE `music_served` ADD `recording_key` text;--> statement-breakpoint
CREATE UNIQUE INDEX `music_served_recording_key_unique` ON `music_served` (`recording_key`);