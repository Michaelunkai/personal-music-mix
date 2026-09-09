"""Stable, implementation-light contracts for the YouTube Music recommender.

The models in this module are deliberately independent of FastAPI, an ORM, a
browser client, and a particular recommendation algorithm.  They are the
boundary between those pieces.  New code should use timezone-aware UTC
``datetime`` values; ``Timestamp``-typed compatibility fields also accept an
already-serialized ISO-8601 string with a ``Z`` suffix for direct SQLite/API
adapters.

Connector implementations must treat the status and result fields as claims
about observed provider state.  In particular, a configured connector is not
the same thing as an authenticated connector, and a requested playlist write
is not the same thing as a confirmed write.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, TypeAlias


JSONScalar: TypeAlias = None | bool | int | float | str
"""A JSON scalar accepted in connector-safe metadata."""


JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
"""A recursively JSON-serializable value."""


Timestamp: TypeAlias = datetime | str
"""A UTC ``datetime`` or its already-serialized ISO-8601 representation."""


def utc_now_iso() -> str:
    """Return the current UTC instant in a SQLite/JSON-friendly form."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _stable_track_key(
    *, video_id: str | None, title: str, artist: str, url: str | None
) -> str:
    """Build a non-account-derived local identity when a provider key is absent."""

    if video_id:
        return f"video:{video_id}"
    material = "|".join((title.strip().casefold(), artist.strip().casefold(), (url or "").strip()))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"track:{digest}"


def _json_timestamp(value: Timestamp | None) -> str | None:
    """Serialize a timestamp without allowing a naive local clock into JSON."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    return value


class ConnectorMode(str, Enum):
    """How a provider connector reaches YouTube Music."""

    BROWSER_CDP = "browser_cdp"
    API = "api"


class ConnectorState(str, Enum):
    """Observed lifecycle state of a connector."""

    READY = "ready"
    CONNECTING = "connecting"
    AUTH_REQUIRED = "auth_required"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ConnectorCapability(str, Enum):
    """Capabilities confirmed by a live connector, not merely configured."""

    READ_HISTORY = "read_history"
    READ_TRACK_METADATA = "read_track_metadata"
    WRITE_PLAYLIST = "write_playlist"
    VERIFY_PLAYLIST_WRITE = "verify_playlist_write"


class ScanRunStatus(str, Enum):
    """Terminal and in-progress states for a history scan run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlaylistWriteMode(str, Enum):
    """Whether a playlist request may mutate the provider."""

    DRY_RUN = "dry_run"
    APPLY = "apply"


