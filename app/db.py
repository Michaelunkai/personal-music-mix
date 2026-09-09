"""Private SQLite persistence for the local YouTube Music recommender.

The helper in this module deliberately owns only local state.  It records
what a connector observed and what a caller requested; it never treats a
configured connector, a dry-run, or an unverified write as account access or
provider success.  SQL is kept parameterized at every value boundary and the
schema is initialized from the adjacent, idempotent ``schema.sql`` baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
"""Latest schema version understood by :class:`Database`."""


def utc_now_iso() -> str:
    """Return a compact, timezone-aware UTC timestamp for persisted values."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _value(source: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a mapping or a dataclass-like object."""

    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _enum_value(value: Any, default: Any = None) -> Any:
    """Use an Enum's wire value without importing the application's contracts."""

    if value is None:
        return default
    return getattr(value, "value", value)


def _iso(value: Any, *, default_now: bool = False) -> str | None:
    if value is None:
        return utc_now_iso() if default_now else None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or (utc_now_iso() if default_now else None)


def _json_text(value: Any) -> str | None:
    """Serialize JSON-compatible metadata while accepting pre-encoded text."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: str | None, *, value_type: str | None = None) -> Any:
    if value is None:
        return None
    if value_type in {"json", "bool", "int", "float", "null"}:
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "liked", "like"}


def _nonnegative(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if converted < 0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _limit(value: int, *, default: int = 100, maximum: int = 10_000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if result < 1 or result > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return result or default


class Database:
    """Thread-safe SQLite facade with idempotent local persistence helpers.

    A connection stays open for the lifetime of the helper so ``:memory:``
    databases behave as expected in tests and short-lived jobs.  Callers can
    use ``with db.transaction() as connection`` for atomic work or the
    higher-level methods below.  Nested transactions use savepoints.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] = ":memory:",
        *,
        busy_timeout_ms: int = 15_000,
        timeout: float | None = None,
        initialize: bool = True,
    ) -> None:
        if int(busy_timeout_ms) < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.timeout = float(timeout if timeout is not None else self.busy_timeout_ms / 1000 or 0.001)
        raw_path = os.fspath(path)
        self._uri = isinstance(raw_path, str) and raw_path.startswith("file:")
        self.path: Path | str = raw_path if self._uri or raw_path == ":memory:" else Path(raw_path).expanduser()
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._savepoint_counter = 0
        self._closed = False
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
            uri=self._uri,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        if initialize:
            self.initialize()

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        # WAL is unavailable for an in-memory database; SQLite simply keeps
        # its memory journal there.  File-backed databases get the requested
        # concurrent-reader behavior.
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("database is closed")

    @property
    def connection_handle(self) -> sqlite3.Connection:
        """Expose the configured connection for narrowly scoped integrations."""

        self._ensure_open()
        return self._connection

    def initialize(self) -> None:
        """Apply the idempotent schema baseline and record its migration.

        The version check happens before executing the script, so a newer
        database is never silently downgraded by an older application binary.
        The schema script itself uses ``IF NOT EXISTS`` and is safe to run on
        every startup.
        """

        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA user_version").fetchone()
            current_version = int(row[0]) if row else 0
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current_version} is newer than supported version {SCHEMA_VERSION}"
                )
            schema_path = Path(__file__).with_name("schema.sql")
            schema = schema_path.read_text(encoding="utf-8")
            self._connection.executescript(schema)
            with self.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, description) VALUES (?, ?)",
                    (SCHEMA_VERSION, "initial normalized SQLite schema"),
                )
                connection.execute(
                    """INSERT INTO app_metadata(key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                    ("schema_version", str(SCHEMA_VERSION), utc_now_iso()),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction, with nested savepoints."""

        self._ensure_open()
        self._lock.acquire()
        nested = self._transaction_depth > 0
        savepoint: str | None = None
        started = False
        self._transaction_depth += 1
        try:
            if nested:
                self._savepoint_counter += 1
                savepoint = f"db_sp_{self._savepoint_counter}"
                self._connection.execute(f"SAVEPOINT {savepoint}")
            else:
                self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            started = True
            yield self._connection
        except BaseException:
            if started:
                if savepoint is not None:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    self._connection.rollback()
            raise
        else:
            if savepoint is not None:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self._connection.commit()
        finally:
            self._transaction_depth -= 1
            self._lock.release()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Compatibility context manager yielding the configured connection."""

        with self.transaction() as connection:
            yield connection

    def execute(self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] = ()) -> sqlite3.Cursor:
        """Execute trusted SQL with bound parameters.

        This low-level escape hatch is intentionally small; application code
        should prefer the fixed query helpers below.  SQL text is never built
        from user values by this module.
        """

        with self._lock:
            self._ensure_open()
            return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            self._ensure_open()
            return self._connection.executemany(sql, parameters)

    def query_one(self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            self._ensure_open()
            return self._connection.execute(sql, parameters).fetchone()

    def query_all(self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            return self._connection.execute(sql, parameters).fetchall()

    def scalar(self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] = ()) -> Any:
        row = self.query_one(sql, parameters)
        return row[0] if row is not None else None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._transaction_depth:
                self._connection.rollback()
                self._transaction_depth = 0
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "Database":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _stable_key(*parts: Any) -> str:
        material = "\x1f".join("" if part is None else str(part) for part in parts)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_video_id(video_id: Any, track_key: Any, title: Any, artist: Any) -> str:
        value = str(video_id or "").strip()
        if value:
            return value
        fallback = str(track_key or "").strip() or f"{title or ''}\x1f{artist or ''}"
        return "synthetic:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:32]

    def _upsert_track_connection(
        self,
        connection: sqlite3.Connection,
        video_id: str,
        title: str | None = None,
        *,
        track_key: str | None = None,
        artist: str | None = None,
        artists: Sequence[str] | str | None = None,
        album: str | None = None,
        url: str | None = None,
        duration_seconds: Any = None,
        is_explicit: Any = None,
        thumbnail_url: str | None = None,
        canonical_url: str | None = None,
        is_available: Any = None,
        source: str = "youtube_music",
        metadata: Any = None,
    ) -> int:
        video_id = str(video_id or "").strip()
        if not video_id:
            raise ValueError("video_id must not be empty")
        clean_key = str(track_key).strip() if track_key is not None and str(track_key).strip() else None
        clean_title = str(title).strip() if title is not None and str(title).strip() else None
        clean_artist = str(artist).strip() if artist is not None and str(artist).strip() else None
        artists_json = _json_text(artists) if artists is not None else None
        if clean_artist is None and artists is not None:
            if isinstance(artists, str):
                clean_artist = artists.strip() or None
            else:
                clean_artist = ", ".join(str(item).strip() for item in artists if str(item).strip()) or None
        clean_duration = _nonnegative(duration_seconds, "duration_seconds")
        clean_explicit = _as_bool(is_explicit)
        clean_available = _as_bool(is_available)
        now = utc_now_iso()
        existing = connection.execute(
            """SELECT track_id FROM canonical_tracks
               WHERE video_id = ? OR (track_key = ? AND track_key IS NOT NULL)
               ORDER BY CASE WHEN video_id = ? THEN 0 ELSE 1 END, track_id
               LIMIT 1""",
            (video_id, clean_key, video_id),
        ).fetchone()
        if existing is not None:
            updates: dict[str, Any] = {"last_seen_at": now, "updated_at": now}
            if clean_key is not None:
                updates["track_key"] = clean_key
            if clean_title is not None:
                updates["title"] = clean_title
            if clean_artist is not None:
                updates["artist"] = clean_artist
            if artists_json is not None:
                updates["artists_json"] = artists_json
            for column, value in (
                ("album", album),
                ("url", url),
                ("duration_seconds", clean_duration),
                ("is_explicit", None if clean_explicit is None else int(clean_explicit)),
                ("thumbnail_url", thumbnail_url),
                ("canonical_url", canonical_url),
                ("is_available", None if clean_available is None else int(clean_available)),
                ("metadata_json", _json_text(metadata)),
            ):
                if value is not None:
                    updates[column] = value
            if source:
                updates["source"] = str(source)
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = list(updates.values()) + [int(existing["track_id"])]
            connection.execute(f"UPDATE canonical_tracks SET {assignments} WHERE track_id = ?", values)
            return int(existing["track_id"])

        if clean_title is None:
            clean_title = video_id
        columns = [
            "track_key",
            "video_id",
            "title",
            "artist",
            "artists_json",
            "album",
            "url",
            "duration_seconds",
            "is_explicit",
            "thumbnail_url",
            "canonical_url",
            "is_available",
            "metadata_json",
            "first_seen_at",
            "last_seen_at",
            "source",
            "created_at",
            "updated_at",
        ]
        values = [
            clean_key,
            video_id,
            clean_title,
            clean_artist,
            artists_json,
            album,
            url,
            clean_duration,
            None if clean_explicit is None else int(clean_explicit),
            thumbnail_url,
            canonical_url,
            1 if clean_available is None else int(clean_available),
            _json_text(metadata),
            now,
            now,
            str(source or "youtube_music"),
            now,
            now,
        ]
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO canonical_tracks ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        row = connection.execute("SELECT track_id FROM canonical_tracks WHERE video_id = ?", (video_id,)).fetchone()
        if row is None:  # pragma: no cover - protected by the UNIQUE constraint
            raise RuntimeError("canonical track insert did not produce a row")
        return int(row["track_id"])

    def upsert_track(
        self,
        video_id: Any,
        title: str | None = None,
        *,
        track_key: str | None = None,
        artist: str | None = None,
        artists: Sequence[str] | str | None = None,
        album: str | None = None,
        url: str | None = None,
        duration_seconds: Any = None,
        is_explicit: Any = None,
        thumbnail_url: str | None = None,
        canonical_url: str | None = None,
        is_available: Any = None,
        source: str = "youtube_music",
        metadata: Any = None,
    ) -> int:
        """Insert or update a canonical track and return its local id."""

        # HistoryService and small connector adapters commonly pass a
        # Track-like object as the first positional argument.  Accepting it
        # here keeps the persistence boundary compatible without importing a
        # particular contracts module.
        if not isinstance(video_id, (str, bytes, os.PathLike)):
            fields = self._track_fields(video_id)
            video_id = fields["video_id"]
            title = title if title is not None else fields["title"]
            track_key = track_key if track_key is not None else fields["track_key"]
            artist = artist if artist is not None else fields["artist"]
            artists = artists if artists is not None else fields["artists"]
            album = album if album is not None else fields["album"]
            url = url if url is not None else fields["url"]
            duration_seconds = duration_seconds if duration_seconds is not None else fields["duration_seconds"]
            is_explicit = is_explicit if is_explicit is not None else fields["is_explicit"]
            thumbnail_url = thumbnail_url if thumbnail_url is not None else fields["thumbnail_url"]
            canonical_url = canonical_url if canonical_url is not None else fields["canonical_url"]
            metadata = metadata if metadata is not None else fields["metadata"]
            source = source if source != "youtube_music" else str(fields["source"] or source)

        with self.transaction(immediate=True) as connection:
            return self._upsert_track_connection(
                connection,
                video_id,
                title,
                track_key=track_key,
                artist=artist,
                artists=artists,
                album=album,
                url=url,
                duration_seconds=duration_seconds,
                is_explicit=is_explicit,
                thumbnail_url=thumbnail_url,
                canonical_url=canonical_url,
                is_available=is_available,
                source=source,
                metadata=metadata,
            )

    get_or_create_track = upsert_track
    save_track = upsert_track

    def get_track(self, video_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM canonical_tracks WHERE video_id = ?", (str(video_id),))

    def get_track_by_id(self, track_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM canonical_tracks WHERE track_id = ?", (int(track_id),))

    def _track_fields(self, event: Any) -> dict[str, Any]:
        track = _value(event, "track", event)
        track_key = _value(track, "track_key")
        video_id = _value(track, "video_id", _value(track, "videoId"))
        title = _value(track, "title", _value(track, "name"))
        artists = _value(track, "artists")
        artist = _value(track, "artist")
        if artist is None and artists is not None:
            artist = artists if isinstance(artists, str) else ", ".join(str(item) for item in artists)
        if artists is None and artist is not None:
            artists = [str(artist)]
        metadata = _value(track, "metadata")
        if metadata is None:
            metadata = _value(track, "raw")
        video_id = self._canonical_video_id(video_id, track_key, title, artist)
        return {
            "track_key": str(track_key).strip() if track_key else None,
            "video_id": video_id,
            "title": str(title).strip() if title else video_id,
            "artist": str(artist).strip() if artist else None,
            "artists": artists,
            "album": _value(track, "album"),
            "url": _value(track, "url", _value(track, "video_url")),
            "duration_seconds": _value(track, "duration_seconds", _value(track, "durationSeconds")),
            "is_explicit": _value(track, "is_explicit"),
            "thumbnail_url": _value(track, "thumbnail_url"),
            "canonical_url": _value(track, "canonical_url"),
            "metadata": metadata,
            "source": _value(event, "source", _value(track, "source", "youtube_music")),
        }

    def _normalized_history(self, event: Any) -> dict[str, Any]:
        track = self._track_fields(event)
        played_at = _value(event, "played_at", _value(track, "played_at"))
        if played_at is None:
            played_at = _value(_value(event, "track", event), "played_at")
        event_id = _value(event, "event_id") or _value(event, "history_id")
        source_event_id = _value(event, "source_event_id") or _value(event, "source_record_id")
        liked = _as_bool(_value(event, "liked", _value(_value(event, "track", event), "liked")), False)
        metadata = _value(event, "metadata")
        if metadata is None:
            metadata = track["metadata"]
        return {
            **track,
            "history_id": _value(event, "history_id"),
            "event_id": str(event_id).strip() if event_id else None,
            "source_event_id": str(source_event_id).strip() if source_event_id else None,
            "played_at": _iso(played_at),
            "played_seconds": _value(event, "played_seconds"),
            "completed": _value(event, "completed"),
            "liked": bool(liked),
            "context": _value(event, "context"),
            "metadata": metadata,
            "scan_id": _value(event, "scan_id") or _value(event, "run_id"),
            "captured_at": _iso(_value(event, "captured_at"), default_now=True),
            "imported_at": _iso(_value(event, "imported_at")),
        }

    def _set_like_connection(
        self,
        connection: sqlite3.Connection,
        *,
        track_id: int,
        video_id: Any,
        liked: bool,
        source: str,
        profile_id: str = "default",
        track_key: str | None = None,
    ) -> None:
        row = connection.execute(
            """SELECT like_id, liked, updated_at FROM user_likes
               WHERE profile_id = ? AND (track_id = ? OR (track_id IS NULL AND video_id = ?))
               ORDER BY like_id LIMIT 1""",
            (profile_id, track_id, video_id),
        ).fetchone()
        new_value = int(bool(liked))
        updated_at = utc_now_iso()
        if row and row['updated_at']:
            previous_time = datetime.fromisoformat(str(row['updated_at']).replace('Z','+00:00'))
            if previous_time.tzinfo and previous_time >= datetime.fromisoformat(updated_at.replace('Z','+00:00')):
                updated_at = (previous_time + timedelta(milliseconds=1)).isoformat().replace('+00:00','Z')
        if row is None:
            connection.execute(
                """INSERT INTO user_likes(profile_id, track_id, track_key, video_id, liked, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, track_id, track_key, video_id, new_value, source or "local", updated_at),
            )
            delta = new_value
        else:
            old_value = int(row["liked"])
            connection.execute(
                """UPDATE user_likes SET track_id = ?, track_key = COALESCE(?, track_key),
                   video_id = COALESCE(?, video_id), liked = ?, source = ?, updated_at = ?
                   WHERE like_id = ?""",
                (track_id, track_key, video_id, new_value, source or "local", updated_at, int(row["like_id"])),
            )
            delta = new_value - old_value
        if delta:
            connection.execute(
                """UPDATE canonical_tracks
                   SET liked_count = CASE WHEN liked_count + ? < 0 THEN 0 ELSE liked_count + ? END,
                       updated_at = ? WHERE track_id = ?""",
                (delta, delta, utc_now_iso(), track_id),
            )

    def _record_history_connection(
        self, connection: sqlite3.Connection, normalized: Mapping[str, Any]
    ) -> tuple[int, bool]:
        track_id = normalized.get("track_id")
        if track_id is None:
            track_id = self._upsert_track_connection(
                connection,
                str(normalized["video_id"]),
                normalized.get("title"),
                track_key=normalized.get("track_key"),
                artist=normalized.get("artist"),
                artists=normalized.get("artists"),
                album=normalized.get("album"),
                url=normalized.get("url"),
                duration_seconds=normalized.get("duration_seconds"),
                is_explicit=normalized.get("is_explicit"),
                thumbnail_url=normalized.get("thumbnail_url"),
                canonical_url=normalized.get("canonical_url"),
                source=str(normalized.get("source") or "youtube_music"),
                metadata=normalized.get("metadata"),
            )
        else:
            track_id = int(track_id)

        source = str(normalized.get("source") or "youtube_music")
        video_id = str(normalized["video_id"])
        played_at = normalized.get("played_at")
        source_event_id = normalized.get("source_event_id")
        event_id = normalized.get("event_id")
        history_id = normalized.get("history_id")
        identity = source_event_id or event_id or history_id or self._stable_key(source, video_id, played_at)
        dedupe_key = self._stable_key(source, identity)
        existing: sqlite3.Row | None = connection.execute(
            "SELECT history_event_id FROM history_events WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)
        ).fetchone()
        if existing is None and source_event_id:
            existing = connection.execute(
                """SELECT history_event_id FROM history_events
                   WHERE source = ? AND source_event_id = ? LIMIT 1""",
                (source, source_event_id),
            ).fetchone()
        if existing is None and event_id:
            existing = connection.execute(
                "SELECT history_event_id FROM history_events WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone()
        if existing is None and played_at is not None:
            existing = connection.execute(
                """SELECT history_event_id FROM history_events
                   WHERE source = ? AND video_id = ? AND played_at = ? LIMIT 1""",
                (source, video_id, played_at),
            ).fetchone()

        now = utc_now_iso()
        if existing is not None:
            history_event_id = int(existing["history_event_id"])
            connection.execute(
                """UPDATE history_events SET track_id = COALESCE(?, track_id), track_key = COALESCE(?, track_key),
                   video_id = COALESCE(?, video_id), source_event_id = COALESCE(?, source_event_id),
                   played_seconds = COALESCE(?, played_seconds), duration_seconds = COALESCE(?, duration_seconds),
                   completed = COALESCE(?, completed), liked = CASE WHEN ? = 1 THEN 1 ELSE liked END,
                   context = COALESCE(?, context), metadata_json = COALESCE(?, metadata_json),
                   scan_id = COALESCE(?, scan_id), imported_at = COALESCE(?, imported_at), updated_at = ?
                   WHERE history_event_id = ?""",
                (
                    track_id,
                    normalized.get("track_key"),
                    video_id,
                    source_event_id,
                    _nonnegative(normalized.get("played_seconds"), "played_seconds"),
                    _nonnegative(normalized.get("duration_seconds"), "duration_seconds"),
                    None if normalized.get("completed") is None else int(bool(normalized.get("completed"))),
                    int(bool(normalized.get("liked"))),
                    normalized.get("context"),
                    _json_text(normalized.get("metadata")),
                    normalized.get("scan_id"),
                    normalized.get("imported_at"),
                    now,
                    history_event_id,
                ),
            )
            inserted = False
        else:
            cursor = connection.execute(
                """INSERT INTO history_events(
                   event_id, history_id, source, source_event_id, dedupe_key, track_id, track_key, video_id,
                   played_at, played_seconds, duration_seconds, completed, liked, context, metadata_json,
                   scan_id, captured_at, imported_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    history_id,
                    source,
                    source_event_id,
                    dedupe_key,
                    track_id,
                    normalized.get("track_key"),
                    video_id,
                    played_at,
                    _nonnegative(normalized.get("played_seconds"), "played_seconds"),
                    _nonnegative(normalized.get("duration_seconds"), "duration_seconds"),
                    None if normalized.get("completed") is None else int(bool(normalized.get("completed"))),
                    int(bool(normalized.get("liked"))),
                    normalized.get("context"),
                    _json_text(normalized.get("metadata")),
                    normalized.get("scan_id"),
                    normalized.get("captured_at") or now,
                    normalized.get("imported_at"),
                    now,
                    now,
                ),
            )
            history_event_id = int(cursor.lastrowid)
            inserted = True

        if bool(normalized.get("liked")):
            self._set_like_connection(
                connection,
                track_id=int(track_id),
                video_id=video_id,
                liked=True,
                source=source,
                track_key=normalized.get("track_key"),
            )
        return history_event_id, inserted

    def record_history_event(
        self,
        video_id: str,
        played_at: Any = None,
        *,
        source: str = "youtube_music",
        source_event_id: str | None = None,
        history_id: str | None = None,
        event_id: str | None = None,
        track_id: int | None = None,
        track_key: str | None = None,
        title: str | None = None,
        artist: str | None = None,
        artists: Sequence[str] | str | None = None,
        album: str | None = None,
        duration_seconds: Any = None,
        played_seconds: Any = None,
        completed: Any = None,
        liked: Any = False,
        context: str | None = None,
        metadata: Any = None,
        scan_id: str | None = None,
        captured_at: Any = None,
        imported_at: Any = None,
    ) -> int:
        """Insert one history observation idempotently and return its row id."""

        # Accept a normalized HistoryEvent/record directly.  This is the
        # shape used by the current HistoryService adapter and avoids turning
        # a dataclass repr into a synthetic video id.
        if not isinstance(video_id, (str, bytes, os.PathLike)):
            normalized = self._normalized_history(video_id)
            if played_at is not None:
                normalized["played_at"] = _iso(played_at)
            with self.transaction(immediate=True) as connection:
                history_event_id, _ = self._record_history_connection(connection, normalized)
                return history_event_id

        canonical_video_id = self._canonical_video_id(video_id, track_key, title, artist)
        normalized = {
            "video_id": canonical_video_id,
            "played_at": _iso(played_at),
            "source": source,
            "source_event_id": source_event_id,
            "history_id": history_id,
            "event_id": event_id,
            "track_id": track_id,
            "track_key": track_key,
            "title": title or canonical_video_id,
            "artist": artist,
            "artists": artists,
            "album": album,
            "duration_seconds": duration_seconds,
            "played_seconds": played_seconds,
            "completed": completed,
            "liked": bool(_as_bool(liked, False)),
            "context": context,
            "metadata": metadata,
            "scan_id": scan_id,
            "captured_at": _iso(captured_at, default_now=True),
            "imported_at": _iso(imported_at),
        }
        with self.transaction(immediate=True) as connection:
            history_event_id, _ = self._record_history_connection(connection, normalized)
            return history_event_id

    upsert_history_event = record_history_event
    save_history_event = record_history_event

    def ingest_events(self, events: Sequence[Any] | Iterable[Any]) -> int:
        """Normalize and ingest event objects, returning newly inserted count."""

        inserted = 0
        with self.transaction(immediate=True) as connection:
            for event in events:
                normalized = self._normalized_history(event)
                _, was_inserted = self._record_history_connection(connection, normalized)
                inserted += int(was_inserted)
        return inserted

    save_history_events = ingest_events
    upsert_history_events = ingest_events

    def get_history(
        self,
        *,
        limit: int = 100,
        before: Any = None,
        after: Any = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return newest-first history rows as ordinary dictionaries."""

        clauses = ["1 = 1"]
        parameters: list[Any] = []
        if before is not None:
            clauses.append("h.played_at < ?")
            parameters.append(_iso(before))
        if after is not None:
            clauses.append("h.played_at >= ?")
            parameters.append(_iso(after))
        if source is not None:
            clauses.append("h.source = ?")
            parameters.append(source)
        parameters.append(_limit(limit))
        rows = self.query_all(
            f"""SELECT h.*, c.title, c.artist, c.artists_json, c.album, c.url, c.duration_seconds AS track_duration_seconds,
                       c.thumbnail_url, c.canonical_url
                FROM history_events h LEFT JOIN canonical_tracks c ON c.track_id = h.track_id
                WHERE {' AND '.join(clauses)}
                ORDER BY h.played_at DESC, h.history_event_id DESC LIMIT ?""",
            parameters,
        )
        return [dict(row) for row in rows]

    list_history = get_history
    recent_history = get_history
    iter_history_events = get_history
    list_history_events = get_history
    get_history_events = get_history

    def list_track_stats(self, *, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.query_all(
            """SELECT c.track_id, COALESCE(c.track_key, 'video:' || c.video_id) AS track_key,
                      c.title, COALESCE(c.artist, 'Unknown artist') AS artist, c.artists_json, c.album,
                      c.url, c.video_id, c.first_seen_at, c.last_seen_at, c.liked_count, c.source, c.updated_at,
                      COUNT(h.history_event_id) AS play_count, MAX(h.played_at) AS latest_played_at,
                      COALESCE(
                          (SELECT MIN(CAST(json_extract(hp.metadata_json, '$.history_position') AS INTEGER))
                           FROM history_events hp
                           WHERE hp.track_id = c.track_id AND hp.source = 'youtube-music-extension'
                             AND json_extract(hp.metadata_json, '$.history_position') IS NOT NULL),
                          (SELECT MIN(ho.history_event_id)
                           FROM history_events ho
                           WHERE ho.track_id = c.track_id AND ho.source = 'youtube-music-extension')
                      ) AS history_position,
                      CASE WHEN EXISTS(SELECT 1 FROM user_likes u WHERE u.track_id=c.track_id AND u.profile_id!='dashboard')
                           THEN COALESCE((SELECT SUM(u.liked) FROM user_likes u WHERE u.track_id=c.track_id AND u.profile_id!='dashboard'),0)
                           ELSE COALESCE(SUM(CASE WHEN h.liked = 1 THEN 1 ELSE 0 END),0) END AS like_events,
                      COALESCE((SELECT SUM(u.liked) FROM user_likes u WHERE u.track_id=c.track_id AND u.profile_id!='dashboard'),0) AS provider_liked_count,
                      EXISTS(SELECT 1 FROM user_likes u WHERE u.track_id = c.track_id
                             AND u.profile_id = 'dashboard' AND u.liked = 1) AS local_favorite,
                      (SELECT u.updated_at FROM user_likes u WHERE u.track_id = c.track_id
                             AND u.profile_id = 'dashboard' ORDER BY u.updated_at DESC LIMIT 1) AS local_favorite_updated_at
               FROM canonical_tracks c LEFT JOIN history_events h ON h.track_id = c.track_id
               GROUP BY c.track_id
               ORDER BY play_count DESC, latest_played_at DESC, c.title COLLATE NOCASE
               LIMIT ?""",
            (_limit(limit, maximum=50_000),),
        )
        result = [dict(row) for row in rows]
        cache = json.loads(self.get_metadata('favorite_discovery_cache') or '{}')
        relationships: dict[str,list[dict]] = {}
        moment = datetime.now(timezone.utc).timestamp()
        for seed_key, entry in cache.items():
            if entry.get('expires_at',0) <= moment:
                continue
            for track_key in entry.get('track_keys',[]):
                relationships.setdefault(track_key,[]).append({
                    'track_key':seed_key,
                    'title':entry.get('title','a song you enjoy'),
                    'expires_at':entry.get('expires_at',0),
                    'seed_kind':entry.get('seed_kind','favorite'),
                    'play_count':entry.get('play_count',0),
                    'liked':bool(entry.get('liked', entry.get('seed_kind') == 'favorite')),
                    'history_position':entry.get('history_position'),
                })
        for row in result:
            row['discovery_seeds'] = relationships.get(row['track_key'],[])
        return result

    def recommendation_exclusion_keys(self) -> set[str]:
        """Return songs already shown by a completed recommendation run.

        Recommendation runs are append-only evidence of what the user was
        served. Keeping this query in SQLite makes the no-repeat rule survive
        restarts and does not depend on an in-memory worker cache.
        """

        rows = self.query_all(
            """SELECT DISTINCT i.track_key
               FROM recommendation_items i
               JOIN recommendation_runs r ON r.run_id = i.run_id
               WHERE r.status = 'completed' AND i.track_key IS NOT NULL"""
        )
        keys = {str(row['track_key']) for row in rows if row['track_key']}
        # The hosted site has its own durable served-song ledger.  Pulling
        # that ledger into the local exclusion set keeps discovery from
        # replenishing candidates that the hosted dashboard has already
        # shown, even when the library fingerprint itself did not change.
        try:
            remote = json.loads(self.get_metadata("cloud_served_keys") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            remote = []
        if isinstance(remote, list):
            keys.update(str(value) for value in remote if isinstance(value, str) and value)
        return keys

    def merge_cloud_served_keys(self, values: Any) -> int:
        """Persist hosted served-song keys without replacing local evidence."""

        incoming = {
            str(value).strip()
            for value in (values if isinstance(values, (list, tuple, set)) else [])
            if isinstance(value, str) and str(value).strip()
        }
        if not incoming:
            return 0
        try:
            current = json.loads(self.get_metadata("cloud_served_keys") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            current = []
        existing = {
            str(value).strip()
            for value in (current if isinstance(current, list) else [])
            if isinstance(value, str) and str(value).strip()
        }
        merged = existing | incoming
        if merged != existing:
            self.set_metadata("cloud_served_keys", json.dumps(sorted(merged), separators=(",", ":")))
        return len(merged - existing)

    def overview(self) -> dict[str, Any]:
        stats = self.list_track_stats(limit=50_000)
        artists: dict[str, int] = {}
        for row in stats:
            artist = str(row.get("artist") or "Unknown artist")
            artists[artist] = artists.get(artist, 0) + int(row.get("play_count") or 0)
        top_artists = sorted(artists.items(), key=lambda pair: (-pair[1], pair[0].casefold()))[:10]
        return {
            "track_count": len(stats),
            "play_count": sum(int(row.get("play_count") or 0) for row in stats),
            "liked_track_count": sum(1 for row in stats if int(row.get("liked_count") or 0) > 0),
            "local_favorite_count": sum(1 for row in stats if row.get("local_favorite")),
            "provider_liked_track_count": int(self.scalar("SELECT COUNT(DISTINCT track_id) FROM user_likes WHERE profile_id != 'dashboard' AND liked = 1") or 0),
            "top_tracks": stats[:10],
            "top_artists": [{"artist": name, "plays": count} for name, count in top_artists],
        }

    def health(self) -> dict[str, Any]:
        self._ensure_open()
        track_count = int(self.scalar("SELECT COUNT(*) FROM canonical_tracks") or 0)
        event_count = int(self.scalar("SELECT COUNT(*) FROM history_events") or 0)
        return {"ok": True, "path": str(self.path), "tracks": track_count, "history_events": event_count}

    def set_preference(self, key: str, value: Any) -> None:
        key = str(key).strip()
        if not key:
            raise ValueError("preference key must not be empty")
        if isinstance(value, str):
            encoded, value_type = value, "text"
        elif value is None:
            encoded, value_type = "null", "null"
        elif isinstance(value, bool):
            encoded, value_type = json.dumps(value), "bool"
        elif isinstance(value, int):
            encoded, value_type = json.dumps(value), "int"
        elif isinstance(value, float):
            encoded, value_type = json.dumps(value), "float"
        else:
            encoded, value_type = _json_text(value) or "null", "json"
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO user_preferences(key, value, value_type, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, value_type=excluded.value_type,
                   updated_at=excluded.updated_at""",
                (key, encoded, value_type, utc_now_iso()),
            )

    upsert_preference = set_preference

    def get_preference(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value, value_type FROM user_preferences WHERE key = ?", (str(key),))
        return default if row is None else _json_value(row["value"], value_type=row["value_type"])

    def get_preferences(self) -> dict[str, Any]:
        rows = self.query_all("SELECT key, value, value_type FROM user_preferences ORDER BY key")
        return {str(row["key"]): _json_value(row["value"], value_type=row["value_type"]) for row in rows}

    def set_like(
        self,
        track: int | str | None = None,
        liked: bool = True,
        *,
        track_id: int | None = None,
        video_id: str | None = None,
        track_key: str | None = None,
        title: str | None = None,
        source: str = "local",
        profile_id: str = "default",
    ) -> int:
        """Persist a local like without implying provider-side state."""

        if track_id is None and video_id is None:
            if isinstance(track, int):
                track_id = track
            elif track is not None:
                video_id = str(track)
        with self.transaction(immediate=True) as connection:
            if track_id is None:
                video_id = self._canonical_video_id(video_id, track_key, title, None)
                track_id = self._upsert_track_connection(
                    connection, video_id, title or video_id, track_key=track_key, source=source
                )
            else:
                row = connection.execute(
                    "SELECT video_id, track_key FROM canonical_tracks WHERE track_id = ?", (int(track_id),)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown track_id: {track_id}")
                video_id = video_id or str(row["video_id"])
                track_key = track_key or row["track_key"]
            self._set_like_connection(
                connection,
                track_id=int(track_id),
                video_id=str(video_id),
                liked=bool(liked),
                source=source,
                profile_id=profile_id,
                track_key=track_key,
            )
            return int(track_id)

    upsert_like = set_like

    def merge_dashboard_favorites(self, records: Sequence[Mapping[str, Any]]) -> int:
        """Merge newer private-site choices without changing provider likes."""
        changed = 0
        with self.transaction(immediate=True) as connection:
            for record in records:
                if record.get("liked") not in (0, 1, False, True):
                    continue
                timestamp = str(record.get("updated_at") or "")
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        continue
                except ValueError:
                    continue
                track = connection.execute("SELECT track_id,video_id,track_key FROM canonical_tracks WHERE track_key=?", (record.get("track_key"),)).fetchone()
                if track is None:
                    continue
                previous = connection.execute("SELECT updated_at FROM user_likes WHERE track_id=? AND profile_id='dashboard'", (track["track_id"],)).fetchone()
                if previous and parsed <= datetime.fromisoformat(str(previous["updated_at"]).replace("Z", "+00:00")):
                    continue
                self._set_like_connection(connection, track_id=track["track_id"], video_id=track["video_id"], track_key=track["track_key"], liked=bool(record["liked"]), source="private_site", profile_id="dashboard")
                connection.execute("UPDATE user_likes SET updated_at=? WHERE track_id=? AND profile_id='dashboard'", (timestamp, track["track_id"]))
                changed += 1
        return changed

    def create_recommendation_run(
        self,
        run_id: str | None = None,
        *,
        source: str = "local",
        algorithm: str | None = None,
        model: str | None = None,
        model_version: str | None = None,
        parameters: Any = None,
        status: Any = "running",
        input_count: int = 0,
        started_at: Any = None,
    ) -> str:
        run_id = str(run_id or uuid.uuid4().hex)
        started = _iso(started_at, default_now=True)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO recommendation_runs(
                   run_id, source, algorithm, model, model_version, parameters_json, status, input_count,
                   started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET source=excluded.source, algorithm=excluded.algorithm,
                   model=excluded.model, model_version=excluded.model_version, parameters_json=excluded.parameters_json,
                   status=excluded.status, input_count=excluded.input_count, updated_at=excluded.updated_at""",
                (
                    run_id,
                    source,
                    algorithm,
                    model,
                    model_version,
                    _json_text(parameters),
                    str(_enum_value(status, "running")),
                    _nonnegative(input_count, "input_count") or 0,
                    started,
                    started,
                    utc_now_iso(),
                ),
            )
        return run_id

    upsert_recommendation_run = create_recommendation_run

    def upsert_recommendation_item(
        self,
        run_id: str,
        rank: int,
        *,
        video_id: str | None = None,
        track_id: int | None = None,
        track_key: str | None = None,
        title: str | None = None,
        artist: str | None = None,
        score: float | None = None,
        confidence: float | None = None,
        reason: str | None = None,
        reasons: Any = None,
        reason_codes: Any = None,
        based_on_history_ids: Any = None,
        source: str = "local",
        generated_at: Any = None,
        metadata: Any = None,
        recommendation_item_id: str | None = None,
    ) -> str:
        try:
            rank = int(rank)
        except (TypeError, ValueError) as exc:
            raise ValueError("rank must be an integer") from exc
        if rank < 0:
            raise ValueError("rank must be non-negative")
        run_id = str(run_id)
        with self.transaction(immediate=True) as connection:
            if track_id is not None:
                row = connection.execute(
                    "SELECT video_id, track_key FROM canonical_tracks WHERE track_id = ?", (int(track_id),)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown track_id: {track_id}")
                video_id = video_id or str(row["video_id"])
                track_key = track_key or row["track_key"]
            else:
                video_id = self._canonical_video_id(video_id, track_key, title, artist)
                track_id = self._upsert_track_connection(
                    connection,
                    video_id,
                    title or video_id,
                    track_key=track_key,
                    artist=artist,
                    source=source,
                )
            item_id = recommendation_item_id or self._stable_key(run_id, rank)[:32]
            now = utc_now_iso()
            connection.execute(
                """INSERT INTO recommendation_items(
                   recommendation_item_id, run_id, track_id, track_key, video_id, rank, score, confidence,
                   reason, reasons_json, reason_codes_json, based_on_history_ids_json, source, generated_at,
                   metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, rank) DO UPDATE SET recommendation_item_id=excluded.recommendation_item_id,
                   track_id=excluded.track_id, track_key=excluded.track_key, video_id=excluded.video_id,
                   score=excluded.score, confidence=excluded.confidence, reason=excluded.reason,
                   reasons_json=excluded.reasons_json, reason_codes_json=excluded.reason_codes_json,
                   based_on_history_ids_json=excluded.based_on_history_ids_json, source=excluded.source,
                   generated_at=excluded.generated_at, metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (
                    item_id,
                    run_id,
                    track_id,
                    track_key,
                    video_id,
                    rank,
                    score,
                    confidence,
                    reason,
                    _json_text(reasons),
                    _json_text(reason_codes),
                    _json_text(based_on_history_ids),
                    source,
                    _iso(generated_at),
                    _json_text(metadata),
                    now,
                    now,
                ),
            )
        return item_id

    add_recommendation_item = upsert_recommendation_item

    def finish_recommendation_run(
        self,
        run_id: str,
        *,
        status: Any = "completed",
        message: str | None = None,
        error_message: str | None = None,
        completed_at: Any = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE recommendation_runs SET status = ?, message = ?, error_message = ?,
                   completed_at = ?, updated_at = ? WHERE run_id = ?""",
                (
                    str(_enum_value(status, "completed")),
                    message,
                    error_message,
                    _iso(completed_at, default_now=True),
                    utc_now_iso(),
                    str(run_id),
                ),
            )

    def list_recommendation_items(self, run_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query_all(
            """SELECT i.*, c.title, c.artist, c.album, c.url, c.thumbnail_url
               FROM recommendation_items i LEFT JOIN canonical_tracks c ON c.track_id = i.track_id
               WHERE i.run_id = ? ORDER BY i.rank, i.recommendation_item_id LIMIT ?""",
            (str(run_id), _limit(limit)),
        )
        return [dict(row) for row in rows]

    def save_recommendations(self, run_id: str, recommendations: Sequence[Any], *, message: str = "") -> None:
        run_id = self.create_recommendation_run(run_id, status="running", input_count=0)
        with self.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM recommendation_items WHERE run_id = ?", (run_id,))
            for ordinal, item in enumerate(recommendations, start=1):
                track = _value(item, "track", item)
                fields = self._track_fields(track)
                rank = int(_value(item, "rank", ordinal) or ordinal)
                score = _value(item, "score")
                confidence = _value(item, "confidence")
                reason = _value(item, "reason")
                reasons = _value(item, "reasons")
                if reasons is None and reason is not None:
                    reasons = [reason]
                recommendation_identity = _value(item, "recommendation_id") or _value(item, "recommendation_item_id")
                track_id = self._upsert_track_connection(
                    connection,
                    fields["video_id"],
                    fields["title"],
                    track_key=fields["track_key"],
                    artist=fields["artist"],
                    artists=fields["artists"],
                    album=fields["album"],
                    url=fields["url"],
                    duration_seconds=fields["duration_seconds"],
                    thumbnail_url=fields["thumbnail_url"],
                    canonical_url=fields["canonical_url"],
                    source=fields["source"] or "local",
                    metadata=fields["metadata"],
                )
                self.upsert_recommendation_item(
                    run_id,
                    rank,
                    video_id=fields["video_id"],
                    track_id=track_id,
                    track_key=fields["track_key"],
                    score=score,
                    confidence=confidence,
                    reason=reason,
                    reasons=reasons,
                    reason_codes=_value(item, "reason_codes"),
                    based_on_history_ids=_value(item, "based_on_history_ids"),
                    source=_value(item, "source", "local"),
                    generated_at=_value(item, "generated_at"),
                    metadata=_value(item, "metadata"),
                    # recommendation_item_id is globally unique in the
                    # normalized schema; scope it to this run even when the
                    # recommender emits a stable per-track recommendation id.
                    recommendation_item_id=(
                        self._stable_key(run_id, recommendation_identity or rank)[:32]
                    ),
                )
            connection.execute(
                "UPDATE recommendation_runs SET message = ?, status = ?, completed_at = ?, updated_at = ? WHERE run_id = ?",
                (message, "completed", utc_now_iso(), utc_now_iso(), run_id),
            )

    def latest_recommendations(self, limit: int = 50) -> list[dict[str, Any]]:
        row = self.query_one(
            "SELECT run_id FROM recommendation_runs WHERE status = 'completed' ORDER BY completed_at DESC, created_at DESC, rowid DESC LIMIT 1"
        )
        if row is None:
            return []
        results: list[dict[str, Any]] = []
        for item in self.list_recommendation_items(str(row["run_id"]), limit=limit):
            reasons = item.get("reasons_json")
            item["reasons"] = _json_value(reasons, value_type="json") if reasons else []
            item["track"] = {
                "track_key": item.get("track_key"),
                "title": item.get("title"),
                "artist": item.get("artist"),
                "album": item.get("album"),
                "url": item.get("url"),
                "video_id": item.get("video_id"),
            }
            results.append(item)
        return results

    def upsert_playlist(
        self,
        playlist_id: str,
        title: str,
        *,
        name: str | None = None,
        description: str | None = None,
        source: str = "local",
        remote_id: str | None = None,
        remote_url: str | None = None,
        status: str = "planned",
        write_mode: str = "dry_run",
        dry_run: bool | None = None,
        is_managed: bool = False,
        provider_confirmed: bool = False,
        requested_count: int = 0,
        confirmed_count: int = 0,
        metadata: Any = None,
    ) -> str:
        playlist_id = str(playlist_id).strip()
        if not playlist_id:
            raise ValueError("playlist_id must not be empty")
        clean_title = str(title or name or playlist_id).strip() or playlist_id
        if dry_run is None:
            dry_run = str(write_mode).lower() != "apply"
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO playlists(
                   playlist_id, title, name, description, source, remote_id, remote_url, status, write_mode,
                   dry_run, is_managed, provider_confirmed, requested_count, confirmed_count, metadata_json,
                   created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET title=excluded.title, name=excluded.name,
                   description=excluded.description, source=excluded.source, remote_id=excluded.remote_id,
                   remote_url=excluded.remote_url, status=excluded.status, write_mode=excluded.write_mode,
                   dry_run=excluded.dry_run, is_managed=excluded.is_managed,
                   provider_confirmed=excluded.provider_confirmed, requested_count=excluded.requested_count,
                   confirmed_count=excluded.confirmed_count, metadata_json=excluded.metadata_json,
                   updated_at=excluded.updated_at""",
                (
                    playlist_id,
                    clean_title,
                    name or clean_title,
                    description,
                    source,
                    remote_id,
                    remote_url,
                    status,
                    write_mode,
                    int(bool(dry_run)),
                    int(bool(is_managed)),
                    int(bool(provider_confirmed)),
                    _nonnegative(requested_count, "requested_count") or 0,
                    _nonnegative(confirmed_count, "confirmed_count") or 0,
                    _json_text(metadata),
                    utc_now_iso(),
                    utc_now_iso(),
                ),
            )
        return playlist_id

    def upsert_playlist_item(
        self,
        playlist_id: str,
        position: int,
        *,
        video_id: str | None = None,
        track_id: int | None = None,
        track_key: str | None = None,
        title: str | None = None,
        remote_item_id: str | None = None,
        source: str = "local",
        metadata: Any = None,
    ) -> int:
        position = int(position)
        if position < 0:
            raise ValueError("position must be non-negative")
        playlist_id = str(playlist_id)
        with self.transaction(immediate=True) as connection:
            if track_id is not None:
                row = connection.execute(
                    "SELECT video_id, track_key FROM canonical_tracks WHERE track_id = ?", (int(track_id),)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown track_id: {track_id}")
                video_id = video_id or str(row["video_id"])
                track_key = track_key or row["track_key"]
            else:
                video_id = self._canonical_video_id(video_id, track_key, title, None)
                track_id = self._upsert_track_connection(
                    connection, video_id, title or video_id, track_key=track_key, source=source
                )
            now = utc_now_iso()
            connection.execute(
                """INSERT INTO playlist_items(
                   playlist_id, track_id, track_key, video_id, remote_item_id, position, rank, source,
                   metadata_json, added_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id, position) DO UPDATE SET track_id=excluded.track_id,
                   track_key=excluded.track_key, video_id=excluded.video_id, remote_item_id=excluded.remote_item_id,
                   rank=excluded.rank, source=excluded.source, metadata_json=excluded.metadata_json,
                   updated_at=excluded.updated_at""",
                (
                    playlist_id,
                    track_id,
                    track_key,
                    video_id,
                    remote_item_id,
                    position,
                    position,
                    source,
                    _json_text(metadata),
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT playlist_item_id FROM playlist_items WHERE playlist_id = ? AND position = ?",
                (playlist_id, position),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the UNIQUE constraint
                raise RuntimeError("playlist item upsert did not produce a row")
            return int(row["playlist_item_id"])

    def list_playlist_items(self, playlist_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.query_all(
            """SELECT p.*, c.title, c.artist, c.album, c.url
               FROM playlist_items p LEFT JOIN canonical_tracks c ON c.track_id = p.track_id
               WHERE p.playlist_id = ? ORDER BY COALESCE(p.position, p.rank), p.playlist_item_id LIMIT ?""",
            (str(playlist_id), _limit(limit, maximum=50_000)),
        )
        return [dict(row) for row in rows]

    def save_playlist(self, plan: Any, result: Any, playlist_id: str) -> None:
        recommendations = _value(plan, "recommendations", ()) or ()
        name = _value(plan, "name", _value(plan, "title", playlist_id))
        description = _value(plan, "description", "") or ""
        mode = _enum_value(_value(result, "mode"), "dry_run")
        outcome = _enum_value(_value(result, "outcome"), _value(result, "status", "planned"))
        requested = len(recommendations)
        applied = _value(result, "applied_track_ids", ()) or ()
        remote_id = _value(result, "playlist_id")
        with self.transaction(immediate=True) as connection:
            self.upsert_playlist(
                playlist_id,
                str(name or playlist_id),
                description=str(description),
                source=str(_value(result, "provider", "local") or "local"),
                remote_id=remote_id,
                remote_url=_value(result, "playlist_url"),
                status=str(outcome),
                write_mode=str(mode),
                dry_run=str(mode) != "apply",
                provider_confirmed=bool(_value(result, "provider_confirmed", False)),
                requested_count=requested,
                confirmed_count=len(applied),
                metadata={"message": _value(result, "message")},
            )
            connection.execute(
                """INSERT INTO playlist_plans(
                   playlist_id, name, description, provider, provider_playlist_id, status, dry_run,
                   requested_count, confirmed_count, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id) DO UPDATE SET name=excluded.name, description=excluded.description,
                   provider=excluded.provider, provider_playlist_id=excluded.provider_playlist_id,
                   status=excluded.status, dry_run=excluded.dry_run, requested_count=excluded.requested_count,
                   confirmed_count=excluded.confirmed_count, message=excluded.message""",
                (
                    playlist_id,
                    str(name or playlist_id),
                    str(description),
                    str(_value(result, "provider", "local") or "local"),
                    remote_id,
                    str(outcome),
                    int(str(mode) != "apply"),
                    requested,
                    len(applied),
                    str(_value(result, "message", "") or ""),
                    _iso(_value(plan, "created_at"), default_now=True),
                ),
            )
            connection.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
            for rank, item in enumerate(recommendations, start=1):
                track = _value(item, "track", item)
                fields = self._track_fields(track)
                track_id = self._upsert_track_connection(
                    connection,
                    fields["video_id"],
                    fields["title"],
                    track_key=fields["track_key"],
                    artist=fields["artist"],
                    artists=fields["artists"],
                    album=fields["album"],
                    url=fields["url"],
                    source=fields["source"] or "local",
                )
                connection.execute(
                    """INSERT INTO playlist_items(playlist_id, track_id, track_key, video_id, position, rank, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (playlist_id, track_id, fields["track_key"], fields["video_id"], rank, rank, "local", utc_now_iso()),
                )

    def latest_playlist_preview(self) -> dict[str, Any] | None:
        """Return the newest locally generated playlist plan and its tracks.

        This is deliberately local state.  A preview may exist without any
        provider-side playlist, and the response never upgrades a dry-run to
        a successful external write.
        """

        row = self.query_one(
            """SELECT p.*, pp.description AS plan_description, pp.provider AS plan_provider,
                      pp.status AS plan_status, pp.dry_run AS plan_dry_run,
                      pp.requested_count AS plan_requested_count,
                      pp.confirmed_count AS plan_confirmed_count, pp.message AS plan_message
               FROM playlists p
               LEFT JOIN playlist_plans pp ON pp.playlist_id = p.playlist_id
               ORDER BY p.created_at DESC, p.updated_at DESC, p.rowid DESC
               LIMIT 1"""
        )
        if row is None:
            return None
        data = dict(row)
        playlist_id = str(data.get("playlist_id") or "")
        data["metadata"] = _json_value(data.get("metadata_json"), value_type="json")
        data.pop("metadata_json", None)
        data["dry_run"] = bool(data.get("dry_run"))
        data["provider_confirmed"] = bool(data.get("provider_confirmed"))
        data["items"] = self.list_playlist_items(playlist_id, limit=500)
        return data

    def start_connector_run(
        self,
        connector_name: str,
        operation: str,
        *,
        run_id: str | None = None,
        connector_id: str | None = None,
        mode: str | None = None,
        status: str = "running",
        dry_run: bool = True,
        authenticated: bool = False,
        metadata: Any = None,
    ) -> str:
        connector_run_id = str(run_id or uuid.uuid4().hex)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO connector_runs(
                   connector_run_id, connector_id, connector_name, operation, mode, status, dry_run,
                   authenticated, metadata_json, started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_run_id) DO UPDATE SET connector_id=excluded.connector_id,
                   connector_name=excluded.connector_name, operation=excluded.operation, mode=excluded.mode,
                   status=excluded.status, dry_run=excluded.dry_run, authenticated=excluded.authenticated,
                   metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (
                    connector_run_id,
                    connector_id,
                    str(connector_name),
                    str(operation),
                    mode,
                    str(status),
                    int(bool(dry_run)),
                    int(bool(authenticated)),
                    _json_text(metadata),
                    utc_now_iso(),
                    utc_now_iso(),
                    utc_now_iso(),
                ),
            )
        return connector_run_id

    def finish_connector_run(
        self,
        connector_run_id: str,
        *,
        status: str,
        records_seen: int = 0,
        records_written: int = 0,
        account_access_confirmed: bool = False,
        history_access_confirmed: bool = False,
        playlist_write_access_confirmed: bool = False,
        write_confirmed: bool = False,
        error_code: str | None = None,
        message: str | None = None,
        metadata: Any = None,
        completed_at: Any = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE connector_runs SET status = ?, records_seen = ?, records_written = ?,
                   account_access_confirmed = ?, history_access_confirmed = ?, playlist_write_access_confirmed = ?,
                   write_confirmed = ?, error_code = ?, message = ?, metadata_json = COALESCE(?, metadata_json),
                   completed_at = ?, updated_at = ? WHERE connector_run_id = ?""",
                (
                    status,
                    _nonnegative(records_seen, "records_seen") or 0,
                    _nonnegative(records_written, "records_written") or 0,
                    int(bool(account_access_confirmed)),
                    int(bool(history_access_confirmed)),
                    int(bool(playlist_write_access_confirmed)),
                    int(bool(write_confirmed)),
                    error_code,
                    message,
                    _json_text(metadata),
                    _iso(completed_at, default_now=True),
                    utc_now_iso(),
                    str(connector_run_id),
                ),
            )

    def list_connector_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.query_all(
            "SELECT * FROM connector_runs ORDER BY started_at DESC, connector_run_id DESC LIMIT ?",
            (_limit(limit),),
        )
        return [dict(row) for row in rows]

    def save_scan_run(self, summary: Any) -> None:
        run_id = str(_value(summary, "run_id"))
        started = _iso(_value(summary, "started_at"), default_now=True)
        finished = _iso(_value(summary, "finished_at"))
        status = str(_enum_value(_value(summary, "status"), "completed"))
        errors = _value(summary, "errors", ()) or ()
        records_scanned = int(_value(summary, "records_scanned", _value(summary, "items_seen", 0)) or 0)
        records_emitted = int(_value(summary, "records_emitted", 0) or 0)
        records_skipped = int(_value(summary, "records_skipped", 0) or 0)
        duplicates = int(_value(summary, "duplicate_records", 0) or 0)
        pages = int(_value(summary, "pages_scanned", 0) or 0)
        complete = bool(_value(summary, "complete", False))
        source = str(_value(summary, "source", "youtube_music") or "youtube_music")
        error_code = _value(summary, "error_code")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO scan_runs(
                   run_id, source, status, started_at, finished_at, items_seen, records_scanned, records_emitted,
                   records_skipped, duplicate_records, pages_scanned, complete, message, error_code, errors_json
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, finished_at=excluded.finished_at,
                   items_seen=excluded.items_seen, records_scanned=excluded.records_scanned,
                   records_emitted=excluded.records_emitted, records_skipped=excluded.records_skipped,
                   duplicate_records=excluded.duplicate_records, pages_scanned=excluded.pages_scanned,
                   complete=excluded.complete, message=excluded.message, error_code=excluded.error_code,
                   errors_json=excluded.errors_json""",
                (
                    run_id,
                    source,
                    status,
                    started,
                    finished,
                    records_scanned,
                    records_scanned,
                    records_emitted,
                    records_skipped,
                    duplicates,
                    pages,
                     int(complete),
                     str(_value(summary, "message", "") or ""),
                     str(error_code) if error_code else None,
                     _json_text(errors),
                 ),
            )
        connector_run_id = self.start_connector_run(
            source,
            "history_scan",
            run_id=f"scan:{run_id}",
            status=status,
            dry_run=True,
            metadata={"scan_run_id": run_id},
        )
        self.finish_connector_run(
            connector_run_id,
            status=status,
            records_seen=records_scanned,
            records_written=records_emitted,
            history_access_confirmed=records_emitted > 0,
            completed_at=finished,
            metadata={"duplicate_records": duplicates, "complete": complete},
        )

    def list_scan_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query_all(
            "SELECT * FROM scan_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
            (_limit(limit),),
        )
        return [dict(row) for row in rows]

    def reconcile_running_scan_runs(self) -> int:
        """Mark scans abandoned by a prior process as interrupted on startup."""

        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE scan_runs
                   SET status = 'interrupted', finished_at = ?, complete = 0,
                       message = CASE
                           WHEN message = '' THEN 'Process exited before the scan completed.'
                           ELSE message || ' Process exited before the scan completed.'
                       END,
                       error_code = COALESCE(error_code, 'process_interrupted')
                   WHERE status = 'running' AND finished_at IS NULL""",
                (now,),
            )
            return int(cursor.rowcount or 0)

    def set_metadata(self, key: str, value: Any) -> None:
        encoded = value if isinstance(value, str) else (_json_text(value) or "null")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO app_metadata(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (str(key), encoded, utc_now_iso()),
            )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_metadata WHERE key = ?", (str(key),))
        return default if row is None else row["value"]

    set_app_metadata = set_metadata
    get_app_metadata = get_metadata


__all__ = ["Database", "SCHEMA_VERSION", "utc_now_iso"]
