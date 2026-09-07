"""Domain models for the local YouTube Music recommendation application.

The models in this module deliberately contain only the small, normalized
subset of a scanner record that is useful for local recommendations.  In
particular, scanner payloads, account identifiers, browser data, URLs, and
unknown fields are not retained by these objects.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse


UTC = timezone.utc

# Keep persisted values bounded.  This is both a storage safeguard and a
# useful privacy boundary when a malformed scanner record contains a large
# blob of text.
MAX_TEXT_LENGTH = 512
MAX_ID_LENGTH = 160

_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def normalize_text(value: Any, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Return a bounded, display-safe representation of a text value.

    The function is intentionally conservative.  It does not attempt to
    infer values from arbitrary objects, and therefore does not accidentally
    serialize a scanner payload or an account object into the local database.
    """

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")

    try:
        text = str(value)
    except Exception:
        return ""

    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARACTER_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def normalize_video_id(value: Any) -> str | None:
    """Extract a stable YouTube video id without retaining a source URL.

    A scanner may return a bare id, a ``youtube:`` key, or one of the common
    YouTube watch/shorts/embed URLs.  Unknown URL forms are rejected rather
    than persisted verbatim because URLs can contain query parameters that
    identify a user or a browsing session.
    """

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None

    text = normalize_text(value, max_length=MAX_ID_LENGTH)
    if not text:
        return None

    if text.startswith(("youtube:", "yt:")):
        text = text.split(":", 1)[1].strip()

    if "://" in text or text.startswith(("www.", "youtu.be/", "youtube.com/")):
        url_text = text if "://" in text else f"https://{text}"
        try:
            parsed = urlparse(url_text)
        except ValueError:
            return None

        host = (parsed.hostname or "").casefold().removeprefix("www.")
        path_parts = [unquote(part) for part in parsed.path.split("/") if part]
        candidate: str | None = None

        if host in {"youtu.be", "youtube-nocookie.com"}:
            candidate = path_parts[0] if path_parts else None
        elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            query_value = parse_qs(parsed.query).get("v", [None])[0]
            if query_value:
                candidate = query_value
            elif path_parts and path_parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = path_parts[1] if len(path_parts) > 1 else None

        text = candidate or ""

    text = normalize_text(text, max_length=MAX_ID_LENGTH)
    if not text or not _SAFE_ID_RE.fullmatch(text):
        return None
    return text


def normalize_key(value: Any) -> str | None:
    """Normalize an already-generated local key.

    Keys with an unexpected character are represented by a digest.  This
    keeps the database key deterministic without storing arbitrary scanner
    text (which may contain a URL or an account-specific identifier).
    """

    text = normalize_text(value, max_length=MAX_ID_LENGTH)
    if not text:
        return None
    if _SAFE_KEY_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"key:{digest}"


def stable_track_key(
    title: Any = "",
    artist: Any = "",
    album: Any = "",
    video_id: Any = None,
) -> str | None:
    """Create a stable, privacy-preserving key for a track.

    A YouTube video id is the strongest identity and is kept in a namespaced
    key.  When it is unavailable, normalized metadata is hashed instead of
    putting the title or artist into the persisted key.  ``None`` is returned
    only when the record has no identifying value at all.
    """

    normalized_video_id = normalize_video_id(video_id)
    if normalized_video_id:
        # This spelling is also used by the shared connector contract.  Keep
        # the prefix provider-neutral even though the current scanner is
        # YouTube Music-specific.
        return f"video:{normalized_video_id}"

    # Metadata identity is case-insensitive while display fields retain their
    # original normalized casing.  This prevents rescans that differ only in
    # capitalization from creating a second track.
    parts = (
        normalize_text(title).casefold(),
        normalize_text(artist).casefold(),
        normalize_text(album).casefold(),
    )
    if not any(parts):
        return None

    canonical = "\x1f".join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"track:{digest}"


# A more explicit alias is convenient for callers that prefer a verb-style
# name, while keeping one implementation of the keying rules.
track_key_for = stable_track_key


