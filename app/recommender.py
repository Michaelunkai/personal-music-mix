"""Deterministic, explainable recommendation scoring."""

from __future__ import annotations

import hashlib
import math
import re
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
        only_unheard: bool = False,
    ) -> list[Recommendation]:
        limit = max(1, min(int(limit), 200))
        excluded = exclude_keys or set()
        if only_unheard:
            return self._recommend_unheard(
                stats,
                related_candidates=related_candidates,
                limit=limit,
                excluded=excluded,
            )
        rows = [row for row in stats if str(row.get("track_key") or "") not in excluded]
        active = {row['track_key'] for row in rows if row.get('local_favorite') or _number(row.get('liked_count')) > 0 or _number(row.get('like_events')) > 0}
        moment = datetime.now(timezone.utc).timestamp()
        def seeds_for(row):
            return [seed for seed in row.get('discovery_seeds', []) if seed.get('track_key') in active and _number(seed.get('expires_at')) > moment]
        rows = [row for row in rows if row.get('source') != 'favorite_discovery' or _number(row.get('play_count')) > 0 or row['track_key'] in active or seeds_for(row)]
        max_plays = max((_number(row.get("play_count")) for row in rows), default=1.0)
        artist_plays: dict[str, float] = {}
        for row in rows:
            artist = str(row.get("artist") or "Unknown artist").casefold()
            favorite = bool(row.get("local_favorite") or _number(row.get("liked_count")) > 0 or _number(row.get("like_events")) > 0)
            artist_plays[artist] = artist_plays.get(artist, 0.0) + _number(row.get("play_count")) + (max(1.0, max_plays) if favorite else 0.0)
        max_artist_plays = max(1.0, max(artist_plays.values(), default=1.0))
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
            like_signal = 1.0 if likes > 0 or row.get("local_favorite") else 0.0
            recent = _recency(row.get("latest_played_at") or row.get("last_played_at") or track.played_at)
            artist_affinity = min(1.0, artist_plays.get(track.artist.casefold(), 0.0) / max_artist_plays)
            score = 0.48 * frequency + 0.27 * like_signal + 0.15 * recent + 0.10 * artist_affinity
            confidence = min(0.99, 0.35 + 0.30 * frequency + 0.25 * like_signal + 0.10 * recent)
            reasons: list[str] = []
            discovery = row.get('source') == 'favorite_discovery' and not plays and not like_signal
            if discovery:
                reasons.extend((f"recommended from your favorite: {seeds_for(row)[0]['title']}", 'new song discovery'))
                score = 0.45 + 0.10 * artist_affinity
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
                source="favorite_discovery" if discovery else "history",
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
        )
        discoveries = [item for item in ordered if item.source == 'favorite_discovery'][:math.ceil(limit * .3)]
        reserved = {item.track.track_key for item in discoveries}
        anchors = [item for item in ordered if item.track.track_key not in reserved][:limit-len(discoveries)]
        ordered = anchors[:3] + discoveries + anchors[3:]
        return [replace(item, rank=index) for index, item in enumerate(ordered, start=1)]

    @staticmethod
    def _liked(row: Mapping[str, Any]) -> bool:
        return bool(
            row.get("local_favorite")
            or row.get("liked") is True
            or _number(row.get("liked_count")) > 0
            or _number(row.get("like_events")) > 0
        )

    @staticmethod
    def _playable_video_id(value: Any) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", str(value or "")))

    def _recommend_unheard(
        self,
        stats: Sequence[Mapping[str, Any]],
        *,
        related_candidates: Iterable[TrackRecord],
        limit: int,
        excluded: set[str],
    ) -> list[Recommendation]:
        """Return only new, playable songs tied to the strongest user signals.

        History and liked rows are preference seeds, never output items.  The
        explicit ``excluded`` set is the persisted served-song ledger, so a
        refresh cannot recycle a song that was already shown in an earlier
        completed run.
        """

        rows = [row for row in stats if str(row.get("track_key") or "")]
        by_key = {str(row["track_key"]): row for row in rows}
        moment = datetime.now(timezone.utc).timestamp()

        def seed_weight(row: Mapping[str, Any]) -> tuple[float, float, float, str]:
            plays = _number(row.get("play_count"))
            liked = 1.0 if self._liked(row) else 0.0
            return (-plays, -liked, -_recency(row.get("latest_played_at")), str(row.get("track_key")))

        # Every played song can seed discovery. Explicit likes remain strong
        # zero-play seeds, so importing a Liked Music snapshot is sufficient.
        seeds = [
            row
            for row in rows
            if (_number(row.get("play_count")) > 0 or self._liked(row))
            and self._playable_video_id(row.get("video_id"))
        ]
        seeds.sort(key=seed_weight)
        seeds = seeds[:10]
        active_keys = {str(row["track_key"]) for row in seeds}
        max_plays = max(1.0, *[_number(row.get("play_count")) for row in seeds])
        artist_weights: dict[str, float] = {}
        for seed in seeds:
            artist = str(seed.get("artist") or "Unknown artist").casefold()
            artist_weights[artist] = max(artist_weights.get(artist, 0.0), _number(seed.get("play_count")))

        def valid_seeds(row: Mapping[str, Any]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for raw in row.get("discovery_seeds") or ():
                if not isinstance(raw, Mapping):
                    continue
                key = str(raw.get("track_key") or "")
                if key not in active_keys or _number(raw.get("expires_at")) <= moment:
                    continue
                seed = dict(raw)
                source = by_key.get(key)
                seed.setdefault("title", source.get("title") if source else "a song you enjoy")
                seed.setdefault("play_count", _number(source.get("play_count")) if source else 0)
                seed.setdefault("liked", self._liked(source) if source else False)
                seed.setdefault("seed_kind", "favorite" if seed.get("liked") else "most_listened")
                result.append(seed)
            result.sort(key=lambda value: (-_number(value.get("play_count")), not bool(value.get("liked")), str(value.get("track_key"))))
            return result

        scored: dict[str, Recommendation] = {}
        for row in rows:
            key = str(row.get("track_key") or "")
            plays = _number(row.get("play_count"))
            if (
                key in excluded
                or plays > 0
                or self._liked(row)
                or row.get("source") != "favorite_discovery"
                or not self._playable_video_id(row.get("video_id"))
            ):
                continue
            seeds_for_row = valid_seeds(row)
            if not seeds_for_row:
                continue
            seed = seeds_for_row[0]
            seed_plays = _number(seed.get("play_count"))
            frequency = min(1.0, math.log1p(seed_plays) / max(1.0, math.log1p(max_plays)))
            artist = str(row.get("artist") or "Unknown artist").casefold()
            artist_affinity = min(1.0, artist_weights.get(artist, 0.0) / max_plays)
            liked_seed = bool(seed.get("liked")) or seed.get("seed_kind") == "favorite"
            score = 0.50 + 0.22 * frequency + 0.16 * float(liked_seed) + 0.12 * artist_affinity
            title = str(seed.get("title") or "a song you enjoy")
            reason = (
                f"recommended from your favorite: {title}"
                if liked_seed
                else f"recommended because you listen to {title} often"
            )
            track = _track_from_row(row)
            if track is None:
                continue
            scored[key] = Recommendation(
                recommendation_id=_stable_id(key),
                track=track,
                score=min(0.99, score),
                confidence=min(0.98, 0.45 + 0.30 * frequency + 0.15 * float(liked_seed) + 0.10 * artist_affinity),
                reasons=(reason, "new to your listening history", f"artist affinity: {track.artist}"),
                source="favorite_discovery",
                generated_at=utc_now_iso(),
            )

        # A direct related response is also accepted when it is genuinely new
        # and playable. Its reason is anchored to the strongest seed rather
        # than presenting the related item as already heard.
        strongest_seed = seeds[0] if seeds else None
        for track in related_candidates:
            if not isinstance(track, TrackRecord) or track.track_key in excluded or track.track_key in by_key or track.track_key in scored:
                continue
            if not self._playable_video_id(track.video_id):
                continue
            seed_title = str((strongest_seed or {}).get("title") or "your listening history")
            seed_plays = _number((strongest_seed or {}).get("play_count"))
            reason = (
                f"recommended because you listen to {seed_title} often"
                if seed_plays > 0
                else f"recommended from your favorite: {seed_title}"
            )
            artist_affinity = min(1.0, artist_weights.get(track.artist.casefold(), 0.0) / max_plays)
            scored[track.track_key] = Recommendation(
                recommendation_id=_stable_id(track.track_key),
                track=track,
                score=min(0.90, 0.44 + 0.24 * artist_affinity),
                confidence=min(0.90, 0.40 + 0.35 * artist_affinity),
                reasons=(reason, "new to your listening history", f"artist affinity: {track.artist}"),
                source="related",
                generated_at=utc_now_iso(),
            )

        ordered = sorted(
            scored.values(),
            key=lambda item: (
                -round(float(item.score), 9),
                -round(float(item.confidence or 0), 9),
                item.track.artist.casefold(),
                item.track.title.casefold(),
            ),
        )[:limit]
        return [replace(item, rank=index) for index, item in enumerate(ordered, start=1)]
