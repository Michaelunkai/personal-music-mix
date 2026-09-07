"""Application settings for the local YouTube Music recommender.

The module deliberately has no logging and does not read credential contents.
It only normalises configuration values supplied by the process environment.
That makes importing this module safe in a web process, a scheduler, or a
command-line tool, while still allowing callers to inject an environment in
tests.

The preferred environment-variable prefix is ``YTMUSIC_RECOMMENDER_``.  The
unprefixed names documented in ``.env.example`` are accepted as aliases so
the app is easy to run locally and can coexist with existing deployments.
Prefixed values take precedence when both forms are present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_DATABASE_PATH: Final[Path] = Path("data/ytmusic_recommender.sqlite3")
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8000
DEFAULT_YOUTUBE_MUSIC_HISTORY_URL: Final[str] = (
    "https://music.youtube.com/history"
)
DEFAULT_SCAN_LIMIT: Final[int] = 1_000
DEFAULT_RECOMMENDATION_LIMIT: Final[int] = 20
DEFAULT_SCHEDULER_INTERVAL_SECONDS: Final[int] = 3_600
DEFAULT_ENABLE_PLAYLIST_WRITES: Final[bool] = False
DEFAULT_CORS_ORIGINS: Final[tuple[str, ...]] = (
    "http://127.0.0.1:8000",
    "http://localhost:8000",
)
DEFAULT_LOG_LEVEL: Final[str] = "INFO"

# A database scan should remain bounded even when an accidental environment
# value is extremely large.  The upper limits are generous for a local app and
# keep memory/time usage predictable for malformed deployments.
MAX_SCAN_LIMIT: Final[int] = 1_000_000
MAX_RECOMMENDATION_LIMIT: Final[int] = 100_000
MAX_SCHEDULER_INTERVAL_SECONDS: Final[int] = 2_147_483_647

_TRUE_VALUES: Final[frozenset[str]] = frozenset(
    {"1", "true", "t", "yes", "y", "on", "enabled", "enable"}
)
_FALSE_VALUES: Final[frozenset[str]] = frozenset(
    {"0", "false", "f", "no", "n", "off", "disabled", "disable"}
)

# The first name is the preferred, collision-resistant form.  The remaining
# names are intentionally kept small and predictable for local deployments and
# compatibility with simple process managers.
_ENV_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "database_path": (
        "YTMUSIC_RECOMMENDER_DATABASE_PATH",
        "DATABASE_PATH",
        "YTREC_DATABASE_PATH",
        "DB_PATH",
    ),
    "host": (
        "YTMUSIC_RECOMMENDER_HOST",
        "HOST",
        "YTREC_HOST",
    ),
    "port": (
        "YTMUSIC_RECOMMENDER_PORT",
        "PORT",
        "YTREC_PORT",
    ),
    "youtube_music_history_url": (
        "YTMUSIC_RECOMMENDER_YOUTUBE_MUSIC_HISTORY_URL",
        "YOUTUBE_MUSIC_HISTORY_URL",
        "YTMUSIC_HISTORY_URL",
        "YTREC_HISTORY_URL",
    ),
    "chrome_cdp_url": (
        "YTMUSIC_RECOMMENDER_CHROME_CDP_URL",
        "CHROME_CDP_URL",
        "YTMUSIC_CDP_URL",
        "YTREC_CHROME_CDP_URL",
    ),
    "ytmusicapi_headers_path": (
        "YTMUSIC_RECOMMENDER_YTMUSICAPI_HEADERS_PATH",
        "YTMUSICAPI_HEADERS_PATH",
        "YTMUSIC_API_HEADERS_PATH",
        "YTMUSIC_HEADERS_PATH",
        "YTREC_YTMUSICAPI_HEADERS_PATH",
    ),
    "scan_limit": (
        "YTMUSIC_RECOMMENDER_SCAN_LIMIT",
        "SCAN_LIMIT",
        "YTREC_SCAN_LIMIT",
    ),
    "recommendation_limit": (
        "YTMUSIC_RECOMMENDER_RECOMMENDATION_LIMIT",
        "RECOMMENDATION_LIMIT",
        "RECOMMENDATIONS_LIMIT",
        "YTREC_RECOMMENDATION_LIMIT",
    ),
    "scheduler_interval_seconds": (
        "YTMUSIC_RECOMMENDER_SCHEDULER_INTERVAL_SECONDS",
        "SCHEDULER_INTERVAL_SECONDS",
        "SCHEDULER_INTERVAL",
        "YTREC_SCHEDULER_INTERVAL_SECONDS",
    ),
    "scheduler_interval_minutes": (
        "YTMUSIC_RECOMMENDER_SCHEDULER_INTERVAL_MINUTES",
        "SCHEDULER_INTERVAL_MINUTES",
        "YTREC_SCHEDULER_INTERVAL_MINUTES",
    ),
    "enable_playlist_writes": (
        "YTMUSIC_RECOMMENDER_ENABLE_PLAYLIST_WRITES",
        "ENABLE_PLAYLIST_WRITES",
        "PLAYLIST_WRITE_ENABLED",
        "ALLOW_PLAYLIST_WRITES",
        "PLAYLIST_WRITE_OPT_IN",
        "YTREC_ENABLE_PLAYLIST_WRITES",
    ),
    "browser_bridge_token": (
        "YTMUSIC_RECOMMENDER_BROWSER_BRIDGE_TOKEN",
        "BROWSER_BRIDGE_TOKEN",
        "YTMUSIC_BROWSER_BRIDGE_TOKEN",
    ),
    "cors_origins": (
        "YTMUSIC_RECOMMENDER_CORS_ORIGINS",
        "CORS_ORIGINS",
    ),
    "log_level": (
        "YTMUSIC_RECOMMENDER_LOG_LEVEL",
        "LOG_LEVEL",
    ),
}


class SettingsError(ValueError):
    """Raised when an environment value cannot form safe application settings."""


def _environment_value(
    environment: Mapping[str, str | None], setting_name: str
) -> str | None:
    """Return the first configured alias without exposing its value.

    An explicit ``None`` is treated as unset for the benefit of test mappings;
    ``os.environ`` itself only contains strings.  Empty strings are preserved
    so required values fail validation instead of silently becoming defaults.
    """

    for name in _ENV_NAMES[setting_name]:
        value = environment.get(name)
        if value is not None:
            return value
    return None


def _value_or_default(
    environment: Mapping[str, str | None],
    setting_name: str,
    default: object,
) -> object:
    value = _environment_value(environment, setting_name)
    return default if value is None else value


def _text(value: object, setting_name: str) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{setting_name} must be text")
    return value.strip()


def _parse_integer(
    value: object,
    setting_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Parse an integer without accepting floats, booleans, or junk suffixes."""

    if isinstance(value, bool):
        raise SettingsError(f"{setting_name} must be an integer")

    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or any(character not in "+-0123456789" for character in text):
            raise SettingsError(f"{setting_name} must be an integer")
        if text.count("+") + text.count("-") > 1 or (
            ("+" in text or "-" in text) and text[0] not in "+-"
        ):
            raise SettingsError(f"{setting_name} must be an integer")
        try:
            result = int(text, 10)
        except ValueError as error:  # defensive: the shape was checked above
            raise SettingsError(f"{setting_name} must be an integer") from error
    else:
        raise SettingsError(f"{setting_name} must be an integer")

    if not minimum <= result <= maximum:
        raise SettingsError(
            f"{setting_name} must be between {minimum} and {maximum}"
        )
    return result


