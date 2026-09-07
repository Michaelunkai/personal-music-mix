"""Optional :mod:`ytmusicapi` connector.

The connector is deliberately optional.  Importing this module never imports
``ytmusicapi`` and therefore does not make the application depend on an
account credential, a browser session, or the third-party package being
installed.  A caller can provide a browser-exported headers JSON file through
``headers_path`` or one of the supported environment variables.

Only provider responses are turned into :class:`~app.contracts.TrackRecord`
objects.  A missing dependency, invalid headers file, authentication failure,
or permission failure is represented by an explicit
:class:`~app.contracts.ConnectorResult`; no placeholder tracks are returned.
Playlist creation is available only to an explicit, separately gated service;
this module does not invoke it during scans or candidate discovery and never
turns a local preview into a provider-confirmed write.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, TypeAlias

from app.contracts import ConnectorError, ConnectorResult, TrackRecord

__all__ = [
    "HEADER_PATH_ENV_VARS",
    "HeaderConfigurationError",
    "load_headers",
    "YTMusicApiConnector",
    "YTMusicAPIConnector",
    "YtMusicAPIConnector",
    "YouTubeMusicApiConnector",
    "YouTubeMusicConnector",
    "YtMusicApiConnector",
    "load_headers_file",
    "resolve_headers_path",
]


# These names intentionally describe paths, not JSON values.  Keeping secrets
# in the environment itself would make accidental process/environment dumps
# much more likely.
HEADER_PATH_ENV_VARS: tuple[str, ...] = (
    "YTMUSIC_RECOMMENDER_YTMUSICAPI_HEADERS_PATH",
    "YTMUSIC_HEADERS_PATH",
    "YTMUSIC_HEADERS_FILE",
    "YTMUSICAPI_HEADERS_PATH",
    "YTMUSICAPI_HEADERS_FILE",
    "YTMUSIC_API_HEADERS_PATH",
    "YTREC_YTMUSICAPI_HEADERS_PATH",
)

DEFAULT_SCAN_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 25
DEFAULT_RELATED_LIMIT = 25
MAX_RESULT_LIMIT = 500
_MAX_HEADERS_FILE_BYTES = 1_048_576

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "oauth",
    "signature",
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_AUTH_FAILURE_PARTS = (
    "401",
    "unauthoriz",
    "authentication",
    "login required",
    "sign in",
    "not logged in",
    "invalid credentials",
    "credentials expired",
)
_PERMISSION_FAILURE_PARTS = (
    "403",
    "forbidden",
    "permission",
    "access denied",
    "not allowed",
    "insufficient scope",
)

HeadersPath: TypeAlias = str | os.PathLike[str]
TrackSeed: TypeAlias = TrackRecord | Mapping[str, Any] | str


@dataclass(frozen=True, slots=True)
class _Failure:
    """Internal, non-secret description of an operation failure."""

    status: str
    code: str
    message: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class HeaderConfigurationError(ValueError):
    """Raised by :func:`load_headers_file` for an unusable headers file.

    The exception intentionally omits the file contents and any JSON parsing
    detail.  Browser headers contain cookies and bearer-like values, so those
    values must never be copied into an error message.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def resolve_headers_path(
    headers_path: HeadersPath | None = None,
    *,
    headers_file: HeadersPath | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an explicit or environment-provided headers *path*.

    ``headers_path`` is preferred.  ``headers_file`` is accepted as a
    readability alias for callers that use the terminology from ytmusicapi's
    documentation.  No environment value is interpreted as inline JSON.
    """

    if headers_path is not None and headers_file is not None:
        raise HeaderConfigurationError(
            "multiple_headers_paths",
            "Provide only one YouTube Music headers path.",
        )

    selected = headers_path if headers_path is not None else headers_file
    if selected is not None and not isinstance(selected, (str, os.PathLike)):
        # Application wiring passes its immutable Settings object as the
        # connector's positional argument.  Keep this adapter duck-typed so
        # importing the connector does not create a config-module cycle.
        for attribute in ("ytmusicapi_headers_path", "ytmusic_headers_path", "headers_path"):
            try:
                configured = getattr(selected, attribute)
            except (AttributeError, TypeError, ValueError):
                continue
            selected = configured
            break
        else:
            raise HeaderConfigurationError(
                "invalid_headers_path",
                "The YouTube Music headers path is not valid.",
            )
    if selected is not None:
        try:
            selected_text = os.fspath(selected)
        except TypeError as exc:
            raise HeaderConfigurationError(
                "invalid_headers_path",
                "The YouTube Music headers path is not valid.",
            ) from exc
        if not isinstance(selected_text, str) or not selected_text.strip():
            raise HeaderConfigurationError(
                "invalid_headers_path",
                "The YouTube Music headers path is empty.",
            )
        return selected_text

    values = os.environ if environ is None else environ
    for variable in HEADER_PATH_ENV_VARS:
        value = values.get(variable)
        if value is not None and value.strip():
            return value.strip()
    return None


def _header_mapping(payload: Any) -> Mapping[str, Any]:
    """Extract headers from common browser-export JSON shapes."""

    if isinstance(payload, Mapping):
        for wrapper in ("headers", "requestHeaders", "request_headers"):
            if wrapper in payload:
                nested = payload[wrapper]
                if not isinstance(nested, Mapping):
                    raise HeaderConfigurationError(
                        "invalid_headers_shape",
                        "The YouTube Music headers JSON has an invalid headers object.",
                    )
                return nested
        return payload

    # Some browser exports use a list such as
    # [{"name": "cookie", "value": "..."}, ...].
    if isinstance(payload, list):
        result: dict[str, str] = {}
        for entry in payload:
            if not isinstance(entry, Mapping):
                raise HeaderConfigurationError(
                    "invalid_headers_shape",
                    "The YouTube Music headers JSON has an invalid header entry.",
                )
            name = entry.get("name")
            value = entry.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise HeaderConfigurationError(
                    "invalid_headers_shape",
                    "The YouTube Music headers JSON has an invalid header entry.",
                )
            result[name] = value
        return result

    raise HeaderConfigurationError(
        "invalid_headers_shape",
        "The YouTube Music headers JSON must be an object or header list.",
    )


def load_headers_file(path: HeadersPath) -> dict[str, str]:
    """Read and validate a user-provided ytmusicapi headers JSON file.

    The returned mapping is safe to pass to ``YTMusic``.  Header names are
    normalized to lower-case because HTTP header names are case-insensitive,
    while values are preserved exactly for the provider.  At least one
    authentication material header (``cookie`` or ``authorization``) is
    required; otherwise a file containing ordinary public request headers
    could be mistaken for an authenticated connection.
    """

    try:
        file_path = Path(os.fspath(path)).expanduser()
    except (TypeError, ValueError) as exc:
        raise HeaderConfigurationError(
            "invalid_headers_path",
            "The YouTube Music headers path is not valid.",
        ) from exc

    try:
        if not file_path.is_file():
            raise HeaderConfigurationError(
                "headers_file_not_found",
                "The YouTube Music headers file was not found.",
            )
        if file_path.stat().st_size > _MAX_HEADERS_FILE_BYTES:
            raise HeaderConfigurationError(
                "headers_file_too_large",
                "The YouTube Music headers file is too large.",
            )
        text = file_path.read_text(encoding="utf-8-sig")
    except HeaderConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise HeaderConfigurationError(
            "headers_file_unreadable",
            "The YouTube Music headers file could not be read.",
        ) from exc

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HeaderConfigurationError(
            "invalid_headers_json",
            "The YouTube Music headers file is not valid JSON.",
        ) from exc

    raw_headers = _header_mapping(payload)
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise HeaderConfigurationError(
                "invalid_headers_shape",
                "The YouTube Music headers JSON contains an invalid header name.",
            )
        if not isinstance(raw_value, str):
            raise HeaderConfigurationError(
                "invalid_headers_shape",
                "The YouTube Music headers JSON contains a non-text header value.",
            )
        name = raw_name.strip().lower()
        value = raw_value.strip()
        if not _HEADER_NAME_RE.fullmatch(name) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HeaderConfigurationError(
                "invalid_headers_shape",
                "The YouTube Music headers JSON contains an invalid header value.",
            )
        if value:
            headers[name] = value

    has_auth_material = bool(headers.get("cookie") or headers.get("authorization"))
    if not has_auth_material:
        raise HeaderConfigurationError(
            "headers_missing_auth_material",
            "The YouTube Music headers file has no cookie or authorization header.",
        )
    return headers


def _redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy provider metadata without retaining obvious credential material."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        lowered = key_text.casefold()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            continue
        if isinstance(item, Mapping):
            result[key_text] = _redact_mapping(item)
        elif isinstance(item, list):
            result[key_text] = [
                _redact_mapping(entry) if isinstance(entry, Mapping) else entry
                for entry in item
            ]
        else:
            result[key_text] = item
    return result


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    return text or None


def _first_value(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _artist_text(item: Mapping[str, Any]) -> str:
    artists = _first_value(item, "artists", "artist")
    if isinstance(artists, str):
        return artists.strip() or "Unknown artist"
    if isinstance(artists, Mapping):
        return _as_text(_first_value(artists, "name", "title")) or "Unknown artist"
    if isinstance(artists, Sequence) and not isinstance(artists, (str, bytes, bytearray)):
        names: list[str] = []
        for artist in artists:
            if isinstance(artist, Mapping):
                name = _as_text(_first_value(artist, "name", "title"))
            else:
                name = _as_text(artist)
            if name and name not in names:
                names.append(name)
        if names:
            return ", ".join(names)
    return "Unknown artist"


def _album_text(item: Mapping[str, Any]) -> str | None:
    album = _first_value(item, "album", "albumName")
    if isinstance(album, Mapping):
        return _as_text(_first_value(album, "name", "title"))
    return _as_text(album)


def _duration_seconds(item: Mapping[str, Any]) -> int | None:
    value = _first_value(item, "duration_seconds", "durationSeconds", "lengthSeconds")
    if value is not None:
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            seconds = 0
        return seconds if seconds >= 0 else None

    duration = _as_text(_first_value(item, "duration", "length"))
    if not duration:
        return None
    parts = duration.split(":")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    try:
        numbers = [int(part) for part in parts]
        if len(numbers) == 1:
            return numbers[0]
        if len(numbers) == 2:
            return numbers[0] * 60 + numbers[1]
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    except (TypeError, ValueError):
        return None


def _liked_value(item: Mapping[str, Any]) -> bool:
    value = _first_value(item, "liked", "isLiked", "likeStatus")
    if isinstance(value, str):
        return value.casefold() in {"like", "liked", "true", "1"}
    return bool(value)


def _track_from_item(
    item: Any,
    *,
    source: str,
    liked_override: bool | None = None,
    signal: str | None = None,
) -> TrackRecord | None:
    if not isinstance(item, Mapping):
        return None
    result_type = _as_text(item.get("resultType"))
    if result_type and result_type.casefold() not in {"song", "video", "track"}:
        return None

    title = _as_text(_first_value(item, "title", "name"))
    if not title:
        return None
    artist = _artist_text(item)
    video_id = _as_text(_first_value(item, "videoId", "video_id"))
    album = _album_text(item)
    url = _as_text(_first_value(item, "url", "webpageUrl"))
    if url is None and video_id:
        url = f"https://music.youtube.com/watch?v={video_id}"
    played_at = _as_text(
        _first_value(item, "played_at", "playedAt", "played", "date", "timestamp")
    )
    safe_raw = _redact_mapping(item)
    if signal:
        safe_raw["library_signal"] = signal
    return TrackRecord(
        # Let the shared contract derive its stable ``video:<id>`` or hashed
        # fallback identity.  Explicit local spellings here would diverge
        # from browser-ingested records for the same song.
        track_key=None,
        title=title,
        artist=artist,
        album=album,
        url=url,
        video_id=video_id,
        played_at=played_at,
        liked=_liked_value(item) if liked_override is None else liked_override,
        duration_seconds=_duration_seconds(item),
        source=source,
        raw=safe_raw,
    )


def _payload_items(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, Mapping):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                return list(value)
            if isinstance(value, Mapping):
                nested = _payload_items(value, "tracks", "items", "results", "songs")
                if nested:
                    return nested
        if _as_text(_first_value(payload, "title", "name")):
            return [payload]
        return []
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes, Mapping)):
        return list(payload)
    return []


def _status_code(exc: BaseException) -> int | None:
    for owner in (exc, getattr(exc, "response", None)):
        if owner is None:
            continue
        value = getattr(owner, "status_code", None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _signature_mismatch(exc: TypeError) -> bool:
    message = str(exc).casefold()
    return any(
        part in message
        for part in (
            "unexpected keyword",
            "unexpected argument",
            "required positional",
            "positional argument",
            "takes ",
            "got an unexpected",
        )
    )


class YTMusicApiConnector:
    """Read-only connector backed by an optionally installed ``ytmusicapi``.

    ``scan`` and library methods require a user-provided headers file.  Public
    search and related-song discovery can use an anonymous ytmusicapi client
    when the provider supports it, but their successful result never implies
    that account access was established.

    The constructor accepts ``client`` and ``factory`` only to make the
    connector easy to test without network access.  Production callers should
    leave both unset so dependency loading and client construction stay lazy.
    """

    def __init__(
        self,
        headers_path: HeadersPath | Any | None = None,
        *,
        headers_file: HeadersPath | None = None,
        settings: Any | None = None,
        config: Any | None = None,
        environ: Mapping[str, str] | None = None,
        client: Any | None = None,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._client = client
        self._factory = factory
        self._header_path: str | None = None
        self._headers: Mapping[str, str] = MappingProxyType({})
        self._header_failure: _Failure | None = None
        self._client_failure: _Failure | None = None

        if settings is not None and config is not None:
            self._header_failure = _Failure(
                "unavailable",
                "multiple_connector_configs",
                "Provide only one YouTube Music connector settings object.",
            )
        elif settings is not None:
            headers_path = settings if headers_path is None else headers_path
        elif config is not None:
            headers_path = config if headers_path is None else headers_path

        try:
            selected_path = resolve_headers_path(
                headers_path,
                headers_file=headers_file,
                environ=environ,
            )
        except HeaderConfigurationError as exc:
            self._header_failure = _Failure("unavailable", exc.code, exc.message)
        else:
            self._header_path = selected_path
            if selected_path is not None:
                try:
                    loaded = load_headers_file(selected_path)
                except HeaderConfigurationError as exc:
                    self._header_failure = _Failure("unavailable", exc.code, exc.message)
                else:
                    self._headers = MappingProxyType(dict(loaded))

    def __repr__(self) -> str:
        # Never include header values, even if a caller logs the connector.
        return (
            f"{type(self).__name__}(headers_path={self._header_path!r}, "
            f"headers_configured={bool(self._headers)}, client_loaded={self._client is not None})"
        )

    @property
    def headers_path(self) -> str | None:
        """The configured path, never the parsed headers themselves."""

        return self._header_path

    @property
    def headers_configured(self) -> bool:
        return bool(self._headers)

    def status(self) -> ConnectorResult:
        """Probe optional dependency availability without reading account data."""

        client, failure = self._get_client(require_auth=False)
        if failure is not None:
            return self._failure_result(failure, operation="status")
        if client is None:  # Defensive guard; _get_client either returns one or a failure.
            return self._failure_result(
                _Failure("unavailable", "client_unavailable", "The YouTube Music client is unavailable."),
                operation="status",
            )
        return ConnectorResult(
            status="ok",
            message="The optional YouTube Music provider client is available.",
            metadata={
                "provider": "ytmusicapi",
                "headers_configured": self.headers_configured,
            },
        )

    def is_available(self) -> bool:
        """Return whether the optional client can be constructed."""

        return self.status().status == "ok"

    def self_check(self) -> ConnectorResult:
        """Compatibility name for browser connector configuration probes."""

        return self.status()

    dry_run_check = self_check

    def scan(self, limit: int = DEFAULT_SCAN_LIMIT) -> ConnectorResult:
        """Read provider history, returning canonical track-shaped records."""

        bounded, warnings, failure = self._bounded_limit(limit, DEFAULT_SCAN_LIMIT)
        if failure is not None:
            return self._failure_result(failure, operation="scan")
        if bounded == 0:
            return self._empty_result("scan", warnings=warnings)

        client, client_failure = self._get_client(require_auth=True)
        if client_failure is not None:
            return self._failure_result(client_failure, operation="scan")
        if client is None:
            return self._failure_result(
                _Failure("unavailable", "client_unavailable", "The YouTube Music client is unavailable."),
                operation="scan",
            )

        payload, provider_failure = self._call_variants(
            client,
            "get_history",
            (((), {}), ((bounded,), {})),
            operation="scan history",
        )
        if provider_failure is not None:
            return self._failure_result(provider_failure, operation="scan")
        raw_items = _payload_items(payload, "history", "items", "tracks")
        return self._records_result(
            raw_items,
            limit=bounded,
            warnings=warnings,
            operation="scan",
            kind="history",
            deduplicate=False,
        )

    def scan_history(self, limit: int = DEFAULT_SCAN_LIMIT) -> ConnectorResult:
        """Compatibility alias for browser connectors exposing ``scan_history``."""

        return self.scan(limit=limit)

    def fetch_history(
        self,
        limit: int = DEFAULT_SCAN_LIMIT,
        *,
        dry_run: bool | None = None,
        **_: Any,
    ) -> ConnectorResult:
        """Compatibility alias for connectors exposing ``fetch_history``."""

        if dry_run is True:
            return self.self_check()
        return self.scan(limit=limit)

    collect = fetch_history

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> ConnectorResult:
        """Search provider songs without inventing results when unavailable."""

        bounded, warnings, failure = self._bounded_limit(limit, DEFAULT_SEARCH_LIMIT)
        if failure is not None:
            return self._failure_result(failure, operation="search")
        query_text = _as_text(query)
        if not query_text:
            return self._failure_result(
                _Failure("error", "invalid_query", "A non-empty search query is required."),
                operation="search",
            )
        if bounded == 0:
            return self._empty_result("search", warnings=warnings)

        client, client_failure = self._get_client(require_auth=False)
        if client_failure is not None:
            return self._failure_result(client_failure, operation="search")
        if client is None:
            return self._failure_result(
                _Failure("unavailable", "client_unavailable", "The YouTube Music client is unavailable."),
                operation="search",
            )

        payload, provider_failure = self._call_variants(
            client,
            "search",
            (
                ((query_text,), {"filter": "songs", "limit": bounded}),
                ((query_text,), {"filter": "songs"}),
                ((query_text, bounded), {}),
                ((query_text,), {}),
            ),
            operation="search",
        )
        if provider_failure is not None:
            return self._failure_result(provider_failure, operation="search")
        raw_items = _payload_items(payload, "items", "tracks", "results")
        return self._records_result(
            raw_items,
            limit=bounded,
            warnings=warnings,
            operation="search",
            kind="search result",
        )

    def search_tracks(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> ConnectorResult:
        """Compatibility alias for connectors exposing ``search_tracks``."""

        return self.search(query=query, limit=limit)

    def related(self, track: TrackSeed, limit: int = DEFAULT_RELATED_LIMIT) -> ConnectorResult:
        """Return provider-confirmed tracks related to a seed video."""

        bounded, warnings, failure = self._bounded_limit(limit, DEFAULT_RELATED_LIMIT)
        if failure is not None:
            return self._failure_result(failure, operation="related")
        video_id = self._seed_video_id(track)
        if not video_id:
            return self._failure_result(
                _Failure(
                    "error",
                    "seed_video_id_required",
                    "Related-song discovery requires a provider video id.",
                ),
                operation="related",
            )
        if bounded == 0:
            return self._empty_result("related", warnings=warnings, seed_video_id=video_id)

        client, client_failure = self._get_client(require_auth=False)
        if client_failure is not None:
            return self._failure_result(client_failure, operation="related")
        if client is None:
            return self._failure_result(
                _Failure("unavailable", "client_unavailable", "The YouTube Music client is unavailable."),
                operation="related",
            )

        payload, provider_failure = self._call_variants(
            client,
            "get_watch_playlist",
            (
                (( ), {"videoId": video_id, "limit": bounded, "radio": True}),
                (( ), {"videoId": video_id, "limit": bounded}),
                ((video_id, bounded), {}),
                ((video_id,), {}),
            ),
            operation="related songs",
        )
        if provider_failure is not None:
            return self._failure_result(provider_failure, operation="related")
        raw_items = _payload_items(payload, "tracks", "items", "playlist")
        return self._records_result(
            raw_items,
            limit=bounded,
            warnings=warnings,
            operation="related",
            kind="related track",
            exclude_video_id=video_id,
            seed_video_id=video_id,
        )

    def related_tracks(self, track: TrackSeed, limit: int = DEFAULT_RELATED_LIMIT) -> ConnectorResult:
        """Compatibility alias for browser connector related-track calls."""

        return self.related(track=track, limit=limit)

    def get_related_tracks(self, track: TrackSeed, limit: int = DEFAULT_RELATED_LIMIT) -> ConnectorResult:
        return self.related(track=track, limit=limit)

    def discover_related_songs(
        self,
        seed: TrackSeed,
        limit: int = DEFAULT_RELATED_LIMIT,
    ) -> ConnectorResult:
        """Discover related songs from a track, video id, or search phrase.

        When only a phrase is supplied, the seed video id is obtained from an
        actual provider search result before related-song lookup.  If either
        provider operation is unavailable, its explicit error is returned.
        """

        video_id = self._seed_video_id(seed)
        if video_id:
            return self.related(seed, limit=limit)

        if isinstance(seed, Mapping):
            title = _as_text(_first_value(seed, "title", "name"))
            artist = _as_text(_first_value(seed, "artist", "artists"))
            query = " ".join(part for part in (title, artist) if part)
        elif not isinstance(seed, str):
            title = _as_text(getattr(seed, "title", None))
            artist = _as_text(getattr(seed, "artist", None) or getattr(seed, "artists", None))
            query = " ".join(part for part in (title, artist) if part)
        else:
            query = _as_text(seed)
        if not query:
            return self._failure_result(
                _Failure("error", "invalid_seed", "A track, video id, or search phrase is required."),
                operation="related",
            )

        search_result = self.search(query, limit=1)
        if not search_result.ok:
            return search_result
        if not search_result.items or not search_result.items[0].video_id:
            return self._failure_result(
                _Failure(
                    "error",
                    "seed_not_found",
                    "The provider returned no video id for the related-song seed.",
                ),
                operation="related",
            )
        result = self.related(search_result.items[0], limit=limit)
        result.metadata.setdefault("seed_discovered_by", "search")
        return result

    def discover_related(self, seed: TrackSeed, limit: int = DEFAULT_RELATED_LIMIT) -> ConnectorResult:
        """Short compatibility alias for related-song candidate discovery."""

        return self.discover_related_songs(seed=seed, limit=limit)

    def discover_liked_library_signals(
        self,
        limit: int = DEFAULT_RELATED_LIMIT,
        *,
        include_library: bool = True,
    ) -> ConnectorResult:
        """Read liked-song and optional library candidates as explicit signals.

        The ``liked`` endpoint is the authoritative liked signal.  Library
        rows are marked in each record's redacted ``raw`` metadata and are not
        silently promoted to liked rows.  The operation requires configured
        headers because both endpoints are account-scoped.
        """

        bounded, warnings, failure = self._bounded_limit(limit, DEFAULT_RELATED_LIMIT)
        if failure is not None:
            return self._failure_result(failure, operation="liked library")
        if bounded == 0:
            return self._empty_result("liked_library_signals", warnings=warnings)

        client, client_failure = self._get_client(require_auth=True)
        if client_failure is not None:
            return self._failure_result(client_failure, operation="liked library")
        if client is None:
            return self._failure_result(
                _Failure("unavailable", "client_unavailable", "The YouTube Music client is unavailable."),
                operation="liked library",
            )

        errors: list[dict[str, Any]] = []
        records: list[TrackRecord] = []
        liked_count = 0
        library_count = 0
        library_failure: _Failure | None = None

        liked_payload, liked_failure = self._call_variants(
            client,
            "get_liked_songs",
            ((( ), {"limit": bounded}), ((bounded,), {}), (( ), {})),
            operation="liked songs",
        )
        if liked_failure is None:
            liked_raw = _payload_items(liked_payload, "tracks", "items", "songs")
            liked_records, _ = self._convert_records(liked_raw, liked_override=True, signal="liked")
            liked_count = len(liked_records)
            records.extend(liked_records)
        else:
            errors.append(self._failure_dict(liked_failure))

        if include_library:
            library_payload, library_failure = self._call_variants(
                client,
                "get_library_songs",
                ((( ), {"limit": bounded}), ((bounded,), {}), (( ), {})),
                operation="library songs",
            )
            if library_failure is None:
                library_raw = _payload_items(library_payload, "tracks", "items", "songs")
                library_records, _ = self._convert_records(library_raw, signal="library")
                library_count = len(library_records)
                records.extend(library_records)
            else:
                errors.append(self._failure_dict(library_failure))

        deduplicated: list[TrackRecord] = []
        seen: set[str] = set()
        for record in records:
            if record.track_key in seen:
                continue
            seen.add(record.track_key)
            deduplicated.append(record)
            if len(deduplicated) >= bounded:
                break

        metadata: dict[str, Any] = {
            "provider": "ytmusicapi",
            "operation": "liked_library_signals",
            "requested_limit": bounded,
            "liked_count": liked_count,
            "library_count": library_count,
            "returned_count": len(deduplicated),
        }
        if errors:
            metadata["errors"] = errors
        if deduplicated and errors:
            status = "partial"
            code = "liked_library_partial"
            message = "The provider returned only some requested library signals."
        elif errors:
            primary = liked_failure or library_failure
            assert primary is not None
            return self._failure_result(
                primary,
                operation="liked library",
                metadata=metadata,
            )
        else:
            status = "ok"
            code = None
            message = "The provider returned liked and library signals."

        return ConnectorResult(
            status=status,
            items=deduplicated,
            message=message,
            code=code,
            warnings=warnings,
            metadata=metadata,
        )

    def discover_liked_tracks(self, limit: int = DEFAULT_RELATED_LIMIT) -> ConnectorResult:
        """Return liked-song candidates without adding library-only rows."""

        return self.discover_liked_library_signals(limit=limit, include_library=False)

    def discover_liked_library(
        self,
        limit: int = DEFAULT_RELATED_LIMIT,
        *,
        include_library: bool = True,
    ) -> ConnectorResult:
        """Compatibility alias for liked/library candidate discovery."""

        return self.discover_liked_library_signals(limit=limit, include_library=include_library)

    def get_liked_library_signals(
        self,
        limit: int = DEFAULT_RELATED_LIMIT,
        *,
        include_library: bool = True,
    ) -> ConnectorResult:
        return self.discover_liked_library_signals(limit=limit, include_library=include_library)

    liked_tracks = discover_liked_tracks
    related_candidates = discover_related_songs

    def create_playlist(
        self,
        name: str,
        description: str,
        tracks: Sequence[TrackRecord],
    ) -> tuple[str, int]:
        """Create a private playlist and verify the provider response.

        This is intentionally explicit and is only called by the playlist
        service after both configuration and request-level confirmation have
        passed.  The method returns a confirmed provider id/count or raises a
        redacted provider error; it never treats a local preview as a write.
        """

        client, failure = self._get_client(require_auth=True)
        if failure is not None or client is None:
            raise RuntimeError(failure.message if failure else "The authenticated provider client is unavailable.")
        clean_name = " ".join(str(name or "").split())[:120] or "Your High-Confidence Mix"
        clean_description = " ".join(str(description or "").split())[:500]
        video_ids: list[str] = []
        seen: set[str] = set()
        for track in tracks:
            video_id = str(track.video_id or "").strip()
            if video_id and video_id not in seen:
                seen.add(video_id)
                video_ids.append(video_id)
        try:
            playlist_id = client.create_playlist(
                clean_name,
                clean_description,
                privacy_status="PRIVATE",
            )
        except Exception as exc:
            failure = self._provider_failure(exc, "playlist creation")
            raise RuntimeError(failure.message) from None
        if not playlist_id:
            raise RuntimeError("The provider did not return a playlist id after creation.")
        if video_ids:
            try:
                client.add_playlist_items(str(playlist_id), video_ids)
            except Exception as exc:
                failure = self._provider_failure(exc, "playlist item insertion")
                raise RuntimeError(failure.message) from None
        confirmed = len(video_ids)
        verifier = getattr(client, "get_playlist", None)
        if video_ids and not callable(verifier):
            raise RuntimeError(
                "The provider did not expose playlist read-back verification; write confirmation is unavailable."
            )
        if video_ids and callable(verifier):
            try:
                snapshot = verifier(str(playlist_id), limit=max(len(video_ids) + 10, 50)) or {}
            except Exception as exc:
                failure = self._provider_failure(exc, "playlist verification")
                raise RuntimeError(failure.message) from None
            provider_items = snapshot.get("tracks", []) if isinstance(snapshot, Mapping) else []
            provider_ids = {
                str(item.get("videoId"))
                for item in provider_items
                if isinstance(item, Mapping) and item.get("videoId")
            }
            confirmed = len(provider_ids.intersection(video_ids))
            if confirmed == 0:
                raise RuntimeError("The provider returned a playlist but no requested items were visible during verification.")
        return str(playlist_id), confirmed

    def liked_library_signals(
        self,
        limit: int = DEFAULT_RELATED_LIMIT,
        *,
        include_library: bool = True,
    ) -> ConnectorResult:
        return self.discover_liked_library_signals(limit=limit, include_library=include_library)

    def _bounded_limit(
        self,
        limit: Any,
        default: int,
    ) -> tuple[int, list[str], _Failure | None]:
        if limit is None:
            limit = default
        if isinstance(limit, bool) or not isinstance(limit, int):
            return (
                0,
                [],
                _Failure("error", "invalid_limit", "Result limit must be a non-negative integer."),
            )
        if limit < 0:
            return (
                0,
                [],
                _Failure("error", "invalid_limit", "Result limit must be a non-negative integer."),
            )
        if limit > MAX_RESULT_LIMIT:
            return (
                MAX_RESULT_LIMIT,
                [f"Result limit capped at {MAX_RESULT_LIMIT}.",],
                None,
            )
        return limit, [], None

    def _get_client(self, *, require_auth: bool) -> tuple[Any | None, _Failure | None]:
        with self._lock:
            if self._header_failure is not None:
                return None, self._header_failure
            if require_auth and not self._headers:
                return (
                    None,
                    _Failure(
                        "unavailable",
                        "auth_not_configured",
                        "This account-scoped operation requires a headers JSON file.",
                        {"error_class": "auth"},
                    ),
                )
            # A client constructed by status()/search() may be anonymous.
            # Never let that cached object satisfy an account-scoped call.
            if self._client is not None:
                return self._client, None
            if self._client_failure is not None:
                return None, self._client_failure

            module: Any | None = None
            if self._factory is None:
                try:
                    module = importlib.import_module("ytmusicapi")
                except (ImportError, ModuleNotFoundError):
                    failure = _Failure(
                        "unavailable",
                        "dependency_unavailable",
                        "The optional ytmusicapi package is not installed.",
                    )
                    self._client_failure = failure
                    return None, failure
                except Exception:
                    failure = _Failure(
                        "unavailable",
                        "dependency_unavailable",
                        "The optional ytmusicapi package could not be loaded.",
                    )
                    self._client_failure = failure
                    return None, failure

            factory = self._factory or getattr(module, "YTMusic", None)
            if not callable(factory):
                failure = _Failure(
                    "unavailable",
                    "dependency_unavailable",
                    "The installed ytmusicapi package has no YTMusic client.",
                )
                self._client_failure = failure
                return None, failure
            try:
                self._client = factory(dict(self._headers)) if self._headers else factory()
            except TypeError as exc:
                # A small number of wrappers expose ``headers`` as a keyword
                # even though ytmusicapi itself accepts the positional form.
                if self._headers and _signature_mismatch(exc):
                    try:
                        self._client = factory(headers=dict(self._headers))
                    except Exception as retry_exc:  # Provider-specific classes vary by version.
                        return None, self._provider_failure(retry_exc, "client initialization")
                else:
                    return None, self._provider_failure(exc, "client initialization")
            except Exception as exc:  # Provider-specific exception classes vary by version.
                return None, self._provider_failure(exc, "client initialization")
            return self._client, None

    def _call_variants(
        self,
        client: Any,
        method_name: str,
        variants: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]],
        *,
        operation: str,
    ) -> tuple[Any | None, _Failure | None]:
        method = getattr(client, method_name, None)
        if not callable(method):
            return None, _Failure(
                "unavailable",
                "unsupported_operation",
                f"The configured ytmusicapi client does not support {operation}.",
            )

        for index, (args, kwargs) in enumerate(variants):
            try:
                return method(*args, **dict(kwargs)), None
            except TypeError as exc:
                if index + 1 < len(variants) and _signature_mismatch(exc):
                    continue
                return None, self._provider_failure(exc, operation)
            except Exception as exc:  # ytmusicapi changes exception classes between releases.
                return None, self._provider_failure(exc, operation)
        return None, _Failure("error", "provider_error", f"The provider rejected {operation}.")

    def _provider_failure(self, exc: BaseException, operation: str) -> _Failure:
        code = _status_code(exc)
        description = f"{type(exc).__name__} {str(exc)}".casefold()
        if code == 401 or any(part in description for part in _AUTH_FAILURE_PARTS):
            return _Failure(
                "unauthorized",
                "authentication_failed",
                f"The provider did not authenticate the {operation} request.",
                {"error_class": "auth", **({"provider_status": code} if code else {})},
            )
        if code == 403 or any(part in description for part in _PERMISSION_FAILURE_PARTS):
            return _Failure(
                "error",
                "permission_denied",
                f"The provider denied permission for the {operation} request.",
                {"error_class": "permission", **({"provider_status": code} if code else {})},
            )
        return _Failure(
            "error",
            "provider_error",
            f"The provider could not complete the {operation} request.",
            {"provider_status": code} if code else {},
        )

    def _convert_records(
        self,
        raw_items: Iterable[Any],
        *,
        liked_override: bool | None = None,
        signal: str | None = None,
        deduplicate: bool = True,
    ) -> tuple[list[TrackRecord], int]:
        records: list[TrackRecord] = []
        dropped = 0
        seen: set[str] = set()
        for raw in raw_items:
            record = _track_from_item(
                raw,
                source="ytmusicapi",
                liked_override=liked_override,
                signal=signal,
            )
            if record is None:
                dropped += 1
                continue
            if deduplicate and record.track_key in seen:
                continue
            if deduplicate:
                seen.add(record.track_key)
            records.append(record)
        return records, dropped

    def _records_result(
        self,
        raw_items: Iterable[Any],
        *,
        limit: int,
        warnings: Sequence[str],
        operation: str,
        kind: str,
        exclude_video_id: str | None = None,
        deduplicate: bool = True,
        **metadata: Any,
    ) -> ConnectorResult:
        records, dropped = self._convert_records(raw_items, deduplicate=deduplicate)
        if exclude_video_id:
            records = [record for record in records if record.video_id != exclude_video_id]
        records = records[:limit]
        all_metadata: dict[str, Any] = {
            "provider": "ytmusicapi",
            "operation": operation,
            "requested_limit": limit,
            "returned_count": len(records),
            "dropped_count": dropped,
        }
        all_metadata.update(metadata)
        output_warnings = list(warnings)
        if dropped:
            output_warnings.append("Some provider rows did not contain a usable track title.")
        status = "partial" if dropped else "ok"
        return ConnectorResult(
            status=status,
            items=records,
            message=f"The provider returned {len(records)} {kind} record(s).",
            warnings=output_warnings,
            metadata=all_metadata,
        )

    def _empty_result(self, operation: str, *, warnings: Sequence[str] = (), **metadata: Any) -> ConnectorResult:
        return ConnectorResult(
            status="ok",
            items=[],
            message="No results were requested.",
            warnings=list(warnings),
            metadata={
                "provider": "ytmusicapi",
                "operation": operation,
                "requested_limit": 0,
                **metadata,
            },
        )

    def _failure_result(
        self,
        failure: _Failure,
        *,
        operation: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ConnectorResult:
        combined = {
            "provider": "ytmusicapi",
            "operation": operation,
            **dict(failure.metadata),
            **(dict(metadata) if metadata else {}),
        }
        return ConnectorResult(
            status=failure.status,  # type: ignore[arg-type]
            items=[],
            message=failure.message,
            code=failure.code,
            metadata=combined,
            errors=(
                ConnectorError(
                    code=failure.code,
                    message=failure.message,
                    retryable=failure.status in {"unavailable", "unauthorized"},
                    operation=operation,
                    details=dict(failure.metadata),
                ),
            ),
        )

    @staticmethod
    def _failure_dict(failure: _Failure) -> dict[str, Any]:
        return {
            "status": failure.status,
            "code": failure.code,
            "message": failure.message,
            **dict(failure.metadata),
        }

    @staticmethod
    def _seed_video_id(seed: TrackSeed) -> str | None:
        explicit = False
        if isinstance(seed, TrackRecord):
            explicit = seed.video_id is not None
            candidate = seed.video_id or seed.track_key
        elif isinstance(seed, Mapping):
            explicit = any(name in seed and seed[name] is not None for name in ("video_id", "videoId", "youtube_id"))
            candidate = _first_value(seed, "video_id", "videoId", "youtube_id", "track_key")
        elif isinstance(seed, str):
            candidate = seed
        else:
            explicit_value = getattr(seed, "video_id", None)
            explicit = explicit_value is not None
            candidate = explicit_value or getattr(seed, "track_key", None)
        candidate_text = _as_text(candidate)
        if candidate_text and candidate_text.startswith("video:"):
            candidate_text = candidate_text[6:]
        if explicit:
            return (
                candidate_text
                if candidate_text and len(candidate_text) <= 128 and not re.search(r"\s", candidate_text)
                else None
            )
        return candidate_text if candidate_text and _VIDEO_ID_RE.fullmatch(candidate_text) else None


# Naming aliases keep imports tolerant of the spellings used by different
# connector registries while all instances share the same implementation.
load_headers = load_headers_file
YTMusicAPIConnector = YTMusicApiConnector
YtMusicAPIConnector = YTMusicApiConnector
YouTubeMusicApiConnector = YTMusicApiConnector
YouTubeMusicConnector = YTMusicApiConnector
YtMusicApiConnector = YTMusicApiConnector
