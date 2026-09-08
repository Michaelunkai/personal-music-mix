# YouTube Music Personal Mix

The private hosted edition uses the same dashboard with a Cloudflare Worker and
D1 database managed by Sites. Deployment configuration is in `.openai/hosting.json`.
Run `npm ci`, `npm run build`, and `npm test` to validate its production bundle.
Database changes are generated with `npm run db:generate`; applied migrations
are immutable. The Python service and its SQLite database remain the local
bridge for the approved Chrome profile.

The hosted app includes For you, Favorites, All songs, library search, playlist
refresh, and embedded playback. Hosted favorites persist in D1 and survive
repeat imports. Site access is restricted to the owner; listening data is
uploaded after publication, not embedded in the source or deployment archive.

When configured, the local app forwards changed library statistics every 30
seconds. Its Sites credential is encrypted for the current Windows account in
the Git-ignored `data/cloud-sync.json`; it is never delivered to page JavaScript.
Configuration is scoped to the exact production database so tests cannot upload
fixture data. A failed upload is retried without claiming a successful cloud sync.

Favorites now synchronize in both directions. New explicit choices advance past
the stored preference timestamp, so clock differences and older retries cannot
undo a website change. The discovery seeds are the songs with the strongest
observed play counts, with imported and dashboard favorites added as preference
signals. Refresh queues a bounded public related-song request from those seeds.
The local app checks that request every 30 seconds, queries up to three rotating
seeds with up to 25 candidates each, and publishes the result. The hosted site
keeps a durable served-song ledger and its For you mix contains only playable
songs with zero observed plays and no like signal; history rows are never shown
as recommendations. A completed refresh consumes its songs, so the next refresh
cannot repeat them. Suggestions expire after six hours; failed requests retry
after five minutes. No account credentials are needed for public discovery, and
no plays or favorites are invented. If the provider has no unseen candidates,
the mix is empty and the connection panel explains that another refresh can
request a new batch.

The bridge accepts visible rows from the exact history page and the YouTube Music
Liked Music collection (`/playlist?list=LM`). Favorites are stored without adding
fake listening events. These snapshots are partial: missing rows never remove a
favorite. Untimestamped history counts are conservative observed minimums.
An explicitly observed unselected Like control clears that provider preference;
missing or conflicting controls preserve the last known choice. Dashboard hearts
remain separate from these provider preferences.
Live activation of the revised bridge and real embedded playback remain awaiting
the approved browser runtime; passing simulated-player tests is not audio proof.

This project is a local-first recommendation dashboard for an authenticated YouTube Music account. It learns from rendered YouTube Music history and liked signals, stores a private local SQLite model, ranks songs with an explainable deterministic scorer, and previews a playlist before any provider-side write.

## What is real and what is guarded

- The app does not ask for or store a Google password, browser cookie, or raw access token.
- The preferred account path is the optional Chrome extension bridge in `extension/`. It reads only rendered rows from `music.youtube.com/history` and posts them to `127.0.0.1:8000`.
- A second connector can attach to a Chrome CDP endpoint (`YTMUSIC_CDP_URL`) or use a user-supplied `ytmusicapi` headers JSON file (`YTMUSIC_HEADERS_PATH`). These are alternatives, not fabricated fallbacks.
- Playlist writes are preview-only by default. To allow a provider write, set `YTMUSIC_RECOMMENDER_ENABLE_PLAYLIST_WRITES=true` and confirm the write in the dashboard. The API reports the provider-confirmed playlist ID and item count; it never reports a successful write from a local plan alone.
- Every successful history sync automatically recalculates recommendations and persists a local playlist preview. The latest preview is available at `GET /api/playlists/latest`; no provider write is attempted during scanning.
- If no connector is actually authenticated/configured, a scan ends in an explicit `no_connector_configured` or connector error state.

## Run on Windows

