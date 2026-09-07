"""Local browser-extension bridge payload adapter.

The extension sends only user-visible history rows from the current
YouTube Music tab.  This module validates and bounds that payload before it
enters the local domain/persistence layers; it never accepts cookies, tokens,
or arbitrary account metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..contracts import ConnectorResult, TrackRecord


def _video_id(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip()
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    candidate = (query.get("v") or [None])[0]
    if not candidate:
        for marker in ("/watch/", "/song/", "/podcast/"):
            if marker in parsed.path:
                candidate = parsed.path.rsplit("/", 1)[-1]
                break
    if candidate and re.fullmatch(r"[A-Za-z0-9_-]{6,100}", candidate):
        return candidate
    return None


class BrowserBridgeIngestor:
    name = "youtube-music-extension"

    @staticmethod
    def _history_page(page: Any) -> bool:
        try:
            parsed = urlparse(str(page or ""))
        except ValueError:
            return False
        return parsed.scheme == "https" and parsed.netloc.casefold() == "music.youtube.com" and parsed.path.rstrip("/") == "/history"

    @staticmethod
    def _row_identity(
        page: str,
        raw: Mapping[str, Any],
        *,
        index: int,
        title: str,
        artist: str,
        album: str,
        url: str | None,
    ) -> str:
        """Build a stable local identity without using bridge receive time."""

        supplied = (
            raw.get("source_record_id")
            or raw.get("sourceRecordId")
            or raw.get("history_id")
            or raw.get("historyId")
            or raw.get("event_id")
            or raw.get("eventId")
            or raw.get("id")
        )
        supplied_text = str(supplied or "").strip()
        if re.fullmatch(r"bridge:[0-9a-f]{48}", supplied_text, flags=re.IGNORECASE):
            return supplied_text
        played_at = (
            raw.get("played_at")
            or raw.get("playedAt")
            or raw.get("timestamp")
            or raw.get("played_timestamp")
            or raw.get("time")
        )
        material = {
            "page": str(page or "")[:300],
            "supplied": str(supplied or "")[:160],
            "played_at": str(played_at or "")[:128],
            "index": int(index),
            "title": title.casefold(),
            "artist": artist.casefold(),
            "album": album.casefold(),
            "url": str(url or "")[:300],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"bridge:{digest[:48]}"

    @staticmethod
    def parse_payload(payload: Any, *, limit: int = 500) -> ConnectorResult:
        if not isinstance(payload, Mapping):
            return ConnectorResult(status="error", code="invalid_payload", message="The bridge payload must be a JSON object.")
        page = str(payload.get("page") or "")
        if not BrowserBridgeIngestor._history_page(page):
            return ConnectorResult(status="error", code="invalid_history_page", message="The bridge may only ingest the exact YouTube Music history page.")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return ConnectorResult(status="error", code="invalid_items", message="The bridge payload must contain an items list.")
        captured_at = payload.get("captured_at")
        if not isinstance(captured_at, str) or not captured_at.strip():
            captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        items: list[TrackRecord] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_items[: max(1, min(int(limit), 5000))]):
            if not isinstance(raw, Mapping):
                continue
            title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
            if not title:
                continue
            artist = re.sub(r"\s+", " ", str(raw.get("artist") or "Unknown artist")).strip() or "Unknown artist"
            album = re.sub(r"\s+", " ", str(raw.get("album") or "")).strip() or None
            url = str(raw.get("url") or raw.get("video_url") or "").strip() or None
            video_id = _video_id(url)
            key_material = video_id or f"{title.casefold()}|{artist.casefold()}|{album or ''}"
            track_key = f"video:{video_id}" if video_id else "track:" + re.sub(r"[^a-z0-9]+", "-", key_material.casefold()).strip("-")[:180]
            liked = raw.get("liked") is True
            played_at = raw.get("played_at") or raw.get("playedAt") or raw.get("timestamp") or raw.get("played_timestamp") or raw.get("time")
            source_record_id = BrowserBridgeIngestor._row_identity(
                page,
                raw,
                index=index,
                title=title,
                artist=artist,
                album=album or "",
                url=url,
            )
            if source_record_id in seen:
                continue
            seen.add(source_record_id)
            items.append(
                TrackRecord(
                    track_key=track_key,
                    title=title,
                    artist=artist,
                    artists=(artist,),
                    album=album,
                    url=url,
                    canonical_url=url,
                    video_id=video_id,
                    played_at=played_at,
                    liked=liked,
                    source=BrowserBridgeIngestor.name,
                    event_id=source_record_id,
                    source_record_id=source_record_id,
                )
            )
        return ConnectorResult(
            status="ok" if items else "partial",
            items=tuple(items),
            message=f"Validated {len(items)} rendered history rows from the local extension bridge.",
            code=None if items else "no_valid_items",
            metadata={"source": BrowserBridgeIngestor.name, "page": page[:300], "captured_at": captured_at},
        )
