"""Read-only YouTube Music history through an existing Chrome CDP session.

The connector deliberately does not implement authentication. It connects to
a browser that the user has already authenticated, reads the rendered history
page, and closes only pages that it created itself. In particular, it never
launches Chrome, asks for credentials, persists cookies, or calls playlist
write APIs.

Playwright is an optional dependency and is imported only when a real
collection is requested. self_check and dry_run are safe to call in
installations that do not have Playwright installed or do not have a CDP
endpoint configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit


HISTORY_URL = "https://music.youtube.com/history"


class ConnectorStatus(str, Enum):
    """Terminal status values returned by the connector."""

    OK = "ok"
    SUCCESS = "ok"
    DRY_RUN = "dry_run"
    MISSING_CDP = "missing_cdp"
    CONNECTION_FAILED = "connection_failed"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    SIGN_IN_REQUIRED = "sign_in_required"
    AUTH_REQUIRED = "sign_in_required"
    BLOCKED = "blocked"
    SELECTOR_CHANGED = "selector_changed"
    NAVIGATION_FAILED = "navigation_failed"
    INVALID_CONFIG = "invalid_config"
    ERROR = "error"


class ConnectorErrorCode(str, Enum):
    """Stable, actionable error codes for callers and telemetry."""

    MISSING_CDP = "missing_cdp"
    INVALID_CDP = "invalid_cdp"
    CONNECTION_FAILED = "connection_failed"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    SIGN_IN_REQUIRED = "sign_in_required"
    BLOCKED_PAGE = "blocked_page"
    SELECTOR_CHANGED = "selector_changed"
    NAVIGATION_FAILED = "navigation_failed"
    NO_BROWSER_CONTEXT = "no_browser_context"
    EXTRACTION_FAILED = "extraction_failed"
    INVALID_CONFIG = "invalid_config"


@dataclass(frozen=True, slots=True)
class ConnectorError:
    """An actionable error without page contents or browser credentials."""

    code: str
    message: str
    action: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "action": self.action,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """A normalized item from the rendered YouTube Music history page.

    played_at is an ISO-8601 UTC string where the page supplied an
    absolute or relative time. time_text retains only the visible,
    user-facing time hint for callers that need to distinguish approximate
    relative times. like_hint is intentionally tri-state because the
    history page often omits like state.
    """

    title: str
    artist: str | None = None
    album: str | None = None
    video_url: str | None = None
    played_at: str | None = None
    like_hint: bool | None = None
    time_text: str | None = None
    source: str = "dom"

    @property
    def liked(self) -> bool | None:
        """Compatibility alias for callers that use liked."""

        return self.like_hint

    @property
    def time(self) -> str | None:
        """Compatibility alias for callers that use time."""

        return self.played_at

    @property
    def url(self) -> str | None:
        """Compatibility alias for callers that use url."""

        return self.video_url

    @property
    def like(self) -> bool | None:
        """Compatibility alias for callers that use like."""

        return self.like_hint

    def to_dict(self) -> dict[str, Any]:
        """Return the stable dictionary shape used by downstream connectors."""

        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "video_url": self.video_url,
            "played_at": self.played_at,
            "time": self.played_at,
            "time_text": self.time_text,
            "like_hint": self.like_hint,
            "liked": self.like_hint,
            "source": self.source,
        }

    as_dict = to_dict


# Common integration aliases. They remain aliases rather than subclasses so
# isinstance checks continue to work across package boundaries.
HistoryItem = HistoryEntry
YouTubeMusicHistoryEntry = HistoryEntry


@dataclass(frozen=True, slots=True)
class YouTubeMusicBrowserConfig:
    """Limits and endpoint settings for a read-only browser collection.

    The only browser setting accepted by this connector is a CDP endpoint.
    Cookies, storage state, passwords, and user-data directories are
    intentionally not part of this configuration.
    """

    cdp_url: str | None = field(default=None, repr=False)
    history_url: str = HISTORY_URL
    max_pages: int = 3
    max_scrolls: int = 12
    max_entries: int = 500
    no_growth_limit: int = 2
    scroll_pause_seconds: float = 0.8
    navigation_timeout_ms: int = 30_000
    dry_run: bool = False
    reuse_existing_page: bool = False
    close_created_page: bool = True

    @property
    def cdp_endpoint(self) -> str | None:
        """Compatibility alias for callers that name the endpoint explicitly."""

        return self.cdp_url

    @property
    def max_items(self) -> int:
        """Compatibility alias for callers that call entries items."""

        return self.max_entries

    @classmethod
    def from_env(cls) -> "YouTubeMusicBrowserConfig":
        """Build a config from non-secret environment variables.

        YTMUSIC_CDP_URL is preferred; the second spelling is retained for
        compatibility with older local deployments. Invalid numeric values
        are left for validation_errors to report rather than raising while
        loading application configuration.
        """

        import os

        def env(*names: str) -> str | None:
            for name in names:
                value = os.getenv(name)
                if value is not None and value.strip():
                    return value.strip()
            return None

        def env_int(default: int, *names: str) -> int:
            value = env(*names)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError:
                return -1

        def env_float(default: float, *names: str) -> float:
            value = env(*names)
            if value is None:
                return default
            try:
                return float(value)
            except ValueError:
                return -1.0

        def env_bool(default: bool, *names: str) -> bool:
            value = env(*names)
            if value is None:
                return default
            return value.casefold() in {"1", "true", "yes", "on"}

        return cls(
            cdp_url=env("YTMUSIC_CDP_URL", "YT_MUSIC_CDP_URL"),
            history_url=env("YTMUSIC_HISTORY_URL") or HISTORY_URL,
            max_pages=env_int(3, "YTMUSIC_MAX_PAGES"),
            max_scrolls=env_int(12, "YTMUSIC_MAX_SCROLLS"),
            max_entries=env_int(500, "YTMUSIC_MAX_ENTRIES"),
            no_growth_limit=env_int(2, "YTMUSIC_NO_GROWTH_LIMIT"),
            scroll_pause_seconds=env_float(0.8, "YTMUSIC_SCROLL_PAUSE_SECONDS"),
            navigation_timeout_ms=env_int(30_000, "YTMUSIC_NAVIGATION_TIMEOUT_MS"),
            dry_run=env_bool(False, "YTMUSIC_DRY_RUN"),
            reuse_existing_page=env_bool(False, "YTMUSIC_REUSE_EXISTING_PAGE"),
            close_created_page=env_bool(True, "YTMUSIC_CLOSE_CREATED_PAGE"),
        )

    def validation_errors(self) -> list[ConnectorError]:
        errors: list[ConnectorError] = []

        if self.cdp_url:
            parts = urlsplit(self.cdp_url.strip())
            if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.netloc:
                errors.append(
                    ConnectorError(
                        ConnectorErrorCode.INVALID_CDP.value,
                        "The configured CDP endpoint is not a valid HTTP(S) or WebSocket URL.",
                        "Set YTMUSIC_CDP_URL to the local Chrome DevTools endpoint, "
                        "for example http://127.0.0.1:9222.",
                    )
                )
            if parts.username or parts.password:
                errors.append(
                    ConnectorError(
                        ConnectorErrorCode.INVALID_CDP.value,
                        "CDP endpoints containing embedded credentials are not accepted.",
                        "Remove user information from YTMUSIC_CDP_URL and use the local "
                        "Chrome debugging endpoint only.",
                    )
                )

        if not self.history_url:
            errors.append(
                ConnectorError(
                    ConnectorErrorCode.INVALID_CONFIG.value,
                    "The YouTube Music history URL is empty.",
                    f"Use {HISTORY_URL!s} or another explicitly configured YouTube Music history URL.",
                )
            )
        else:
            history_parts = urlsplit(self.history_url)
            if history_parts.scheme not in {"http", "https"} or not history_parts.netloc:
                errors.append(
                    ConnectorError(
                        ConnectorErrorCode.INVALID_CONFIG.value,
                        "The configured history URL is not an absolute HTTP(S) URL.",
                        f"Use {HISTORY_URL!s}.",
                    )
                )

        positive_ints = (
            ("max_pages", self.max_pages),
            ("max_entries", self.max_entries),
            ("no_growth_limit", self.no_growth_limit),
            ("navigation_timeout_ms", self.navigation_timeout_ms),
        )
        for name, value in positive_ints:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(
                    ConnectorError(
                        ConnectorErrorCode.INVALID_CONFIG.value,
                        f"{name} must be a positive integer.",
                        f"Set {name} to a value greater than zero.",
                        {"field": name},
                    )
                )

        if (
            not isinstance(self.max_scrolls, int)
            or isinstance(self.max_scrolls, bool)
            or self.max_scrolls < 0
        ):
            errors.append(
                ConnectorError(
                    ConnectorErrorCode.INVALID_CONFIG.value,
                    "max_scrolls must be a non-negative integer.",
                    "Set max_scrolls to zero to read only the initially rendered page, "
                    "or a positive bound to allow lazy history loading.",
                    {"field": "max_scrolls"},
                )
            )

        if (
            not isinstance(self.scroll_pause_seconds, (int, float))
            or isinstance(self.scroll_pause_seconds, bool)
            or self.scroll_pause_seconds < 0
        ):
            errors.append(
                ConnectorError(
                    ConnectorErrorCode.INVALID_CONFIG.value,
                    "scroll_pause_seconds must be zero or a positive number.",
                    "Increase it if history entries load slowly after scrolling.",
                    {"field": "scroll_pause_seconds"},
                )
            )
        return errors


# Shorter configuration name for integrations that do not want the long class
# name in their dependency-injection declarations.
BrowserConnectorConfig = YouTubeMusicBrowserConfig


@dataclass(slots=True)
class ConnectorResult:
    """The complete, explicit result of a history collection attempt."""

    status: ConnectorStatus
    entries: list[HistoryEntry] = field(default_factory=list)
    errors: list[ConnectorError] = field(default_factory=list)
    pages_scanned: int = 0
    scrolls_performed: int = 0
    source_url: str = HISTORY_URL
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ConnectorStatus.OK

    @property
    def actionable_errors(self) -> list[ConnectorError]:
        return self.errors

    @property
    def items(self) -> tuple[HistoryEntry, ...]:
        """Compatibility view for bounded connector callers."""

        return tuple(self.entries)

    @property
    def code(self) -> str | None:
        return self.errors[0].code if self.errors else None

    @property
    def message(self) -> str | None:
        return self.errors[0].message if self.errors else None

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(error.action for error in self.errors if error.action)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "entries": [entry.to_dict() for entry in self.entries],
            "errors": [error.to_dict() for error in self.errors],
            "pages_scanned": self.pages_scanned,
            "scrolls_performed": self.scrolls_performed,
            "source_url": self.source_url,
            "dry_run": self.dry_run,
        }


HistoryConnectorResult = ConnectorResult


def _clean_text(value: Any) -> str | None:
    """Collapse rendered whitespace and discard non-text values."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        for key in ("text", "simpleText", "runs", "name", "value"):
            if key in value:
                return _clean_text(value[key])
        return None
    if isinstance(value, (list, tuple)):
        parts = [_clean_text(item) for item in value]
        joined = " ".join(part for part in parts if part)
        return re.sub(r"\s+", " ", joined).strip() or None
    if isinstance(value, bool):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _first_text(raw: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key in raw:
            value = _clean_text(raw[key])
            if value:
                return value
    return None


def _artist_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("name", "text", "simpleText", "runs"):
            if key in value:
                return _clean_text(value[key])
        return _clean_text(value)
    if isinstance(value, (list, tuple)):
        names = [_artist_name(item) for item in value]
        return ", ".join(name for name in names if name) or None
    return _clean_text(value)


def normalize_video_url(value: Any, *, base_url: str = HISTORY_URL) -> str | None:
    """Normalize a rendered YouTube/YouTube Music link without inventing one."""

    text = _clean_text(value)
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    absolute = urljoin(base_url, text)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None
    host = parts.netloc.casefold().split("@")[-1].split(":")[0]
    allowed_hosts = {
        "music.youtube.com",
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "youtu.be",
    }
    if host not in allowed_hosts:
        return None

    path = parts.path or "/"
    query = parse_qs(parts.query, keep_blank_values=False)
    if host == "youtu.be":
        video_id = path.strip("/").split("/", 1)[0]
        if not video_id:
            return None
        path = "/watch"
        query = {"v": [video_id]}
    elif path == "/watch":
        video_id = (query.get("v") or [None])[0]
        if not video_id:
            return None
        query = {"v": [video_id]}
    elif path.startswith(("/song/", "/browse/", "/shorts/", "/embed/")):
        # song and browse links are useful history identifiers; retain only
        # non-tracking parameters for those paths.
        query = {
            key: values
            for key, values in query.items()
            if key in {"v", "list", "index"} and values
        }
    else:
        return None

    canonical_host = "music.youtube.com"
    canonical_query = urlencode(query, doseq=True)
    return urlunsplit(("https", canonical_host, path, canonical_query, ""))


def _parse_datetime_text(value: Any, *, now: datetime) -> tuple[str | None, str | None]:
    """Return a UTC ISO timestamp and the visible time hint."""

    if value is None:
        return None, None
    time_text = _clean_text(value)
    if not time_text:
        return None, None

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None, time_text
    else:
        parsed = None
        candidate = time_text
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(candidate)
        if parsed is None:
            for pattern in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%b %d, %Y",
                "%B %d, %Y",
            ):
                with contextlib.suppress(ValueError):
                    parsed = datetime.strptime(time_text, pattern)
                    break

        if parsed is None:
            relative = re.fullmatch(
                r"(?P<count>\d+)\s*(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago",
                time_text.casefold(),
            )
            if relative:
                count = int(relative.group("count"))
                unit = relative.group("unit")
                days = {
                    "second": 1 / 86_400,
                    "minute": 1 / 1_440,
                    "hour": 1 / 24,
                    "day": 1,
                    "week": 7,
                    "month": 30,
                    "year": 365,
                }[unit]
                parsed = now - timedelta(days=count * days)
            elif time_text.casefold() == "yesterday":
                parsed = now - timedelta(days=1)
            elif time_text.casefold() in {"today", "just now", "now"}:
                parsed = now

    if parsed is None:
        return None, time_text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z"), time_text