def _parse_boolean(value: object, setting_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise SettingsError(f"{setting_name} must be a boolean")

    normalised = value.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise SettingsError(
        f"{setting_name} must be one of true/false, yes/no, on/off, or 1/0"
    )


def _parse_optional_secret(value: object, setting_name: str) -> str | None:
    """Validate a secret-like value without ever echoing it in an error."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{setting_name} must be text")
    secret = value.strip()
    if not secret:
        return None
    if any(character.isspace() for character in secret) or len(secret) > 4_096:
        raise SettingsError(f"{setting_name} contains invalid characters")
    return secret


def _normalise_path(
    value: object,
    setting_name: str,
    *,
    require_file: bool,
) -> Path:
    """Expand and validate a local path without creating or deleting anything."""

    if isinstance(value, Path):
        path_text = os.fspath(value)
    elif isinstance(value, str):
        path_text = value.strip()
    else:
        raise SettingsError(f"{setting_name} must be a path")

    if not path_text or "\x00" in path_text:
        raise SettingsError(f"{setting_name} must be a non-empty path")

    try:
        path = Path(os.path.expandvars(path_text)).expanduser()
    except (OSError, RuntimeError, ValueError):
        raise SettingsError(f"{setting_name} is not a valid local path") from None

    # A database file may be created later, but it must never resolve to an
    # existing directory.  A headers file is stricter: if configured, it must
    # already exist as a regular file so authentication failures are explicit.
    try:
        if path.exists():
            if not path.is_file():
                raise SettingsError(f"{setting_name} must point to a file")
        elif require_file:
            raise SettingsError(
                f"{setting_name} does not point to an existing file"
            )

        parent = path.parent
        if parent.exists() and not parent.is_dir():
            raise SettingsError(f"{setting_name} has a non-directory parent")
    except OSError:
        raise SettingsError(f"{setting_name} cannot be inspected") from None

    return path


def _parse_database_path(value: object) -> Path | str:
    if isinstance(value, str) and value.strip() == ":memory:":
        # SQLite's in-memory database is useful for tests and remains local.
        return ":memory:"
    return _normalise_path(value, "database_path", require_file=False)


def _parse_optional_headers_path(value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _normalise_path(
        value,
        "ytmusicapi_headers_path",
        require_file=True,
    )


def _parse_host(value: object) -> str:
    host = _text(value, "host")
    if not host:
        raise SettingsError("host must be non-empty")
    if len(host) > 253 or any(character.isspace() for character in host):
        raise SettingsError("host must be a valid host name or IP address")
    if "/" in host or "\\" in host or "@" in host:
        raise SettingsError("host must be a valid host name or IP address")
    # Brackets belong to URL notation, not a bind host.  Raw IPv6 (for
    # example, ::1) remains valid.
    if host.startswith("[") or host.endswith("]"):
        raise SettingsError("host must be a valid host name or IP address")
    return host


def _parse_url(
    value: object,
    setting_name: str,
    *,
    schemes: frozenset[str],
) -> str:
    url = _text(value, setting_name)
    if not url or any(character.isspace() for character in url):
        raise SettingsError(f"{setting_name} must be a valid URL")

    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        # Accessing .port forces urlsplit to reject malformed numeric ports.
        _ = parts.port
    except ValueError:
        raise SettingsError(f"{setting_name} must be a valid URL") from None

    if parts.scheme.lower() not in schemes or not parts.netloc or not hostname:
        raise SettingsError(f"{setting_name} must be a valid URL")
    # Credentials in URLs are easy to leak through error messages and process
    # diagnostics.  CDP does not need them for the supported local workflow.
    if parts.username is not None or parts.password is not None:
        raise SettingsError(f"{setting_name} must not contain URL credentials")
    return url


def _parse_optional_cdp_url(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _parse_url(
        value,
        "chrome_cdp_url",
        schemes=frozenset({"http", "https", "ws", "wss"}),
    )


def _parse_cors_origins(value: object) -> tuple[str, ...]:
    """Parse explicit browser origins while rejecting wildcard exposure."""

    if value is None:
        return DEFAULT_CORS_ORIGINS

    if isinstance(value, str):
        raw_origins = value.split(",")
    elif isinstance(value, (tuple, list)):
        raw_origins = list(value)
    else:
        raise SettingsError("cors_origins must be a comma-separated list")

    origins: list[str] = []
    for raw_origin in raw_origins:
        if not isinstance(raw_origin, str):
            raise SettingsError("cors_origins must contain text values")
        origin = raw_origin.strip()
        if not origin:
            continue
        parsed = _parse_url(
            origin,
            "cors_origins",
            schemes=frozenset({"http", "https", "chrome-extension"}),
        )
        if parsed.endswith("/"):
            parsed = parsed[:-1]
        parts = urlsplit(parsed)
        if parts.path or parts.query or parts.fragment:
            raise SettingsError("cors_origins must contain origins, not paths")
        origins.append(parsed)

    if not origins:
        return DEFAULT_CORS_ORIGINS
    return tuple(dict.fromkeys(origins))


def _parse_log_level(value: object) -> str:
    level = _text(value, "log_level").upper()
    if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
        raise SettingsError("log_level must be a standard logging level")
    return level


def _safe_url_for_diagnostics(value: str) -> str:
    """Drop query/fragment material before a URL enters diagnostic output."""

    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _scheduler_interval_from_environment(
    environment: Mapping[str, str | None],
) -> object:
    """Resolve the current seconds-based setting, with minute compatibility."""

    seconds = _environment_value(environment, "scheduler_interval_seconds")
    if seconds is not None:
        return seconds

    minutes = _environment_value(environment, "scheduler_interval_minutes")
    if minutes is None:
        return DEFAULT_SCHEDULER_INTERVAL_SECONDS

    return (
        _parse_integer(
            minutes,
            "scheduler_interval_minutes",
            minimum=0,
            maximum=MAX_SCHEDULER_INTERVAL_SECONDS // 60,
        )
        * 60
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable settings shared by application modules.

    ``enable_playlist_writes`` is intentionally false by default.  A caller
    must opt in explicitly through the environment, and downstream code must
    still confirm that the connected browser/API actually supports the write.
    """

    database_path: Path | str = DEFAULT_DATABASE_PATH
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    youtube_music_history_url: str = DEFAULT_YOUTUBE_MUSIC_HISTORY_URL
    chrome_cdp_url: str | None = None
    ytmusicapi_headers_path: Path | None = None
    scan_limit: int = DEFAULT_SCAN_LIMIT
    recommendation_limit: int = DEFAULT_RECOMMENDATION_LIMIT
    scheduler_interval_seconds: int = DEFAULT_SCHEDULER_INTERVAL_SECONDS
    enable_playlist_writes: bool = DEFAULT_ENABLE_PLAYLIST_WRITES
    browser_bridge_token: str | None = None
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    log_level: str = DEFAULT_LOG_LEVEL

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", _parse_database_path(self.database_path))
        object.__setattr__(self, "host", _parse_host(self.host))
        object.__setattr__(
            self,
            "port",
            _parse_integer(self.port, "port", minimum=1, maximum=65_535),
        )
        object.__setattr__(
            self,
            "youtube_music_history_url",
            _parse_url(
                self.youtube_music_history_url,
                "youtube_music_history_url",
                schemes=frozenset({"http", "https"}),
            ),
        )
        parsed_cdp_url = _parse_optional_cdp_url(self.chrome_cdp_url)
        object.__setattr__(self, "chrome_cdp_url", parsed_cdp_url)
        parsed_headers_path = _parse_optional_headers_path(self.ytmusicapi_headers_path)
        object.__setattr__(self, "ytmusicapi_headers_path", parsed_headers_path)
        if parsed_cdp_url and parsed_headers_path:
            raise SettingsError(
                "chrome_cdp_url and ytmusicapi_headers_path are mutually exclusive; "
                "choose one authenticated account connector"
            )
        object.__setattr__(
            self,
            "scan_limit",
            _parse_integer(
                self.scan_limit,
                "scan_limit",
                minimum=1,
                maximum=MAX_SCAN_LIMIT,
            ),
        )
        object.__setattr__(
            self,
            "recommendation_limit",
            _parse_integer(
                self.recommendation_limit,
                "recommendation_limit",
                minimum=1,
                maximum=MAX_RECOMMENDATION_LIMIT,
            ),
        )
        object.__setattr__(
            self,
            "scheduler_interval_seconds",
            _parse_integer(
                self.scheduler_interval_seconds,
                "scheduler_interval_seconds",
                minimum=0,
                maximum=MAX_SCHEDULER_INTERVAL_SECONDS,
            ),
        )
        object.__setattr__(
            self,
            "enable_playlist_writes",
            _parse_boolean(self.enable_playlist_writes, "enable_playlist_writes"),
        )
        object.__setattr__(
            self,
            "browser_bridge_token",
            _parse_optional_secret(self.browser_bridge_token, "browser_bridge_token"),
        )
        object.__setattr__(self, "cors_origins", _parse_cors_origins(self.cors_origins))
        object.__setattr__(self, "log_level", _parse_log_level(self.log_level))

    # Compatibility aliases keep consumers readable while the canonical field
    # names remain explicit about what they contain.
    @property
    def db_path(self) -> Path | str:
        return self.database_path

    @property
    def history_url(self) -> str:
        return self.youtube_music_history_url

    @property
    def headers_path(self) -> Path | None:
        return self.ytmusicapi_headers_path

    @property
    def ytmusic_headers_path(self) -> Path | None:
        """Compatibility name used by the ytmusicapi connector."""

        return self.ytmusicapi_headers_path

    @property
    def scheduler_interval(self) -> int:
        return self.scheduler_interval_seconds

    @property
    def scheduler_interval_minutes(self) -> int:
        """Expose the legacy minute-based scheduler contract as a safe ceiling."""

        if self.scheduler_interval_seconds == 0:
            return 0
        return (self.scheduler_interval_seconds + 59) // 60

    @property
    def allow_playlist_writes(self) -> bool:
        return self.enable_playlist_writes

    @property
    def playlist_writes_enabled(self) -> bool:
        return self.enable_playlist_writes

    @property
    def playlist_write_enabled(self) -> bool:
        return self.enable_playlist_writes

    @property
    def playlist_write_opt_in(self) -> bool:
        return self.enable_playlist_writes

    def safe_dict(self) -> dict[str, object]:
        """Compatibility alias for the redacted diagnostic mapping."""

        return self.to_safe_dict()

    def to_safe_dict(self) -> dict[str, object]:
        """Return diagnostic data with sensitive configuration values redacted."""

        return {
            "database_path": str(self.database_path),
            "host": self.host,
            "port": self.port,
            "youtube_music_history_url": _safe_url_for_diagnostics(
                self.youtube_music_history_url
            ),
            "chrome_cdp_url": "<configured>" if self.chrome_cdp_url else None,
            "ytmusicapi_headers_path": (
                "<configured>" if self.ytmusicapi_headers_path else None
            ),
            "scan_limit": self.scan_limit,
            "recommendation_limit": self.recommendation_limit,
            "scheduler_interval_seconds": self.scheduler_interval_seconds,
            "enable_playlist_writes": self.enable_playlist_writes,
            "browser_bridge_token": (
                "<configured>" if self.browser_bridge_token else None
            ),
            "cors_origins": list(self.cors_origins),
            "log_level": self.log_level,
        }

    # The default dataclass repr would include a possible CDP token or other
    # URL material.  Keep accidental debug/error output safe by redacting it.
    def __repr__(self) -> str:
        return f"Settings({self.to_safe_dict()!r})"

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str | None] | None = None,
    ) -> "Settings":
        """Build settings from ``environment`` or the current process env."""

        return load_settings(environment)


