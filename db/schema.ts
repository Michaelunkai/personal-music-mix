import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';

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
  servedAt: text('served_at').notNull(),
  reservationId: text('reservation_id'),
});