def normalize_timestamp(value: Any) -> datetime | None:
    """Normalize common scanner timestamp forms to timezone-aware UTC.

    Naive ISO timestamps are interpreted as UTC.  Scanner timestamps are
    often epoch milliseconds, so values larger than the normal epoch-second
    range are converted from milliseconds.
    """

    if value is None or isinstance(value, bool):
        return None

    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, (int, float)):
        parsed = _timestamp_from_epoch(float(value))
    else:
        text = normalize_text(value, max_length=128)
        if not text:
            return None
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            try:
                parsed = _timestamp_from_epoch(float(text))
            except ValueError:
                return None
        else:
            iso_text = text
            if iso_text.endswith(("Z", "z")):
                iso_text = f"{iso_text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(iso_text)
            except ValueError:
                # A few scanners use a space-separated UTC value without an
                # ISO timezone marker.  Keep the fallback deliberately small.
                for fmt in (
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d",
                ):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    return parsed


def _timestamp_from_epoch(value: float) -> datetime | None:
    if not value == value:  # NaN
        return None
    # Values above 1e11 are conventionally epoch milliseconds.  The broad
    # bounds stop accidental durations or arbitrary ids becoming timestamps.
    seconds = value / 1000.0 if abs(value) >= 100_000_000_000 else value
    if seconds < 0 or seconds > 4_102_444_800:  # 2100-01-01 UTC
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class LikeState(str, Enum):
    """Normalized YouTube Music feedback state."""

    LIKED = "liked"
    DISLIKED = "disliked"
    UNKNOWN = "unknown"


def coerce_like_state(value: Any) -> LikeState | None:
    """Convert booleans and common YouTube Music like labels."""

    if value is None:
        return None
    if isinstance(value, LikeState):
        return value
    if isinstance(value, bool):
        # Connector ``liked`` fields are usually a positive signal with a
        # false default.  False therefore means "no positive signal", not a
        # provider-confirmed dislike; explicit dislike labels are handled
        # below.
        return LikeState.LIKED if value else LikeState.UNKNOWN
    if isinstance(value, (int, float)):
        if value > 0:
            return LikeState.LIKED
        if value < 0:
            return LikeState.DISLIKED
        return LikeState.UNKNOWN

    text = normalize_text(value, max_length=64).casefold().replace("-", "_")
    text = text.replace(" ", "_")
    if not text:
        return None

    if text in {
        "like",
        "liked",
        "true",
        "yes",
        "1",
        "up",
        "thumb_up",
        "thumbsup",
        "like_status_like",
        "like_status_liked",
    } or "like_status_like" in text:
        return LikeState.LIKED
    if text in {
        "dislike",
        "disliked",
        "-1",
        "down",
        "thumb_down",
        "thumbsdown",
        "like_status_dislike",
        "like_status_disliked",
    } or "like_status_dislike" in text:
        return LikeState.DISLIKED
    if text in {"false", "no", "none", "neutral", "unknown", "indifferent", "0", "unrated"}:
        return LikeState.UNKNOWN
    return None


def like_score(value: LikeState | Any) -> float:
    """Return a small numeric signal useful to recommendation ranking."""

    state = coerce_like_state(value)
    if state is LikeState.LIKED:
        return 1.0
    if state is LikeState.DISLIKED:
        return -1.0
    return 0.0


