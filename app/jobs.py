"""Serialized scan orchestration and optional background scheduling."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
import json
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .connectors.bridge import BrowserBridgeIngestor
from .connectors.ytmusic_api import YtMusicApiConnector
from .connectors.ytmusic_browser import YouTubeMusicBrowserConfig, YouTubeMusicBrowserConnector
from .contracts import ConnectorResult, ScanRunSummary, TrackRecord, utc_now_iso
from .db import Database
from .history import HistoryService
from .observability import get_logger
from .playlists import PlaylistService
from .recommender import RecommendationEngine, _track_from_row


def _connector_status(status: Any) -> str:
    return str(getattr(status, "value", status)).lower()


def _browser_to_contract(result: Any) -> ConnectorResult:
    items: list[TrackRecord] = []
    for entry in getattr(result, "entries", ()):
        items.append(
            TrackRecord(
                title=str(getattr(entry, "title", "") or ""),
                artist=str(getattr(entry, "artist", "Unknown artist") or "Unknown artist"),
                artists=tuple(filter(None, [str(getattr(entry, "artist", "") or "")])),
                album=(str(getattr(entry, "album", "")).strip() if getattr(entry, "album", None) else None),
                url=(str(getattr(entry, "video_url", "")).strip() if getattr(entry, "video_url", None) else None),
                canonical_url=(str(getattr(entry, "video_url", "")).strip() if getattr(entry, "video_url", None) else None),
                played_at=getattr(entry, "played_at", None),
                liked=bool(getattr(entry, "like_hint", False) is True),
                source="youtube-music-browser",
            )
        )
    errors = list(getattr(result, "errors", ()) or ())
    first = errors[0] if errors else None
    status = _connector_status(getattr(result, "status", "error"))
    if status == "ok" and items:
        return ConnectorResult(
            status="ok",
            items=tuple(items),
            message=f"Read {len(items)} rendered history entries from the authenticated browser session.",
            metadata={"source": "youtube-music-browser", "pages_scanned": getattr(result, "pages_scanned", 0)},
        )
    return ConnectorResult(
        status="unauthorized" if "sign" in status or "auth" in status else ("unavailable" if status in {"missing_cdp", "playwright_unavailable", "invalid_config"} else "error"),
        items=tuple(items),
        message=(getattr(first, "message", None) if first else f"Browser connector returned {status}."),
        code=(getattr(first, "code", None) if first else status),
        warnings=tuple(getattr(first, "action", "") for first in errors if getattr(first, "action", "")),
        metadata={"source": "youtube-music-browser", "status": status},
    )


class ScanManager:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.history = HistoryService(database, recent_limit=50)
        self.engine = RecommendationEngine()
        self.playlists = PlaylistService(database, settings)
        self.api_connector = YtMusicApiConnector(headers_path=settings.ytmusicapi_headers_path)
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._running = False
        self._last_result: dict[str, Any] | None = None
        self._scheduler_error: str | None = None
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self.logger = get_logger()
        try:
            interrupted = self.database.reconcile_running_scan_runs()
            if interrupted:
                self.logger.warning("reconciled %s interrupted scan run(s)", interrupted)
        except Exception as exc:
            self.logger.warning("could not reconcile interrupted scans: %s", type(exc).__name__)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            scheduler = self._scheduler_thread is not None and self._scheduler_thread.is_alive()
            direct_configured = bool(self.settings.chrome_cdp_url or self.settings.ytmusicapi_headers_path)
            enabled = self.settings.scheduler_interval_seconds > 0 and direct_configured
            return {
                "running": self._running,
                "scheduler_running": scheduler,
                "scheduler_enabled": enabled,
                "scheduler_mode": "direct_interval" if direct_configured else "browser_bridge_event_driven",
                "scheduler_healthy": (not enabled) or self._scheduler_error is None,
                "scheduler_error": self._scheduler_error,
                "last_result": self._last_result,
            }

    def connection_status(self) -> dict[str, Any]:
        """Describe connector readiness without pretending the account is connected."""

        overview = self.database.overview()
        health = self.database.health()
        configured_direct = bool(self.settings.chrome_cdp_url or self.settings.ytmusicapi_headers_path)
        recent_runs = self.database.list_scan_runs(20)
        successful_run = next((run for run in recent_runs if run.get("status") in {"completed", "partial"}), None)
        bridge_last_sync = self.database.get_metadata("browser_bridge_last_sync")
        bridge_heartbeat_raw = self.database.get_metadata("browser_bridge_heartbeat")
        try:
            bridge_heartbeat = json.loads(bridge_heartbeat_raw) if bridge_heartbeat_raw else None
        except (TypeError, ValueError):
            bridge_heartbeat = None
        heartbeat_age_seconds: float | None = None
        heartbeat_live = False
        heartbeat_received_at = (bridge_heartbeat or {}).get("received_at")
        if heartbeat_received_at:
            try:
                heartbeat_time = datetime.fromisoformat(str(heartbeat_received_at).replace("Z", "+00:00"))
                heartbeat_age_seconds = max(0.0, (datetime.now(timezone.utc) - heartbeat_time).total_seconds())
                heartbeat_live = heartbeat_age_seconds <= 180
            except (TypeError, ValueError):
                heartbeat_age_seconds = None
        authenticated = (bridge_heartbeat or {}).get("authenticated")
        bridge_ready = bool(bridge_last_sync) and heartbeat_live and authenticated is True
        if authenticated is False and heartbeat_live:
            state = "awaiting_account_authentication"
            message = "The exact YouTube Music history page is open, but this Chrome profile is signed out."
        elif health.get("history_events", 0) and not bridge_ready and not configured_direct:
            state = "history_cached_bridge_offline"
            message = "Saved history and playlist previews are available. Automatic scanning is waiting for a live authenticated browser bridge."
        elif health.get("history_events", 0):
            state = "history_ingested"
            message = "History is present locally; recommendations and playlist previews can update automatically."
        elif configured_direct:
            state = "direct_connector_ready"
            message = "A direct authenticated connector is configured; the scheduler will scan on its interval."
        else:
            state = "awaiting_browser_bridge_sync"
            message = "The automatic browser bridge is waiting for the authenticated YouTube Music history tab."
        return {
            "state": state,
            "message": message,
            "history_url": self.settings.history_url,
            "browser_bridge": {
                "endpoint": "/api/browser/sync",
                "token_required": bool(self.settings.browser_bridge_token),
                "ready": bridge_ready,
                "last_sync_at": bridge_last_sync,
                "live": heartbeat_live,
                "heartbeat_age_seconds": heartbeat_age_seconds,
                "last_heartbeat_at": heartbeat_received_at,
                "heartbeat_page": (bridge_heartbeat or {}).get("page"),
                "extension_version": (bridge_heartbeat or {}).get("extension_version"),
                "authenticated": authenticated,
            },
            "direct_connector_configured": configured_direct,
            "history_events": int(health.get("history_events", 0)),
            "tracks": int(overview.get("track_count", 0)),
            "last_successful_run": successful_run,
        }

    def bridge_heartbeat(self, payload: Any) -> dict[str, Any]:
        """Record bridge liveness without accepting account credentials or history rows."""

        if not isinstance(payload, dict):
            return {"status": "failed", "code": "invalid_heartbeat", "message": "The bridge heartbeat must be a JSON object."}
        page = str(payload.get("page") or "")[:300]
        if not BrowserBridgeIngestor._history_page(page):
            return {"status": "failed", "code": "invalid_history_page", "message": "The bridge heartbeat must originate from the exact history page."}
        heartbeat = {
            "page": page,
            "extension_version": str(payload.get("extension_version") or "")[:64],
            "sent_at": str(payload.get("sent_at") or "")[:128],
            "authenticated": payload.get("authenticated") if isinstance(payload.get("authenticated"), bool) else None,
            "visible_items": max(0, min(int(payload.get("visible_items") or 0), 5000)) if str(payload.get("visible_items") or "").lstrip("-").isdigit() else 0,
            "received_at": utc_now_iso(),
        }
        self.database.set_metadata("browser_bridge_heartbeat", json.dumps(heartbeat, ensure_ascii=False, separators=(",", ":")))
        return {"status": "ok", "heartbeat": heartbeat}

    def _complete_history_result(
        self,
        summary: ScanRunSummary,
        result: ConnectorResult,
        *,
        attempts: list[dict[str, Any]] | None = None,
        include_related: bool = False,
    ) -> dict[str, Any]:
        """Persist one connector result, recommendations, and the local plan."""

        ingestion_result = self.history.ingest([item.to_dict() for item in result.items])
        ingestion = {
            "received": ingestion_result.received,
            "normalized": ingestion_result.normalized,
            "inserted": ingestion_result.stored,
            "duplicates": ingestion_result.duplicates,
            "skipped": ingestion_result.skipped,
            "overview": self.database.overview(),
        }
        related: list[TrackRecord] = []
        if include_related and self.settings.ytmusicapi_headers_path:
            for row in self.database.list_track_stats(limit=5):
                seed = _track_from_row(row)
                if seed:
                    related.extend(self.api_connector.related(seed, limit=5).items)
        recommendations = self.engine.recommend(
            self.database.list_track_stats(limit=self.settings.scan_limit),
            related_candidates=related,
            limit=self.settings.recommendation_limit,
        )
        recommendation_run_id = "recommend:" + uuid.uuid4().hex
        self.database.save_recommendations(recommendation_run_id, recommendations, message="Generated from local history")
        playlist_preview = self._build_playlist_preview(recommendations)
        summary = replace(
            summary,
            status="completed" if _connector_status(result.status) == "ok" else "partial",
            finished_at=utc_now_iso(),
            items_seen=len(result.items),
            records_scanned=len(result.items),
            records_emitted=ingestion_result.stored,
            records_skipped=ingestion_result.skipped,
            duplicate_records=ingestion_result.duplicates,
            recommendations_created=len(recommendations),
            complete=True,
            message=result.message or "History scan completed",
            error_code=result.code,
        )
        self.database.save_scan_run(summary)
        return self._finish(
            summary,
            {
                "connector": result.to_dict(),
                "attempts": attempts or [result.to_dict()],
                "ingestion": ingestion,
                "recommendations": [item.to_dict() for item in recommendations],
                "playlist_preview": playlist_preview,
            },
        )

    def run_now(self, *, limit: int | None = None, include_related: bool = True) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "blocked", "code": "scan_already_running", "message": "A scan is already running."}
        run_id = "scan:" + uuid.uuid4().hex
        started = utc_now_iso()
        summary = ScanRunSummary(run_id=run_id, status="running", started_at=started, source="auto")
        with self._state_lock:
            self._running = True
        try:
            self.database.save_scan_run(summary)
            result, connector_attempts = self._scan_sources(limit)
            if not result.ok or not result.items:
                summary = replace(summary, status="failed", finished_at=utc_now_iso(), message=result.message or "No history returned", error_code=result.code)
                self.database.save_scan_run(summary)
                return self._finish(summary, {"connector": result.to_dict(), "attempts": connector_attempts})
            return self._complete_history_result(
                summary,
                result,
                attempts=connector_attempts,
                include_related=include_related,
            )
        except Exception as exc:
            summary = replace(summary, status="failed", finished_at=utc_now_iso(), message=f"Scan orchestration failed: {type(exc).__name__}: {exc}", error_code="scan_orchestration_failed")
            try:
                self.database.save_scan_run(summary)
            except Exception as persist_exc:
                self.logger.error("could not persist failed scan state: %s", type(persist_exc).__name__)
            return self._finish(summary, {"error": summary.message})
        finally:
            with self._state_lock:
                self._running = False
            self._run_lock.release()

    def ingest_bridge(self, payload: Any, *, limit: int | None = None) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "blocked", "code": "scan_already_running", "message": "A scan is already running."}
        run_id = "scan:" + uuid.uuid4().hex
        summary = ScanRunSummary(
            run_id=run_id,
            status="running",
            started_at=utc_now_iso(),
            source="browser_bridge",
        )
        with self._state_lock:
            self._running = True
        try:
            self.database.save_scan_run(summary)
            result = BrowserBridgeIngestor.parse_payload(payload, limit=limit or self.settings.scan_limit)
            if not result.ok or not result.items:
                summary = replace(
                    summary,
                    status="failed",
                    finished_at=utc_now_iso(),
                    message=result.message or "The browser bridge returned no usable history rows.",
                    error_code=result.code or "bridge_no_items",
                )
                self.database.save_scan_run(summary)
                return self._finish(summary, {"connector": result.to_dict(), "attempts": [result.to_dict()]})
            self.database.set_metadata("browser_bridge_last_sync", utc_now_iso())
            return self._complete_history_result(
                summary,
                result,
                attempts=[result.to_dict()],
                include_related=False,
            )
        except Exception as exc:
            summary = replace(
                summary,
                status="failed",
                finished_at=utc_now_iso(),
                message=f"Browser bridge ingestion failed: {type(exc).__name__}: {exc}",
                error_code="bridge_ingestion_failed",
            )
            try:
                self.database.save_scan_run(summary)
            except Exception as persist_exc:
                self.logger.error("could not persist failed bridge state: %s", type(persist_exc).__name__)
            return self._finish(summary, {"error": summary.message})
        finally:
            with self._state_lock:
                self._running = False
            self._run_lock.release()

    def rebuild_from_local_history(self) -> dict[str, Any]:
        """Re-rank persisted history when the bridge is event-driven.

        The browser bridge owns acquisition.  This compatibility operation
        makes the dashboard scan action useful without pretending that a
        second provider read happened.
        """

        if not self._run_lock.acquire(blocking=False):
            return {"status": "blocked", "code": "scan_already_running", "message": "A scan is already running."}
        summary = ScanRunSummary(
            run_id="scan:" + uuid.uuid4().hex,
            status="running",
            started_at=utc_now_iso(),
            source="local_history",
        )
        with self._state_lock:
            self._running = True
        try:
            self.database.save_scan_run(summary)
            event_count = int(self.database.health().get("history_events", 0))
            if not event_count:
                summary = replace(
                    summary,
                    status="failed",
                    finished_at=utc_now_iso(),
                    message="No locally persisted history is available yet; the browser bridge must sync first.",
                    error_code="no_local_history",
                )
                self.database.save_scan_run(summary)
                return self._finish(summary, {})
            recommendations = self.engine.recommend(
                self.database.list_track_stats(limit=self.settings.scan_limit),
                limit=self.settings.recommendation_limit,
            )
            self.database.save_recommendations(
                "recommend:" + uuid.uuid4().hex,
                recommendations,
                message="Re-ranked persisted local history",
            )
            playlist_preview = self._build_playlist_preview(recommendations)
            summary = replace(
                summary,
                status="completed",
                finished_at=utc_now_iso(),
                items_seen=event_count,
                records_scanned=event_count,
                recommendations_created=len(recommendations),
                complete=True,
                message="Re-ranked persisted local history; no provider read was attempted.",
            )
            self.database.save_scan_run(summary)
            return self._finish(
                summary,
                {
                    "ingestion": {"received": 0, "inserted": 0, "duplicates": 0, "skipped": 0, "overview": self.database.overview()},
                    "recommendations": [item.to_dict() for item in recommendations],
                    "playlist_preview": playlist_preview,
                },
            )
        except Exception as exc:
            summary = replace(
                summary,
                status="failed",
                finished_at=utc_now_iso(),
                message=f"Local history rebuild failed: {type(exc).__name__}: {exc}",
                error_code="local_rebuild_failed",
            )
            try:
                self.database.save_scan_run(summary)
            except Exception as persist_exc:
                self.logger.error("could not persist failed local rebuild: %s", type(persist_exc).__name__)
            return self._finish(summary, {"error": summary.message})
        finally:
            with self._state_lock:
                self._running = False
            self._run_lock.release()

    def _build_playlist_preview(self, recommendations: list[Any]) -> dict[str, Any] | None:
        """Persist a local playlist plan after every successful recommendation run.

        The plan is intentionally a dry run.  It makes the requested automatic
        generation useful immediately while keeping provider-side mutation behind
        the separate confirmation and opt-in gates in PlaylistService.write.
        """

        if not recommendations:
            return None
        try:
            plan = self.playlists.build_plan(
                recommendations,
                name="Your High-Confidence Mix",
                description="Automatically generated from your listening history and liked signals.",
            )
            return {
                "status": "preview",
                "provider_write_attempted": False,
                "plan": plan.to_dict(),
            }
        except Exception as exc:  # Keep a connector/history success visible if local plan persistence fails.
            self.logger.warning("automatic playlist preview failed: %s", type(exc).__name__)
            return {
                "status": "error",
                "provider_write_attempted": False,
                "message": f"Automatic playlist preview failed: {type(exc).__name__}",
            }

    def start_scheduler(self) -> None:
        with self._state_lock:
            if self.settings.scheduler_interval_seconds <= 0 or not (self.settings.chrome_cdp_url or self.settings.ytmusicapi_headers_path):
                self._stop_event.set()
                self._scheduler_thread = None
                return
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return
            self._stop_event.clear()
            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="ytmusic-scheduler", daemon=True)
            self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()
        thread = self._scheduler_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._state_lock:
            if self._scheduler_thread is thread and (thread is None or not thread.is_alive()):
                self._scheduler_thread = None

    def _scheduler_loop(self) -> None:
        # Direct connectors can scan immediately; the extension bridge is
        # event-driven and will submit its first sync when the history tab is
        # rendered.  A zero interval is a real disabled state.
        if self.settings.chrome_cdp_url or self.settings.ytmusicapi_headers_path:
            self._safe_scheduled_scan("startup")
        interval = min(max(300, self.settings.scheduler_interval_seconds), 86_400 * 365)
        while interval > 0 and not self._stop_event.wait(interval):
            self._safe_scheduled_scan("interval")

    def _safe_scheduled_scan(self, reason: str) -> None:
        for attempt in range(2):
            if self._stop_event.is_set():
                return
            try:
                self.logger.info("scheduled YouTube Music scan starting (%s, attempt %s)", reason, attempt + 1)
                result = self.run_now()
            except Exception as exc:
                message = f"scheduled scan crashed: {type(exc).__name__}"
                self.logger.exception(message)
                with self._state_lock:
                    self._scheduler_error = message
                result = None
            run = result.get("run") if isinstance(result, dict) else None
            error_code = str((run or {}).get("error_code") or "")
            status = str((run or {}).get("status") or (result or {}).get("status") or "")
            if status not in {"failed", "blocked"} or error_code == "no_connector_configured":
                with self._state_lock:
                    self._scheduler_error = None
                return
            if attempt == 0 and not self._stop_event.wait(2):
                continue
            with self._state_lock:
                self._scheduler_error = str((run or {}).get("message") or "scheduled scan failed")
            return

    def _scan_sources(self, limit: int | None) -> tuple[ConnectorResult, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        if self.settings.chrome_cdp_url:
            config = YouTubeMusicBrowserConfig(
                cdp_url=self.settings.chrome_cdp_url,
                history_url=self.settings.history_url,
                max_entries=max(1, min(int(limit or self.settings.scan_limit), 5000)),
                dry_run=False,
                reuse_existing_page=True,
            )
            browser_result = YouTubeMusicBrowserConnector(config).fetch_history(dry_run=False)
            result = _browser_to_contract(browser_result)
            attempts.append(result.to_dict())
            if result.ok and result.items:
                return result, attempts
        if self.settings.ytmusicapi_headers_path:
            result = self.api_connector.scan(limit or self.settings.scan_limit)
            attempts.append(result.to_dict())
            if result.ok and result.items:
                return result, attempts
        if attempts:
            return ConnectorResult(status="unavailable", message="No configured YouTube Music connector returned history.", code="no_history_source_succeeded", warnings=tuple(attempt.get("message", "") for attempt in attempts if attempt.get("message")), metadata={"source": "auto"}), attempts
        return ConnectorResult(status="unavailable", message="Configure the local browser extension bridge, Chrome CDP, or YTMUSIC_HEADERS_PATH before scanning.", code="no_connector_configured", metadata={"source": "auto"}), attempts

    def _finish(self, summary: ScanRunSummary, payload: dict[str, Any]) -> dict[str, Any]:
        result = {"status": summary.to_dict().get("status"), "run": summary.to_dict(), **payload}
        with self._state_lock:
            self._last_result = result
        return result
