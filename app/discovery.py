"""Bounded public song discovery from listening signals; never account access."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .connectors.ytmusic_api import YtMusicApiConnector


def _public_client():
    import requests
    from ytmusicapi import YTMusic

    class BoundedSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs['timeout'] = (3, 5)
            return super().request(*args, **kwargs)

    return YTMusic(requests_session=BoundedSession())


class FavoriteDiscovery:
    def __init__(self, database, connector=None, clock=time.time):
        self.database = database
        self.connector = connector or YtMusicApiConnector(environ={}, factory=_public_client)
        self.clock = clock

    def refresh(self, request_id: str | None = None) -> dict:
        moment = self.clock()
        cache = json.loads(self.database.get_metadata('favorite_discovery_cache') or '{}')
        stats = self.database.list_track_stats(limit=10000)
        def is_liked(row):
            return bool(row.get('local_favorite') or row.get('liked') is True
                        or row.get('liked_count', 0) > 0 or row.get('like_events', 0) > 0)
        # Most-listened songs are the primary real-time taste signal. Explicit
        # favorites with no plays are retained as equally valid zero-play seeds.
        seeds = [row for row in stats
                 if (row.get('play_count', 0) > 0 or is_liked(row))
                 and re.fullmatch(r'[\w-]{11}', row.get('video_id') or '')]
        seeds.sort(key=lambda row: (-int(row.get('play_count') or 0), -int(is_liked(row)), row['track_key']))
        # Always give explicit favorites a seed slot. A large history should
        # not crowd zero-play favorites out of discovery just because their
        # play count is lower.
        favorite_seeds = [row for row in seeds if is_liked(row)]
        listened_seeds = [row for row in seeds if not is_liked(row)]
        favorite_seeds.sort(key=lambda row: (-int(row.get('liked_count') or 0),
                                             -int(row.get('like_events') or 0),
                                             -int(row.get('play_count') or 0), row['track_key']))
        listened_seeds.sort(key=lambda row: (-int(row.get('play_count') or 0),
                                             -int(row.get('liked_count') or 0), row['track_key']))
        new_request = bool(request_id and request_id != self.database.get_metadata('favorite_discovery_request'))
        if new_request and listened_seeds:
            # Rotate the leading seed window on each acknowledged refresh. A
            # request ID is still used as the durable acknowledgement key, but
            # the sequence avoids repeatedly selecting the same top three rows.
            try:
                sequence = int(self.database.get_metadata('favorite_discovery_sequence') or 0) + 1
            except (TypeError, ValueError):
                sequence = 1
            self.database.set_metadata('favorite_discovery_sequence', str(sequence))
            offset = (sequence * 3) % len(listened_seeds)
            listened_seeds = listened_seeds[offset:] + listened_seeds[:offset]
        selected = favorite_seeds[:3]
        selected.extend(row for row in listened_seeds if row not in selected)
        selected = selected[:3]
        seeds = [*favorite_seeds, *listened_seeds]
        failures = 0
        fetched = 0
        for seed in selected:
            key = seed['track_key']
            previous = cache.get(key, {})
            if previous.get('retry_after', 0) > moment:
                failures += 1
                continue
            if (not new_request or previous.get('request_id') == request_id) and previous.get('expires_at', 0) > moment:
                continue
            # This network call is outside any SQLite transaction or scan lock.
            result = self.connector.related(seed['video_id'], limit=25)
            if not result.ok:
                cache[key] = {**previous, 'retry_after':moment+300, 'error':result.code or 'provider_unavailable'}
                failures += 1
                continue
            keys = []
            with self.database.transaction(immediate=True):
                for track in result.items[:25]:
                    if track.track_key == key or not re.fullmatch(r'[\w-]{11}', track.video_id or ''):
                        continue
                    existing = self.database.get_track(track.video_id)
                    # Never turn an already-heard or already-liked song back
                    # into a discovery candidate. Existing unplayed discovery
                    # rows can be enriched by a later provider response.
                    existing_stats = next((row for row in stats if row.get('track_key') == track.track_key), None)
                    if existing_stats and (
                        existing_stats.get('play_count', 0) > 0
                        or existing_stats.get('local_favorite')
                        or existing_stats.get('liked_count', 0) > 0
                        or existing_stats.get('like_events', 0) > 0
                    ):
                        continue
                    if existing is None or existing['source'] == 'favorite_discovery':
                        self.database.upsert_track(track, source='favorite_discovery')
                    keys.append(track.track_key)
            old_keys = previous.get('track_keys', []) if isinstance(previous.get('track_keys', []), list) else []
            cache[key] = {
                **previous,
                'title':seed['title'],
                'track_keys':list(dict.fromkeys([*old_keys, *keys])),
                'fetched_at':moment,
                'expires_at':moment+21600,
                'request_id':request_id,
                'retry_after':0,
                'error':None,
                'seed_kind':'favorite' if is_liked(seed) else 'most_listened',
                'play_count':int(seed.get('play_count') or 0),
                'liked':is_liked(seed),
            }
            fetched += 1
        # Keep only current seeds; candidate tracks/history are never deleted.
        active = {seed['track_key'] for seed in seeds}
        cache = {key:value for key,value in cache.items() if key in active}
        self.database.set_metadata('favorite_discovery_cache',json.dumps(cache))
        if request_id and not failures:
            self.database.set_metadata('favorite_discovery_request',request_id)
        exclusion = self.database.recommendation_exclusion_keys()
        current = {row.get('track_key'):row for row in self.database.list_track_stats(limit=10000)}
        candidates = {
            key for value in cache.values() if value.get('expires_at',0)>moment
            for key in value.get('track_keys',[])
            if key not in exclusion and not (
                current.get(key, {}).get('play_count', 0) > 0
                or current.get(key, {}).get('local_favorite')
                or current.get(key, {}).get('liked_count', 0) > 0
                or current.get(key, {}).get('like_events', 0) > 0
            )
        }
        state = 'needs_favorites' if not seeds else 'temporarily_unavailable' if failures else 'updated' if fetched else 'cached'
        if seeds and not failures and not candidates:
            state = 'exhausted'
        status = {'state':state,
                  'seed_count':len(selected), 'candidate_count':len(candidates), 'error_count':failures,
                  'checked_at':datetime.fromtimestamp(moment,timezone.utc).isoformat(), 'request_id':self.database.get_metadata('favorite_discovery_request')}
        self.database.set_metadata('favorite_discovery_status',json.dumps(status))
        return status