@dataclass(frozen=True, slots=True)
class Track:
    """A normalized music track with no raw scanner payload attached."""

    title: str = ""
    artist: str = ""
    album: str = ""
    video_id: str | None = None
    track_key: str | None = None
    duration_seconds: int | None = None

    def __post_init__(self) -> None:
        title = normalize_text(self.title)
        artist = normalize_text(self.artist)
        album = normalize_text(self.album)
        video_id = normalize_video_id(self.video_id)
        key = normalize_key(self.track_key)
        if not key:
            key = stable_track_key(title, artist, album, video_id)

        duration = self.duration_seconds
        if isinstance(duration, bool):
            duration = None
        elif duration is not None:
            try:
                duration = int(float(duration))
            except (TypeError, ValueError, OverflowError):
                duration = None
            if duration is not None and (duration < 0 or duration > 86_400):
                duration = None

        object.__setattr__(self, "title", title)
        object.__setattr__(self, "artist", artist)
        object.__setattr__(self, "album", album)
        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "track_key", key)
        object.__setattr__(self, "duration_seconds", duration)

    @property
    def id(self) -> str | None:
        """Compatibility alias for consumers that call the video id ``id``."""

        return self.video_id

    @property
    def artist_name(self) -> str:
        return self.artist

    @property
    def artists(self) -> tuple[str, ...]:
        """Compatibility view used by the shared ``TrackRecord`` contract."""

        return tuple(part.strip() for part in self.artist.split(",") if part.strip())

    @property
    def canonical_url(self) -> str | None:
        if not self.video_id:
            return None
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @property
    def url(self) -> str | None:
        # Only a canonical provider URL is synthesized; scanner query
        # parameters are intentionally never retained.
        return self.canonical_url

    def to_dict(self) -> dict[str, Any]:
        """Return only fields that are safe and useful to persist locally."""

        return {
            "track_key": self.track_key,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "video_id": self.video_id,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    """One normalized play-history observation."""

    track_key: str | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    video_id: str | None = None
    played_at: datetime | None = None
    liked: bool = False
    like_state: LikeState | None = None
    source: str = "youtube_music"
    event_key: str | None = None
    duration_seconds: int | None = None
    source_record_id: str | None = None

    def __post_init__(self) -> None:
        title = normalize_text(self.title)
        artist = normalize_text(self.artist)
        album = normalize_text(self.album)
        video_id = normalize_video_id(self.video_id)
        key = normalize_key(self.track_key) or stable_track_key(
            title, artist, album, video_id
        )
        played_at = normalize_timestamp(self.played_at)
        like_state = coerce_like_state(
            self.like_state if self.like_state is not None else self.liked
        )
        liked = like_state is LikeState.LIKED
        source_record_id = normalize_key(self.source_record_id)
        source = normalize_text(self.source, max_length=64).casefold()
        if source not in {
            "youtube_music",
            "youtube_music_scan",
            "ytmusicapi",
            "browser",
            "history",
            "local",
        } and not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", source):
            source = "youtube_music"

        duration = self.duration_seconds
        if isinstance(duration, bool):
            duration = None
        elif duration is not None:
            try:
                duration = int(float(duration))
            except (TypeError, ValueError, OverflowError):
                duration = None
            if duration is not None and (duration < 0 or duration > 86_400):
                duration = None

        event_key = normalize_key(self.event_key)
        if not event_key and key:
            # Do not include title, artist, source URLs, or any raw payload in
            # the event identity.  A source event id is preferred when a
            # provider supplied one; otherwise a missing timestamp
            # intentionally collapses repeated scans of the same track.
            timestamp_part = played_at.isoformat() if played_at else ""
            source_id_part = source_record_id or ""
            canonical = "\x1f".join((key, timestamp_part, source, source_id_part))
            event_key = f"evt:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

        object.__setattr__(self, "track_key", key)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "artist", artist)
        object.__setattr__(self, "album", album)
        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "played_at", played_at)
        object.__setattr__(self, "liked", liked)
        object.__setattr__(self, "like_state", like_state)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "source_record_id", source_record_id)

    @property
    def track(self) -> Track:
        return Track(
            title=self.title,
            artist=self.artist,
            album=self.album,
            video_id=self.video_id,
            track_key=self.track_key,
            duration_seconds=self.duration_seconds,
        )

    @property
    def like_signal(self) -> float:
        return like_score(self.like_state)

    @property
    def disliked(self) -> bool:
        return self.like_state is LikeState.DISLIKED

    @property
    def history_id(self) -> str | None:
        return self.event_key

    @property
    def event_id(self) -> str | None:
        return self.event_key

    @property
    def timestamp(self) -> datetime | None:
        return self.played_at

    def to_dict(self) -> dict[str, Any]:
        """Return a database/API-safe representation of this event."""

        return {
            "event_key": self.event_key,
            "track_key": self.track_key,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "video_id": self.video_id,
            "played_at": self.played_at.isoformat() if self.played_at else None,
            "liked": self.liked,
            "like_state": self.like_state.value if self.like_state else None,
            "source": self.source,
            "duration_seconds": self.duration_seconds,
            "source_record_id": self.source_record_id,
        }


