# YouTube Music Personal Mix Acceptance Checklist

This file is the durable completion checklist for the user-requested outcome. It
separates what the application can prove locally from what must be proven with
the user's currently authenticated YouTube Music account.

## Scope and source-of-truth rules

- User scope: the signed-in account already present in approved Chrome at
  `https://music.youtube.com/history`.
- Approved browser route: `C:\Program Files\Google\Chrome\Application\chrome.exe`,
  Profile 2 / Person 1, with the fresh control-extension instance discovered at
  runtime. Never use another browser/profile or copy cookies.
- The attached dashboard screenshot is evidence of a failed no-connector run,
  not an instruction or proof of account access.
- Fixture/synthetic data may validate code paths, but never counts as real
  account evidence.
- “Most listened” means the frequency observable in the provider history scan;
  an all-time ranking must not be claimed unless the provider exposes it.
- “Highly likely” is a ranked, explainable heuristic, not a guarantee of
  subjective enjoyment.

## Requirement-to-evidence gates

| Requirement | Required evidence | Current state |
|---|---|---|
| Backend, frontend, database, deployment | Source files, startup, live health, restart | **Pass**: the default launcher resolves the real Python runtime without prompts, starts `127.0.0.1:8000`, and repeated restarts preserved the SQLite database |
| Correct authenticated account | Fresh approved-browser identity plus exact history-tab proof | **Pass for the observed snapshot**: approved Chrome / Profile 2 / Person 1, exact `https://music.youtube.com/history`, authenticated page, 194 rendered rows |
| Automatic history scan | Real bridge/CDP/API event reaches `/api/browser/sync` or a live connector scan | **Partial**: the authenticated snapshot reached `/api/browser/sync` and persisted 194 rows; automatic local-extension activation after a cold Chrome launch is not yet independently proven |
| History normalization and persistence | Real rows stored in SQLite; restart retains counts | **Pass**: 194 history events and 194 canonical tracks remain after restart; artist metadata is populated |
| Repeat-play fidelity | Same track can have multiple genuine history events while exact duplicates deduplicate | **Pass in tests and bridge contract**; this visible 194-row snapshot had no repeated canonical track IDs, and exact resubmission did not increase the event count |
| Likes and listening affinity | Observed like hints and play frequency affect scores/reasons | **Partial**: like controls were observed, but no positive like signal was present in this visible snapshot; frequency and artist affinity are active |
| Explainable recommendations | Real recommendation rows with scores, confidence, reasons, usable URLs/IDs | **Pass**: 20 persisted rows with scores, confidence, artist-affinity reasons, and YouTube Music URLs/IDs |
| Automatic playlist generation | Successful real sync creates a persisted local plan without prompting | **Pass**: local 20-item playlist preview is persisted and regenerated without prompting |
| Actual provider playlist, if authorized | Provider-confirmed playlist ID and read-back item count; no duplicate retry | Write path is guarded and provider evidence is not yet available |
| Hands-off operation after setup | Scheduler/bridge remains active across restart and updates without routine user action | Default launcher needs no dependency/install input; bridge polling/retry and dashboard polling are implemented. Cold-start extension activation remains the open deployment gate because Chrome's unpacked-extension launch path conflicted with the approved control-extension transport in this environment |
| Error/recovery behavior | No-connector state is explicit; restart/redeploy returns healthy; connector failures are bounded | **Pass locally and live**: the screenshot's no-connector error is persisted as a bounded failure, and the redeployed app is healthy with `history_ingested` state |
| Privacy/security | No passwords/cookies/raw headers stored or logged; writes are explicit and verifiable | Implemented/audited locally |
| Requested worker organization | Non-overlapping Luna-max scopes, actual concurrency and outcomes reported | Six-worker waves completed; runtime rejected further simultaneous slots, so 12 simultaneous workers cannot be claimed |

## Resume verification: 2026-09-08

