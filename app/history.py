"""Normalization, persistence, and aggregation for YouTube Music history.

``HistoryService`` is intentionally small at the database boundary.  The
application's SQLite implementation can expose the conventional methods in
``DatabaseProtocol`` below, while tests or parallel work can use a compatible
object with any of the documented method aliases.  The service never keeps a
raw scanner record and never performs network or account operations.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol, runtime_checkable

try:  # The relative import is the normal package path.
    from .models import (
        HistoryEvent,
        HistoryOverview,
        IngestResult,
        LikeState,
        Track,
        TrackSummary,
        coerce_like_state,
        normalize_key,
        normalize_text,
        normalize_timestamp,
        normalize_video_id,
        stable_track_key,
    )
except ImportError:  # pragma: no cover - useful when run as a loose script.
    from models import (  # type: ignore[no-redef]
        HistoryEvent,
        HistoryOverview,
        IngestResult,
        LikeState,
        Track,
        TrackSummary,
        coerce_like_state,
        normalize_key,
        normalize_text,
        normalize_timestamp,
        normalize_video_id,
        stable_track_key,
    )


UTC = timezone.utc
_MISSING = object()
_CANONICAL_KEY_RE = re.compile(r"[^a-z0-9]+")
_DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2})(?:\.\d+)?$"
)


class HistoryError(Exception):
    """Base class for history normalization and persistence errors."""


class DatabaseInterfaceError(HistoryError):
    """Raised when a supplied database has no compatible write method."""


@runtime_checkable
class DatabaseProtocol(Protocol):
    """The preferred database contract used by ``HistoryService``.

    A concrete database may implement single-item methods, batch methods, or
    both.  Read methods are optional; without one, an instance still provides
    an accurate overview for events ingested during its lifetime.
    """

    def save_track(self, track: Track) -> Any:
        ...

    def save_history_event(self, event: HistoryEvent) -> Any:
        ...


# This alias makes annotations friendly to callers that refer to the
# dependency simply as ``Database`` without forcing an import of app.database.
Database = DatabaseProtocol


def _canonical_name(name: Any) -> str:
    return _CANONICAL_KEY_RE.sub("", str(name).casefold())


def _mapping_for(value: Any) -> Mapping[Any, Any] | None:
    if isinstance(value, Mapping):
        return value
    try:
        # ``vars`` avoids invoking arbitrary properties on normal objects.
        values = vars(value)
    except (TypeError, ValueError):
        return None
    return values if isinstance(values, Mapping) else None


def _lookup(value: Any, *names: str) -> Any:
    """Look up a field with case/camel/snake spelling tolerance."""

    if value is None:
        return _MISSING

    mapping = _mapping_for(value)
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
        canonical = {_canonical_name(key): item for key, item in mapping.items()}
        for name in names:
            found = canonical.get(_canonical_name(name), _MISSING)
            if found is not _MISSING:
                return found
        return _MISSING

    for name in names:
        try:
            found = getattr(value, name)
        except (AttributeError, TypeError, ValueError):
            continue
        if not callable(found):
            return found

    # A small fallback for objects exposing camelCase fields but not a vars()
    # mapping.  It still reads only explicitly requested names.
    wanted = {_canonical_name(name) for name in names}
    for name in dir(value):
        if _canonical_name(name) not in wanted:
            continue
        try:
            found = getattr(value, name)
        except (AttributeError, TypeError, ValueError):
            continue
        if not callable(found):
            return found
    return _MISSING


def _first_present(value: Any, *names: str) -> Any:
    for name in names:
        found = _lookup(value, name)
        if found is _MISSING or found is None:
            continue
        if isinstance(found, str) and not found.strip():
            continue
        if isinstance(found, (list, tuple, set, dict)) and not found:
            continue
        return found
    return None


def _nested_values(record: Any) -> tuple[Any, ...]:
    values: list[Any] = []
    for name in ("track", "song", "music_item", "musicItem", "item", "video"):
        value = _lookup(record, name)
        if value is not _MISSING and value is not None:
            values.append(value)
    return tuple(values)


def _first_from_record(record: Any, nested: Sequence[Any], *names: str) -> Any:
    value = _first_present(record, *names)
    if value is not None:
        return value
    for item in nested:
        value = _first_present(item, *names)
        if value is not None:
            return value
    return None


def _artist_text(value: Any) -> str:
    """Normalize artist values returned as strings, dicts, or name lists."""

    if value is None:
        return ""
    if isinstance(value, Mapping) or _mapping_for(value) is not None:
        found = _first_present(value, "name", "artist", "artist_name", "title", "text")
        return normalize_text(found)
    if isinstance(value, (list, tuple, set)):
        names: list[str] = []
        for item in value:
            name = _artist_text(item)
            if name and name not in names:
                names.append(name)
        return ", ".join(names)
    return normalize_text(value)


def _duration_seconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds.is_integer() and 0 <= seconds <= 86_400:
            return int(seconds)
        return None

    text = normalize_text(value, max_length=64)
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = -1
    if numeric >= 0 and numeric <= 86_400 and numeric.is_integer():
        return int(numeric)

    match = _DURATION_RE.fullmatch(text)
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    total = hours * 3600 + minutes * 60 + seconds
    return total if 0 <= total <= 86_400 else None


def _safe_source(value: Any, default: str) -> str:
    source = normalize_text(value, max_length=64).casefold()
    if source in {
        "youtube_music",
        "youtube_music_scan",
        "ytmusicapi",
        "browser",
        "history",
        "local",
    } or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", source):
        return source
    return default


def normalize_history_record(
    record: Any,
    *,
    default_source: str = "youtube_music",
) -> HistoryEvent | None:
    """Normalize one scanner record into a privacy-safe ``HistoryEvent``.

    Unknown keys are ignored.  A record must contain either a video id, an
    existing local track key, or at least one piece of identifying metadata;
    otherwise it cannot be useful for recommendations and is skipped.
    """

    if isinstance(record, HistoryEvent):
        return record

    if isinstance(record, Track):
        return HistoryEvent(
            track_key=record.track_key,
            title=record.title,
            artist=record.artist,
            album=record.album,
            video_id=record.video_id,
            duration_seconds=record.duration_seconds,
            source=default_source,
        )

    if isinstance(record, str):
        # A bare scanner id is accepted only when it has the shape of a
        # YouTube id.  Arbitrary strings are not persisted as track metadata.
        video_id = normalize_video_id(record)
        if not video_id:
            return None
        return HistoryEvent(video_id=video_id, source=default_source)

    nested = _nested_values(record)
    title_value = _first_from_record(
        record,
        nested,
        "title",
        "track_title",
        "trackTitle",
        "song_title",
        "songTitle",
        "name",
    )
    title = normalize_text(title_value)

    artist_value = _first_from_record(
        record,
        nested,
        "artist",
        "artist_name",
        "artistName",
        "artists",
        "author",
        "creator",
        "byline",
    )
    artist = _artist_text(artist_value)

    album_value = _first_from_record(
        record,
        nested,
        "album",
        "album_name",
        "albumName",
        "release",
    )
    album = _artist_text(album_value)

    id_value = _first_from_record(
        record,
        nested,
        "video_id",
        "videoId",
        "youtube_id",
        "youtubeId",
        "videoID",
    )
    video_id = normalize_video_id(id_value)
    if not video_id:
        # ``id`` is deliberately lower priority: in scanner payloads it can
        # refer to an album, playlist, or account object instead of a video.
        id_value = _first_from_record(record, nested, "id")
        video_id = normalize_video_id(id_value)
    if not video_id:
        url_value = _first_from_record(
            record,
            nested,
            "video_url",
            "videoUrl",
            "watch_url",
            "watchUrl",
            "url",
        )
        video_id = normalize_video_id(url_value)

    supplied_key_value = _first_from_record(
        record,
        nested,
        "track_key",
        "trackKey",
        "stable_track_key",
        "stableTrackKey",
    )
    supplied_key = normalize_key(supplied_key_value)
    # Connector ``TrackRecord`` instances already carry the shared contract's
    # stable key (``video:...`` or ``track:...``).  Preserve it when present;
    # otherwise generate the same key locally from normalized fields.
    track_key = supplied_key or stable_track_key(title, artist, album, video_id)
    if not track_key:
        return None

    timestamp_value = _first_from_record(
        record,
        nested,
        "played_at",
        "playedAt",
        "timestamp",
        "played_timestamp",
        "playedTimestamp",
        "occurred_at",
        "occurredAt",
        "created_at",
        "createdAt",
        "date",
        "time",
    )
    played_at = normalize_timestamp(timestamp_value)

    like_value = _first_from_record(
        record,
        nested,
        "liked",
        "is_liked",
        "isLiked",
        "like_status",
        "likeStatus",
        "rating",
        "feedback",
    )
    liked = coerce_like_state(like_value)

    source_record_id = normalize_key(
        _first_from_record(
            record,
            nested,
            "source_record_id",
            "sourceRecordId",
            "history_id",
            "historyId",
        )
    )
    event_key = normalize_key(
        _first_from_record(
            record,
            nested,
            "event_key",
            "eventKey",
            "event_id",
            "eventId",
        )
    )

    duration_value = _first_from_record(
        record,
        nested,
        "duration_seconds",
        "durationSeconds",
        "duration",
        "length_seconds",
        "lengthSeconds",
    )
    duration_seconds = _duration_seconds(duration_value)

    source_value = _first_present(record, "source")
    source = _safe_source(source_value, _safe_source(default_source, "youtube_music"))
    return HistoryEvent(
        track_key=track_key,
        title=title,
        artist=artist,
        album=album,
        video_id=video_id,
        played_at=played_at,
        liked=liked,
        source=source,
        duration_seconds=duration_seconds,
        source_record_id=source_record_id,
        event_key=event_key,
    )


# Short aliases are useful to scanner adapters and keep the normalization
# function easy to discover without introducing another implementation.
normalize_record = normalize_history_record


def normalize_history_records(
    records: Iterable[Any],
    *,
    default_source: str = "youtube_music",
) -> tuple[HistoryEvent, ...]:
    """Normalize and deduplicate an iterable of scanner records."""

    if isinstance(records, (Mapping, str, bytes, HistoryEvent, Track)):
        records = (records,)

    result: list[HistoryEvent] = []
    seen: set[str] = set()
    for record in records:
        event = normalize_history_record(record, default_source=default_source)
        if event is None:
            continue
        identity = event.event_key or _fallback_event_key(event)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(event)
    return tuple(result)


def _fallback_event_key(event: HistoryEvent) -> str:
    canonical = "\x1f".join(
        (
            event.track_key or "",
            event.played_at.isoformat() if event.played_at else "",
            event.source,
        )
    )
    return f"evt:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


@dataclass
class _TrackAccumulator:
    track_key: str
    title: str = ""
    artist: str = ""
    album: str = ""
    video_id: str | None = None
    play_count: int = 0
    first_played_at: datetime | None = None
    last_played_at: datetime | None = None
    liked: bool | None = None
    like_signal: float = 0.0
    like_count: int = 0
    dislike_count: int = 0

    def update(self, event: HistoryEvent) -> None:
        self.play_count += 1
        if not self.title and event.title:
            self.title = event.title
        if not self.artist and event.artist:
            self.artist = event.artist
        if not self.album and event.album:
            self.album = event.album
        if not self.video_id and event.video_id:
            self.video_id = event.video_id

        if event.played_at is not None:
            if self.first_played_at is None or event.played_at < self.first_played_at:
                self.first_played_at = event.played_at
            if self.last_played_at is None or event.played_at > self.last_played_at:
                self.last_played_at = event.played_at

        if event.like_state is LikeState.LIKED:
            self.liked = True
            self.like_signal = 1.0
            self.like_count += 1
        elif event.like_state is LikeState.DISLIKED:
            self.liked = False
            self.like_signal = -1.0
            self.dislike_count += 1

    def freeze(self) -> TrackSummary:
        return TrackSummary(
            track_key=self.track_key,
            title=self.title,
            artist=self.artist,
            album=self.album,
            video_id=self.video_id,
            play_count=self.play_count,
            first_played_at=self.first_played_at,
            last_played_at=self.last_played_at,
            liked=self.liked,
            like_signal=self.like_signal,
            like_count=self.like_count,
            dislike_count=self.dislike_count,
        )


class HistoryService:
    """Normalize, deduplicate, persist, and summarize local play history."""

    _SINGLE_TRACK_METHODS = (
        "upsert_track",
        "save_track",
        "store_track",
        "insert_track",
        "add_track",
    )
    _BATCH_TRACK_METHODS = (
        "upsert_tracks",
        "save_tracks",
        "store_tracks",
        "insert_tracks",
        "add_tracks",
    )
    _SINGLE_EVENT_METHODS = (
        "upsert_history_event",
        "save_history_event",
        "store_history_event",
        "insert_history_event",
        "add_history_event",
        "upsert_event",
        "save_event",
        "store_event",
        "insert_event",
        "add_event",
    )
    _BATCH_EVENT_METHODS = (
        "ingest_events",
        "upsert_history_events",
        "save_history_events",
        "store_history_events",
        "insert_history_events",
        "add_history_events",
        "upsert_events",
        "save_events",
        "store_events",
        "insert_events",
        "add_events",
        "save_history",
        "store_history",
        "insert_history",
    )
    _READ_EVENT_METHODS = (
        "iter_history_events",
        "list_history_events",
        "get_history_events",
        "fetch_history_events",
        "read_history_events",
        "iter_events",
        "list_events",
        "get_events",
        "fetch_events",
        "get_history",
        "fetch_history",
        "list_history",
    )
    _EXISTS_EVENT_METHODS = (
        "history_event_exists",
        "event_exists",
        "has_history_event",
        "has_event",
        "contains_event",
    )

    def __init__(
        self,
        database: DatabaseProtocol | Any,
        *,
        recent_limit: int = 50,
        default_source: str = "youtube_music",
    ) -> None:
        self.database = database
        self.recent_limit = max(0, min(int(recent_limit), 500))
        self.default_source = _safe_source(default_source, "youtube_music")
        self._events: dict[str, HistoryEvent] = {}
        self._lock = RLock()

    def normalize(self, record: Any, *, source: str | None = None) -> HistoryEvent | None:
        return normalize_history_record(
            record,
            default_source=_safe_source(source, self.default_source)
            if source is not None
            else self.default_source,
        )

    def normalize_records(
        self,
        records: Iterable[Any],
        *,
        source: str | None = None,
    ) -> tuple[HistoryEvent, ...]:
        return normalize_history_records(
            records,
            default_source=_safe_source(source, self.default_source)
            if source is not None
            else self.default_source,
        )

    def ingest(
        self,
        records: Iterable[Any],
        *,
        source: str | None = None,
    ) -> IngestResult:
        """Normalize, deduplicate, and persist scanner records.

        The returned ``stored`` count is the number of new event identities
        accepted by this service after local/database deduplication.  A
        database implementation with an idempotent upsert may still report a
        successful no-op internally; the service will not count an identity
        twice in the same process.
        """

        raw_records = self._materialize_records(records)
        received = len(raw_records)
        normalized_events: list[HistoryEvent] = []
        existing_events_to_refresh: list[HistoryEvent] = []
        duplicates = 0
        skipped = 0

        with self._lock:
            ingest_source = (
                _safe_source(source, self.default_source)
                if source is not None
                else self.default_source
            )
            existing_events = self._load_database_events()
            existing_keys = {
                event.event_key or _fallback_event_key(event)
                for event in existing_events
                if event.track_key
            }
            existing_keys.update(self._events)

            batch_keys: set[str] = set()
            for record in raw_records:
                event = self.normalize(record, source=ingest_source)
                if event is None or not event.track_key:
                    skipped += 1
                    continue
                identity = event.event_key or _fallback_event_key(event)
                if identity in batch_keys:
                    duplicates += 1
                    continue
                if identity in existing_keys:
                    duplicates += 1
                    existing_events_to_refresh.append(event)
                    continue
                batch_keys.add(identity)
                normalized_events.append(event)

            events_to_persist = normalized_events + existing_events_to_refresh
            if events_to_persist:
                self._persist(events_to_persist)
                for event in events_to_persist:
                    identity = event.event_key or _fallback_event_key(event)
                    self._events[identity] = event

        return IngestResult(
            received=received,
            normalized=len(normalized_events),
            stored=len(normalized_events),
            duplicates=duplicates,
            skipped=skipped,
            events=tuple(normalized_events),
        )

    # Common adapter names retained as thin aliases.
    add_records = ingest
    ingest_records = ingest
    process = ingest
    store = ingest

    def ingest_result(self, result: Any) -> dict[str, Any]:
        """Ingest a connector result and return a JSON-safe count summary.

        Connector results expose their records as ``items``; a mapping-based
        bridge may instead call the field ``records`` or ``history``.  Only
        those normalized item containers are read, and connector status or
        metadata is never persisted as listening history.
        """

        if isinstance(result, Mapping):
            records = _first_present(result, "items", "records", "history", "tracks")
        else:
            records = _first_present(result, "items", "records", "history", "tracks")
        if records is None:
            records = ()
        ingestion = self.ingest(records)
        return ingestion.to_dict()

    def overview(
        self,
        *,
        recent_limit: int | None = None,
        limit: int | None = None,
    ) -> HistoryOverview:
        """Return aggregated play counts, recency, and like signals."""

        if recent_limit is None and limit is not None:
            recent_limit = limit

        with self._lock:
            events = self._all_events()

        # Preserve source order for untimestamped events, while ensuring the
        # same event key cannot contribute twice if a database also returned a
        # just-ingested in-memory event.
        ordered_for_aggregation = list(events)
        ordered_for_aggregation.sort(
            key=lambda pair: (
                pair[1].played_at is not None,
                pair[1].played_at or datetime.min.replace(tzinfo=UTC),
                pair[0],
            )
        )

        accumulators: dict[str, _TrackAccumulator] = {}
        artists: set[str] = set()
        latest_event_at: datetime | None = None
        for _, event in ordered_for_aggregation:
            if not event.track_key:
                continue
            accumulator = accumulators.get(event.track_key)
            if accumulator is None:
                accumulator = _TrackAccumulator(track_key=event.track_key)
                accumulators[event.track_key] = accumulator
            accumulator.update(event)
            if event.artist:
                artists.add(event.artist.casefold())
            if event.played_at and (
                latest_event_at is None or event.played_at > latest_event_at
            ):
                latest_event_at = event.played_at

        summaries = [accumulator.freeze() for accumulator in accumulators.values()]
        summaries.sort(key=_summary_sort_key)

        requested_limit = self.recent_limit if recent_limit is None else recent_limit
        requested_limit = max(0, min(int(requested_limit), 500))
        recent_events = [event for _, event in events]
        recent_events.sort(key=_recent_event_sort_key, reverse=True)

        liked_tracks = sum(summary.liked is True for summary in summaries)
        disliked_tracks = sum(summary.liked is False for summary in summaries)
        return HistoryOverview(
            total_events=len(events),
            unique_tracks=len(summaries),
            unique_artists=len(artists),
            last_played_at=latest_event_at,
            tracks=tuple(summaries),
            recent_events=tuple(recent_events[:requested_limit]),
            liked_tracks=liked_tracks,
            disliked_tracks=disliked_tracks,
        )

    get_overview = overview
    summary = overview
    get_history_overview = overview

    def recommendation_candidates(self, *, limit: int | None = None) -> tuple[TrackSummary, ...]:
        """Return summaries in a deterministic recommendation-friendly order."""

        tracks = self.overview(recent_limit=0).tracks
        if limit is None:
            return tracks
        return tracks[: max(0, int(limit))]

    def _materialize_records(self, records: Iterable[Any]) -> list[Any]:
        if isinstance(records, (Mapping, str, bytes, HistoryEvent, Track)):
            return [records]
        try:
            return list(records)
        except TypeError:
            return [records]

    def _load_database_events(self) -> tuple[HistoryEvent, ...]:
        database = self.database
        if database is None:
            return ()

        for name in self._READ_EVENT_METHODS:
            method = getattr(database, name, None)
            if not callable(method):
                continue
            try:
                payload = method()
            except TypeError:
                # A compatible read method may require optional keyword
                # parameters.  It is safe to skip that method and try the
                # next known contract rather than guessing account filters.
                continue
            except (AttributeError, KeyError, ValueError):
                continue
            return self._normalize_read_payload(payload)

        # Some small SQLite wrappers expose a property rather than a method.
        for name in ("history_events", "events", "history"):
            payload = getattr(database, name, _MISSING)
            if payload is not _MISSING and not callable(payload):
                return self._normalize_read_payload(payload)
        return ()

    def _normalize_read_payload(self, payload: Any) -> tuple[HistoryEvent, ...]:
        if payload is None:
            return ()
        if isinstance(payload, Mapping):
            nested = _first_present(payload, "events", "history_events", "history", "items")
            payload = nested if nested is not None else (payload,)
        elif isinstance(payload, (str, bytes, HistoryEvent, Track)):
            payload = (payload,)

        try:
            values = list(payload)
        except TypeError:
            values = [payload]

        result: list[HistoryEvent] = []
        seen: set[str] = set()
        for value in values:
            event = normalize_history_record(value, default_source=self.default_source)
            if event is None or not event.track_key:
                continue
            identity = event.event_key or _fallback_event_key(event)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(event)
        return tuple(result)

    def _all_events(self) -> list[tuple[str, HistoryEvent]]:
        combined: dict[str, HistoryEvent] = {}
        for event in self._load_database_events():
            identity = event.event_key or _fallback_event_key(event)
            combined[identity] = event
        for identity, event in self._events.items():
            combined.setdefault(identity, event)
        return list(combined.items())

    def _persist(self, events: Sequence[HistoryEvent]) -> None:
        database = self.database
        if database is None:
            raise DatabaseInterfaceError("HistoryService requires a database object")

        batch_event = self._find_method(database, self._BATCH_EVENT_METHODS)
        single_event = self._find_method(database, self._SINGLE_EVENT_METHODS)
        if batch_event is None and single_event is None:
            raise DatabaseInterfaceError(
                "database must expose a history-event writer such as "
                "save_history_event(event) or save_history_events(events)"
            )

        tracks: list[Track] = []
        seen_tracks: set[str] = set()
        for event in events:
            if event.track_key and event.track_key not in seen_tracks:
                tracks.append(event.track)
                seen_tracks.add(event.track_key)

        single_track = self._find_method(database, self._SINGLE_TRACK_METHODS)
        batch_track = self._find_method(database, self._BATCH_TRACK_METHODS)
        if single_track is not None:
            for track in tracks:
                # The SQLite ``upsert_track`` contract requires a provider
                # video id.  Metadata-only events are still persisted by the
                # history writer, which derives a local synthetic id; do not
                # send ``None`` to a positional video-id method first.
                if not track.video_id and self._first_parameter_name(single_track) == "video_id":
                    continue
                self._call_writer(single_track, track)
        elif batch_track is not None and tracks:
            self._call_writer(batch_track, tracks, many=True)

        # A batch database contract is preferred when available.  Besides
        # being more efficient, it lets SQLite perform its own insert-vs-
        # duplicate decision inside one transaction.
        if batch_event is not None:
            self._call_writer(batch_event, list(events), many=True)
        else:
            assert single_event is not None
            for event in events:
                self._call_writer(single_event, event)

    @staticmethod
    def _find_method(database: Any, names: Sequence[str]) -> Any | None:
        for name in names:
            method = getattr(database, name, None)
            if callable(method):
                return method
        return None

    @staticmethod
    def _first_parameter_name(method: Any) -> str:
        try:
            for parameter in inspect.signature(method).parameters.values():
                if parameter.kind in (
                    parameter.POSITIONAL_ONLY,
                    parameter.POSITIONAL_OR_KEYWORD,
                ):
                    return parameter.name
        except (TypeError, ValueError):
            pass
        return ""

    @staticmethod
    def _call_writer(method: Any, payload: Any, *, many: bool = False) -> Any:
        """Call a compatible writer with a model, then a safe dict fallback."""

        # The concrete SQLite Database uses keyword-oriented methods such as
        # ``upsert_track(video_id, ...)`` and
        # ``record_history_event(video_id, ...)``.  Passing a model as their
        # first positional argument would stringify the entire object into
        # the video_id column, so detect that signature before trying the
        # generic model-oriented contract.
        if not many:
            first_name = HistoryService._first_parameter_name(method)

            if first_name not in {
                "event",
                "history_event",
                "record",
                "item",
                "payload",
                "track",
                "value",
                "object",
            }:
                safe_payload = HistoryService._storage_payload(payload)
                try:
                    signature = inspect.signature(method)
                    accepts_kwargs = any(
                        parameter.kind is parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                    if accepts_kwargs:
                        kwargs = safe_payload
                    else:
                        kwargs = {
                            key: value
                            for key, value in safe_payload.items()
                            if key in signature.parameters
                        }
                    if kwargs:
                        return method(**kwargs)
                except (TypeError, ValueError):
                    # Fall through to the model/dict compatibility calls.
                    pass

        try:
            return method(payload)
        except TypeError as first_error:
            if many:
                safe_payload = [
                    HistoryService._storage_payload(item)
                    if hasattr(item, "to_dict")
                    else item
                    for item in payload
                ]
            else:
                safe_payload = HistoryService._storage_payload(payload)

            try:
                return method(safe_payload)
            except TypeError:
                if not many and isinstance(safe_payload, Mapping):
                    try:
                        return method(**safe_payload)
                    except TypeError:
                        pass
                raise first_error

    @staticmethod
    def _storage_payload(payload: Any) -> dict[str, Any]:
        """Build keyword-safe persistence fields without scanner extras."""

        if isinstance(payload, Track):
            return {
                **payload.to_dict(),
                "artists": payload.artists,
                "url": payload.url,
                "canonical_url": payload.canonical_url,
                "source": "youtube_music",
            }
        if isinstance(payload, HistoryEvent):
            return {
                **payload.to_dict(),
                "history_id": payload.history_id,
                "event_id": payload.event_id,
                "source_record_id": payload.source_record_id,
                "liked": payload.liked,
            }
        if hasattr(payload, "to_dict"):
            value = payload.to_dict()
            return dict(value) if isinstance(value, Mapping) else {"value": value}
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"value": payload}


def _summary_sort_key(summary: TrackSummary) -> tuple[Any, ...]:
    # Counts are the most stable signal; recency and positive feedback break
    # ties without making a single like erase a substantial play history.
    timestamp = summary.last_played_at.timestamp() if summary.last_played_at else float("-inf")
    return (-summary.play_count, -timestamp, -summary.like_signal, summary.track_key)


def _recent_event_sort_key(event: HistoryEvent) -> tuple[Any, ...]:
    timestamp = event.played_at.timestamp() if event.played_at else float("-inf")
    return (event.played_at is not None, timestamp, event.event_key or "")


__all__ = [
    "Database",
    "DatabaseInterfaceError",
    "DatabaseProtocol",
    "HistoryError",
    "HistoryService",
    "normalize_history_record",
    "normalize_history_records",
    "normalize_record",
]
