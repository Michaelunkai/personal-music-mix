import { sqliteTable, text, integer, real, index, uniqueIndex } from 'drizzle-orm/sqlite-core';

export const library = sqliteTable('music_library', {
  trackKey: text('track_key').primaryKey(),
  payload: text('payload').notNull(),
});
export const favorites = sqliteTable('music_favorites', {
  trackKey: text('track_key').primaryKey(),
  liked: integer('liked').notNull().default(1),
  updatedAt: text('updated_at').notNull(),
});
export const state = sqliteTable('music_state', {
  key: text('key').primaryKey(),
  payload: text('payload').notNull(),
});
export const served = sqliteTable('music_served', {
  trackKey: text('track_key').primaryKey(),
  videoId: text('video_id').unique(),
  recordingKey: text('recording_key'),
  servedAt: text('served_at').notNull(),
  reservationId: text('reservation_id'),
}, table => [uniqueIndex('music_served_recording_key_unique').on(table.recordingKey)]);
export const providerCandidates = sqliteTable('music_candidates', {
  candidateKey: text('candidate_key').primaryKey(),
  recordingKey: text('recording_key').notNull(),
  provider: text('provider').notNull(),
  providerTrackId: text('provider_track_id').notNull(),
  title: text('title').notNull(),
  artist: text('artist').notNull(),
  album: text('album').notNull().default(''),
  genre: text('genre').notNull().default(''),
  mood: text('mood').notNull().default(''),
  durationSeconds: real('duration_seconds'),
  audioUrl: text('audio_url').notNull(),
  providerUrl: text('provider_url').notNull(),
  artworkUrl: text('artwork_url'),
  seedKeys: text('seed_keys').notNull().default('[]'),
  discoveredAt: text('discovered_at').notNull(),
  lastVerifiedAt: text('last_verified_at'),
}, table => [
  uniqueIndex('music_candidates_recording_key_unique').on(table.recordingKey),
  index('idx_music_candidates_discovered_at').on(table.discoveredAt),
]);
export const candidateOrigins = sqliteTable('music_candidate_origins', {
  candidateKey: text('candidate_key').notNull(),
  seedTrackKey: text('seed_track_key').notNull(),
  relationship: text('relationship').notNull().default('direct'),
}, table => [
  uniqueIndex('music_candidate_origins_identity').on(table.candidateKey, table.seedTrackKey, table.relationship),
  index('idx_music_candidate_origins_seed').on(table.seedTrackKey),
]);
export const mixRequests = sqliteTable('music_mix_requests', {
  requestId: text('request_id').primaryKey(),
  state: text('state').notNull().default('pending'),
  leaseOwner: text('lease_owner'),
  leaseUntilMs: integer('lease_until_ms').notNull().default(0),
  responseJson: text('response_json'),
  createdAt: text('created_at').notNull(),
  updatedAt: text('updated_at').notNull(),
}, table => [
  index('idx_music_mix_requests_created').on(table.createdAt),
]);
export const feedback = sqliteTable('music_feedback', {
  eventId: text('event_id').primaryKey(),
  trackKey: text('track_key').notNull(),
  recordingKey: text('recording_key').notNull(),
  provider: text('provider').notNull(),
  event: text('event').notNull(),
  listenedSeconds: integer('listened_seconds').notNull().default(0),
  durationSeconds: integer('duration_seconds'),
  createdAt: text('created_at').notNull(),
}, table => [
  index('idx_music_feedback_track_created').on(table.trackKey, table.createdAt),
  index('idx_music_feedback_recording_created').on(table.recordingKey, table.createdAt),
]);
