-- SQLite schema for the local YouTube Music recommender.
--
-- This file is an idempotent version-one baseline.  Database.initialize()
-- executes it on every open, records version one in schema_migrations, and
-- keeps PRAGMA user_version in sync.  Future migrations should be added as
-- numbered, transactional steps in app/db.py rather than replacing this
-- baseline.

PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One row per canonical YouTube video.  track_key is retained for callers
-- that already have a local stable key; video_id remains the provider-neutral
-- identity used for history de-duplication.
CREATE TABLE IF NOT EXISTS canonical_tracks (
    track_id INTEGER PRIMARY KEY,
    track_key TEXT UNIQUE,
    video_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    artist TEXT,
    artists_json TEXT,
    album TEXT,
    url TEXT,
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    is_explicit INTEGER CHECK (is_explicit IS NULL OR is_explicit IN (0, 1)),
    thumbnail_url TEXT,
    canonical_url TEXT,
    is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    metadata_json TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    liked_count INTEGER NOT NULL DEFAULT 0 CHECK (liked_count >= 0),
    source TEXT NOT NULL DEFAULT 'youtube_music',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_canonical_tracks_track_key
    ON canonical_tracks(track_key);
CREATE INDEX IF NOT EXISTS idx_canonical_tracks_artist
    ON canonical_tracks(artist);
CREATE INDEX IF NOT EXISTS idx_canonical_tracks_last_seen
    ON canonical_tracks(last_seen_at DESC);

-- History is append-oriented.  dedupe_key is populated by Database helpers;
-- the partial indexes also protect callers that insert normalized rows
-- directly.  Nullable legacy columns keep imports without a provider event
-- identifier possible while the helper still validates required values.
CREATE TABLE IF NOT EXISTS history_events (
    history_event_id INTEGER PRIMARY KEY,
    event_id TEXT UNIQUE,
    history_id TEXT,
    source TEXT NOT NULL DEFAULT 'youtube_music',
    source_event_id TEXT,
    dedupe_key TEXT UNIQUE,
    track_id INTEGER REFERENCES canonical_tracks(track_id) ON DELETE SET NULL,
    track_key TEXT,
    video_id TEXT,
    played_at TEXT,
    played_seconds INTEGER CHECK (played_seconds IS NULL OR played_seconds >= 0),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    completed INTEGER CHECK (completed IS NULL OR completed IN (0, 1)),
    liked INTEGER NOT NULL DEFAULT 0 CHECK (liked IN (0, 1)),
    context TEXT,
    metadata_json TEXT,
    scan_id TEXT,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    imported_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_history_source_event
    ON history_events(source, source_event_id)
    WHERE source_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_history_natural_key
    ON history_events(source, video_id, played_at)
    WHERE video_id IS NOT NULL AND played_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_history_played_at
    ON history_events(played_at DESC, history_event_id DESC);
CREATE INDEX IF NOT EXISTS idx_history_track
    ON history_events(track_id, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_video
    ON history_events(video_id, played_at DESC);

-- Preferences are local application state.  Values are encoded by
-- Database.set_preference; no credentials, cookies, or raw connector headers
-- belong in this table.
CREATE TABLE IF NOT EXISTS user_preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'text',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_likes (
    like_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL DEFAULT 'default',
    track_id INTEGER REFERENCES canonical_tracks(track_id) ON DELETE CASCADE,
    track_key TEXT,
    video_id TEXT,
    liked INTEGER NOT NULL DEFAULT 1 CHECK (liked IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'local',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, track_id),
    UNIQUE(profile_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_user_likes_video
    ON user_likes(profile_id, video_id);

CREATE TABLE IF NOT EXISTS recommendation_runs (
    run_id TEXT PRIMARY KEY,
    source_run_id TEXT,
    source TEXT NOT NULL DEFAULT 'local',
    algorithm TEXT,
    model TEXT,
    model_version TEXT,
    parameters_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT,
    error_message TEXT,
    input_count INTEGER NOT NULL DEFAULT 0 CHECK (input_count >= 0),
    item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    recommendation_count INTEGER NOT NULL DEFAULT 0 CHECK (recommendation_count >= 0),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_items (
    recommendation_item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recommendation_runs(run_id) ON DELETE CASCADE,
    track_id INTEGER REFERENCES canonical_tracks(track_id) ON DELETE SET NULL,
    track_key TEXT,
    video_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 0),
    score REAL,
    confidence REAL,
    reason TEXT,
    reasons_json TEXT,
    reason_codes_json TEXT,
    based_on_history_ids_json TEXT,
    source TEXT NOT NULL DEFAULT 'local',
    generated_at TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_recommendation_items_run
    ON recommendation_items(run_id, rank);
CREATE INDEX IF NOT EXISTS idx_recommendation_items_video
    ON recommendation_items(video_id);

CREATE TRIGGER IF NOT EXISTS trg_recommendation_items_insert
AFTER INSERT ON recommendation_items
BEGIN
    UPDATE recommendation_runs
    SET item_count = item_count + 1,
        recommendation_count = recommendation_count + 1,
        updated_at = CURRENT_TIMESTAMP
    WHERE run_id = NEW.run_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_recommendation_items_delete
AFTER DELETE ON recommendation_items
BEGIN
    UPDATE recommendation_runs
    SET item_count = CASE WHEN item_count > 0 THEN item_count - 1 ELSE 0 END,
        recommendation_count = CASE WHEN recommendation_count > 0 THEN recommendation_count - 1 ELSE 0 END,
        updated_at = CURRENT_TIMESTAMP
    WHERE run_id = OLD.run_id;
END;

CREATE TABLE IF NOT EXISTS playlists (
    playlist_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    name TEXT,
    description TEXT,
    source TEXT NOT NULL DEFAULT 'local',
    remote_id TEXT,
    remote_url TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    write_mode TEXT NOT NULL DEFAULT 'dry_run',
    dry_run INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    is_managed INTEGER NOT NULL DEFAULT 0 CHECK (is_managed IN (0, 1)),
    provider_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (provider_confirmed IN (0, 1)),
    requested_count INTEGER NOT NULL DEFAULT 0 CHECK (requested_count >= 0),
    confirmed_count INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0),
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_remote
    ON playlists(source, remote_id)
    WHERE remote_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_item_id INTEGER PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    track_id INTEGER REFERENCES canonical_tracks(track_id) ON DELETE SET NULL,
    track_key TEXT,
    video_id TEXT NOT NULL,
    remote_item_id TEXT,
    position INTEGER,
    rank INTEGER,
    source TEXT NOT NULL DEFAULT 'local',
    metadata_json TEXT,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(playlist_id, position)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_items_remote
    ON playlist_items(playlist_id, remote_item_id)
    WHERE remote_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_items_track
    ON playlist_items(playlist_id, track_key)
    WHERE track_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_playlist_items_order
    ON playlist_items(playlist_id, COALESCE(position, rank), playlist_item_id);

-- Connector runs record observed connector activity, not credentials.  The
-- confirmation flags are deliberately false by default; a requested write
-- or configured connector must never be represented as provider success.
CREATE TABLE IF NOT EXISTS connector_runs (
    connector_run_id TEXT PRIMARY KEY,
    connector_id TEXT,
    connector_name TEXT NOT NULL,
    operation TEXT NOT NULL,
    mode TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    dry_run INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    authenticated INTEGER NOT NULL DEFAULT 0 CHECK (authenticated IN (0, 1)),
    account_access_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (account_access_confirmed IN (0, 1)),
    history_access_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (history_access_confirmed IN (0, 1)),
    playlist_write_access_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (playlist_write_access_confirmed IN (0, 1)),
    write_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (write_confirmed IN (0, 1)),
    records_seen INTEGER NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
    records_written INTEGER NOT NULL DEFAULT 0 CHECK (records_written >= 0),
    error_code TEXT,
    message TEXT,
    metadata_json TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_connector_runs_status
    ON connector_runs(connector_name, started_at DESC);

-- Compatibility tables for the first local app surface.  The normalized
-- tables above are authoritative; Database's compatibility methods update
-- both representations when a legacy caller uses them.
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'youtube_music',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    items_seen INTEGER NOT NULL DEFAULT 0,
    records_scanned INTEGER NOT NULL DEFAULT 0,
    records_emitted INTEGER NOT NULL DEFAULT 0,
    records_skipped INTEGER NOT NULL DEFAULT 0,
    duplicate_records INTEGER NOT NULL DEFAULT 0,
    pages_scanned INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
    recommendations_created INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    errors_json TEXT
);

CREATE TABLE IF NOT EXISTS playlist_plans (
    playlist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'local',
    provider_playlist_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    dry_run INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    requested_count INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_started
    ON scan_runs(started_at DESC);

INSERT OR IGNORE INTO schema_migrations(version, description)
VALUES (1, 'initial normalized SQLite schema');