@dataclass(frozen=True, slots=True)
class TrackSummary:
    """Aggregated play, recency, and feedback signals for one track."""

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

    @property
    def recent_played_at(self) -> datetime | None:
        return self.last_played_at

    @property
    def recency(self) -> datetime | None:
        return self.last_played_at

    @property
    def latest_played_at(self) -> datetime | None:
        """Database/recommender spelling for the most recent play."""

        return self.last_played_at

    @property
    def liked_count(self) -> int:
        return self.like_count

    @property
    def like_events(self) -> int:
        return self.like_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_key": self.track_key,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "video_id": self.video_id,
            "play_count": self.play_count,
            "first_played_at": self.first_played_at.isoformat()
            if self.first_played_at
            else None,
            "last_played_at": self.last_played_at.isoformat()
            if self.last_played_at
            else None,
            "latest_played_at": self.last_played_at.isoformat()
            if self.last_played_at
            else None,
            "liked": self.liked,
            "like_signal": self.like_signal,
            "like_count": self.like_count,
            "liked_count": self.like_count,
            "like_events": self.like_count,
            "dislike_count": self.dislike_count,
        }


@dataclass(frozen=True, slots=True)
class HistoryOverview:
    """A compact, recommendation-ready view of local listening history."""

    total_events: int = 0
    unique_tracks: int = 0
    unique_artists: int = 0
    last_played_at: datetime | None = None
    tracks: tuple[TrackSummary, ...] = ()
    recent_events: tuple[HistoryEvent, ...] = ()
    liked_tracks: int = 0
    disliked_tracks: int = 0

    @property
    def total_plays(self) -> int:
        return self.total_events

    @property
    def play_count(self) -> int:
        return self.total_events

    @property
    def track_summaries(self) -> tuple[TrackSummary, ...]:
        return self.tracks

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "total_plays": self.total_events,
            "history_event_count": self.total_events,
            "unique_tracks": self.unique_tracks,
            "unique_artists": self.unique_artists,
            "last_played_at": self.last_played_at.isoformat()
            if self.last_played_at
            else None,
            "liked_tracks": self.liked_tracks,
            "disliked_tracks": self.disliked_tracks,
            "tracks": [track.to_dict() for track in self.tracks],
            "track_stats": [track.to_dict() for track in self.tracks],
            "recent_events": [event.to_dict() for event in self.recent_events],
        }


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Counters and normalized events returned by a history ingestion."""

    received: int = 0
    normalized: int = 0
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0
    events: tuple[HistoryEvent, ...] = ()

    @property
    def inserted(self) -> int:
        return self.stored

    @property
    def persisted(self) -> int:
        return self.stored

    def to_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "normalized": self.normalized,
            "stored": self.stored,
            "inserted": self.stored,
            "persisted": self.stored,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "events": [event.to_dict() for event in self.events],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __len__(self) -> int:
        return self.stored


try:  # Shared contracts are optional while parallel modules are bootstrapped.
    from .contracts import HistoryRecord as ContractHistoryRecord
    from .contracts import TrackRecord
except ImportError:  # pragma: no cover - exercised only in loose-module use.
    ContractHistoryRecord = Any  # type: ignore[assignment,misc]
    TrackRecord = Any  # type: ignore[assignment,misc]


def _contract_value(record: Any, *names: str) -> Any:
    if isinstance(record, Mapping):
        canonical = {
            re.sub(r"[^a-z0-9]+", "", str(key).casefold()): value
            for key, value in record.items()
        }
        for name in names:
            if name in record:
                return record[name]
            found = canonical.get(re.sub(r"[^a-z0-9]+", "", name.casefold()))
            if found is not None:
                return found
        return None
    for name in names:
        try:
            value = getattr(record, name)
        except (AttributeError, TypeError, ValueError):
            continue
        if not callable(value):
            return value
    return None


def _contract_artist_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                item = _contract_value(item, "name", "artist", "title")
            text = normalize_text(item)
            if text and text not in values:
                values.append(text)
        return tuple(values)
    text = normalize_text(value)
    return tuple(part.strip() for part in text.split(",") if part.strip())


def normalize_track(record: Any, *, source: str = "history") -> Any | None:
    """Return a shared-contract ``TrackRecord`` from a safe local value.

    The recommendation and API agents use ``TrackRecord`` as their boundary.
    This adapter intentionally drops arbitrary ``raw``/``metadata`` payloads
    and retains only canonical music fields, so it is safe to use for local
    database rows as well as connector objects.
    """

    if TrackRecord is Any:
        return None
    if isinstance(record, TrackRecord):
        # Fall through to the field-by-field path below.  A connector may
        # have retained a redacted ``raw`` mapping on its contract object;
        # returning it directly would let that payload escape this privacy
        # boundary.
        source = normalize_text(_contract_value(record, "source")) or source
    if isinstance(record, Track):
        title = record.title
        artist_values = record.artists
        album = record.album or None
        video_id = record.video_id
        track_key = record.track_key
        duration = record.duration_seconds
        played_at = None
        liked = False
    elif isinstance(record, HistoryEvent):
        title = record.title
        artist_values = record.track.artists
        album = record.album or None
        video_id = record.video_id
        track_key = record.track_key
        duration = record.duration_seconds
        played_at = record.played_at
        liked = record.liked
    else:
        title = normalize_text(_contract_value(record, "title", "track_title", "name"))
        artist_values = _contract_artist_values(
            _contract_value(record, "artists", "artist", "artist_name", "artistName")
        )
        album_value = _contract_value(record, "album", "album_name", "albumName")
        album = normalize_text(album_value) or None
        video_id = normalize_video_id(
            _contract_value(record, "video_id", "videoId", "youtube_id", "id")
        )
        track_key = normalize_key(_contract_value(record, "track_key", "trackKey"))
        duration = _contract_value(record, "duration_seconds", "durationSeconds", "duration")
        if duration is not None:
            try:
                duration = int(float(duration))
            except (TypeError, ValueError, OverflowError):
                duration = None
        played_at = normalize_timestamp(
            _contract_value(record, "played_at", "playedAt", "timestamp")
        )
        like_state = coerce_like_state(
            _contract_value(record, "like_state", "likeStatus", "liked", "is_liked")
        )
        liked = like_state is LikeState.LIKED

    artist = ", ".join(artist_values)
    track_key = track_key or stable_track_key(title, artist, album or "", video_id)
    if not track_key:
        return None
    normalized_source = normalize_text(source, max_length=64).casefold()
    if normalized_source not in {
        "history",
        "youtube_music",
        "ytmusicapi",
        "browser",
        "related",
    } and not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", normalized_source):
        normalized_source = "history"
    return TrackRecord(
        video_id=video_id,
        title=title,
        artists=artist_values,
        album=album,
        duration_seconds=duration,
        canonical_url=(
            f"https://music.youtube.com/watch?v={video_id}" if video_id else None
        ),
        track_key=track_key,
        artist=artist,
        played_at=played_at,
        liked=liked,
        source=normalized_source,
        raw={},
    )


normalize_track_record = normalize_track


def event_from_raw(
    record: Any,
    *,
    ordinal: int = 0,
    source: str = "youtube_music",
) -> HistoryEvent | None:
    """Build a deterministic local event from one provider-shaped record.

    ``ordinal`` is part of the fallback identity for timestamped positions;
    callers can use it to preserve two otherwise identical entries from a
    source that does not expose provider event ids.  It is never persisted as
    account data and does not affect the normalized track key.
    """

    track = normalize_track(record, source=source)
    if track is None:
        return None
    played_at = normalize_timestamp(track.played_at)
    source_value = normalize_text(source, max_length=64).casefold() or "youtube_music"
    canonical = "\x1f".join(
        (
            track.track_key or "",
            played_at.isoformat() if played_at else "",
            source_value,
            str(int(ordinal)),
        )
    )
    event_key = f"evt:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return HistoryEvent(
        track_key=track.track_key,
        title=track.title,
        artist=track.artist,
        album=track.album or "",
        video_id=track.video_id,
        played_at=played_at,
        liked=track.liked,
        source=source_value,
        event_key=event_key,
        duration_seconds=track.duration_seconds,
    )


# Names used by earlier integrations are retained as harmless aliases.  The
# shared contract's nested ``HistoryRecord`` is preferred when available;
# local normalization continues to use the richer ``HistoryEvent`` above.
HistoryRecord = (
    ContractHistoryRecord if ContractHistoryRecord is not Any else HistoryEvent
)
PlayEvent = HistoryEvent
HistorySummary = HistoryOverview


__all__ = [
    "HistoryEvent",
    "HistoryOverview",
    "HistoryRecord",
    "HistorySummary",
    "IngestResult",
    "LikeState",
    "PlayEvent",
    "Track",
    "TrackRecord",
    "TrackSummary",
    "coerce_like_state",
    "event_from_raw",
    "like_score",
    "normalize_key",
    "normalize_text",
    "normalize_timestamp",
    "normalize_track",
    "normalize_track_record",
    "normalize_video_id",
    "stable_track_key",
    "track_key_for",
]
