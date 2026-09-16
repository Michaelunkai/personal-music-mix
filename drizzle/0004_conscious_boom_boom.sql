CREATE TABLE `music_mix_requests` (
	`request_id` text PRIMARY KEY NOT NULL,
	`state` text DEFAULT 'pending' NOT NULL,
	`lease_owner` text,
	`lease_until_ms` integer DEFAULT 0 NOT NULL,
	`response_json` text,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `idx_music_mix_requests_created` ON `music_mix_requests` (`created_at`);