from pathlib import Path

from app.db import Database
from app.history import HistoryService


def test_history_ingestion_is_idempotent(tmp_path: Path):
    db = Database(tmp_path / "music.sqlite3")
    db.initialize()
    service = HistoryService(db)
    records = [{"title": "Song", "artist": "Artist", "video_id": "abc", "played_at": "2026-09-07T10:00:00Z", "liked": True}]
    first = service.ingest(records)
    second = service.ingest(records)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert db.overview()["track_count"] == 1
    assert db.overview()["play_count"] == 1


def test_database_health_is_safe(tmp_path: Path):
    db = Database(tmp_path / "music.sqlite3")
    db.initialize()
    health = db.health()
    assert health["ok"] is True
    assert health["tracks"] == 0


def test_current_unlike_overrides_historical_like_and_cloud_choices_merge_by_time(tmp_path):
    db=Database(tmp_path / 'preferences.sqlite3'); db.initialize()
    HistoryService(db).ingest([{'title':'Song','artist':'Artist','video_id':'aaaaaaaaaaa','liked':True,'played_at':'2026-01-01T00:00:00Z'}])
    row=db.list_track_stats()[0]
    db.set_like(track_id=row['track_id'],liked=False,profile_id='default')
    assert db.list_track_stats()[0]['like_events'] == 0
    record={'track_key':row['track_key'],'liked':1,'updated_at':'2090-01-01T00:00:00Z'}
    assert db.merge_dashboard_favorites([record]) == 1
    assert db.merge_dashboard_favorites([record]) == 0
    assert db.list_track_stats()[0]['local_favorite'] == 1
    assert db.list_track_stats()[0]['provider_liked_count'] == 0
    assert db.merge_dashboard_favorites([{**record,'liked':0,'updated_at':'2080-01-01T00:00:00Z'}]) == 0
    assert db.merge_dashboard_favorites([{**record,'liked':0,'updated_at':'2090-01-01T00:00:01Z'}]) == 1
    assert db.list_track_stats()[0]['liked_count'] == 0
    db.set_like(track_id=row['track_id'],liked=True,profile_id='dashboard')
    assert db.merge_dashboard_favorites([{**record,'liked':0,'updated_at':'2090-01-01T00:00:01Z'}]) == 0
    assert db.list_track_stats()[0]['local_favorite'] == 1


def test_cloud_sync_pulls_newer_favorites_even_without_local_changes(tmp_path, monkeypatch):
    import io
    import json
    from app import cloud_sync
    db=Database(tmp_path/'sync.sqlite3'); db.initialize()
    HistoryService(db).ingest([{'title':'Song','artist':'Artist','video_id':'aaaaaaaaaaa'}])
    key=db.list_track_stats()[0]['track_key']
    config=tmp_path/'cloud.json'
    config.write_text(json.dumps({'origin':'https://test.chatgpt.site','database_path':str(db.path),'encrypted_token':'test'}))
    monkeypatch.setattr(cloud_sync,'_unprotect',lambda _: 'test-only')
    records=[]; imported=[]
    def urlopen(request, timeout):
        if request.get_method() == 'POST':
            imported.append(json.loads(request.data))
            return io.StringIO('{"status":"completed"}')
        return io.StringIO(json.dumps({'records':records}))
    monkeypatch.setattr(cloud_sync,'urlopen',urlopen)
    assert cloud_sync.publish_library(db,config)['state'] == 'synced'
    assert cloud_sync.publish_library(db,config)['state'] == 'unchanged'
    db.execute("UPDATE canonical_tracks SET updated_at='2099-01-01T00:00:00Z',last_seen_at='2099-01-01T00:00:00Z'")
    assert cloud_sync.publish_library(db,config)['state'] == 'unchanged'
    records.append({'track_key':key,'liked':1,'updated_at':'2090-01-01T00:00:00Z'})
    assert cloud_sync.publish_library(db,config)['state'] == 'synced'
    assert imported[-1]['tracks'][0]['local_favorite'] == 1
    assert cloud_sync.publish_library(db,config)['state'] == 'unchanged'
    config.write_text(json.dumps({'origin':'https://other.chatgpt.site','database_path':str(db.path),'encrypted_token':'test'}))
    assert cloud_sync.publish_library(db,config)['state'] == 'synced'
