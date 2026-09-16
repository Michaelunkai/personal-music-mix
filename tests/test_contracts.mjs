import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { MUSIC_FEEDBACK_EVENTS, normalizeFeedback, normalizeProviderCandidate, recordingKeyFor } from '../worker/domain.js';

test('music identity normalizes artist and title while keeping track versions distinct', () => {
  assert.equal(recordingKeyFor('  Blue—Monday! ', 'New Order'), recordingKeyFor('blue monday', 'NEW   ORDER'));
  assert.notEqual(recordingKeyFor('Blue Monday - slowed', 'New Order'), recordingKeyFor('Blue Monday', 'New Order'));
  assert.equal(recordingKeyFor('Blue Monday', 'Unknown artist'), null);
});

test('only verified, ungated full Audius tracks become background stream candidates', () => {
  const base = { provider:'audius', id: 'ABC_123-def', title: 'Blue Monday', user: {name: 'New Order'}, is_streamable: true, duration: 255, artwork: {'150x150':'https://api.audius.co/image.jpg'} };
  const item = normalizeProviderCandidate(base, '2026-09-16T00:00:00.000Z');
  assert.equal(item.provider, 'audius');
  assert.equal(item.provider_track_id, base.id);
  assert.equal(item.duration_seconds, 255);
  assert.match(item.audio_url, /^https:\/\/api\.audius\.co\/v1\/tracks\/ABC_123-def\/stream\?/);
  assert.deepEqual(item.seed_keys, []);
  assert.equal(normalizeProviderCandidate({...base, is_stream_gated: true}), null);
  assert.equal(normalizeProviderCandidate({...base, is_preview_only: true}), null);
  assert.equal(normalizeProviderCandidate({...base, is_streamable: false}), null);
  assert.equal(normalizeProviderCandidate({...base, id: 'bad&id=evil'}), null);
});

test('feedback contract is bounded, idempotent by event id, and excludes accidental playback stops', () => {
  assert.deepEqual(MUSIC_FEEDBACK_EVENTS, ['play_progress','completed','skipped','like','dislike']);
  const event = normalizeFeedback({event_id:'event-123456789012',track_key:'audius:ABC_123-def',provider:'audius',event:'play_progress',title:'Blue Monday',artist:'New Order',listened_seconds:99999,duration_seconds:255}, '2026-09-16T00:00:00.000Z');
  assert.equal(event.listened_seconds, 7200);
  assert.equal(event.recording_key, 'recording:new order:blue monday');
  assert.equal(event.created_at, '2026-09-16T00:00:00.000Z');
  for (const eventName of ['paused','buffering','failed','app_switched','force_stopped']) {
    assert.equal(normalizeFeedback({event_id:'event-abcdefghijkl',track_key:'audius:ABC_123-def',provider:'audius',event:eventName,title:'Blue Monday',artist:'New Order'}), null);
  }
  assert.equal(normalizeFeedback({event_id:'x',track_key:'a',provider:'other',event:'like',title:'Blue Monday',artist:'New Order'}), null);
});

test('migration adds provider data and served identity without altering existing songs or favorites', () => {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('../drizzle/0000_lively_gressill.sql', import.meta.url), 'utf8'));
  sqlite.exec(readFileSync(new URL('../drizzle/0001_durable_served_ledger.sql', import.meta.url), 'utf8'));
  sqlite.prepare('INSERT INTO music_library(track_key,payload) VALUES(?,?)').run('video:aaaaaaaaaaa', JSON.stringify({title:'Existing track'}));
  sqlite.prepare('INSERT INTO music_favorites(track_key,liked,updated_at) VALUES(?,?,?)').run('video:aaaaaaaaaaa',1,'2026-09-15T00:00:00.000Z');
  sqlite.prepare('INSERT INTO music_served(track_key,video_id,served_at) VALUES(?,?,?)').run('video:aaaaaaaaaaa','aaaaaaaaaaa','2026-09-15T00:00:00.000Z');
  sqlite.exec(readFileSync(new URL('../drizzle/0002_thick_praxagora.sql', import.meta.url), 'utf8').split('--> statement-breakpoint').join('\n'));
  sqlite.exec(readFileSync(new URL('../drizzle/0003_curly_xorn.sql', import.meta.url), 'utf8').split('--> statement-breakpoint').join('\n'));
  sqlite.exec(readFileSync(new URL('../drizzle/0004_conscious_boom_boom.sql', import.meta.url), 'utf8').split('--> statement-breakpoint').join('\n'));
  assert.equal(sqlite.prepare('SELECT payload FROM music_library WHERE track_key=?').get('video:aaaaaaaaaaa').payload, JSON.stringify({title:'Existing track'}));
  assert.equal(sqlite.prepare('SELECT liked FROM music_favorites WHERE track_key=?').get('video:aaaaaaaaaaa').liked, 1);
  assert.equal(sqlite.prepare('SELECT recording_key FROM music_served WHERE track_key=?').get('video:aaaaaaaaaaa').recording_key, null);
  sqlite.prepare('INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,audio_url,provider_url,discovered_at) VALUES(?,?,?,?,?,?,?,?,?)').run('audius:ABC_123-def','recording:new order:blue monday','audius','ABC_123-def','Blue Monday','New Order','https://api.audius.co/v1/tracks/ABC_123-def/stream','https://api.audius.co/v1/tracks/ABC_123-def','2026-09-16T00:00:00.000Z');
  assert.throws(() => sqlite.prepare('INSERT INTO music_candidates(candidate_key,recording_key,provider,provider_track_id,title,artist,audio_url,provider_url,discovered_at) VALUES(?,?,?,?,?,?,?,?,?)').run('audius:other','recording:new order:blue monday','audius','other','Blue Monday','New Order','https://api.audius.co/v1/tracks/other/stream','https://api.audius.co/v1/tracks/other','2026-09-16T00:00:00.000Z'));
  sqlite.close();
});
