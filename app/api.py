"""HTTP API and local dashboard integration."""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Any

try:  # Keep pure domain imports usable before web dependencies are installed.
    from fastapi import Request
except ImportError:  # pragma: no cover - runtime startup reports the missing dependency.
    Request = Any  # type: ignore[misc,assignment]

from .config import Settings, get_settings
from .contracts import Recommendation, TrackRecord
from .db import Database
from .jobs import ScanManager


def _latest_recommendation_objects(manager: ScanManager, limit: int | None = None) -> list[Recommendation]:
    rows = manager.database.latest_recommendations(limit=limit or manager.settings.recommendation_limit)
    result: list[Recommendation] = []
    for row in rows:
        track_data = row.get("track") or {}
        track = TrackRecord(
            track_key=str(track_data.get("track_key") or ""),
            title=str(track_data.get("title") or ""),
            artist=str(track_data.get("artist") or "Unknown artist"),
            album=track_data.get("album"),
            url=track_data.get("url"),
            video_id=track_data.get("video_id"),
            source=str(row.get("source") or "history"),
        )
        if track.track_key and track.title:
            result.append(
                Recommendation(
                    recommendation_id=str(row.get("recommendation_id") or track.track_key),
                    track=track,
                    score=float(row.get("score") or 0),
                    confidence=float(row.get("confidence") or 0),
                    reasons=list(row.get("reasons") or []),
                    source=str(row.get("source") or "history"),
                )
            )
    return result


