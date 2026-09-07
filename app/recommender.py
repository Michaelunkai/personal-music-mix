"""Deterministic, explainable recommendation scoring."""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .contracts import Recommendation, TrackRecord, utc_now_iso


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _recency(value: Any) -> float:
    if not value:
        return 0.0
    try:
        text = str(value).replace("Z", "+00:00")
        date = datetime.fromisoformat(text)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - date.astimezone(timezone.utc)).total_seconds() / 86400)
        return math.exp(-days / 45.0)
    except (TypeError, ValueError):
        return 0.0


def _stable_id(track_key: str) -> str:
    return "rec:" + hashlib.sha256(track_key.encode("utf-8")).hexdigest()[:24]


def _track_from_row(row: Mapping[str, Any]) -> TrackRecord | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    artist = str(row.get("artist") or "Unknown artist").strip() or "Unknown artist"
    key = str(row.get("track_key") or "").strip()
    if not key:
        return None
    url = str(row.get("url")).strip() if row.get("url") else None
    return TrackRecord(
        track_key=key,
        title=title,
        artist=artist,
        artists=tuple(part.strip() for part in artist.split(",") if part.strip()),
        album=(str(row.get("album")).strip() if row.get("album") else None),
        url=url,
        canonical_url=url,
        video_id=(str(row.get("video_id")).strip() if row.get("video_id") else None),
        played_at=(str(row.get("latest_played_at") or row.get("last_played_at")).strip() if row.get("latest_played_at") or row.get("last_played_at") else None),
        liked=bool(row.get("liked") is True or _number(row.get("liked_count")) > 0 or _number(row.get("like_events")) > 0),
        source=str(row.get("source") or "history"),
    )


class RecommendationEngine:
    """Rank history and optional provider-related candidates locally."""

    def recommend(
        self,
        stats: Sequence[Mapping[str, Any]],
        *,
        related_candidates: Iterable[TrackRecord] = (),
        limit: int = 30,
        exclude_keys: set[str] | None = None,
    ) -> list[Recommendation]:
        limit = max(1, min(int(limit), 200))
        excluded = exclude_keys or set()
        rows = [row for row in stats if str(row.get("track_key") or "") not in excluded]
        max_plays = max((_number(row.get("play_count")) for row in rows), default=1.0)
        artist_plays: dict[str, float] = {}
        for row in rows:
            artist = str(row.get("artist") or "Unknown artist").casefold()
            artist_plays[artist] = artist_plays.get(artist, 0.0) + _number(row.get("play_count"))
        max_artist_plays = max(artist_plays.values(), default=1.0)
        seen_titles = {str(row.get("title") or "").casefold() for row in rows}
        scored: dict[str, Recommendation] = {}

        for row in rows:
            track = _track_from_row(row)
            if track is None:
                continue
            plays = _number(row.get("play_count"))
            likes = _number(row.get("liked_count")) + _number(row.get("like_events"))
            frequency = min(1.0, math.log1p(plays) / max(1.0, math.log1p(max_plays)))
            # A favorite is a preference, not a fraction of listening events.
            # Replaying it must not dilute that preference.
            like_signal = 1.0 if likes > 0 else 0.0
            recent = _recency(row.get("latest_played_at") or row.get("last_played_at") or track.played_at)
            artist_affinity = min(1.0, artist_plays.get(track.artist.casefold(), 0.0) / max_artist_plays)
            score = 0.48 * frequency + 0.27 * like_signal + 0.15 * recent + 0.10 * artist_affinity
            confidence = min(0.99, 0.35 + 0.30 * frequency + 0.25 * like_signal + 0.10 * recent)
            reasons: list[str] = []
            if plays >= 2:
                reasons.append(f"listened {int(plays)} times")
            elif plays:
                reasons.append("appears in your listening history")
            if likes:
                reasons.append("saved as a dashboard favorite" if row.get("local_favorite") else "matches your liked-music signal")
            if recent > 0.4:
                reasons.append("fits your recent listening pattern")
            reasons.append(f"artist affinity: {track.artist}")
            scored[track.track_key] = Recommendation(
                recommendation_id=_stable_id(track.track_key),
                track=track,
                score=score,
                confidence=confidence,
                reasons=tuple(reasons),
                source="history",
                generated_at=utc_now_iso(),
            )

        for track in related_candidates:
            if not isinstance(track, TrackRecord) or track.track_key in excluded or track.track_key in scored:
                continue
            affinity = artist_plays.get(track.artist.casefold(), 0.0) / max_artist_plays
            title_match = 0.05 if track.title.casefold() in seen_titles else 0.0
            scored[track.track_key] = Recommendation(
                recommendation_id=_stable_id(track.track_key),
                track=track,
                score=min(0.75, 0.42 * affinity + title_match + 0.12),
                confidence=min(0.85, 0.30 + 0.45 * affinity),
                reasons=(f"related to an artist you listen to: {track.artist}", "new-to-history discovery"),
                source="related",
                generated_at=utc_now_iso(),
            )

        ordered = sorted(
            scored.values(),
            key=lambda item: (-round(float(item.score), 9), -round(float(item.confidence or 0), 9), item.track.artist.casefold(), item.track.title.casefold()),
        )[:limit]
        return [replace(item, rank=index) for index, item in enumerate(ordered, start=1)]