def load_settings(
    environment: Mapping[str, str | None] | None = None,
) -> Settings:
    """Load and validate settings without mutating the environment or filesystem.

    Passing a mapping is useful for tests and for callers that already have a
    controlled environment.  The function intentionally does not auto-load a
    ``.env`` file: secrets should be injected by the process manager, and a
    local example file must never be treated as credentials automatically.
    """

    env: Mapping[str, str | None] = os.environ if environment is None else environment
    return Settings(
        database_path=_value_or_default(
            env, "database_path", DEFAULT_DATABASE_PATH
        ),
        host=_value_or_default(env, "host", DEFAULT_HOST),
        port=_value_or_default(env, "port", DEFAULT_PORT),
        youtube_music_history_url=_value_or_default(
            env,
            "youtube_music_history_url",
            DEFAULT_YOUTUBE_MUSIC_HISTORY_URL,
        ),
        chrome_cdp_url=_environment_value(env, "chrome_cdp_url"),
        ytmusicapi_headers_path=_environment_value(
            env, "ytmusicapi_headers_path"
        ),
        scan_limit=_value_or_default(env, "scan_limit", DEFAULT_SCAN_LIMIT),
        recommendation_limit=_value_or_default(
            env,
            "recommendation_limit",
            DEFAULT_RECOMMENDATION_LIMIT,
        ),
        scheduler_interval_seconds=_scheduler_interval_from_environment(env),
        enable_playlist_writes=_value_or_default(
            env,
            "enable_playlist_writes",
            DEFAULT_ENABLE_PLAYLIST_WRITES,
        ),
        browser_bridge_token=_environment_value(env, "browser_bridge_token"),
        cors_origins=_value_or_default(env, "cors_origins", DEFAULT_CORS_ORIGINS),
        log_level=_value_or_default(env, "log_level", DEFAULT_LOG_LEVEL),
    )


def get_settings(
    environment: Mapping[str, str | None] | None = None,
) -> Settings:
    """Convenience wrapper for modules that prefer a getter-style import.

    This is intentionally not cached: long-running processes and tests can
    choose when to re-read their process environment explicitly.
    """

    return load_settings(environment)


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_CORS_ORIGINS",
    "DEFAULT_ENABLE_PLAYLIST_WRITES",
    "DEFAULT_HOST",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_PORT",
    "DEFAULT_RECOMMENDATION_LIMIT",
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_SCHEDULER_INTERVAL_SECONDS",
    "DEFAULT_YOUTUBE_MUSIC_HISTORY_URL",
    "MAX_RECOMMENDATION_LIMIT",
    "MAX_SCAN_LIMIT",
    "MAX_SCHEDULER_INTERVAL_SECONDS",
    "Settings",
    "SettingsError",
    "get_settings",
    "load_settings",
]
