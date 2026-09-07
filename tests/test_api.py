from pathlib import Path

import pytest

from app.config import Settings


def test_api_smoke(tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    settings = Settings(
        database_path=tmp_path / "music.sqlite3",
        host="127.0.0.1",
        port=8000,
        youtube_music_history_url="https://music.youtube.com/history",
        chrome_cdp_url=None,
        ytmusicapi_headers_path=None,
        scan_limit=100,
        recommendation_limit=10,
        scheduler_interval_seconds=3600,
        enable_playlist_writes=False,
        browser_bridge_token=None,
        cors_origins=("http://127.0.0.1:8000",),
        log_level="INFO",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/overview").json()["track_count"] == 0
        assert client.get("/api/connection").json()["state"] == "awaiting_browser_bridge_sync"
        assert client.post("/api/scan", json={"include_related": False}).json()["run"]["status"] in {"failed", "completed", "partial"}


def test_browser_sync_generates_local_playlist_preview(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    settings = Settings(
        database_path=tmp_path / "music.sqlite3",
        host="127.0.0.1",
        port=8000,
        youtube_music_history_url="https://music.youtube.com/history",
        chrome_cdp_url=None,
        ytmusicapi_headers_path=None,
        scan_limit=100,
        recommendation_limit=10,
        scheduler_interval_seconds=3600,
        enable_playlist_writes=False,
        browser_bridge_token=None,
        cors_origins=("http://127.0.0.1:8000",),
        log_level="INFO",
    )
    app = create_app(settings)
    payload = {
        "page": "https://music.youtube.com/history",
        "captured_at": "2026-09-07T10:00:00Z",
        "items": [
            {"title": "Loved Song", "artist": "Loved Artist", "url": "https://music.youtube.com/watch?v=abc12345678", "liked": True},
            {"title": "Second Song", "artist": "Second Artist", "url": "https://music.youtube.com/watch?v=def12345678", "liked": False},
        ],
    }
    with TestClient(app) as client:
        response = client.post("/api/browser/sync", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["playlist_preview"]["status"] == "preview"
        latest = client.get("/api/playlists/latest")
        assert latest.status_code == 200
        latest_body = latest.json()
        assert latest_body["available"] is True
        assert latest_body["write_enabled"] is False
        assert latest_body["plan"]["requested_count"] >= 1
        assert client.get("/api/connection").json()["state"] == "history_cached_bridge_offline"


def test_favorites_refresh_ranking_persist_and_preserve_provider_likes(tmp_path):
    from fastapi.testclient import TestClient
    from app.api import create_app

    settings = Settings(database_path=tmp_path / "favorites.sqlite3", scheduler_interval_seconds=0)
    app = create_app(settings)
    payload = {"page": "https://music.youtube.com/history", "items": [
        {"title": "Alpha", "artist": "Artist", "url": "https://music.youtube.com/watch?v=aaaaaaaaaaa", "liked": False},
        {"title": "Zebra", "artist": "Artist", "url": "https://music.youtube.com/watch?v=zzzzzzzzzzz", "liked": False},
    ]}
    with TestClient(app) as client:
        assert client.post("/api/browser/sync", json=payload).status_code == 200
        assert client.get("/api/recommendations").json()["items"][0]["track"]["title"] == "Alpha"
        favorite = {"track_key": "video:zzzzzzzzzzz", "liked": True}
        saved = client.post("/api/favorites", json=favorite)
        assert saved.json()["mix_status"] == "completed"
        assert client.get("/api/recommendations").json()["items"][0]["track"]["title"] == "Zebra"
        for _ in range(2):
            assert client.post("/api/scan", json={}).json()["status"] == "completed"
            assert client.get("/api/recommendations").json()["items"][0]["track"]["title"] == "Zebra"
        overview = client.get("/api/overview").json()
        assert overview["play_count"] == 2
        assert overview["local_favorite_count"] == 1
        assert overview["provider_liked_track_count"] == 0
        assert client.get("/api/playlists/latest").json()["plan"]["requested_count"] == 2
        assert client.post("/api/favorites", json={"track_key": "missing", "liked": True}).status_code == 404
        assert client.post("/api/favorites", json={**favorite, "liked": "false"}).status_code == 422

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/favorites").json()["track_keys"] == ["video:zzzzzzzzzzz"]
        payload["items"][1]["liked"] = True
        assert client.post("/api/browser/sync", json=payload).status_code == 200
        assert client.post("/api/favorites", json={**favorite, "liked": False}).status_code == 200
        assert client.get("/api/favorites").json()["track_keys"] == []
        overview = client.get("/api/overview").json()
        assert overview["local_favorite_count"] == 0
        assert overview["provider_liked_track_count"] == 1