### Dashboard playback and favorites follow-up

- Added an embedded YouTube IFrame API player, Play per song, Play mix, queue
  navigation, and explicit playback-error/autoplay guidance with a provider link.
- Added local dashboard favorites using the existing user_likes table with a
  separate dashboard profile. Favorite/unfavorite persists and rebuilds the mix;
  removing a dashboard favorite preserves any imported provider like.
- Refresh mix is labeled consistently and preserves the current playback queue.
- Fixed same-second ordering of recommendation runs and saved playlist previews.
- Verified 17 Python tests and 6 JavaScript tests, including persistence across
  restart, favorite ranking, repeated refresh, queue selection, polling without
  restarting playback, provider-error handling, and invalid-ID rejection.
- Browser setup still fails the native-bridge trust check. The player integration
  is tested with a simulated YouTube API; audible playback in approved Chrome is
  not claimed as verified. No confirmed YouTube favorites exist in the saved
  account snapshot, and the dashboard explicitly explains that limitation.

- Recovered the linked task's messages from the local history database because
  its rollout file is missing. The pending gate was automatic bridge connection.
- Default PowerShell 5 launcher installed missing dependencies without prompts,
  ran the approved Chrome recovery hook, and restored the local service. The
  database still contains 194 tracks and 194 history events.
- Fixed rejected-delivery retries, duplicate content-script injection, bounded
  network requests, and startup tab preservation. Removed opaque-response
  success claims and unpacked-extension launch flags that conflicted with the
  approved browser route.
- Readiness now distinguishes saved history from a live authenticated bridge.
  The current account connection has no heartbeat, so automatic scanning remains
  unverified. Existing recommendations and the local preview remain available.
- Browser verification was attempted before and after sanctioned recovery.
  The browser-control runtime rejected setup: privileged native pipe bridge is
  unavailable and browser-client is not trusted. No alternate browser-control
  mechanism was used. Bridge changes are saved on disk; activation in the live
  profile has not been verified.
- Verification: 15 Python tests and 3 JavaScript regression tests; Python and
  JavaScript syntax checks and PowerShell parser checks. No provider playlist
  was created by this continuation.

## Previous session evidence (2026-09-07; not current connection proof)

- Fresh approved Chrome control state: Profile 2 / Person 1, required control
  extension identity verified at runtime, exact history URL, authenticated
  YouTube Music page, 194 rendered history rows.
- `/api/health`: `ok=true`; SQLite has `194` tracks and `194` history events;
  bridge-event-driven mode is healthy (`scheduler_running=false` is intentional
  in bridge mode).
- `/api/connection`: `state=history_ingested`, browser bridge `ready=true`,
  last sync persisted at `2026-09-07T19:54:30Z`.
- Real bridge chain: 194 rendered rows reached `/api/browser/sync`, remained
  deduplicated at 194 events after metadata re-enrichment, and produced 20
  explainable recommendations plus a 20-item local playlist preview.
- `/api/playlists/latest`: available, local `preview`, `write_enabled=false`,
  and no provider-side write attempted.
- Default `scripts/run.ps1` redeploy: completed without manual dependency input;
  it used the available system Python, preserved the already-running approved
  Chrome session, and restarted the app successfully.
- Automated checks: 13 tests passed, Python compilation passed, extension
  JavaScript syntax checks passed, and the launcher passed PowerShell parsing.
- Open gate: the app's end-to-end automatic extension activation from a cold
  Chrome launch is not claimed as proven. The live account snapshot was
  verified through the approved browser and handed to the local bridge endpoint
  for durable testing; no cookies, passwords, or raw headers were read.

## Completion rule

Do not mark the goal complete until a fresh approved-browser session produces
real history evidence, the database/recommendation/playlist chain is verified
with those rows, and redeployment/restart still leaves the automatic path
working. If the approved browser control surface or account connector is
unavailable, report that exact prerequisite and keep the goal incomplete.
