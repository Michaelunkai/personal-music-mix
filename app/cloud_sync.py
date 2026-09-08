"""Forward normalized library statistics to the user's private hosted site.

The account-scoped Sites credential is encrypted with Windows DPAPI in data/.
No Google credentials or raw history payloads are forwarded.
"""
from __future__ import annotations

import base64
import ctypes
import json
import hashlib
import threading
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _unprotect(encoded: str) -> str:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = base64.b64decode(encoded)
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    incoming, outgoing = Blob(len(raw), buffer), Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
        raise RuntimeError("Private site credential cannot be decrypted by this Windows account")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


def _recommendation_keys(payload: object) -> list[str]:
    """Extract hosted fresh-song keys from a successful import response."""

    if not isinstance(payload, dict):
        return []
    result: list[str] = []
    for item in payload.get("recommendations", ()):
        if not isinstance(item, dict):
            continue
        track = item.get("track", item)
        if not isinstance(track, dict):
            continue
        key = track.get("track_key")
        if isinstance(key, str) and key.strip():
            result.append(key.strip())
    return list(dict.fromkeys(result))


def publish_library(database, config_path: Path | None = None, *, discovery=None) -> dict:
    config_path = config_path or Path(__file__).resolve().parent.parent / "data" / "cloud-sync.json"
    if not config_path.is_file():
        return {"state": "not_configured"}
    cloud_served_merged = 0
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if Path(config.get("database_path", "")).resolve() != Path(database.path).resolve():
            return {"state": "not_configured_for_database"}
        origin = config["origin"]
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".chatgpt.site") or parsed.path not in {"", "/"} or parsed.username or parsed.query:
            raise ValueError("Invalid private site destination")
        token = _unprotect(config["encrypted_token"])
        # Pull before publishing, including during otherwise unchanged cycles.
        request = Request(origin.rstrip("/") + "/api/favorites", headers={"OAI-Sites-Authorization": "Bearer " + token})
        with urlopen(request, timeout=20) as response:
            favorites = json.load(response)
        merged = database.merge_dashboard_favorites(favorites.get("records", []))
        # A hosted refresh may have already shown songs that this local
        # process has not seen. Persist that union before discovery so the
        # next local recommendation run cannot resurrect those songs.
        cloud_served_merged = database.merge_cloud_served_keys(favorites.get("served_keys", []))
        if discovery is not None:
            discovery_status = discovery.refresh((favorites.get('discovery_request') or {}).get('id'))

        def publish_served_ledger():
            keys = sorted(database.recommendation_exclusion_keys())
            ledger_request = Request(
                origin.rstrip("/") + "/api/sync/ledger",
                data=json.dumps({"served_keys": keys}).encode("utf-8"),
                headers={"Content-Type": "application/json", "OAI-Sites-Authorization": "Bearer " + token},
                method="POST",
            )
            with urlopen(ledger_request, timeout=20) as response:
                result = json.load(response)
            if result.get("status") not in {"completed", "ok", "unchanged"}:
                raise RuntimeError("Private site did not confirm the served-song ledger")
        def report_discovery():
            if discovery is None:
                return
            heartbeat = Request(origin.rstrip('/')+'/api/sync/heartbeat', data=json.dumps({'discovery':discovery_status}).encode(),headers={'Content-Type':'application/json','OAI-Sites-Authorization':'Bearer '+token},method='POST')
            with urlopen(heartbeat,timeout=20) as response:
                json.load(response)
        published_fields = ('track_key','title','artist','album','video_id','url','play_count','liked_count','provider_liked_count','like_events','latest_played_at','local_favorite','local_favorite_updated_at','source','discovery_seeds')
        tracks = [{key:row.get(key) for key in published_fields} for row in database.list_track_stats(limit=10000)]
        fingerprint = hashlib.sha256(json.dumps({"origin":origin.rstrip("/"),"tracks":tracks}, sort_keys=True).encode()).hexdigest()
        if database.get_metadata("cloud_synced_fingerprint") == fingerprint:
            publish_served_ledger()
            report_discovery()
            result = {
                "state": "unchanged",
                "tracks": len(tracks),
                "origin": origin,
                "served_keys_merged": cloud_served_merged,
            }
            database.set_metadata("cloud_sync_status", json.dumps(result))
            return result
        # Keep one import transaction for a normal library so the hosted
        # worker rebuilds the fresh mix once, after the complete candidate set
        # is present.  The bound still protects the request body on unusually
        # large histories.
        hosted_recommendation_keys: list[str] = []
        for offset in range(0, len(tracks), 500):
            chunk = tracks[offset:offset+500]
            defer_rebuild = offset + len(chunk) < len(tracks)
            body = json.dumps({
                "tracks": chunk,
                "last_sync_at": database.get_metadata("browser_bridge_last_sync"),
                # The hosted worker rebuilds only after the final chunk.  A
                # rebuild per chunk can make the last response replace a
                # healthy fresh mix with an empty one when its chunk has no
                # active seed rows.
                "defer_rebuild": defer_rebuild,
            }).encode("utf-8")
            request = Request(origin.rstrip("/") + "/api/sync/import", data=body, headers={"Content-Type": "application/json", "OAI-Sites-Authorization": "Bearer " + token}, method="POST")
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
            if result.get("status") != "completed":
                raise RuntimeError("Private site did not confirm the library update")
            if not defer_rebuild:
                hosted_recommendation_keys = _recommendation_keys(result)
        if hosted_recommendation_keys:
            # The hosted rebuild may have consumed a fresh batch while this
            # publisher was uploading the library. Pull those keys into the
            # local exclusion set before the local callback re-ranks.
            cloud_served_merged += database.merge_cloud_served_keys(hosted_recommendation_keys)
        result = {
            "state": "synced",
            "tracks": len(tracks),
            "favorites_merged": merged,
            "origin": origin,
            "served_keys_merged": cloud_served_merged,
        }
        database.set_metadata("cloud_synced_fingerprint", fingerprint)
        publish_served_ledger()
        report_discovery()
    except Exception as exc:
        # Exception messages may contain request details. Persist only the type.
        # Keep the merge count even when a later publish step fails. The
        # caller can still rebuild its local fresh cache around the newly
        # learned hosted exclusions.
        result = {
            "state": "failed",
            "error": type(exc).__name__,
            "served_keys_merged": cloud_served_merged,
        }
    database.set_metadata("cloud_sync_status", json.dumps(result))
    return result


def start_cloud_sync(database, on_library_changed=None, *, config_path: Path | None = None, wake_event: threading.Event | None = None):
    config = config_path or (Path(__file__).resolve().parent.parent / "data" / "cloud-sync.json")
    stop = threading.Event()
    wake = wake_event or threading.Event()
    if not config.is_file():
        return stop
    from .discovery import FavoriteDiscovery
    discovery = FavoriteDiscovery(database)

    def run():
        first_pass = True
        while not stop.is_set():
            result = publish_library(database, config, discovery=discovery)
            ledger_changed = int(result.get('served_keys_merged') or 0) > 0
            initial_cache_refresh = first_pass and result.get('state') in {'synced', 'unchanged'}
            if (result.get('state') == 'synced' or ledger_changed or initial_cache_refresh) and on_library_changed:
                try:
                    on_library_changed()
                except Exception as exc:
                    database.set_metadata('cloud_local_rebuild_error',type(exc).__name__)
            first_pass = False
            wake.wait(30)
            wake.clear()
            if stop.is_set():
                break

    threading.Thread(target=run, name="private-music-sync", daemon=True).start()
    return stop
