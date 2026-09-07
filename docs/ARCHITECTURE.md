# YouTube Music history recommender architecture

Status: accepted baseline decision record
Scope: local application, SQLite persistence, FastAPI-compatible HTTP API, and a static frontend

## Decision

The application is organized around a small, dependency-free contract module:
`app/contracts.py`. Connectors normalize provider data into immutable records
where practical; storage, recommendation, HTTP, and UI layers consume those
records without importing one another. This keeps browser automation, provider
APIs, SQLite, and the recommendation algorithm replaceable.

The application is local-first. A connector may read only what it can observe
from the configured provider route. The system must not infer account access,
history availability, playlist ownership, or playlist writes from configuration
alone. Those claims require a live `ConnectorStatus` or `PlaylistWriteResult`
with provider evidence.

## Data flow

```text
Browser bridge / Browser-CDP       API connector
             \                       /
              \                     /
               -> normalized contracts -> SQLite repository
                                         |
                                         v
                                recommendation engine
                                         |
                                         v
                              FastAPI JSON boundary
                                         |
                                         v
                                 static local frontend
                                         |
                            preview (default) or apply
                                         |
                                         v
                              playlist connector + readback
```

1. A connector reports a sanitized status snapshot before a scan. The status
   says what was observed at `checked_at`; it is not a promise about future
   availability.
2. A history scan returns `HistoryScanResult`, including normalized
   `HistoryRecord` and `TrackRecord` values, a `ScanRunSummary`, pagination
   state, and structured errors. The repository persists records idempotently
   using the run and provider event identity.
3. The recommendation engine consumes normalized history and returns ranked
   `RecommendationRecord` values with a human-readable reason and optional
   history evidence IDs. Recommendations are derived from the selected local
   snapshot; they are not provider playlist state.
4. The frontend requests a preview before any mutation. A preview is represented
   by `PlaylistWriteResult(outcome="dry_run", provider_confirmed=false)`.
5. Only an explicit apply request may reach a mutating connector. The connector
   must verify the provider response (preferably by re-reading the target
   playlist) before returning `outcome="applied"` or `"partial"`.

## Contract and persistence boundaries

`app/contracts.py` is intentionally free of FastAPI, SQLite, browser, and ORM
imports. Dataclasses are frozen and slot-based where they cross module
boundaries; `ScanRunSummary` is mutable because a long-running scan updates
its pollable counters. Adapters should use these serialization rules:

- enum values are serialized with their string `.value`;
- tuples become JSON arrays and mappings become JSON objects;
- new `datetime` values are timezone-aware UTC and serialize as ISO-8601
  strings with `Z`; compatibility timestamp fields also accept an already
  serialized ISO-8601 string for direct SQLite/API adapters;
- connector error details are JSON-safe and sanitized before persistence or
  display;
- provider IDs are opaque strings. They are not account identifiers unless a
  connector explicitly documents them as such;
- `HistoryEvent`, `Recommendation`, `ConnectorResult`, and `PlaylistPlan` are
  compatibility spellings for the same stable contract family, not separate
  persistence models.

A minimal SQLite layout is expected to contain these logical tables (the
repository may choose different physical names):

| Logical data | Contract source | Important identity/index |
| --- | --- | --- |
| tracks | `TrackRecord` | provider/video ID or deterministic local track key |
| history events | `HistoryRecord`/`HistoryEvent` | provider event ID or deterministic local history ID |
| scan runs | `ScanRunSummary` | `run_id`, status, timestamps |
| recommendations | `RecommendationRecord` | recommendation ID plus source run |
| playlist attempts | `PlaylistWriteRequest`/`PlaylistWriteResult` | request ID and outcome |

History is sensitive personal data. The repository should minimize duplicate
copies, use parameterized SQL, enforce a local file/OS permission boundary,
and make destructive cleanup explicit. A failed or partial scan must not cause
the repository to delete a previous good snapshot.

## Connector modes

### Browser bridge and CDP

The browser-bridge connector accepts a bounded, validated payload from an
already authenticated, user-owned browser session. The browser-CDP connector
attaches through the approved local CDP route and may read rendered or
browser-exposed YouTube Music data. Neither route may scrape cookies, export
session tokens, silently sign in, or treat the presence of a tab as proof of
account access. A fresh status check and successful provider response are
required for each capability used. Browser state remains outside SQLite.

CDP is useful when the provider does not expose the required history surface
through a stable API. It is inherently more fragile: UI changes, a closed
browser, expired authentication, or a missing tab must produce a structured
connector error or a non-ready status rather than fabricated empty history.

### API