def _parse_like_hint(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = (_clean_text(value) or "").casefold()
    if not text:
        return None
    if text in {"true", "1", "yes", "liked", "remove like", "unlike", "unlike this song"}:
        return True
    if text in {
        "false",
        "0",
        "no",
        "not liked",
        "unliked",
        "like",
        "like this song",
        "like this video",
    }:
        return False
    if "remove like" in text or text.startswith("unlike"):
        return True
    if "not liked" in text or text.startswith("like"):
        return False
    return None


def normalize_history_entry(
    raw: Mapping[str, Any],
    *,
    now: datetime | None = None,
    base_url: str = HISTORY_URL,
    source: str | None = None,
) -> HistoryEntry | None:
    """Normalize one DOM or structured-data candidate.

    Candidates without a title are ignored. Returning no item is preferable
    to fabricating a title from a video identifier or returning navigation
    chrome as a history record.
    """

    if not isinstance(raw, Mapping):
        return None
    now = now or datetime.now(timezone.utc)

    title = _first_text(raw, "title", "name", "track", "song")
    artist = _artist_name(raw.get("artist") or raw.get("byArtist") or raw.get("artists"))
    aria_label = _clean_text(raw.get("ariaLabel") or raw.get("aria_label"))
    if not title and aria_label:
        aria_parts = re.split(r"\s+by\s+", aria_label, maxsplit=1, flags=re.IGNORECASE)
        title = aria_parts[0].strip() or None
        if artist is None and len(aria_parts) > 1:
            artist = aria_parts[1].strip() or None
    album = _artist_name(raw.get("album") or raw.get("inAlbum"))
    video_url = None
    for key in ("video_url", "videoUrl", "url", "href", "link"):
        if key in raw:
            video_url = normalize_video_url(raw[key], base_url=base_url)
            if video_url:
                break

    if not title:
        return None

    if artist and " • " in artist:
        artist_parts = [part.strip() for part in artist.split(" • ") if part.strip()]
        artist = artist_parts[0] if artist_parts else None
        if album is None and len(artist_parts) > 1:
            candidate_album = artist_parts[1]
            if not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", candidate_album):
                album = candidate_album
    if album and " • " in album:
        album = album.split(" • ", 1)[0].strip() or None

    time_value = None
    for key in ("played_at", "playedAt", "timestamp", "datePublished", "uploadDate", "time", "datetime"):
        if key in raw:
            time_value = raw[key]
            break
    played_at, time_text = _parse_datetime_text(time_value, now=now)

    like_value = None
    for key in ("like_hint", "likeHint", "liked", "like", "ariaPressed", "dataLiked"):
        if key in raw:
            like_value = raw[key]
            break

    return HistoryEntry(
        title=title,
        artist=artist,
        album=album,
        video_url=video_url,
        played_at=played_at,
        like_hint=_parse_like_hint(like_value),
        time_text=time_text,
        source=source or _clean_text(raw.get("_source")) or "dom",
    )


def _entry_key(entry: HistoryEntry) -> tuple[str, ...]:
    """Deduplicate repeated DOM nodes while retaining repeated plays."""

    if entry.video_url and entry.played_at:
        return ("url-time", entry.video_url, entry.played_at)
    if entry.video_url:
        return ("url", entry.video_url, entry.title.casefold())
    return (
        "text",
        entry.title.casefold(),
        (entry.artist or "").casefold(),
        (entry.album or "").casefold(),
        entry.played_at or "",
    )


def _entry_is_duplicate(
    entry: HistoryEntry,
    *,
    existing: Iterable[HistoryEntry],
    keys: set[tuple[str, ...]],
) -> bool:
    key = _entry_key(entry)
    if key in keys:
        return True
    return any(
        entry.video_url
        and entry.video_url == previous.video_url
        and entry.title.casefold() == previous.title.casefold()
        and (not entry.played_at or not previous.played_at)
        for previous in existing
    )


def _iter_structured_candidates(
    value: Any,
    *,
    _depth: int = 0,
) -> Iterable[Mapping[str, Any]]:
    """Extract conservative music candidates from JSON-LD/initial-data maps."""

    if _depth > 10:
        return
    if isinstance(value, Mapping):
        candidate: dict[str, Any] = {}
        if "name" in value:
            candidate["title"] = value.get("name")
        elif "title" in value:
            candidate["title"] = value.get("title")
        for source_key, target_key in (
            ("url", "url"),
            ("videoUrl", "video_url"),
            ("videoId", "video_url"),
            ("byArtist", "artist"),
            ("artist", "artist"),
            ("inAlbum", "album"),
            ("album", "album"),
            ("datePublished", "played_at"),
            ("uploadDate", "played_at"),
            ("timestamp", "played_at"),
            ("like", "like_hint"),
            ("liked", "like_hint"),
        ):
            if source_key in value:
                source_value = value[source_key]
                if source_key == "videoId" and source_value:
                    source_value = f"https://music.youtube.com/watch?v={source_value}"
                candidate[target_key] = source_value
        has_media_context = bool(
            candidate.get("video_url")
            or value.get("@type") in {"MusicRecording", "VideoObject", "MusicAlbum"}
            or any(
                key in value
                for key in ("byArtist", "artist", "inAlbum", "album", "videoId", "videoUrl")
            )
        )
        if candidate.get("title") and has_media_context:
            candidate["_source"] = "structured_data"
            yield candidate
        for child in value.values():
            yield from _iter_structured_candidates(child, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_structured_candidates(child, _depth=_depth + 1)


def normalize_history_payload(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    base_url: str = HISTORY_URL,
    max_entries: int | None = None,
) -> list[HistoryEntry]:
    """Normalize and deduplicate one browser extraction payload."""

    if not isinstance(payload, Mapping):
        return []
    if max_entries is not None and max_entries <= 0:
        return []
    candidates: list[tuple[Mapping[str, Any], str]] = []
    for item in payload.get("items") or ():
        if isinstance(item, Mapping):
            candidates.append((item, "dom"))
    structured_values = payload.get("structured") or ()
    if isinstance(structured_values, Mapping):
        structured_values = (structured_values,)
    for structured in structured_values:
        candidates.extend(
            (candidate, "structured_data")
            for candidate in _iter_structured_candidates(structured)
        )

    result: list[HistoryEntry] = []
    seen: set[tuple[str, ...]] = set()
    seen_entries: list[HistoryEntry] = []
    for raw, source in candidates:
        entry = normalize_history_entry(
            raw,
            now=now,
            base_url=base_url,
            source=source,
        )
        if entry is None:
            continue
        key = _entry_key(entry)
        if _entry_is_duplicate(entry, existing=seen_entries, keys=seen):
            continue
        seen.add(key)
        seen_entries.append(entry)
        result.append(entry)
        if max_entries is not None and len(result) >= max_entries:
            break
    return result


# This script reads only the rendered DOM and bounded structured data. It
# deliberately returns selected fields rather than document HTML or browser
# storage, so connector results cannot accidentally persist cookies or tokens.
DOM_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => {
    if (value === null || value === undefined) return null;
    const text = String(value).replace(/\s+/g, " ").trim();
    return text || null;
  };
  const textOf = (node) => clean(node && (node.innerText || node.textContent));
  const attr = (node, name) => clean(node && node.getAttribute(name));
  const firstText = (node, selectors) => {
    for (const selector of selectors) {
      const child = node && node.querySelector(selector);
      const value = textOf(child) || attr(child, "title") || attr(child, "aria-label");
      if (value) return value;
    }
    return null;
  };
  const firstHref = (node) => {
    const links = node ? Array.from(node.querySelectorAll("a[href]")) : [];
    const preferred = links.find((link) => {
      const href = link.getAttribute("href") || "";
      return /\/(watch|song|browse)\//.test(href) || href.includes("youtu.be/");
    });
    return attr(preferred || links[0], "href");
  };
  const itemSelectors = [
    "ytmusic-history-item-renderer",
    "ytmusic-responsive-list-item-renderer",
    "ytmusic-two-row-item-renderer",
    "ytmusic-shelf-renderer ytmusic-responsive-list-item-renderer",
    "[data-history-item]",
    "[data-video-id]",
    "ytmusic-player-queue-item",
    ".ytmusic-history-item-renderer"
  ];
  const nodes = [];
  const seenNodes = new Set();
  for (const selector of itemSelectors) {
    for (const node of document.querySelectorAll(selector)) {
      if (!seenNodes.has(node)) {
        seenNodes.add(node);
        nodes.push(node);
      }
    }
  }
  const items = nodes.map((node) => {
    const likeButton = node.querySelector(
      "[aria-label*='like' i], [title*='like' i], [data-liked], [aria-pressed]"
    );
    const datetimeNode = node.querySelector(
      "time[datetime], [datetime], time, [class*='time' i], [class*='date' i], [aria-label*='ago' i]"
    );
    const ariaLabel = attr(node, "aria-label");
    const ariaTitle = ariaLabel && ariaLabel.split(/\s+by\s+/i)[0];
    const ariaArtist = ariaLabel && ariaLabel.match(/\s+by\s+(.+)$/i)?.[1];
    const dataVideoId = attr(node, "data-video-id");
    const title = firstText(node, [
      "[data-title]",
      ".title",
      ".ytmusic-item-title",
      "yt-formatted-string.title",
      "[class*='title' i]"
    ]) || attr(node, "data-title") || attr(node, "title") || ariaTitle;
    const artist = firstText(node, [
      ".subtitle",
      ".byline",
      ".secondary",
      "[class*='artist' i]",
      "[class*='subtitle' i]"
    ]) || ariaArtist;
    const album = firstText(node, [
      ".album",
      "[class*='album' i]"
    ]);
    const href = firstHref(node) ||
      (dataVideoId ? "/watch?v=" + encodeURIComponent(dataVideoId) : null);
    const time = attr(datetimeNode, "datetime") || textOf(datetimeNode) ||
      attr(node, "data-played-at") || attr(node, "data-timestamp");
    const likeHint = attr(likeButton, "aria-pressed") ||
      attr(likeButton, "data-liked") || attr(likeButton, "aria-label") ||
      attr(likeButton, "title");
    return {
      title,
      artist,
      album,
      href,
      time,
      likeHint,
      ariaLabel,
      text: textOf(node),
      _source: "dom"
    };
  }).filter((item) => item.title || item.href);

  const structured = [];
  const addJson = (text, source) => {
    if (!text || text.length > 2_000_000) return;
    try {
      const value = JSON.parse(text);
      structured.push({ source, value });
    } catch (_) {
      // A partially loaded script is not a connector failure.
    }
  };
  for (const script of document.querySelectorAll("script[type='application/ld+json']")) {
    addJson(script.textContent, "jsonld");
  }
  const walk = (value, depth) => {
    if (depth > 8 || structured.length > 250 || value === null || value === undefined) return;
    if (Array.isArray(value)) {
      for (const child of value) walk(child, depth + 1);
      return;
    }
    if (typeof value !== "object") return;
    const videoId = value.videoId || value.video_id;
    const titleValue = value.title || value.name;
    if (videoId && titleValue) {
      structured.push({
        source: "yt_initial_data",
        value: {
          title: titleValue,
          videoId,
          artist: value.artist || value.byline || value.shortBylineText,
          album: value.album || value.inAlbum,
          played_at: value.timestamp || value.datePublished,
          like_hint: value.like || value.liked
        }
      });
    }
    for (const key of Object.keys(value)) {
      if (key === "responseContext" || key === "trackingParams") continue;
      walk(value[key], depth + 1);
    }
  };
  for (const script of document.scripts) {
    const id = script.id || "";
    const text = script.textContent || "";
    if (id === "ytInitialData" || id === "initial-data" ||
        (text.length > 20 && text.length < 2_000_000 && text.trim().startsWith("{") &&
         text.includes("\"videoId\""))) {
      try {
        walk(JSON.parse(text), 0);
      } catch (_) {
        // Ignore scripts that are not complete JSON.
      }
    }
  }
  return {
    items,
    structured: structured.map((entry) => entry.value),
    pageText: clean(document.body && document.body.innerText)?.slice(0, 12000) || "",
    title: clean(document.title) || "",
    url: window.location.href
  };
}
"""


SCROLL_SCRIPT = r"""
() => {
  const before = window.scrollY || window.pageYOffset || 0;
  const viewport = window.innerHeight || 800;
  const height = Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0
  );
  window.scrollTo(0, Math.max(0, height - viewport));
  const after = window.scrollY || window.pageYOffset || 0;
  return { before, after, height };
}
"""


NEXT_SELECTORS = (
    "ytmusic-paginator button[aria-label*='next' i]",
    "ytmusic-paginator tp-yt-paper-button[aria-label*='next' i]",
    "button[aria-label='Next']",
    "button[title='Next']",
    "[data-next-page]",
)


_SIGN_IN_MARKERS = (
    "sign in to youtube",
    "sign in to youtube music",
    "sign in to view your history",
    "sign in to see your history",
    "sign in with google",
    "sign in",
    "you’re signed out",
    "you're signed out",
    "session has expired",
)
_BLOCKED_MARKERS = (
    "access denied",
    "unusual traffic",
    "automated queries",
    "not a robot",
    "temporarily blocked",
    "temporarily unavailable",
    "too many requests",
    "you have been blocked",
    "request blocked",
    "consent required",
    "enable javascript and cookies",
)
_EMPTY_HISTORY_MARKERS = (
    "your history is empty",
    "your watch history is empty",
    "history is empty",
    "watch history is empty",
    "history will show up here",
    "history will appear here",
    "no history",
    "nothing here",
    "no watch history",
)


def _safe_exception_text(error: BaseException) -> str:
    """Avoid echoing endpoint query tokens or credential-like values."""

    text = re.sub(
        r"(?i)(cookie|password|passwd|token|authorization|secret)=([^&\s]+)",
        r"\1=[redacted]",
        str(error),
    )
    text = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:https?|wss?)://[^\s]+",
        lambda match: _redact_endpoint(match.group(0)) or "<url>",
        text,
    )
    return text[:500] or error.__class__.__name__


def _redact_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.netloc:
        return "<configured>"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class YouTubeMusicBrowserConnector:
    """Read YouTube Music history from a pre-authenticated Chrome session."""

    def __init__(
        self,
        config: YouTubeMusicBrowserConfig | None = None,
        *,
        cdp_url: str | None = None,
        cdp_endpoint: str | None = None,
        history_url: str | None = None,
        max_pages: int | None = None,
        max_scrolls: int | None = None,
        max_scroll_attempts: int | None = None,
        max_entries: int | None = None,
        max_items: int | None = None,
        dry_run: bool | None = None,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        base = config or YouTubeMusicBrowserConfig.from_env()
        overrides: dict[str, Any] = {}
        endpoint = cdp_url if cdp_url is not None else cdp_endpoint
        if endpoint is not None:
            overrides["cdp_url"] = endpoint
        if history_url is not None:
            overrides["history_url"] = history_url
        if max_pages is not None:
            overrides["max_pages"] = max_pages
        if max_scrolls is not None:
            overrides["max_scrolls"] = max_scrolls
        if max_scroll_attempts is not None:
            overrides["max_scrolls"] = max_scroll_attempts
        entry_limit = max_entries if max_entries is not None else max_items
        if entry_limit is not None:
            overrides["max_entries"] = entry_limit
        if dry_run is not None:
            overrides["dry_run"] = dry_run
        if overrides:
            base = replace(base, **overrides)
        self.config = base
        # This hook is intentionally limited to the optional Playwright
        # factory; it cannot inject cookies, storage state, or credentials.
        self._playwright_factory = playwright_factory

    def self_check(self) -> ConnectorResult:
        """Perform a safe local configuration check without browser I/O."""

        errors = self.config.validation_errors()
        warnings: list[ConnectorError] = []
        if not self.config.cdp_url:
            warnings.append(
                ConnectorError(
                    ConnectorErrorCode.MISSING_CDP.value,
                    "No Chrome DevTools Protocol endpoint is configured.",
                    "Start Chrome with remote debugging and set YTMUSIC_CDP_URL "
                    "(for example http://127.0.0.1:9222), then retry a real collection.",
                )
            )
        if self._playwright_factory is None and not _playwright_available():
            warnings.append(
                ConnectorError(
                    ConnectorErrorCode.PLAYWRIGHT_UNAVAILABLE.value,
                    "The optional Playwright package is not installed.",
                    "Install Playwright in the application environment before a real collection; "
                    "dry-run remains safe without it.",
                )
            )
        return ConnectorResult(
            status=ConnectorStatus.DRY_RUN,
            errors=[*errors, *warnings],
            source_url=self.config.history_url,
            dry_run=True,
        )

    # Explicit alias for callers that use a verb rather than a noun.
    dry_run_check = self_check
    check = self_check

    def status(self) -> ConnectorResult:
        """Return the safe local status probe without touching browser state."""

        return self.self_check()

    def fetch_history(
        self,
        limit: int | None = None,
        *,
        dry_run: bool | None = None,
    ) -> ConnectorResult:
        """Synchronously collect history, or return a no-I/O dry-run result."""

        if dry_run is True or (dry_run is None and self.config.dry_run):
            return self.self_check()
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                return ConnectorResult(
                    status=ConnectorStatus.INVALID_CONFIG,
                    errors=[
                        ConnectorError(
                            ConnectorErrorCode.INVALID_CONFIG.value,
                            "The history limit must be a non-negative integer.",
                            "Call fetch_history with limit=0 or a positive bounded integer.",
                        )
                    ],
                    source_url=self.config.history_url,
                )
            if limit == 0:
                return ConnectorResult(
                    status=ConnectorStatus.OK,
                    source_url=self.config.history_url,
                )
            if limit != self.config.max_entries:
                limited = type(self)(
                    replace(self.config, max_entries=limit),
                    playwright_factory=self._playwright_factory,
                )
                return limited.fetch_history(dry_run=False)
        return _run_sync(self.fetch_history_async())

    # Common connector naming aliases.
    collect = fetch_history
    fetch = fetch_history
    scan = fetch_history
    scan_history = fetch_history
    get_history = fetch_history
    read_history = fetch_history
    run = fetch_history

    async def fetch_history_async(
        self,
        limit: int | None = None,
        *,
        dry_run: bool | None = None,
    ) -> ConnectorResult:
        """Asynchronously collect rendered history through Chrome CDP."""

        if dry_run is True or (dry_run is None and self.config.dry_run):
            return self.self_check()
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                return ConnectorResult(
                    status=ConnectorStatus.INVALID_CONFIG,
                    errors=[
                        ConnectorError(
                            ConnectorErrorCode.INVALID_CONFIG.value,
                            "The history limit must be a non-negative integer.",
                            "Call fetch_history_async with limit=0 or a positive bounded integer.",
                        )
                    ],
                    source_url=self.config.history_url,
                )
            if limit == 0:
                return ConnectorResult(
                    status=ConnectorStatus.OK,
                    source_url=self.config.history_url,
                )
            if limit != self.config.max_entries:
                limited = type(self)(
                    replace(self.config, max_entries=limit),
                    playwright_factory=self._playwright_factory,
                )
                return await limited.fetch_history_async(dry_run=False)

        config_errors = self.config.validation_errors()
        if config_errors:
            return ConnectorResult(
                status=ConnectorStatus.INVALID_CONFIG,
                errors=config_errors,
                source_url=self.config.history_url,
            )
        if not self.config.cdp_url:
            return ConnectorResult(
                status=ConnectorStatus.MISSING_CDP,
                errors=[
                    ConnectorError(
                        ConnectorErrorCode.MISSING_CDP.value,
                        "A Chrome DevTools Protocol endpoint is required for a real collection.",
                        "Start the already-authenticated Chrome profile with remote debugging "
                        "and set YTMUSIC_CDP_URL. This connector never accepts passwords or cookies.",
                    )
                ],
                source_url=self.config.history_url,
            )

        try:
            async_playwright = self._playwright_factory
            if async_playwright is None:
                from playwright.async_api import async_playwright as imported_async_playwright

                async_playwright = imported_async_playwright
        except (ImportError, ModuleNotFoundError):
            return ConnectorResult(
                status=ConnectorStatus.PLAYWRIGHT_UNAVAILABLE,
                errors=[
                    ConnectorError(
                        ConnectorErrorCode.PLAYWRIGHT_UNAVAILABLE.value,
                        "The optional Playwright dependency is not available.",
                        "Install Playwright in the same Python environment as the app, "
                        "then retry. No browser credentials are needed by this connector.",
                    )
                ],
                source_url=self.config.history_url,
            )

        manager: Any = None
        browser: Any = None
        page: Any = None
        created_page = False
        pages_scanned = 0
        scrolls_performed = 0
        try:
            manager = async_playwright()
            manager = await _maybe_await(manager)
            manager = await _maybe_await(manager.start())
            browser = await _maybe_await(
                manager.chromium.connect_over_cdp(self.config.cdp_url)
            )
            context = await self._select_context(browser)
            if context is None:
                return ConnectorResult(
                    status=ConnectorStatus.CONNECTION_FAILED,
                    errors=[
                        ConnectorError(
                            ConnectorErrorCode.NO_BROWSER_CONTEXT.value,
                            "Chrome CDP connected but exposed no browser context.",
                            "Keep the authenticated Chrome profile open and verify that "
                            "the configured endpoint is the browser's /json/version endpoint.",
                            {"cdp_endpoint": _redact_endpoint(self.config.cdp_url)},
                        )
                    ],
                    source_url=self.config.history_url,
                )

            page, created_page = await self._select_page(context)
            if page is None:
                return ConnectorResult(
                    status=ConnectorStatus.CONNECTION_FAILED,
                    errors=[
                        ConnectorError(
                            ConnectorErrorCode.CONNECTION_FAILED.value,
                            "The connected Chrome context has no usable page.",
                            "Leave an authenticated Chrome tab open or allow the connector "
                            "to create a temporary tab in that same context.",
                        )
                    ],
                    source_url=self.config.history_url,
                )

            try:
                response = await _maybe_await(
                    page.goto(
                        self.config.history_url,
                        wait_until="domcontentloaded",
                        timeout=self.config.navigation_timeout_ms,
                    )
                )
            except TypeError:
                # Small Playwright-compatible test doubles sometimes expose
                # only goto(url); real Playwright uses the bounded form above.
                response = await _maybe_await(page.goto(self.config.history_url))
            status_code = self._response_status(response)
            if status_code in {401, 407}:
                return self._sign_in_result(status_code)
            if status_code in {403, 429}:
                return self._blocked_result(status_code)
            if status_code is not None and status_code >= 400:
                return ConnectorResult(
                    status=ConnectorStatus.NAVIGATION_FAILED,
                    errors=[
                        ConnectorError(
                            ConnectorErrorCode.NAVIGATION_FAILED.value,
                            f"YouTube Music history navigation returned HTTP {status_code}.",
                            "Open the history URL in the same authenticated Chrome profile, "
                            "resolve any browser interstitial, and retry.",
                            {"http_status": status_code},
                        )
                    ],
                    source_url=self.config.history_url,
                )

            await self._wait_after_scroll(page)
            first_payload = await self._extract_payload(page)
            page_state = self._page_state(page, first_payload)
            page_status = self._classify_page(
                page_state["url"],
                page_state["title"],
                page_state["text"],
            )
            if page_status == ConnectorStatus.SIGN_IN_REQUIRED:
                initial_entries = normalize_history_payload(
                    first_payload,
                    base_url=self.config.history_url,
                    max_entries=1,
                )
                if not initial_entries:
                    return self._sign_in_result()
                page_status = ConnectorStatus.OK
            if page_status == ConnectorStatus.BLOCKED:
                return self._blocked_result()

            entries, pages_scanned, scrolls_performed = await self._scan(
                page,
                initial_payload=first_payload,
            )
            if not entries:
                if self._is_empty_history(page_state["text"]):
                    page_status = ConnectorStatus.OK
                else:
                    return ConnectorResult(
                        status=ConnectorStatus.SELECTOR_CHANGED,
                        errors=[
                            ConnectorError(
                                ConnectorErrorCode.SELECTOR_CHANGED.value,
                                "The history page loaded, but no supported rendered history entries were found.",
                                "Inspect the current rendered DOM for history item elements or update "
                                "the connector selectors. The connector returned no fabricated fallback data.",
                            )
                        ],
                        pages_scanned=pages_scanned,
                        scrolls_performed=scrolls_performed,
                        source_url=self.config.history_url,
                    )

            return ConnectorResult(
                status=ConnectorStatus.OK,
                entries=entries,
                pages_scanned=pages_scanned,
                scrolls_performed=scrolls_performed,
                source_url=self.config.history_url,
            )
        except Exception as error:
            message = _safe_exception_text(error)
            lowered = message.casefold()
            if "timeout" in lowered or "navigation" in lowered:
                status = ConnectorStatus.NAVIGATION_FAILED
                code = ConnectorErrorCode.NAVIGATION_FAILED.value
                action = (
                    "Confirm that the history page loads in the connected Chrome session, "
                    "then increase navigation_timeout_ms if the page is slow."
                )
            elif "connect" in lowered or "cdp" in lowered or "websocket" in lowered:
                status = ConnectorStatus.CONNECTION_FAILED
                code = ConnectorErrorCode.CONNECTION_FAILED.value
                action = (
                    "Verify that Chrome is running with CDP enabled and that YTMUSIC_CDP_URL "
                    "points to its reachable endpoint."
                )
            else:
                status = ConnectorStatus.ERROR
                code = ConnectorErrorCode.EXTRACTION_FAILED.value
                action = (
                    "Review the rendered history page and connector diagnostics; no fallback "
                    "data was generated."
                )
            return ConnectorResult(
                status=status,
                errors=[
                    ConnectorError(
                        code,
                        f"YouTube Music history collection failed: {message}",
                        action,
                    )
                ],
                pages_scanned=pages_scanned,
                scrolls_performed=scrolls_performed,
                source_url=self.config.history_url,
            )
        finally:
            if created_page and page is not None and self.config.close_created_page:
                with contextlib.suppress(Exception):
                    await _maybe_await(page.close())
            # Do not call browser.close(): with CDP that can terminate the
            # user's existing Chrome process. Stopping Playwright disconnects
            # its driver while leaving the remote browser/session intact.
            if manager is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(manager.stop())

    collect_async = fetch_history_async

    async def _select_context(self, browser: Any) -> Any | None:
        contexts = getattr(browser, "contexts", None)
        if callable(contexts):
            contexts = await _maybe_await(contexts())
        contexts = list(contexts or ())
        if not contexts:
            return None
        for context in contexts:
            pages = getattr(context, "pages", None)
            if callable(pages):
                pages = await _maybe_await(pages())
            if pages:
                return context
        return contexts[0]

    async def _select_page(self, context: Any) -> tuple[Any | None, bool]:
        pages = getattr(context, "pages", None)
        if callable(pages):
            pages = await _maybe_await(pages())
        pages = list(pages or ())
        for page in pages:
            page_url = getattr(page, "url", "")
            if callable(page_url):
                page_url = await _maybe_await(page_url())
            if self._is_history_page_url(str(page_url or "")):
                return page, False
        new_page = getattr(context, "new_page", None)
        if callable(new_page):
            return await _maybe_await(new_page()), True
        return None, False

    def _is_history_page_url(self, value: str) -> bool:
        """Only reuse an existing page that is already the requested history tab."""

        try:
            expected = urlsplit(self.config.history_url)
            actual = urlsplit(value)
        except ValueError:
            return False
        expected_path = expected.path.rstrip("/") or "/"
        actual_path = actual.path.rstrip("/") or "/"
        return (
            actual.scheme.casefold() == expected.scheme.casefold()
            and actual.netloc.casefold() == expected.netloc.casefold()
            and actual_path == expected_path
        )

    async def _extract_payload(self, page: Any) -> Mapping[str, Any]:
        payload = await _maybe_await(page.evaluate(DOM_EXTRACTION_SCRIPT))
        if isinstance(payload, Mapping):
            return payload
        return {"items": [], "structured": [], "pageText": ""}

    async def _wait_after_scroll(self, page: Any) -> None:
        if self.config.scroll_pause_seconds <= 0:
            return
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            await _maybe_await(waiter(int(self.config.scroll_pause_seconds * 1000)))
        else:
            await asyncio.sleep(self.config.scroll_pause_seconds)

    async def _scroll_once(self, page: Any) -> bool:
        try:
            result = await _maybe_await(page.evaluate(SCROLL_SCRIPT))
        except Exception:
            return False
        if not isinstance(result, Mapping):
            return True
        before = result.get("before")
        after = result.get("after")
        height = result.get("height")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            if after > before:
                return True
            if isinstance(height, (int, float)) and height <= before + 2:
                return False
            return False
        return True

    async def _scan(
        self,
        page: Any,
        *,
        initial_payload: Mapping[str, Any],
    ) -> tuple[list[HistoryEntry], int, int]:
        entries: list[HistoryEntry] = []
        seen: set[tuple[str, ...]] = set()
        pages_scanned = 0
        scrolls_performed = 0
        payload: Mapping[str, Any] = initial_payload
        scan_now = datetime.now(timezone.utc)

        for page_number in range(self.config.max_pages):
            pages_scanned += 1
            no_growth = 0
            for scroll_number in range(self.config.max_scrolls + 1):
                if scroll_number > 0:
                    scrolls_performed += 1
                    await self._scroll_once(page)
                    await self._wait_after_scroll(page)
                    payload = await self._extract_payload(page)
                batch = normalize_history_payload(
                    payload,
                    now=scan_now,
                    base_url=self.config.history_url,
                    max_entries=self.config.max_entries,
                )
                added = 0
                for entry in batch:
                    key = _entry_key(entry)
                    if _entry_is_duplicate(entry, existing=entries, keys=seen):
                        continue
                    seen.add(key)
                    entries.append(entry)
                    added += 1
                    if len(entries) >= self.config.max_entries:
                        return entries, pages_scanned, scrolls_performed
                if added:
                    no_growth = 0
                else:
                    no_growth += 1
                if no_growth >= self.config.no_growth_limit:
                    break

            if page_number + 1 >= self.config.max_pages:
                break
            if not await self._click_next_page(page):
                break
            await self._wait_after_scroll(page)
            payload = await self._extract_payload(page)

        return entries, pages_scanned, scrolls_performed

    async def _click_next_page(self, page: Any) -> bool:
        for selector in NEXT_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await _maybe_await(locator.count())
            except Exception:
                continue
            for index in range(int(count or 0)):
                candidate = locator.nth(index)
                try:
                    visible = await _maybe_await(candidate.is_visible())
                    if not visible:
                        continue
                    disabled = await _maybe_await(candidate.get_attribute("disabled"))
                    aria_disabled = await _maybe_await(candidate.get_attribute("aria-disabled"))
                    if disabled is not None or str(aria_disabled).casefold() == "true":
                        continue
                    try:
                        click_result = candidate.click(
                            timeout=self.config.navigation_timeout_ms
                        )
                    except TypeError:
                        click_result = candidate.click()
                    await _maybe_await(click_result)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _response_status(response: Any) -> int | None:
        if response is None:
            return None
        value = getattr(response, "status", None)
        if callable(value):
            value = value()
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _page_state(page: Any, payload: Mapping[str, Any]) -> dict[str, str]:
        url = _clean_text(payload.get("url")) or _clean_text(getattr(page, "url", None)) or ""
        title = _clean_text(payload.get("title")) or ""
        text = _clean_text(payload.get("pageText")) or ""
        return {"url": url, "title": title, "text": text}

    @staticmethod
    def _classify_page(url: str, title: str, text: str) -> ConnectorStatus:
        lowered_url = url.casefold()
        lowered = f"{title}\n{text}".casefold()
        if "accounts.google." in lowered_url or "servicelogin" in lowered_url:
            return ConnectorStatus.SIGN_IN_REQUIRED
        if any(marker in lowered for marker in _SIGN_IN_MARKERS):
            return ConnectorStatus.SIGN_IN_REQUIRED
        if any(marker in lowered for marker in _BLOCKED_MARKERS):
            return ConnectorStatus.BLOCKED
        return ConnectorStatus.OK

    @staticmethod
    def _is_empty_history(text: str) -> bool:
        lowered = text.casefold()
        return any(marker in lowered for marker in _EMPTY_HISTORY_MARKERS)

    def _sign_in_result(self, status_code: int | None = None) -> ConnectorResult:
        details = {"http_status": status_code} if status_code is not None else {}
        return ConnectorResult(
            status=ConnectorStatus.SIGN_IN_REQUIRED,
            errors=[
                ConnectorError(
                    ConnectorErrorCode.SIGN_IN_REQUIRED.value,
                    "The connected Chrome session is not signed in to YouTube Music.",
                    "Sign in manually in the existing Chrome profile, open the history page "
                    "once, and retry. This connector never asks for or stores passwords/cookies.",
                    details,
                )
            ],
            source_url=self.config.history_url,
        )

    def _blocked_result(self, status_code: int | None = None) -> ConnectorResult:
        details = {"http_status": status_code} if status_code is not None else {}
        return ConnectorResult(
            status=ConnectorStatus.BLOCKED,
            errors=[
                ConnectorError(
                    ConnectorErrorCode.BLOCKED_PAGE.value,
                    "YouTube Music returned a blocked, consent, or anti-automation page.",
                    "Resolve the interstitial in the already-authenticated Chrome session "
                    "and retry; do not provide credentials or cookies to this connector.",
                    details,
                )
            ],
            source_url=self.config.history_url,
        )


# Friendly class aliases for existing application wiring.
YouTubeMusicHistoryConnector = YouTubeMusicBrowserConnector
YtMusicBrowserConnector = YouTubeMusicBrowserConnector
YTMusicBrowserConnector = YouTubeMusicBrowserConnector
YouTubeMusicConnector = YouTubeMusicBrowserConnector
ConnectorConfig = YouTubeMusicBrowserConfig


def _playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def run_self_check(
    config: YouTubeMusicBrowserConfig | None = None,
) -> ConnectorResult:
    """Run the safe no-browser self-check."""

    return YouTubeMusicBrowserConnector(config).self_check()


def _run_sync(awaitable: Awaitable[ConnectorResult]) -> ConnectorResult:
    """Run async collection from sync code, including an active event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    # A caller may invoke the sync connector from a notebook or async web
    # handler. A dedicated thread avoids nested-loop errors without touching
    # the caller's event loop.
    result: list[ConnectorResult] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=runner, name="ytmusic-browser-connector", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


__all__ = [
    "BrowserConnectorConfig",
    "ConnectorError",
    "ConnectorErrorCode",
    "ConnectorConfig",
    "ConnectorResult",
    "ConnectorStatus",
    "DOM_EXTRACTION_SCRIPT",
    "HISTORY_URL",
    "HistoryConnectorResult",
    "HistoryEntry",
    "HistoryItem",
    "YtMusicBrowserConnector",
    "YTMusicBrowserConnector",
    "YouTubeMusicConnector",
    "YouTubeMusicBrowserConfig",
    "YouTubeMusicBrowserConnector",
    "YouTubeMusicHistoryConnector",
    "YouTubeMusicHistoryEntry",
    "normalize_history_entry",
    "normalize_history_payload",
    "normalize_video_url",
    "run_self_check",
]
