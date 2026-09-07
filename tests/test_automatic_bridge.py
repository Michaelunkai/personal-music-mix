import json
from pathlib import Path

import pytest

from app.config import Settings


def _settings(path: Path) -> Settings:
    return Settings(
        database_path=path,
        host="127.0.0.1",
        port=8000,
        youtube_music_history_url="https://music.youtube.com/history",
        chrome_cdp_url=None,
        ytmusicapi_headers_path=None,
        scan_limit=100,
        recommendation_limit=10,
        scheduler_interval_seconds=0,
        enable_playlist_writes=False,
        browser_bridge_token=None,
        cors_origins=("http://127.0.0.1:8000",),
        log_level="INFO",
    )


def _payload(liked: bool = True) -> dict:
    return {
        "page": "https://music.youtube.com/history",
        "captured_at": "2026-09-07T10:00:00Z",
        "items": [
            {
                "title": "Repeated Song",
                "artist": "Artist",
                "url": "https://music.youtube.com/watch?v=abc12345678",
                "liked": liked,
                "position": 0,
            },
            {
                "title": "Repeated Song",
                "artist": "Artist",
                "url": "https://music.youtube.com/watch?v=abc12345678",
                "liked": False,
                "position": 1,
            },
        ],
    }


def test_repeated_bridge_snapshot_is_idempotent_and_preserves_repeated_rows(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        first = client.post("/api/browser/sync", json=_payload(True))
        second = client.post("/api/browser/sync", json=_payload(True))
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["ingestion"]["inserted"] == 2
        assert second.json()["ingestion"]["inserted"] == 0
        assert second.json()["ingestion"]["duplicates"] == 2
        assert client.get("/api/overview").json()["play_count"] == 2
        assert len(client.get("/api/runs").json()["items"]) == 2


def test_recommendation_runs_remain_persistable_across_syncs(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        assert client.post("/api/browser/sync", json=_payload()).status_code == 200
        assert client.post("/api/browser/sync", json=_payload()).status_code == 200
        recommendations = client.get("/api/recommendations").json()["items"]
        assert recommendations
        assert all(item["track"]["track_key"] for item in recommendations)


def test_text_plain_bridge_fallback_accepts_rendered_json(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        response = client.post(
            "/api/browser/sync",
            content=json.dumps(_payload()),
            headers={"Content-Type": "text/plain;charset=UTF-8"},
        )
        assert response.status_code == 200
        assert response.json()["ingestion"]["inserted"] == 2


def test_stable_bridge_ids_allow_metadata_reenrichment_without_duplicate_events(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        first = client.post("/api/browser/sync", json=_payload())
        assert first.status_code == 200
        history = client.get("/api/history?limit=10").json()["items"]
        enriched = _payload()
        ordered_history = sorted(history, key=lambda item: item["history_event_id"])
        for item, existing in zip(enriched["items"], ordered_history):
            item["artist"] = "Correct Artist"
            item["source_record_id"] = existing["source_event_id"]
        second = client.post("/api/browser/sync", json=enriched)
        assert second.status_code == 200
        assert second.json()["ingestion"]["inserted"] == 0
        assert second.json()["ingestion"]["duplicates"] == 2
        assert client.get("/api/overview").json()["play_count"] == 2
        assert {item["artist"] for item in client.get("/api/history?limit=10").json()["items"]} == {"Correct Artist"}


def test_bridge_accepts_youtube_music_podcast_track_urls():
    from app.connectors.bridge import BrowserBridgeIngestor

    result = BrowserBridgeIngestor.parse_payload(
        {
            "page": "https://music.youtube.com/history",
            "items": [
                {
                    "title": "Podcast-shaped history row",
                    "artist": "Artist",
                    "url": "https://music.youtube.com/podcast/mzeceRI-Zqo",
                }
            ],
        }
    )
    assert result.items[0].video_id == "mzeceRI-Zqo"


def test_bridge_rejects_non_history_page_and_scan_error_code_is_persisted(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        invalid = client.post("/api/browser/sync", json={**_payload(), "page": "https://music.youtube.com/"})
        assert invalid.status_code == 422
        failed_scan = client.post("/api/scan", json={"include_related": False})
        assert failed_scan.status_code == 200
        body = failed_scan.json()
        assert body["run"]["error_code"] == "no_connector_configured"
        runs = client.get("/api/runs").json()["items"]
        assert next(item for item in runs if item["run_id"] == body["run"]["run_id"])["error_code"] == "no_connector_configured"


def test_bridge_heartbeat_proves_loaded_history_page_without_accepting_rows(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        response = client.post(
            "/api/browser/heartbeat",
            json={
                "page": "https://music.youtube.com/history",
                "extension_version": "0.1.0",
                "sent_at": "2026-09-07T10:00:00Z",
                "authenticated": False,
                "visible_items": 0,
            },
        )
        assert response.status_code == 200
        connection = client.get("/api/connection").json()
        assert connection["browser_bridge"]["last_heartbeat_at"]
        assert connection["browser_bridge"]["heartbeat_page"] == "https://music.youtube.com/history"
        assert connection["browser_bridge"]["extension_version"] == "0.1.0"
        assert connection["browser_bridge"]["authenticated"] is False
        assert connection["state"] == "awaiting_account_authentication"
        assert client.post("/api/browser/heartbeat", json={"page": "https://music.youtube.com/"}).status_code == 422


def test_cached_history_does_not_claim_a_live_account_connection(tmp_path: Path):
    from fastapi.testclient import TestClient
    from app.api import create_app

    app = create_app(_settings(tmp_path / "music.sqlite3"))
    with TestClient(app) as client:
        assert client.post("/api/browser/sync", json=_payload()).status_code == 200
        cached = client.get("/api/connection").json()
        assert cached["history_events"] == 2
        assert cached["state"] == "history_cached_bridge_offline"
        assert cached["browser_bridge"]["ready"] is False
        heartbeat = {"page": "https://music.youtube.com/history", "authenticated": True, "visible_items": 2}
        assert client.post("/api/browser/heartbeat", json=heartbeat).status_code == 200
        connected = client.get("/api/connection").json()
        assert connected["state"] == "history_ingested"
        assert connected["browser_bridge"]["ready"] is True
        assert client.post("/api/browser/heartbeat", json={**heartbeat, "authenticated": False}).status_code == 200
        signed_out = client.get("/api/connection").json()
        assert signed_out["state"] == "awaiting_account_authentication"
        assert signed_out["browser_bridge"]["ready"] is False
