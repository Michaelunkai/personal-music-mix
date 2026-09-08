CREATE TABLE `music_served` (
	`track_key` text PRIMARY KEY NOT NULL,
	`video_id` text,
	`served_at` text NOT NULL,
	`reservation_id` text
);
--> statement-breakpoint
CREATE UNIQUE INDEX `music_served_video_id_unique` ON `music_served` (`video_id`);