def create_app(settings: Settings | None = None):
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - clear runtime error when optional dependency is absent
        raise RuntimeError("FastAPI is required. Install requirements.txt before starting the app.") from exc

    settings = settings or get_settings()
    database = Database(settings.database_path)
    database.initialize()
    manager = ScanManager(settings, database)
    app = FastAPI(title="YouTube Music Personal Mix", version="0.1.0")
    app.state.settings = settings
    app.state.database = database
    app.state.manager = manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-YouTube-Music-Bridge-Token"],
    )

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.on_event("startup")
    def _startup() -> None:
        manager.start_scheduler()
        from .cloud_sync import start_cloud_sync
        app.state.cloud_sync_stop = start_cloud_sync(database)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        manager.stop_scheduler()
        if hasattr(app.state, "cloud_sync_stop"):
            app.state.cloud_sync_stop.set()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        database_health = database.health()
        scheduler = manager.status()
        # This is process/readiness health, not proof of account access.  The
        # account evidence is exposed separately through /api/connection.
        return {
            "ok": bool(database_health.get("ok") and scheduler.get("scheduler_healthy", True)),
            "database": database_health,
            "scheduler": scheduler,
            "account_readiness": manager.connection_status(),
        }

    @app.get("/api/settings")
    def safe_settings() -> dict[str, Any]:
        return settings.safe_dict()

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return manager.status()

    @app.get("/api/connection")
    def connection() -> dict[str, Any]:
        return manager.connection_status()

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        return database.overview()

    @app.get("/api/recommendations")
    def recommendations(limit: int = 30) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        items = database.latest_recommendations(limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/api/library")
    def library() -> dict[str, Any]:
        rows = database.list_track_stats(limit=10000)
        return {"items": [{"track": row} for row in rows], "count": len(rows)}

    @app.get("/api/runs")
    def runs(limit: int = 20) -> dict[str, Any]:
        return {"items": database.list_scan_runs(max(1, min(limit, 100)))}

    @app.get("/api/favorites")
    def favorites() -> dict[str, Any]:
        rows = database.query_all("SELECT track_key FROM user_likes WHERE profile_id = 'dashboard' AND liked = 1")
        return {"track_keys": [row["track_key"] for row in rows], "source": "dashboard"}

    @app.post("/api/favorites")
    def save_favorite(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        key, liked = payload.get("track_key"), payload.get("liked")
        if not isinstance(key, str) or not isinstance(liked, bool):
            raise HTTPException(status_code=422, detail="track_key and a boolean liked value are required")
        row = database.query_one("SELECT track_id FROM canonical_tracks WHERE track_key = ?", (key,))
        if row is None:
            raise HTTPException(status_code=404, detail="Track was not found in your local library")
        database.set_like(track_id=row["track_id"], liked=liked, profile_id="dashboard", source="dashboard")
        rebuilt = manager.rebuild_from_local_history()
        return {"saved": True, "liked": liked, "track_key": key, "mix_status": rebuilt.get("status"), "source": "dashboard"}

    @app.get("/api/connectors")
    def connectors() -> dict[str, Any]:
        connection_state = manager.connection_status()
        return {
            "items": [
                {
                    "connector_id": "youtube-music-extension",
                    "mode": "browser_bridge",
                    "configured": True,
                    "ready": connection_state["browser_bridge"]["ready"],
                    "history_access_confirmed": connection_state["history_events"] > 0,
                    "message": connection_state["message"],
                    "last_sync_at": connection_state["browser_bridge"]["last_sync_at"],
                },
                {
                    "connector_id": "youtube-music-direct",
                    "mode": "direct",
                    "configured": connection_state["direct_connector_configured"],
                    "ready": connection_state["state"] == "direct_connector_ready",
                    "history_access_confirmed": False,
                    "message": "Direct connector status is only confirmed after a successful history scan.",
                    "last_sync_at": None,
                },
            ]
        }

    @app.get("/api/history")
    def history(limit: int = 100, before: str | None = None, after: str | None = None) -> dict[str, Any]:
        bounded = max(1, min(limit, 500))
        return {"items": database.get_history(limit=bounded, before=before, after=after), "limit": bounded}

    @app.get("/api/history/scans/{run_id}")
    def history_scan(run_id: str) -> dict[str, Any]:
        for item in database.list_scan_runs(100):
            if str(item.get("run_id")) == str(run_id):
                return item
        raise HTTPException(status_code=404, detail="Scan run not found")

    @app.post("/api/history/scans")
    def history_scan_start(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return manager.run_now(limit=payload.get("max_records"), include_related=bool(payload.get("include_related", True)))

    @app.get("/api/playlists/latest")
    def latest_playlist() -> dict[str, Any]:
        plan = database.latest_playlist_preview()
        return {"available": plan is not None, "plan": plan, "write_enabled": settings.allow_playlist_writes}

    @app.post("/api/scan")
    def scan(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        limit = payload.get("limit")
        include_related = bool(payload.get("include_related", True))
        try:
            parsed_limit = int(limit) if limit is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="limit must be an integer") from exc
        if not settings.chrome_cdp_url and not settings.ytmusicapi_headers_path and database.health().get("history_events", 0):
            return manager.rebuild_from_local_history()
        return manager.run_now(limit=parsed_limit, include_related=include_related)

    @app.post("/api/browser/sync")
    async def browser_sync(request: Request) -> dict[str, Any]:
        if settings.browser_bridge_token:
            provided = request.headers.get("X-YouTube-Music-Bridge-Token", "")
            if not hmac.compare_digest(provided, settings.browser_bridge_token):
                raise HTTPException(status_code=401, detail="Invalid browser bridge token")
        try:
            content_type = (request.headers.get("content-type") or "").casefold()
            if content_type.startswith("text/plain"):
                payload = json.loads((await request.body()).decode("utf-8"))
            else:
                payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        result = manager.ingest_bridge(payload)
        result_status = str(result.get("status") or "")
        if result_status == "blocked":
            raise HTTPException(status_code=409, detail=result.get("message") or "A scan is already running")
        if result_status == "failed":
            connector = result.get("connector") or {}
            code = str(connector.get("code") or (result.get("run") or {}).get("error_code") or "bridge_failed")
            status_code = 422 if code.startswith("invalid_") else 503
            raise HTTPException(status_code=status_code, detail=(result.get("run") or {}).get("message") or connector.get("message") or "Browser bridge sync failed")
        return result

    @app.post("/api/browser/heartbeat")
    async def browser_heartbeat(request: Request) -> dict[str, Any]:
        if settings.browser_bridge_token:
            provided = request.headers.get("X-YouTube-Music-Bridge-Token", "")
            if not hmac.compare_digest(provided, settings.browser_bridge_token):
                raise HTTPException(status_code=401, detail="Invalid browser bridge token")
        try:
            content_type = (request.headers.get("content-type") or "").casefold()
            if content_type.startswith("text/plain"):
                payload = json.loads((await request.body()).decode("utf-8"))
            else:
                payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        result = manager.bridge_heartbeat(payload)
        if result.get("status") == "failed":
            status_code = 422 if result.get("code") == "invalid_history_page" else 400
            raise HTTPException(status_code=status_code, detail=result.get("message") or "Browser bridge heartbeat failed")
        return result

    @app.post("/api/scheduler")
    def scheduler(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        action = str(payload.get("action") or "").lower()
        if action == "start":
            manager.start_scheduler()
        elif action == "stop":
            manager.stop_scheduler()
        else:
            raise HTTPException(status_code=422, detail="action must be start or stop")
        return manager.status()

    @app.post("/api/playlists/preview")
    def playlist_preview(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        recommendations = _latest_recommendation_objects(manager, int(payload.get("limit") or settings.recommendation_limit))
        if not recommendations:
            raise HTTPException(status_code=409, detail="Run a successful scan before creating a playlist preview")
        plan = manager.playlists.build_plan(
            recommendations,
            name=str(payload.get("name") or "Your High-Confidence Mix"),
            description=str(payload.get("description") or "Generated from your listening history and likes."),
        )
        return {"plan": plan.to_dict(), "write_enabled": settings.allow_playlist_writes}

    @app.post("/api/playlists/write")
    def playlist_write(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        recommendations = _latest_recommendation_objects(manager, int(payload.get("limit") or settings.recommendation_limit))
        if not recommendations:
            raise HTTPException(status_code=409, detail="Run a successful scan before writing a playlist")
        plan = manager.playlists.build_plan(
            recommendations,
            name=str(payload.get("name") or "Your High-Confidence Mix"),
            description=str(payload.get("description") or "Generated from your listening history and likes."),
        )
        result = manager.playlists.write(plan, confirm=bool(payload.get("confirm", False)))
        status_code = 200 if result.status in {"created", "partial", "preview"} else 409
        return JSONResponse(result.to_dict(), status_code=status_code)

    @app.get("/")
    def index():
        path = frontend_dir / "index.html"
        if not path.is_file():
            return JSONResponse({"ok": True, "message": "Frontend files are not installed."})
        return FileResponse(path)

    return app
