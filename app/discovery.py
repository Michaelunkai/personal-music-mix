"""Bounded public song discovery from current favorites; never account access."""
from __future__ import annotations

import hashlib
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
        seeds = [row for row in self.database.list_track_stats(limit=10000)
                 if row.get('liked_count', 0) > 0 and re.fullmatch(r'[\w-]{11}', row.get('video_id') or '')]
        seeds.sort(key=lambda row: (-row['play_count'], row['track_key']))
        new_request = bool(request_id and request_id != self.database.get_metadata('favorite_discovery_request'))
        if new_request and seeds:
            offset = int(hashlib.sha256(request_id.encode()).hexdigest()[:8],16) % len(seeds)
            seeds = seeds[offset:] + seeds[:offset]
        selected = seeds[:3]
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
            result = self.connector.related(seed['video_id'], limit=8)
            if not result.ok:
                cache[key] = {**previous, 'retry_after':moment+300, 'error':result.code or 'provider_unavailable'}
                failures += 1
                continue
            keys = []
            with self.database.transaction(immediate=True):
                for track in result.items[:8]:
                    if track.track_key == key or not re.fullmatch(r'[\w-]{11}', track.video_id or ''):
                        continue
                    existing = self.database.get_track(track.video_id)
                    if existing is None or existing['source'] == 'favorite_discovery':
                        self.database.upsert_track(track, source='favorite_discovery')
                    keys.append(track.track_key)
            cache[key] = {'title':seed['title'], 'track_keys':list(dict.fromkeys(keys)), 'fetched_at':moment, 'expires_at':moment+21600, 'request_id':request_id}
            fetched += 1
        # Keep only current seeds; candidate tracks/history are never deleted.
        active = {seed['track_key'] for seed in seeds}
        cache = {key:value for key,value in cache.items() if key in active}
        self.database.set_metadata('favorite_discovery_cache',json.dumps(cache))
        if request_id and not failures:
            self.database.set_metadata('favorite_discovery_request',request_id)
        candidates = {key for value in cache.values() if value.get('expires_at',0)>moment for key in value.get('track_keys',[])}
        status = {'state':'needs_favorites' if not seeds else 'temporarily_unavailable' if failures else 'updated' if fetched else 'cached',
                  'seed_count':len(selected), 'candidate_count':len(candidates), 'error_count':failures,
                  'checked_at':datetime.fromtimestamp(moment,timezone.utc).isoformat(), 'request_id':self.database.get_metadata('favorite_discovery_request')}
        self.database.set_metadata('favorite_discovery_status',json.dumps(status))
        return status
