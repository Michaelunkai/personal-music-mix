import io
import json
import time

from app.contracts import ConnectorResult, TrackRecord
from app.db import Database
from app.discovery import FavoriteDiscovery
from app.recommender import RecommendationEngine


def track(letter, title=None):
    return TrackRecord(track_key='video:'+letter*11, video_id=letter*11, title=title or letter, artist='Artist '+letter)


class Provider:
    def __init__(self, items):
        self.items, self.calls, self.failed = items, [], False

    def related(self, video_id, limit):
        self.calls.append(video_id)
        return ConnectorResult(status='unavailable' if self.failed else 'ok', items=() if self.failed else self.items, code='temporary' if self.failed else None)


def test_discovery_uses_only_favorites_and_never_invents_plays():
    with Database() as db:
        seed, candidate = track('a'), track('b')
        db.upsert_track(seed)
        provider=Provider((seed,candidate))
        service=FavoriteDiscovery(db,provider)
        assert service.refresh()['state'] == 'needs_favorites'
        assert provider.calls == []
        db.set_like(seed.video_id,True,profile_id='dashboard')
        assert service.refresh()['candidate_count'] == 1
        assert db.health()['history_events'] == 0
        assert db.overview()['liked_track_count'] == 1
        stats=db.list_track_stats()
        row=next(row for row in stats if row['track_key']==candidate.track_key)
        assert row['source']=='favorite_discovery' and row['liked_count']==0
        result=RecommendationEngine().recommend(stats)
        discovered=next(item for item in result if item.track.track_key==candidate.track_key)
        assert discovered.source=='favorite_discovery'
        assert 'history' not in ' '.join(discovered.reasons)
        db.save_recommendations('test-discovery',result)
        assert db.get_track(candidate.video_id)['source']=='favorite_discovery'
        assert db.health()['history_events']==0
        db.set_like(seed.video_id,False,profile_id='dashboard')
        assert candidate.track_key not in {item.track.track_key for item in RecommendationEngine().recommend(db.list_track_stats())}
        db.set_like(candidate.video_id,True,profile_id='dashboard')
        assert candidate.track_key in {item.track.track_key for item in RecommendationEngine().recommend(db.list_track_stats())}


def test_forced_refresh_retry_survives_valid_cache_and_cooldown():
    with Database() as db:
        seed=track('a');db.upsert_track(seed);db.set_like(seed.video_id,True)
        provider=Provider((track('b'),));moment=[time.time()]
        service=FavoriteDiscovery(db,provider,clock=lambda:moment[0])
        service.refresh();service.refresh()
        assert len(provider.calls)==1
        provider.failed=True
        status=service.refresh('requested');service.refresh('requested')
        assert status['state']=='temporarily_unavailable' and status['request_id'] is None
        assert len(provider.calls)==2
        assert db.get_metadata('favorite_discovery_request') is None
        moment[0]+=301;provider.failed=False
        service.refresh('requested')
        assert len(provider.calls)==3
        assert db.get_metadata('favorite_discovery_request')=='requested'
        service.refresh('requested')
        assert len(provider.calls)==3
        service.refresh('next-request')
        assert len(provider.calls)==4


def test_expiry_is_projected_without_network_and_other_seed_retains_candidate():
    with Database() as db:
        seeds=[track('a'),track('c')];candidate=track('b')
        for seed in seeds:
            db.upsert_track(seed);db.set_like(seed.video_id,True)
        FavoriteDiscovery(db,Provider((candidate,))).refresh()
        db.set_like(seeds[0].video_id,False)
        assert candidate.track_key in {item.track.track_key for item in RecommendationEngine().recommend(db.list_track_stats())}
        cache=json.loads(db.get_metadata('favorite_discovery_cache'))
        for entry in cache.values():entry['expires_at']=time.time()-1
        db.set_metadata('favorite_discovery_cache',json.dumps(cache))
        row=next(row for row in db.list_track_stats() if row['track_key']==candidate.track_key)
        assert row['discovery_seeds']==[]
        assert candidate.track_key not in {item.track.track_key for item in RecommendationEngine().recommend(db.list_track_stats())}


def test_discovery_gets_space_in_a_full_mix():
    rows=[{'track_key':str(i),'title':str(i),'artist':'A','play_count':100,'liked_count':1} for i in range(30)]
    rows += [{'track_key':'new'+str(i),'title':'New','artist':'B','play_count':0,'source':'favorite_discovery','discovery_seeds':[{'track_key':'0','title':'Seed','expires_at':time.time()+300}]} for i in range(8)]
    result=RecommendationEngine().recommend(rows,limit=20)
    assert len(result)==20
    assert sum(item.source=='favorite_discovery' for item in result)==6


def test_cloud_does_not_ack_discovery_until_import_succeeds(tmp_path,monkeypatch):
    from app import cloud_sync
    with Database(tmp_path/'fixture.sqlite3') as db:
        db.upsert_track(track('a'))
        config=tmp_path/'config.json'
        config.write_text(json.dumps({'database_path':str(db.path),'origin':'https://fixture.chatgpt.site','encrypted_token':'fixture'}))
        monkeypatch.setattr(cloud_sync,'_unprotect',lambda _: 'fixture')
        calls=[];fail=[True]
        def request(req,timeout):
            calls.append(req.full_url)
            if req.full_url.endswith('/api/sync/import'):
                if fail[0]:raise OSError('fixture')
                return io.StringIO('{"status":"completed"}')
            return io.StringIO('{"records":[]}')
        monkeypatch.setattr(cloud_sync,'urlopen',request)
        discovery=FavoriteDiscovery(db,Provider(()))
        assert cloud_sync.publish_library(db,config,discovery=discovery)['state']=='failed'
        assert not any(url.endswith('/heartbeat') for url in calls)
        calls.clear();fail[0]=False
        assert cloud_sync.publish_library(db,config,discovery=discovery)['state']=='synced'
        assert calls[-1].endswith('/heartbeat')
        calls.clear()
        assert cloud_sync.publish_library(db,config,discovery=discovery)['state']=='unchanged'
        assert calls[-1].endswith('/heartbeat')
        assert not any(url.endswith('/import') for url in calls)


def test_local_mix_keeps_discovery_beyond_history_scan_limit(tmp_path):
    from dataclasses import replace
    from app.config import get_settings
    from app.jobs import ScanManager
    settings=replace(get_settings(),database_path=tmp_path/'music.sqlite3',scan_limit=500,recommendation_limit=20)
    with Database(settings.database_path) as db:
        with db.transaction(immediate=True):
            for i in range(500):
                video_id=f'{i:011d}'
                db.upsert_track(TrackRecord(track_key='video:'+video_id,video_id=video_id,title='A'+str(i),artist='A'))
                db.set_like(video_id,True)
        FavoriteDiscovery(db,Provider((track('z','Z discovery'),))).refresh()
        manager=ScanManager(settings,db)
        assert manager.rebuild_from_local_history()['status']=='completed'
        assert any(item['source']=='favorite_discovery' for item in db.latest_recommendations())