class PlaylistWriteOutcome(str, Enum):
    """Observed outcome of a playlist operation."""

    DRY_RUN = "dry_run"
    APPLIED = "applied"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConnectorError:
    """A safe, structured connector error.

    ``message`` and ``details`` must be sanitized by the connector.  They may
    be returned to a local UI, so they must never contain cookies, access
    tokens, authorization headers, or raw account identifiers.
    """

    code: str
    message: str
    retryable: bool = False
    operation: str | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return only sanitized fields suitable for a local API response."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "operation": self.operation,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    """A point-in-time, evidence-backed connector status snapshot."""

    connector_id: str
    mode: ConnectorMode
    state: ConnectorState
    checked_at: datetime
    capabilities: tuple[ConnectorCapability, ...] = ()
    authenticated: bool = False
    history_access_confirmed: bool = False
    playlist_write_access_confirmed: bool = False
    error: ConnectorError | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a sanitized status payload; capability claims remain explicit."""

        mode = self.mode.value if isinstance(self.mode, Enum) else self.mode
        state = self.state.value if isinstance(self.state, Enum) else self.state
        return {
            "connector_id": self.connector_id,
            "mode": mode,
            "state": state,
            "checked_at": _json_timestamp(self.checked_at),
            "capabilities": [
                capability.value if isinstance(capability, Enum) else capability
                for capability in self.capabilities
            ],
            "authenticated": self.authenticated,
            "history_access_confirmed": self.history_access_confirmed,
            "playlist_write_access_confirmed": self.playlist_write_access_confirmed,
            "message": self.message,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    """Compatibility result for bounded connector scans.

    New connectors should prefer ``HistoryScanResult`` so they can report a
    full status snapshot and scan summary.  This smaller result remains a
    useful adapter boundary for optional/browser connectors that return a
    bounded list in one call.
    """

    status: ConnectorState | str
    items: tuple[TrackRecord, ...] = ()
    message: str | None = None
    code: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    errors: tuple[ConnectorError, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def ok(self) -> bool:
        """Whether the connector returned usable data or a usable partial page."""

        value = self.status.value if isinstance(self.status, Enum) else str(self.status)
        return value in {"ok", "ready", "partial"}

    def to_dict(self) -> dict[str, object]:
        """Return a redaction-ready JSON-shaped connector result."""

        status = self.status.value if isinstance(self.status, Enum) else self.status
        return {
            "status": status,
            "ok": self.ok,
            "items": [item.to_dict() for item in self.items],
            "message": self.message,
            "code": self.code,
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class TrackRecord:
    """Normalized track metadata shared by history and recommendations.

    The ``artist``, ``url``, ``track_key``, ``played_at``, ``liked``, and
    ``raw`` fields are compatibility fields for the ingestion/storage layer.
    They remain provider-neutral and contain no credentials.  New code may
    use ``artists`` and ``canonical_url``; both spellings are synchronized at
    construction time.
    """

    video_id: str | None = None
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str | None = None
    duration_seconds: int | None = None
    is_explicit: bool | None = None
    thumbnail_url: str | None = None
    canonical_url: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    track_key: str | None = None
    artist: str = ""
    url: str | None = None
    played_at: Timestamp | None = None
    liked: bool = False
    source: str = "unknown"
    raw: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)
    event_id: str | None = None
    source_record_id: str | None = None

    def __post_init__(self) -> None:
        artists = tuple(
            artist.strip() for artist in self.artists if isinstance(artist, str) and artist.strip()
        )
        artist = self.artist.strip()
        if not artists and artist:
            artists = tuple(part.strip() for part in artist.split(",") if part.strip())
        if not artist and artists:
            artist = ", ".join(artists)
        if self.canonical_url and not self.url:
            object.__setattr__(self, "url", self.canonical_url)
        elif self.url and not self.canonical_url:
            object.__setattr__(self, "canonical_url", self.url)
        if self.track_key is None:
            object.__setattr__(
                self,
                "track_key",
                _stable_track_key(
                    video_id=self.video_id,
                    title=self.title,
                    artist=artist,
                    url=self.url,
                ),
            )
        object.__setattr__(self, "artists", artists)
        object.__setattr__(self, "artist", artist)

    def to_dict(self) -> dict[str, object]:
        """Return only normalized fields needed by ingestion adapters."""

        return {
            "track_key": self.track_key,
            "title": self.title,
            "artist": self.artist,
            "artists": list(self.artists),
            "album": self.album,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "video_id": self.video_id,
            "played_at": self.played_at,
            "liked": self.liked,
            "duration_seconds": self.duration_seconds,
            "source": self.source,
            "event_id": self.event_id,
            "source_record_id": self.source_record_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One normalized listening event from a provider history source.

    ``event_id``/``captured_at``/``liked`` are retained because the SQLite
    ingestion contract historically used those names.  ``history_id`` and
    ``source_record_id`` are the provider-neutral names used by new code.
    """

    history_id: str = ""
    track: TrackRecord = field(default_factory=TrackRecord)
    played_at: Timestamp | None = None
    source: str = "youtube_music"
    source_record_id: str | None = None
    played_seconds: int | None = None
    completed: bool | None = None
    imported_at: Timestamp | None = None
    event_id: str | None = None
    liked: bool = False
    captured_at: Timestamp | None = None

    def __post_init__(self) -> None:
        if not self.history_id and self.event_id:
            object.__setattr__(self, "history_id", self.event_id)
        elif self.history_id and not self.event_id:
            object.__setattr__(self, "event_id", self.history_id)

    def to_dict(self) -> dict[str, object]:
        """Return a normalized history event without raw provider payloads."""

        return {
            "history_id": self.history_id,
            "event_id": self.event_id,
            "track": self.track.to_dict(),
            "played_at": _json_timestamp(self.played_at),
            "source": self.source,
            "source_record_id": self.source_record_id,
            "played_seconds": self.played_seconds,
            "completed": self.completed,
            "liked": self.liked,
            "captured_at": _json_timestamp(self.captured_at),
            "imported_at": _json_timestamp(self.imported_at),
        }


# The ingestion/storage layer uses the event-oriented spelling.  Keeping an
# alias, rather than a second dataclass, prevents the two record shapes from
# drifting apart and avoids an import cycle with ``app.models``.
HistoryEvent = HistoryRecord


@dataclass(frozen=True, slots=True)
class HistoryScanRequest:
    """Optional bounds and pagination controls for a history scan."""

    connector_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    max_records: int | None = None
    page_size: int = 100
    cursor: str | None = None
    include_track_metadata: bool = True


@dataclass(slots=True)
class ScanRunSummary:
    """Counters and state for one scan, suitable for persistence and polling."""

    run_id: str = ""
    status: ScanRunStatus | str = ScanRunStatus.PENDING
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    source: str = "youtube_music"
    records_scanned: int = 0
    records_emitted: int = 0
    records_skipped: int = 0
    duplicate_records: int = 0
    pages_scanned: int = 0
    complete: bool = False
    errors: tuple[ConnectorError, ...] = ()
    # Compatibility fields used by the first SQLite schema and API adapter.
    items_seen: int = 0
    recommendations_created: int = 0
    message: str = ""
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.records_scanned == 0 and self.items_seen:
            object.__setattr__(self, "records_scanned", self.items_seen)
        elif self.items_seen == 0 and self.records_scanned:
            object.__setattr__(self, "items_seen", self.records_scanned)

    def to_dict(self) -> dict[str, object]:
        """Return a pollable summary without exposing connector secrets."""

        status = self.status.value if isinstance(self.status, Enum) else self.status
        return {
            "run_id": self.run_id,
            "status": status,
            "started_at": _json_timestamp(self.started_at),
            "finished_at": _json_timestamp(self.finished_at),
            "source": self.source,
            "records_scanned": self.records_scanned,
            "records_emitted": self.records_emitted,
            "records_skipped": self.records_skipped,
            "duplicate_records": self.duplicate_records,
            "pages_scanned": self.pages_scanned,
            "complete": self.complete,
            "items_seen": self.items_seen,
            "recommendations_created": self.recommendations_created,
            "message": self.message,
            "error_code": self.error_code,
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class HistoryScanResult:
    """Normalized output of a connector history scan page or run."""

    summary: ScanRunSummary
    records: tuple[HistoryRecord, ...]
    connector: ConnectorStatus
    next_cursor: str | None = None
    has_more: bool = False
    errors: tuple[ConnectorError, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def run_id(self) -> str:
        """Expose the stable run identifier without duplicating state."""

        return self.summary.run_id

    @property
    def connector_status(self) -> ConnectorStatus:
        """Compatibility alias for callers that use an explicit name."""

        return self.connector

    def to_dict(self) -> dict[str, object]:
        """Return a page/run payload suitable for a local API response."""

        return {
            "summary": self.summary.to_dict(),
            "records": [record.to_dict() for record in self.records],
            "connector": self.connector.to_dict(),
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    """One ranked recommendation and its user-visible explanation."""

    recommendation_id: str = ""
    track: TrackRecord = field(default_factory=TrackRecord)
    rank: int = 0
    score: float = 0.0
    reason: str = ""
    generated_at: Timestamp | None = None
    reason_codes: tuple[str, ...] = ()
    based_on_history_ids: tuple[str, ...] = ()
    # Compatibility fields used by the SQLite recommendation table.
    confidence: float | None = None
    reasons: tuple[str, ...] = ()
    source: str = "history"

    def __post_init__(self) -> None:
        reasons = tuple(self.reasons)
        reason = self.reason.strip()
        if not reasons and reason:
            reasons = (reason,)
        if not reason and reasons:
            reason = reasons[0]
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON shape used by the static frontend."""

        return {
            "recommendation_id": self.recommendation_id,
            "track": self.track.to_dict(),
            "rank": self.rank,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "reason_codes": list(self.reason_codes),
            "based_on_history_ids": list(self.based_on_history_ids),
            "generated_at": _json_timestamp(self.generated_at),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    """A recommendation batch associated with a scan snapshot."""

    generated_at: Timestamp | None = None
    recommendations: tuple[RecommendationRecord, ...] = ()
    source_run_id: str | None = None
    model_version: str | None = None
    errors: tuple[ConnectorError, ...] = ()


# The shorter spelling is used by the initial storage/recommendation adapter.
Recommendation = RecommendationRecord


@dataclass(frozen=True, slots=True)
class PlaylistWriteRequest:
    """Explicit playlist intent; the default is always a non-mutating preview."""

    request_id: str = ""
    tracks: tuple[TrackRecord, ...] = ()
    mode: PlaylistWriteMode = PlaylistWriteMode.DRY_RUN
    playlist_id: str | None = None
    playlist_name: str | None = None
    create_if_missing: bool = False


@dataclass(frozen=True, slots=True)
class PlaylistWriteResult:
    """Provider-facing playlist outcome with an explicit confirmation boundary.

    ``applied_track_ids`` and ``provider_confirmed`` may only be populated
    after the connector has observed provider success.  A dry-run preview must
    leave both empty/false even when every requested track is valid locally.
    """

    request_id: str = ""
    mode: PlaylistWriteMode = PlaylistWriteMode.DRY_RUN
    outcome: PlaylistWriteOutcome = PlaylistWriteOutcome.DRY_RUN
    requested_track_ids: tuple[str, ...] = ()
    applied_track_ids: tuple[str, ...] = ()
    rejected_track_ids: tuple[str, ...] = ()
    playlist_id: str | None = None
    playlist_url: str | None = None
    provider_confirmed: bool = False
    verification_method: str | None = None
    completed_at: datetime | None = None
    error: ConnectorError | None = None
    message: str | None = None
    # Compatibility fields used by the local playlist-plan persistence layer.
    provider: str = "local"
    status: str = PlaylistWriteOutcome.DRY_RUN.value
    dry_run: bool = True
    requested_count: int = 0
    confirmed_count: int = 0
    name: str = ""

    def __post_init__(self) -> None:
        requested_track_ids = tuple(self.requested_track_ids)
        applied_track_ids = tuple(self.applied_track_ids)
        rejected_track_ids = tuple(self.rejected_track_ids)
        object.__setattr__(self, "requested_track_ids", requested_track_ids)
        object.__setattr__(self, "applied_track_ids", applied_track_ids)
        object.__setattr__(self, "rejected_track_ids", rejected_track_ids)
        if self.requested_count == 0 and requested_track_ids:
            object.__setattr__(self, "requested_count", len(requested_track_ids))
        if self.confirmed_count == 0 and applied_track_ids:
            object.__setattr__(self, "confirmed_count", len(applied_track_ids))
        outcome_value = self.outcome.value if isinstance(self.outcome, Enum) else str(self.outcome)
        mode_value = self.mode.value if isinstance(self.mode, Enum) else str(self.mode)
        if outcome_value != PlaylistWriteOutcome.DRY_RUN.value and self.status == PlaylistWriteOutcome.DRY_RUN.value:
            object.__setattr__(self, "status", outcome_value)
        if mode_value == PlaylistWriteMode.APPLY.value or outcome_value in {
            PlaylistWriteOutcome.APPLIED.value,
            PlaylistWriteOutcome.PARTIAL.value,
        }:
            object.__setattr__(self, "dry_run", False)
        if self.dry_run:
            # A preview must never be described as provider-confirmed.
            object.__setattr__(self, "provider_confirmed", False)
        elif self.confirmed_count and not self.provider_confirmed:
            object.__setattr__(self, "provider_confirmed", True)

    def to_dict(self) -> dict[str, object]:
        """Return explicit preview/apply facts for the API boundary."""

        mode = self.mode.value if isinstance(self.mode, Enum) else self.mode
        outcome = self.outcome.value if isinstance(self.outcome, Enum) else self.outcome
        return {
            "request_id": self.request_id,
            "mode": mode,
            "outcome": outcome,
            "provider": self.provider,
            "name": self.name,
            "status": self.status,
            "dry_run": self.dry_run,
            "requested_track_ids": list(self.requested_track_ids),
            "applied_track_ids": list(self.applied_track_ids),
            "rejected_track_ids": list(self.rejected_track_ids),
            "requested_count": self.requested_count,
            "confirmed_count": self.confirmed_count,
            "playlist_id": self.playlist_id,
            "playlist_url": self.playlist_url,
            "provider_confirmed": self.provider_confirmed,
            "verification_method": self.verification_method,
            "completed_at": _json_timestamp(self.completed_at),
            "message": self.message,
            "error": self.error.to_dict() if self.error else None,
        }

    @property
    def written_track_ids(self) -> tuple[str, ...]:
        """Readable alias for confirmed/applied track IDs."""

        return self.applied_track_ids


@dataclass(frozen=True, slots=True)
class PlaylistPlan:
    """Local playlist intent used before a provider write is attempted."""

    name: str
    description: str = ""
    recommendations: tuple[RecommendationRecord, ...] = ()
    created_at: Timestamp | None = None

    @property
    def track_keys(self) -> tuple[str, ...]:
        return tuple(item.track.track_key or "" for item in self.recommendations)

    def to_dict(self) -> dict[str, object]:
        """Return a local preview without implying provider-side mutation."""

        return {
            "name": self.name,
            "description": self.description,
            "recommendations": [item.to_dict() for item in self.recommendations],
            "requested_count": len(self.recommendations),
            "created_at": self.created_at,
            "dry_run": True,
        }


class Connector(Protocol):
    """Common status surface implemented by live connectors."""

    @property
    def status(self) -> ConnectorStatus:
        """Return a fresh, sanitized status snapshot."""


class HistoryConnector(Connector, Protocol):
    """Read-only normalized history connector."""

    def scan_history(self, request: HistoryScanRequest) -> HistoryScanResult:
        """Scan provider history without claiming more than the provider shows."""


class RecommendationEngine(Protocol):
    """Algorithm boundary for generating ranked recommendations."""

    def recommend(
        self,
        history: Sequence[HistoryRecord],
        *,
        source_run_id: str | None = None,
        limit: int = 20,
    ) -> RecommendationResult:
        """Generate recommendations from normalized history records."""


class PlaylistWriter(Connector, Protocol):
    """Opt-in playlist writer with provider-side confirmation."""

    def write_playlist(self, request: PlaylistWriteRequest) -> PlaylistWriteResult:
        """Preview or apply a playlist request according to its explicit mode."""


__all__ = [
    "Connector",
    "ConnectorCapability",
    "ConnectorError",
    "ConnectorMode",
    "ConnectorResult",
    "ConnectorState",
    "ConnectorStatus",
    "HistoryConnector",
    "HistoryEvent",
    "HistoryRecord",
    "HistoryScanRequest",
    "HistoryScanResult",
    "JSONScalar",
    "JSONValue",
    "PlaylistPlan",
    "PlaylistWriteMode",
    "PlaylistWriteOutcome",
    "PlaylistWriteRequest",
    "PlaylistWriteResult",
    "PlaylistWriter",
    "Recommendation",
    "RecommendationEngine",
    "RecommendationRecord",
    "RecommendationResult",
    "ScanRunStatus",
    "ScanRunSummary",
    "Timestamp",
    "TrackRecord",
    "utc_now_iso",
]