1. Use `.env.example` as a reference and inject only the settings you need into the process environment (for example, with PowerShell `$env:YTMUSIC_RECOMMENDER_PORT = '8000'`). The application intentionally does not auto-load a `.env` file.
2. Install and start the hands-off Windows route:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\run.ps1
```

On a clean checkout the script installs missing dependencies automatically. If
the approved Chrome Profile 2 is closed, it invokes the existing
`Ensure-VisibleChromeProfile2.ps1` recovery hook. It preserves the profile's
extensions and restored tabs. If Chrome is already open, it leaves that session
running. The application bridge must already be installed in that profile;
the launcher does not claim that launching Chrome installs or connects it.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Listen, favorite, and refresh

- **Play mix** starts the ranked songs in the embedded YouTube player. Each song
  also has **Play**; the player provides pause, volume, seeking, and fullscreen,
  with **Previous** and **Next** beside it. Dashboard refreshes preserve the
  current playback queue.
- **Favorite** saves a local preference and rebuilds the mix immediately. These
  favorites survive app restarts and remain separate from imported YouTube likes.
- **Refresh mix** consumes the current fresh mix, requests another provider
  discovery batch, and shows only songs that are new to your listening history.
  The strongest play-count seeds drive discovery even before a song is explicitly
  liked; favorites refine the preference signal. With an active connector, new
  history arrives through that connector, while refreshing cached data does not
  claim a new account sync.
- If the provider has not returned a new playable candidate yet, the For you tab
  stays empty and the connection panel reports whether the request is pending,
  temporarily unavailable, or exhausted. The Favorites and All songs tabs still
  expose the saved library.
- YouTube may block embedding, age-restrict, remove, or region-restrict a song.
  Playback errors offer **Next** and **Open song in YouTube Music**. Browser
  autoplay restrictions may require clicking Play inside the embedded player.
  Embedded playback is not a guarantee that every provider track can play here.

For a repeat start after installation, use `.\scripts\run.ps1`. Data is stored in `data\ytmusic_recommender.sqlite3` and is excluded from Git.

## Browser bridge behavior

The normal `scripts\run.ps1` route starts the approved Chrome profile when
Chrome is closed and requests no credentials or prompts. Once the extension is
available in Profile 2, it syncs rendered rows automatically when the exact
history page loads, changes, or is revisited, retries failed local deliveries,
and the dashboard refreshes without a button click. A running Chrome profile
cannot be modified silently by a local app. Cached history remains usable,
but connection readiness requires a recent authenticated bridge heartbeat.
Without that signal the dashboard reports that automatic scanning is waiting.
Failed local deliveries retry; unreadable HTTP responses never count as success.

The extension is intentionally narrow: it does not use cookies, passwords, history outside YouTube Music, or a remote service.

## Alternative authenticated connector

If you deliberately export a compatible `ytmusicapi` headers JSON file, set `YTMUSIC_HEADERS_PATH` to that private path. Install the connector dependency with the normal requirements, then restart the app. A browser CDP endpoint can be configured with `YTMUSIC_CDP_URL=http://127.0.0.1:9222` when Chrome was started with remote debugging. Do not point the app at a profile that is not yours or share the headers file.

## API surface

- `GET /api/health`, `/api/settings`, `/api/status`, `/api/connection`, `/api/overview`, `/api/recommendations`, `/api/runs`, `/api/playlists/latest`
- `POST /api/scan` with `{ "include_related": true }`
- `POST /api/browser/sync` for the local extension bridge
- `POST /api/scheduler` with `{ "action": "start" | "stop" }`
- `POST /api/playlists/preview` and `POST /api/playlists/write` (the latter requires `{ "confirm": true }` plus the environment opt-in)

The dashboard sends `X-Mix-Mode: fresh` on recommendation and playlist reads so
the visible site uses the unseen-only contract. The hosted worker persists the
served-song ledger in D1; the local SQLite bridge persists the equivalent set in
completed recommendation runs.

## Verification

```powershell
python -m pytest
python -m compileall app
node --test tests/test_extension.cjs
node --test tests/test_dashboard.cjs
```

To test the guarded no-connector behavior without touching YouTube Music:

```powershell
$env:YTMUSIC_DATABASE_PATH = "$PWD\data\verification.sqlite3"
python -m app.main
```

Then open the dashboard and run **Scan history now**. The expected result is an explicit configuration error until a connector is set up.

## Privacy and maintenance

The database is local SQLite with WAL enabled. Clear it by stopping the app and removing only the intended `data\*.sqlite3*` files if you want to rebuild the model. Keep `.env`, headers JSON, and database backups private. YouTube Music's private DOM and provider endpoints can change; connector status messages are designed to expose that condition rather than silently claim a complete scan.
