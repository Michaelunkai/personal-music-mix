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
