CREATE TABLE `music_favorites` (
	`track_key` text PRIMARY KEY NOT NULL,
	`liked` integer DEFAULT 1 NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `music_library` (
	`track_key` text PRIMARY KEY NOT NULL,
	`payload` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `music_state` (
	`key` text PRIMARY KEY NOT NULL,
	`payload` text NOT NULL
);
