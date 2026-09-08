"""Bounded public song discovery from listening signals; never account access."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .connectors.ytmusic_api import YtMusicApiConnector


# Discovery is intentionally bounded per request so one refresh cannot hold
# the local sync loop hostage. The frontier is persisted in app metadata and
# lets later refreshes expand through provider-confirmed songs discovered from
# the user's strongest signals instead of repeatedly asking only the same
# three roots.
_CACHE_KEY = "favorite_discovery_cache"
_FRONTIER_KEY = "favorite_discovery_frontier"
_SEARCH_KEY = "favorite_discovery_search"
_REQUEST_KEY = "favorite_discovery_request"
_SEQUENCE_KEY = "favorite_discovery_sequence"
_STATUS_KEY = "favorite_discovery_status"
_CACHE_SECONDS = 6 * 60 * 60
_RELATED_LIMIT = 25
_SEED_LIMIT = 8
_FAVORITE_SEED_LIMIT = 4
_FRONTIER_BATCH = 3
_MAX_FRONTIER_DEPTH = 2
_SEARCH_BATCH = 2
_MAX_FRONTIER_ENTRIES = 2_000


def _public_client():
    import requests
    from ytmusicapi import YTMusic

    class BoundedSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["timeout"] = (3, 5)
            return super().request(*args, **kwargs)

    return YTMusic(requests_session=BoundedSession())


def _metadata_object(database, key: str) -> dict:
    try:
        value = json.loads(database.get_metadata(key) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class FavoriteDiscovery:
    def __init__(self, database, connector=None, clock=time.time):
        self.database = database
        self.connector = connector or YtMusicApiConnector(environ={}, factory=_public_client)
        self.clock = clock

    def refresh(self, request_id: str | None = None) -> dict:
        moment = self.clock()
        cache = _metadata_object(self.database, _CACHE_KEY)
        frontier = _metadata_object(self.database, _FRONTIER_KEY)
        search_state = _metadata_object(self.database, _SEARCH_KEY)
        previous_request = self.database.get_metadata(_REQUEST_KEY)
        had_acknowledged_request = bool(previous_request)
        stats = self.database.list_track_stats(limit=10000)

        def is_liked(row):
            return bool(
                row.get("local_favorite")
                or row.get("liked") is True
                or row.get("liked_count", 0) > 0
                or row.get("like_events", 0) > 0
            )

        def playable(video_id):
            return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")))

        stats_by_key = {str(row.get("track_key")): row for row in stats if row.get("track_key")}

        # Most-listened songs are the primary real-time taste signal. Explicit
        # favorites with no plays are retained as equally valid zero-play seeds.
        seeds = [
            row
            for row in stats
            if (row.get("play_count", 0) > 0 or is_liked(row)) and playable(row.get("video_id"))
        ]
        seeds.sort(key=lambda row: (-int(row.get("play_count") or 0), -int(is_liked(row)), row["track_key"]))

        # Always give explicit favorites a seed slot. A large history should
        # not crowd zero-play favorites out of discovery just because their
        # play count is lower.
        favorite_seeds = [row for row in seeds if is_liked(row)]
        listened_seeds = [row for row in seeds if not is_liked(row)]
        favorite_seeds.sort(
            key=lambda row: (
                -int(row.get("liked_count") or 0),
                -int(row.get("like_events") or 0),
                -int(row.get("play_count") or 0),
                row["track_key"],
            )
        )
        listened_seeds.sort(
            key=lambda row: (-int(row.get("play_count") or 0), -int(row.get("liked_count") or 0), row["track_key"])
        )

        new_request = bool(request_id and request_id != previous_request)
        if new_request and listened_seeds:
            # Keep the strongest most-listened song in every window while
            # rotating the remaining listening seeds on each acknowledged
            # refresh. The sequence prevents a large history from selecting
            # one static slice forever.
            try:
                sequence = int(self.database.get_metadata(_SEQUENCE_KEY) or 0) + 1
            except (TypeError, ValueError):
                sequence = 1
            self.database.set_metadata(_SEQUENCE_KEY, str(sequence))
            if len(listened_seeds) > 1:
                leading, rotating = listened_seeds[0], listened_seeds[1:]
                offset = ((sequence - 1) * max(1, _SEED_LIMIT - _FAVORITE_SEED_LIMIT)) % len(rotating)
                listened_seeds = [leading, *rotating[offset:], *rotating[:offset]]

        selected = favorite_seeds[:_FAVORITE_SEED_LIMIT]
        selected.extend(row for row in listened_seeds if row not in selected)
        selected = selected[:_SEED_LIMIT]
        seeds = [*favorite_seeds, *listened_seeds]
        failures = 0
        fetched = 0
        expanded = 0
        searched = 0

        def remember_frontier(track, *, root_key, parent_key, root_entry, depth):
            """Retain a provider-confirmed song as a future exploration seed."""

            track_key = str(getattr(track, "track_key", "") or "")
            video_id = str(getattr(track, "video_id", "") or "")
            if not track_key or not playable(video_id) or depth > _MAX_FRONTIER_DEPTH:
                return
            prior = frontier.get(track_key, {})
            if not isinstance(prior, dict):
                prior = {}
            # A candidate may be reached from several favorite roots. Keep the
            # first lineage stable so all output reasons remain tied to an
            # active favorite/most-listened seed.
            frontier[track_key] = {
                **prior,
                "video_id": video_id,
                "title": str(getattr(track, "title", "") or prior.get("title") or "a song you enjoy")[:500],
                "root_key": str(prior.get("root_key") or root_key),
                "parent_key": str(prior.get("parent_key") or parent_key),
                "depth": min(int(prior.get("depth") or depth), depth),
                "seed_kind": root_entry.get("seed_kind", "favorite"),
                "play_count": int(root_entry.get("play_count") or 0),
                "liked": bool(root_entry.get("liked")),
                # A song discovered during this request is deliberately held
                # for the next request so one click performs one bounded hop.
                "last_request_id": prior.get("last_request_id") or request_id,
                "expanded_count": int(prior.get("expanded_count") or 0),
                "retry_after": float(prior.get("retry_after") or 0),
                "exhausted": bool(prior.get("exhausted", False)),
            }

        def store_items(result, *, root_key, parent_key, root_entry, depth):
            """Persist only provider-confirmed, still-unheard candidates."""

            keys = []
            items = getattr(result, "items", ()) or ()
            with self.database.transaction(immediate=True):
                for track in items[:_RELATED_LIMIT]:
                    track_key = str(getattr(track, "track_key", "") or "")
                    video_id = str(getattr(track, "video_id", "") or "")
                    if not track_key or not playable(video_id) or track_key == parent_key:
                        continue
                    existing = self.database.get_track(video_id)
                    existing_stats = stats_by_key.get(track_key)
                    if existing_stats and (
                        existing_stats.get("play_count", 0) > 0
                        or existing_stats.get("local_favorite")
                        or existing_stats.get("liked_count", 0) > 0
                        or existing_stats.get("like_events", 0) > 0
                    ):
                        # A song that has since been heard or liked remains in
                        # the lineage for auditability, but is never a fresh
                        # output candidate.
                        continue
                    if existing is None or existing["source"] == "favorite_discovery":
                        self.database.upsert_track(track, source="favorite_discovery")
                        remember_frontier(
                            track,
                            root_key=root_key,
                            parent_key=parent_key,
                            root_entry=root_entry,
                            depth=depth + 1,
                        )
                    keys.append(track_key)
            return list(dict.fromkeys(keys))

        def update_root(seed, previous, keys):
            old_keys = previous.get("track_keys", []) if isinstance(previous.get("track_keys", []), list) else []
            return {
                **previous,
                "title": seed["title"],
                "track_keys": list(dict.fromkeys([*(key for key in old_keys if isinstance(key, str)), *keys])),
                "fetched_at": moment,
                "expires_at": moment + _CACHE_SECONDS,
                "request_id": request_id,
                "retry_after": 0,
                "error": None,
                "seed_kind": "favorite" if is_liked(seed) else "most_listened",
                "play_count": int(seed.get("play_count") or 0),
                "liked": is_liked(seed),
            }

        # Root related calls are the first pass. On a new request, expand the
        # durable frontier only after roots have had one successful fetch; this
        # keeps a transient root failure from fanning out into more provider
        # calls and makes retries cheap.
        root_entries_ready = True
        for seed in selected:
            key = seed["track_key"]
            previous = cache.get(key, {})
            if not isinstance(previous, dict):
                previous = {}
            if previous.get("retry_after", 0) > moment:
                failures += 1
                root_entries_ready = False
                continue
            if (not new_request or previous.get("request_id") == request_id) and previous.get("expires_at", 0) > moment:
                continue
            # This network call is outside any SQLite transaction or scan lock.
            try:
                result = self.connector.related(seed["video_id"], limit=_RELATED_LIMIT)
            except Exception:
                result = None
            if result is None or not getattr(result, "ok", False):
                code = getattr(result, "code", None) if result is not None else None
                cache[key] = {**previous, "retry_after": moment + 300, "error": code or "provider_unavailable"}
                failures += 1
                root_entries_ready = False
                continue
            root_entry = update_root(seed, previous, ())
            keys = store_items(result, root_key=key, parent_key=key, root_entry=root_entry, depth=0)
            cache[key] = update_root(seed, root_entry, keys)
            fetched += 1

        def cached_candidate_count():
            """Count currently deliverable cached rows before expanding."""

            served = self.database.recommendation_exclusion_keys()
            current_rows = {row.get("track_key"): row for row in self.database.list_track_stats(limit=10000)}
            return sum(
                1
                for value in cache.values()
                if value.get("expires_at", 0) > moment
                for key in value.get("track_keys", [])
                if key not in served
                and not (
                    current_rows.get(key, {}).get("play_count", 0) > 0
                    or current_rows.get(key, {}).get("local_favorite")
                    or current_rows.get(key, {}).get("liked_count", 0) > 0
                    or current_rows.get(key, {}).get("like_events", 0) > 0
                )
            )

        if (
            new_request
            and had_acknowledged_request
            and root_entries_ready
            and selected
            # Consume the current durable batch before spending network calls
            # on another frontier hop. This keeps refreshes fast while still
            # ensuring the next batch is ready once the existing one is gone.
            and cached_candidate_count() == 0
        ):
            # Expand a rotating subset of provider-confirmed songs. Results
            # are attributed to the original root cache entry so recommender
            # explanations remain favorite/most-listened based.
            active_roots = {seed["track_key"] for seed in seeds}
            frontier_candidates = []
            for track_key, entry in frontier.items():
                if not isinstance(entry, dict):
                    continue
                root_key = str(entry.get("root_key") or "")
                if root_key not in active_roots or not playable(entry.get("video_id")):
                    continue
                if int(entry.get("depth") or 0) > _MAX_FRONTIER_DEPTH or entry.get("exhausted"):
                    continue
                if float(entry.get("retry_after") or 0) > moment or entry.get("last_request_id") == request_id:
                    continue
                root_entry = cache.get(root_key)
                if not isinstance(root_entry, dict) or root_entry.get("expires_at", 0) <= moment:
                    continue
                frontier_candidates.append(
                    (
                        0 if root_entry.get("seed_kind") == "favorite" else 1,
                        -int(root_entry.get("play_count") or 0),
                        int(entry.get("expanded_count") or 0),
                        str(track_key),
                    )
                )
            frontier_candidates.sort()
            for _, _, _, track_key in frontier_candidates[:_FRONTIER_BATCH]:
                entry = frontier[track_key]
                try:
                    result = self.connector.related(entry["video_id"], limit=_RELATED_LIMIT)
                except Exception:
                    result = None
                if result is None or not getattr(result, "ok", False):
                    entry["retry_after"] = moment + 300
                    failures += 1
                    continue
                root_key = str(entry["root_key"])
                root_entry = cache.get(root_key, {})
                before_keys = set(root_entry.get("track_keys", []) or []) if isinstance(root_entry, dict) else set()
                keys = store_items(
                    result,
                    root_key=root_key,
                    parent_key=track_key,
                    root_entry=root_entry,
                    depth=int(entry.get("depth") or 0),
                )
                new_keys = [key for key in keys if key not in before_keys]
                root_entry["track_keys"] = list(dict.fromkeys([*(root_entry.get("track_keys", []) or []), *keys]))
                root_entry["expires_at"] = moment + _CACHE_SECONDS
                root_entry["fetched_at"] = moment
                root_entry["request_id"] = request_id
                cache[root_key] = root_entry
                entry["last_request_id"] = request_id
                entry["expanded_count"] = int(entry.get("expanded_count") or 0) + 1
                entry["retry_after"] = 0
                entry["exhausted"] = not new_keys
                expanded += 1

            # Public search is a second, diversified provider path. It is
            # optional on connectors that do not expose search (including the
            # small test doubles), and every returned row still passes the
            # same provider-id and heard/liked filters.
            search_method = getattr(self.connector, "search", None)
            if callable(search_method):
                for seed in selected[:_SEARCH_BATCH]:
                    title = str(seed.get("title") or "").strip()
                    artist = str(seed.get("artist") or "").strip()
                    variants = [
                        part
                        for part in (
                            " ".join(part for part in (artist, title) if part),
                            title,
                            artist,
                        )
                        if part
                    ]
                    if not variants:
                        continue
                    state_key = str(seed["track_key"])
                    previous_search = search_state.get(state_key, {})
                    if not isinstance(previous_search, dict):
                        previous_search = {}
                    variant = (
                        int(previous_search.get("variant") or 0)
                        + (1 if previous_search.get("last_request_id") else 0)
                    ) % len(variants)
                    query = variants[variant]
                    try:
                        result = search_method(query, limit=_RELATED_LIMIT)
                    except Exception:
                        result = None
                    if result is None or not getattr(result, "ok", False):
                        failures += 1
                        previous_search["retry_after"] = moment + 300
                        search_state[state_key] = previous_search
                        continue
                    root_entry = cache.get(state_key, {})
                    before_keys = set(root_entry.get("track_keys", []) or []) if isinstance(root_entry, dict) else set()
                    keys = store_items(
                        result,
                        root_key=state_key,
                        parent_key=state_key,
                        root_entry=root_entry,
                        depth=0,
                    )
                    new_keys = [key for key in keys if key not in before_keys]
                    root_entry["track_keys"] = list(dict.fromkeys([*(root_entry.get("track_keys", []) or []), *keys]))
                    root_entry["expires_at"] = moment + _CACHE_SECONDS
                    root_entry["fetched_at"] = moment
                    root_entry["request_id"] = request_id
                    cache[state_key] = root_entry
                    search_state[state_key] = {
                        "variant": variant,
                        "query": query,
                        "last_request_id": request_id,
                        "retry_after": 0,
                        "exhausted": not new_keys,
                    }
                    searched += 1

        # Keep only current roots; candidate tracks/history are never deleted.
        active = {seed["track_key"] for seed in seeds}
        cache = {key: value for key, value in cache.items() if key in active}
        frontier = {
            key: value
            for key, value in frontier.items()
            if isinstance(value, dict) and str(value.get("root_key") or "") in active
        }
        if len(frontier) > _MAX_FRONTIER_ENTRIES:
            ordered_frontier = sorted(
                frontier.items(),
                key=lambda pair: (
                    bool(pair[1].get("exhausted")),
                    int(pair[1].get("expanded_count") or 0),
                    pair[0],
                ),
            )
            frontier = dict(ordered_frontier[:_MAX_FRONTIER_ENTRIES])
        search_state = {key: value for key, value in search_state.items() if key in active}
        self.database.set_metadata(_CACHE_KEY, json.dumps(cache))
        self.database.set_metadata(_FRONTIER_KEY, json.dumps(frontier))
        self.database.set_metadata(_SEARCH_KEY, json.dumps(search_state))
        if request_id and not failures:
            self.database.set_metadata(_REQUEST_KEY, request_id)

        exclusion = self.database.recommendation_exclusion_keys()
        current = {row.get("track_key"): row for row in self.database.list_track_stats(limit=10000)}
        candidates = {
            key
            for value in cache.values()
            if value.get("expires_at", 0) > moment
            for key in value.get("track_keys", [])
            if key not in exclusion
            and not (
                current.get(key, {}).get("play_count", 0) > 0
                or current.get(key, {}).get("local_favorite")
                or current.get(key, {}).get("liked_count", 0) > 0
                or current.get(key, {}).get("like_events", 0) > 0
            )
        }
        expandable = any(
            isinstance(entry, dict)
            and str(entry.get("root_key") or "") in active
            and playable(entry.get("video_id"))
            and not entry.get("exhausted")
            and int(entry.get("depth") or 0) <= _MAX_FRONTIER_DEPTH
            for entry in frontier.values()
        )
        state = (
            "needs_favorites"
            if not seeds
            else "temporarily_unavailable"
            if failures
            else "updated"
            if (fetched or expanded or searched)
            else "cached"
        )
        if seeds and not failures and not candidates and not expandable:
            state = "exhausted"
        status = {
            "state": state,
            "seed_count": len(selected),
            "candidate_count": len(candidates),
            "error_count": failures,
            "frontier_count": len(frontier),
            "expanded_count": expanded,
            "search_count": searched,
            "checked_at": datetime.fromtimestamp(moment, timezone.utc).isoformat(),
            "request_id": self.database.get_metadata(_REQUEST_KEY),
        }
        self.database.set_metadata(_STATUS_KEY, json.dumps(status))
        return status
