CREATE TABLE `music_candidate_origins` (
	`candidate_key` text NOT NULL,
	`seed_track_key` text NOT NULL,
	`relationship` text DEFAULT 'direct' NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `music_candidate_origins_identity` ON `music_candidate_origins` (`candidate_key`,`seed_track_key`,`relationship`);--> statement-breakpoint
CREATE INDEX `idx_music_candidate_origins_seed` ON `music_candidate_origins` (`seed_track_key`);