The API connector uses an explicitly configured provider/API integration with
least-privilege scopes. Credentials belong in the OS credential store or a
runtime secret mechanism, never in contracts, SQLite rows, frontend payloads,
or ordinary logs. API configuration alone does not set
`ConnectorStatus.authenticated` or a write capability; a live authenticated
request must establish those claims.

Both modes normalize into the same `HistoryConnector`/`ConnectorResult`
surface and, when enabled, `PlaylistWriter`. The rest of the application does
not branch on browser selectors, HTTP response shapes, or credential formats.

## Security and privacy boundaries

- Bind the local API to loopback by default. If remote access is intentionally
  enabled, require an explicit authentication and network-boundary decision.
- Keep raw browser sessions, cookies, OAuth refresh tokens, authorization
  headers, and account email addresses out of contract objects and logs.
- Treat track titles, artist names, URLs, playlist names, and history times as
  user data. Escape them for HTML and do not put them into log formats that may
  be uploaded automatically.
- Validate provider IDs, URL schemes, pagination bounds, and playlist target
  fields at the adapter/API boundary. Do not allow arbitrary URLs to become
  browser-navigation or server-fetch instructions.
- Expose connector errors to the UI only after redaction. A useful error code
  such as `auth_required`, `rate_limited`, or `provider_unavailable` is safer
  than a raw traceback or response body.
- Make read operations the default. A connector capability is an observed
  permission, not a request to exercise it.

## Playlist write policy

`PlaylistWriteRequest.mode` defaults to `DRY_RUN`. Preview code may resolve
tracks, deduplicate IDs, and show the intended playlist, but it must not send a
provider mutation. A dry-run result must have `outcome=DRY_RUN`, an empty
`applied_track_ids`, and `provider_confirmed=false`.

An apply operation requires all of the following:

1. the user explicitly selected apply (the API must not silently promote a
   preview);
2. the selected connector currently reports playlist-write capability and a
   usable status;
3. the connector sends the provider mutation and handles provider errors; and
4. the connector verifies the resulting playlist, or clearly returns a failed
   or partial result when verification is unavailable.

Only provider-confirmed IDs belong in `applied_track_ids`. A local optimistic
update, HTTP request dispatch, or an absent error is not confirmation. The UI
must use the result outcome and confirmation flag when describing what
actually happened. Playlist creation is opt-in through
`create_if_missing=false` by default. The compatibility `/api/playlists/write`
route maps its explicit `confirm=true` gate to apply intent; all other calls
remain previews or blocked dry-runs.

## Expected HTTP API

The stable contract-oriented routes are the target FastAPI surface. The
current thin application also exposes the compatibility routes in the second
table. Request and response bodies map to contracts by explicit adapters; the
contracts themselves do not depend on FastAPI.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Process/readiness check; never implies account access. |
| `GET` | `/api/connectors` | Return sanitized `ConnectorStatus` snapshots. |
| `POST` | `/api/history/scans` | Start a scan from `HistoryScanRequest`; return the `run_id` and current summary. |
| `GET` | `/api/history/scans/{run_id}` | Poll a `ScanRunSummary` and scan errors. |
| `GET` | `/api/history` | Read locally persisted history with bounded time, page, and limit parameters. |
| `GET` | `/api/recommendations` | Return recommendations for a selected local scan snapshot and limit. |
| `POST` | `/api/playlists/preview` | Validate and preview a playlist request; always forces dry-run semantics. |
| `POST` | `/api/playlists/write` | Execute `DRY_RUN` by default; permit `APPLY` only after explicit opt-in and connector checks. |

| Compatibility route | Contract role |
| --- | --- |
| `GET /api/status` | Local scheduler/last-run status; not account authentication. |
| `GET /api/overview` | Local SQLite history aggregate. |
| `GET /api/runs` | Persisted scan summaries. |
| `POST /api/scan` | Synchronous bounded scan request. |
| `POST /api/browser/sync` | Validated browser-bridge ingestion; optional local token gate. |
| `POST /api/scheduler` | Start/stop the local scheduler. |

The API should use `409` for an unsafe or stale write precondition,
`422` for invalid request data, and `503`/`502` for connector availability or
provider failures as appropriate. A successful HTTP response still does not
mean a playlist changed: clients must inspect `PlaylistWriteResult.outcome`
and `provider_confirmed`.

## Operational invariants

- A missing or expired connector produces an explicit status or error, never a
  successful empty scan that looks like no listening history.
- Scan runs are append-only in their audit fields; retries create a new run or
  update the same run with monotonic counters and preserved errors.
- Recommendation output is reproducible for a given history snapshot and
  algorithm version when the engine supports deterministic operation.
- No default endpoint mutates provider state.
- Provider success, provider readback, and local persistence are reported as
  separate facts so a local database failure cannot be mistaken for a provider
  failure (or vice versa).
