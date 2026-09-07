"""Playlist previews and explicitly authorized provider writes."""

from __future__ import annotations

import uuid
from typing import Sequence

from .config import Settings
from .connectors.ytmusic_api import YtMusicApiConnector
from .contracts import (
    PlaylistPlan,
    PlaylistWriteMode,
    PlaylistWriteOutcome,
    PlaylistWriteResult,
    Recommendation,
    utc_now_iso,
)
from .db import Database


class PlaylistService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self._api = YtMusicApiConnector(headers_path=settings.ytmusicapi_headers_path)

    def build_plan(
        self,
        recommendations: Sequence[Recommendation],
        *,
        name: str = "Your High-Confidence Mix",
        description: str = "Generated from your YouTube Music listening history and likes.",
    ) -> PlaylistPlan:
        safe_name = " ".join(str(name).split())[:120] or "Your High-Confidence Mix"
        safe_description = " ".join(str(description).split())[:500]
        unique: list[Recommendation] = []
        seen: set[str] = set()
        for recommendation in recommendations:
            if recommendation.track.track_key not in seen:
                unique.append(recommendation)
                seen.add(recommendation.track.track_key)
        plan = PlaylistPlan(
            name=safe_name,
            description=safe_description,
            recommendations=tuple(unique),
            created_at=utc_now_iso(),
        )
        preview = PlaylistWriteResult(
            request_id="preview:" + uuid.uuid4().hex,
            mode=PlaylistWriteMode.DRY_RUN,
            outcome=PlaylistWriteOutcome.DRY_RUN,
            requested_track_ids=tuple(plan.track_keys),
            message="Preview only; no provider write was attempted.",
            provider="local",
            status="preview",
            dry_run=True,
            requested_count=len(unique),
        )
        self.database.save_playlist(plan, preview, playlist_id="plan:" + uuid.uuid4().hex)
        return plan

    def write(self, plan: PlaylistPlan, *, confirm: bool = False) -> PlaylistWriteResult:
        requested = len(plan.recommendations)
        track_keys = tuple(item.track.track_key for item in plan.recommendations)
        if not confirm:
            return self._persist_blocked(
                plan,
                PlaylistWriteResult(
                    request_id="write:" + uuid.uuid4().hex,
                    outcome=PlaylistWriteOutcome.SKIPPED,
                    requested_track_ids=track_keys,
                    message="Explicit confirmation is required before a provider-side playlist write.",
                    provider="ytmusicapi",
                    status="blocked",
                    dry_run=True,
                    requested_count=requested,
                ),
            )
        if not self.settings.enable_playlist_writes:
            return self._persist_blocked(
                plan,
                PlaylistWriteResult(
                    request_id="write:" + uuid.uuid4().hex,
                    outcome=PlaylistWriteOutcome.SKIPPED,
                    requested_track_ids=track_keys,
                    message="Playlist writes are disabled. Set the explicit opt-in environment setting and confirm again.",
                    provider="ytmusicapi",
                    status="blocked",
                    dry_run=True,
                    requested_count=requested,
                ),
            )

        try:
            playlist_id, confirmed = self._api.create_playlist(
                plan.name,
                plan.description,
                [item.track for item in plan.recommendations],
            )
            outcome = PlaylistWriteOutcome.APPLIED if confirmed == requested else PlaylistWriteOutcome.PARTIAL
            result = PlaylistWriteResult(
                request_id="write:" + uuid.uuid4().hex,
                mode=PlaylistWriteMode.APPLY,
                outcome=outcome,
                requested_track_ids=track_keys,
                applied_track_ids=track_keys[:confirmed],
                playlist_id=playlist_id,
                provider_confirmed=True,
                message="Provider confirmed playlist creation and item insertion.",
                provider="ytmusicapi",
                status="created" if outcome is PlaylistWriteOutcome.APPLIED else "partial",
                dry_run=False,
                requested_count=requested,
                confirmed_count=confirmed,
            )
        except Exception as exc:
            result = PlaylistWriteResult(
                request_id="write:" + uuid.uuid4().hex,
                mode=PlaylistWriteMode.APPLY,
                outcome=PlaylistWriteOutcome.FAILED,
                requested_track_ids=track_keys,
                message=f"Provider playlist write failed: {type(exc).__name__}: {exc}",
                provider="ytmusicapi",
                status="error",
                dry_run=False,
                requested_count=requested,
            )
        self.database.save_playlist(plan, result, playlist_id="plan:" + uuid.uuid4().hex)
        return result

    def _persist_blocked(self, plan: PlaylistPlan, result: PlaylistWriteResult) -> PlaylistWriteResult:
        self.database.save_playlist(plan, result, playlist_id="plan:" + uuid.uuid4().hex)
        return result
