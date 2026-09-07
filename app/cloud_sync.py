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


def publish_library(database, config_path: Path | None = None) -> dict:
    config_path = config_path or Path(__file__).resolve().parent.parent / "data" / "cloud-sync.json"
    if not config_path.is_file():
        return {"state": "not_configured"}
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
        published_fields = ('track_key','title','artist','album','video_id','url','play_count','liked_count','provider_liked_count','like_events','latest_played_at','local_favorite','local_favorite_updated_at')
        tracks = [{key:row.get(key) for key in published_fields} for row in database.list_track_stats(limit=10000)]
        fingerprint = hashlib.sha256(json.dumps({"origin":origin.rstrip("/"),"tracks":tracks}, sort_keys=True).encode()).hexdigest()
        if database.get_metadata("cloud_synced_fingerprint") == fingerprint:
            result = {"state": "unchanged", "tracks": len(tracks), "origin": origin}
            database.set_metadata("cloud_sync_status", json.dumps(result))
            return result
        for offset in range(0, len(tracks), 20):
            body = json.dumps({"tracks": tracks[offset:offset+20], "last_sync_at": database.get_metadata("browser_bridge_last_sync")}).encode("utf-8")
            request = Request(origin.rstrip("/") + "/api/sync/import", data=body, headers={"Content-Type": "application/json", "OAI-Sites-Authorization": "Bearer " + token}, method="POST")
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
            if result.get("status") != "completed":
                raise RuntimeError("Private site did not confirm the library update")
        result = {"state": "synced", "tracks": len(tracks), "favorites_merged": merged, "origin": origin}
        database.set_metadata("cloud_synced_fingerprint", fingerprint)
    except Exception as exc:
        # Exception messages may contain request details. Persist only the type.
        result = {"state": "failed", "error": type(exc).__name__}
    database.set_metadata("cloud_sync_status", json.dumps(result))
    return result


def start_cloud_sync(database):
    config = Path(__file__).resolve().parent.parent / "data" / "cloud-sync.json"
    stop = threading.Event()
    if not config.is_file():
        return stop

    def run():
        while not stop.is_set():
            publish_library(database, config)
            if stop.wait(30):
                break

    threading.Thread(target=run, name="private-music-sync", daemon=True).start()
    return stop
