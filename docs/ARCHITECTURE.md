# MotoGParr — Architecture

Agent-facing architecture reference, last refreshed against repository release
**v2.9.0** on 2026-11-07. It describes the current implementation, not a promise
that every historic note or release-specific detail remains accurate. When code
and this document disagree, verify the code and tests, then update this document
in the same change.

Intended audience: an agent about to change something. If you are only installing
the app, read `README.md` instead.

---

## 1. Shape of the system

One Docker image, one container, **two long-lived processes** supervised by a
bash entrypoint, plus short-lived child processes the WebUI spawns on demand.

```
entrypoint.sh (bash, PID 1)                       ← supervises, never exits
├── motogp_webui.py        Flask on 0.0.0.0:5679  ← restarted up to 5x if it dies
└── motogp_scheduler.py    loop, no port         ← NOT restarted; its death stops the container
        └── motogp_watcher.py (thread)
                └── motogp_sort.py, motogp_plex_tagger.py   (subprocess per download)
```

The WebUI additionally spawns `motogp_grabber.py` and `motogp_overlay_utility.py`
as children of itself. Everything talks to everything else through **files in
`/config`** — there is no queue, no broker, no database.

Why the asymmetry: a dead WebUI behind a healthy container was a real outage
(blank page, scheduler happily running). So the WebUI is supervised and probed by
`HEALTHCHECK`. A dead *scheduler* means the container has stopped doing its job,
so stopping is honest.

### Components

| File | Role |
|---|---|
| `entrypoint.sh` | Banner/version, calendar sync into `/config`, starter `settings.yml`, config + path validation, path severity gate, start and supervise both processes |
| `motogp_webui.py` | Flask UI + JSON API; route inventory in §5. Spawns grabber/overlay |
| `motogp_scheduler.py` | Sleep loop. Decides whether it is a race weekend and how often to poll |
| `motogp_watcher.py` | Watches the downloads dir, waits for folder stability, drives sort → tag → Discord |
| `motogp_grabber.py` | Prowlarr search, release filtering, send to SABnzbd. `backfill` / `scheduled` / `status` / `clear` |
| `motogp_sort.py` | Moves a completed download into `Round NN Venue/` structure, writes artwork |
| `motogp_plex_tagger.py` | Labels, genres, collections in Plex. Uses **exact path matching** |
| `motogp_plex_tagger.py` + `motogp_overlay_utility.py` | Artwork overlay/poster generation |
| `motogp_common.py` | Shared spine: session enum, logging, calendar access, release parsing/filtering, prefix build/parse |
| `activity_emitter.py` | JSON event bus for the Activity tab (`ActivityStage` enum); every emit is one locked transaction via `utils/json_store.py` (§4) |
| `utils/json_store.py` | The one persistence contract for shared `/config` JSON: sidecar lock held across load-mutate-commit, unique temp file, atomic replace, loud corruption (§4) |
| `utils/webui_jobs.py` | The WebUI's one-at-a-time background job: synchronous slot reservation, bounded output, exit code, timeout. Process-local by design — the reason Slice 6 cannot add WSGI workers yet |
| `utils/origin_guard.py` | Browser-write defence-in-depth: write methods need an `Origin`/`Referer` host matching the request host (headerless non-browser clients pass to auth). CSP is report-only in `motogp_webui.add_security_headers` |
| `grab_stats.py` | Stats derived from `grabber_state.json` |
| `motogp_discord.py` | Webhook notifications, has its own rate-limit file |
| `utils/config_loader.py` | **`ENV_SPEC`**, settings precedence, validation, startup report |
| `utils/calendar_sync.py` | Same-season check + safe replacement of `/config` calendars |
| `utils/validate_calendar.py` | CLI calendar/library checker |
| `utils/healthcheck.py` | stdlib-only `HEALTHCHECK` probe for `/healthz` |
| `utils/background_health.py` | Scheduler-loop and watcher-thread progress records plus the staleness rule (§8b). Reported to the user; deliberately not part of the Docker probe |
| `utils/settings_schema.py` | Permitted shape of `settings.yml`; unknown/wrong-typed keys |
| `utils/plexcheck.py` | Is Plex actually scanning the library folder? |
| `utils/permissions.py` | Can PUID:PGID write where it must? |
| `utils/release_mismatches.py` | Marked-wrong releases: the block list, the atomic store and the library quarantine move |

---

## 2. The data pipeline

```
config/motogp_<year>.json
    │  rounds: number, venue, dates, classes, weekend format
    ▼
grabber ── Prowlarr /api/v1/search ──► filter_releases() ──► SABnzbd addurl
    │                                          │
    │                                          └─ desired sessions from
    │                                             webui_settings.json
    ▼
/downloads/complete/motogp/<folder>
    │  watcher: folder must be stable 15s, checked every 10s
    ▼
motogp_sort.py ──► /media/.../2026/Round 07 France/26R07E21 MotoGP Race/
    │                  └─ folder + file naming carries season/round/episode
    ▼
motogp_plex_tagger.py ── Plex API ──► labels/genres/collections
    │                       ▲
    │                       └─ needs path translation (§3)
    ▼
motogp_discord.py ──► webhook
```

### Naming is identity

`26R07E21 MotoGP Race` — season, round, session-as-episode. The round number is
simultaneously a sort key and the episode's identity in Plex, so renaming a round
renames files and Plex sees new items. See `docs/CALENDAR_CHANGES.md`; do not
invent new naming schemes.

### Two clocks

- **Race weekend** (detected from calendar dates): poll every 60 min.
- **Off-weekend**: every 360 min. Quiet hours: 120 min.

The watcher polls independently every 10 s and only processes a folder once it
has been unchanged for 15 s.

---

## 3. Configuration

Three layers, resolved per key:

```
environment variable   >   settings.yml   >   built-in default
```

An env var that is set but **empty** counts as unset. That is deliberate: the
Unraid template ships blank path fields so `settings.yml` stays authoritative.

`ENV_SPEC` in `utils/config_loader.py` is the registry. `--env-help` renders it;
the Unraid template test cross-checks it. Anything not in `ENV_SPEC` is not read.

Three env vars are read by `entrypoint.sh`, not Python, and are listed separately
(`ENTRYPOINT_ENV`): `MOTOGPARR_CALENDAR_UPDATE`, `MOTOGPARR_SKIP_PATH_CHECK`,
plus `CONFIG_DIR`. `MOTOGPARR_VERSION` is informational.

### Two settings stores (important, easy to get wrong)

| Store | Written by | Read by | Holds |
|---|---|---|---|
| `/config/settings.yml` | entrypoint (template), WebUI `/app-config` + `/settings` | `config_loader` | services, paths, plex mapping, `grabber.*` (including `max_grabs_per_run` and `search_delay_minutes`), `webui.quick_status_log_lines`, `timezone` |
| `/config/webui_settings.json` | WebUI `/settings` (sessions only) | `motogp_common.get_desired_sessions()`, WebUI | desired sessions only |

The desired-sessions setting **only** exists in JSON. `max_grabs_per_run` is a
`grabber.*` setting: it lives in settings.yml, which is what the grabber reads
via `config_loader.get_max_grabs_per_run()`. The UI merges it in at
presentation time (`/settings` and the settings page), so there is exactly one
source of truth. On first load, a legacy `max_grabs_per_run` still sitting in
`webui_settings.json` is migrated once into settings.yml and then dropped from
the JSON; settings.yml wins on disagreement. Neither store may silently shadow
the other.

### Plex path mapping

Plex matches by **exact string** against paths as *Plex* sees them. If Plex runs
in its own container the prefixes differ (`/media` vs `/data/media`), nothing
matches, and tagging fails silently. `plex.path_mapping.container` and
`.plex` — both halves required — or `PLEX_PATH_CONTAINER` / `PLEX_PATH_PLEX`.

This is the single most common cause of "tagging stopped working".

---

## 4. Runtime state in `/config`

| File | Purpose | Safe to delete? |
|---|---|---|
| `settings.yml` | Main config | No |
| `webui_settings.json` | Desired sessions only | No (resets to defaults) |
| `grabber_state.json` | `searched` / `grabbed` maps; also the stats source | No — rewrites history |
| `watcher_state.json` | processed / pending / failed, retry backoff | Loses retry state |
| `activity_events.json` | Activity tab events (file-locked) | Yes |
| `discord_rate_limit.json` | Webhook pacing | Yes |
| `release_mismatches.json` (+ `.lock`) | Releases marked as wrong, with the matcher's reasoning. Written by the WebUI under a sidecar lock, read by the grabber | No — un-blocks every marked release |
| `background_health.json` (+ `.lock`) | Scheduler-loop and watcher progress timestamps (§8b). Written by the scheduler and the watcher thread, read by the WebUI | Yes — resets both components to "unknown", which is reported as such |
| `logs/motogp.log` (+ debug) | `RotatingFileHandler`, 50 MB debug cap | Yes |
| `motogp_<year>.json` | Season calendars, refreshed from image | No — re-copied on start |
| `*.bak` | Pre-refresh calendar backup | Yes |
| `*.lock` | Sidecar locks for the JSON stores above (`utils/json_store.py`) | Leave them: empty, tiny, and deleting one a process holds open breaks its locking |

Most state is plain JSON shared by separate processes, but the four stores
below no longer write it directly. They all go through `utils/json_store.py`,
which is the single implementation of the persistence contract: a stable
sidecar lock (`<name>.lock`, never the data file or a temp file) held across
the whole load-mutate-commit transaction, a unique temporary file in the same
directory, flush + fsync, then `os.replace`. A reader therefore sees the old
file or the new one, never a truncated mixture, and two writers cannot read
the same snapshot and overwrite each other's updates. Corrupt JSON is logged
at ERROR naming the path before the caller's default is used - silently
treating corrupt state as empty state is how "the file is broken" used to
become an evening of "why did it do that again".

| Store | Transaction model |
|---|---|
| `activity_events.json` | `update_json`: reloaded under the lock for every emit/progress/clear |
| `release_mismatches.json` | Same contract (the original implementation, now delegating to the helper) |
| `grabber_state.json` | Long-lived in-memory state; `save()` reloads under the lock and three-way merges against the load-time snapshot, so overlapping scheduled/manual grabbers keep each other's entries and this process's deletions still apply |
| `watcher_state.json` | Same three-way merge on `_save()` (`processed` merged as a set) |
| `webui_settings.json` | Locked atomic whole-document write; callers submit the complete document. The one-time `max_grabs_per_run` migration re-reads and writes under the same lock |

Still outside the contract, deliberately: `discord_rate_limit.json` (one
writer, disposable), `settings.yml` (owned by `utils/config_loader.py`, which
has its own write path), and `scripts/cleanup_utility.py` (offline,
`--confirm`-gated, not in the image). New code must use `utils.json_store`
for any new shared JSON state and must handle malformed state visibly rather
than silently replacing useful data with defaults.

---

### Correcting a mismatch (the loop this exists to break)

`filter_releases()` is a set of rules, so a release the rules wrongly accept is
selected every single time the session is searched. Deleting the file and asking
again therefore does nothing. Since this feature, the download tab's session row
carries a 🚫 button that opens a dialog with two jobs:

1. **show the evidence** - the release title the grabber recorded
   (`grabber_state.json`), and `explain_release()`, which runs the *real* filter
   over that title and returns every check in order with the value that matched
   (venue alias, round pattern, resolved session, score). This is the part that
   makes a pattern fix possible: "it matched" is what has to be corrected, and
   a second description of the rules in the UI would drift and then explain the
   wrong thing.
2. **record the block** - `utils/release_mismatches.py` stores
   `release_mismatches.json` with the title, scope, target and evidence.
   `filter_releases(excluded_titles=...)` drops it before scoring, so the next
   `backfill --force` selects the next-best candidate instead.

Scope defaults to the one session; a global entry is offered for a title that is
wrong wherever it appears (an off-by-one round number). Matching is on a
normalised title (case, dots and separators folded) and nothing fuzzier - a
looser rule would block a good release because it resembles a bad one, and the
symptom would be a session that silently never downloads.

Two details matter and are covered by tests:

- **The old folder is moved, never deleted.** If the session is already sorted,
  a replacement download's destination exists, and `shutil.move(src, dst)` would
  nest the new folder *inside* the old one - Plex keeps showing the wrong file
  and nothing has visibly failed. Both the dialog (opt-in, default on) and
  `motogp_sort._clear_existing_session_folder()` move it to
  `<library>/.mismatched/<season>/`. The leading dot keeps it out of Plex's
  scanner and out of every folder walk in this app; the media is still on disk.
  Deleting media from a web request is not something this app does.
- **The replacement is not a second download path.** It runs the existing
  `backfill --season S --round R --sessions E<element> --force` command, built
  server-side from a resolved `SessionType`. `--force` is required because the
  grabber's 30-minute search cooldown would otherwise skip the session and the
  UI would report a download that never started.

## 5. WebUI surface

The route inventory below is descriptive, not a route-count invariant; use
`app.url_map` and `tests/test_webui_auth.py` for the current route set. Nav tabs:
Home, Download, Settings, Stats, Calendar, Activity; a bottom nav for mobile;
`/logs` is reachable but not in the top nav.

| Route | Method | What it does | Risk |
|---|---|---|---|
| `/` | GET | Dashboard | low |
| `/library` | GET | Download/library view | low |
| `/settings-page` | GET | Settings + admin sub-tab | low; server-side auth applies to the route |
| `/logs` | GET | Log viewer | **exposes log text** |
| `/run` | POST | Starts one typed job (`{action, season, round, sessions, scale, dry_run, force}`); `build_job()` validates fields and builds the argv server-side | **write — authenticated action; singleton slot claimed synchronously under a lock, output bounded, 1h timeout, exit code recorded (utils/webui_jobs.py)** |
| `/settings` | GET/POST | Reads/writes sessions in `webui_settings.json`; `max_grabs_per_run` is read from and written to `settings.yml` via `config_loader` | **write** |
| `/status` | GET | Background command output | low |
| `/api/activity-events` | GET | Activity events | low |
| `/api/activity-events/clear` | POST | Clears events | write |
| `/library-status` | GET | Scans media library for a season | medium (filesystem walk) |
| `/app-config` | GET/POST | **Returns Plex token + Prowlarr/SAB keys**; POST rewrites `settings.yml` | **highest** |
| `/test-connections` | GET | Probes Prowlarr/SAB/Plex with stored keys | medium |
| `/logs/stream` | GET | SSE log tail | **exposes log text** |
| `/logs/recent` | GET | Recent log lines | **exposes log text** |
| `/stats` | GET | Stats page | low |
| `/failed-sessions` | GET | Failed watcher items | low |
| `/download-selected` | POST | Triggers downloads | **high — action** |
| `/mismatch` | GET | Release-mismatch log (blocked releases, evidence, reason) | low |
| `/api/release-mismatch` | GET | Evidence for one session: the recorded release and *why* the matcher accepted it | low (release titles only) |
| `/api/release-mismatch` | POST | Records a block; optionally moves the wrong library folder into `<library>/.mismatched/` | **write — changes future selection and moves media** |
| `/api/release-mismatch/<id>` | DELETE | Unblocks a release | write |
| `/api/release-mismatches` | GET | All recorded mismatches (badge counts) | low |
| `/api/release-mismatch/redownload` | POST | Starts `backfill --force` for one session via the existing grabber | **high — action** |
| `/activity`, `/activity/stream` | GET | Activity feed + SSE | low |
| `/calendar`, `/calendar-data` | GET | Calendar views | low |
| `/api/sabnzbd/queue` | GET | Proxies SABnzbd queue with the API key | medium |
| `/diagnostics` | GET | Schema issues, credential provenance, Plex library check, background-progress heartbeat (§8b) | medium (no values) |
| `/flags/<path>`, `/assets/<path>` | GET | Static files | low (path must be checked) |
| `/manifest.json`, `/sw.js` | GET | PWA — public | — |
| `/healthz` | GET | Liveness for the Docker HEALTHCHECK — public | — |

Every route requires a verified Cloudflare identity except endpoints in
`PUBLIC_ENDPOINTS` (static assets, PWA plumbing and `/healthz`). With
`AUTH_MODE=none`, the guard is deliberately disabled and the port must only be
reachable on a trusted network. `app.after_request` adds `X-Content-Type-Options: nosniff`,
`X-Frame-Options: SAMEORIGIN` and `Referrer-Policy: no-referrer`; it does **not**
set `Access-Control-Allow-Origin` (that wildcard was removed in v2.5.0 — the UI
is same-origin and the header let any LAN page read `/app-config`).

---

## 6. Authentication model

Since **v2.5.0**. Before that, anything that reached 5679 got full access.

```
utils/auth.py       AccessVerifier — signature (RS256 against CF JWKS),
                    issuer, audience, expiry, email claim, allowlist.
                    No Flask import; no network in tests.
utils/web_auth.py   AuthGate (mode resolution) + install() -> before_request
                    hook + denial page/JSON. Owns PUBLIC_ENDPOINTS.
```

Modes: `cloudflare` (default when `AUTH_ISSUER` is set), `none` (explicit,
warns loudly, banner in UI). A partial `AUTH_*` config **fails closed** — gate
enabled, every request 401, reason printed at startup.

`PUBLIC_ENDPOINTS` is the whole exception list: `static`, `static_files`,
`flag_files`, `manifest`, `service_worker`, `health`. Everything else is
guarded. The guard is a `before_request` hook precisely so a new route cannot be
added unauthenticated by omission; `tests/test_webui_auth.py` enumerates
`url_map` to enforce it.

Denial is 401 with an HTML page for browsers (it must not extend `base.html` —
at `before_request` time there is no view context and rendering raises) and JSON
for `/api/*`.

The session cookie carries only the verified email, for display. It is not the
auth mechanism; `SESSION_COOKIE_SECURE` is off by default because the container
is reached over plain HTTP inside the LAN and TLS terminates at Cloudflare.

`uiadminpassword` and `/verify-admin` were **removed**. The old check compared a
plaintext value with `==`, was unknown to `config_loader`, gated only the
client-side tab while `/app-config` stayed open, and was silently deleted by the
next save from the UI.

---

## 7. Tests and CI

- **423 tests** passed in the last local verification on 2026-11-07 with
  `python -m pytest tests -q`. Treat this as a dated snapshot; rerun the suite
  rather than assuming the count is current.
- `tests/test_webui_security.py` now contains regression tests for the fixed
  `/app-config` merge, CORS removal and route security behaviour. They assert
  safe behaviour; route enumeration is in `tests/test_webui_auth.py`.
- `tests/test_container_lifecycle.py` extracts shell blocks from `entrypoint.sh`
  and runs them against a throwaway `/config`. Good, but it is string/block
  extraction — it does not prove the image runs. Hence the smoke-test item.
- `tests/test_unraid_template.py` cross-checks `unraid/motogparr.xml` against
  `ENV_SPEC`.
- `tests/test_config_loader.py` covers precedence and the empty-means-unset rule.
- CI: `docker-publish.yml` (tests → build → push), `calendar-check.yml`
  (calendar validation, duplicate JSON key rejection), `container-smoke.yml`
  (build and run the image, assert non-root + auth), `release.yml` (generate
  notes, publish, open release-notes PR), and `pr-label-check.yml` (require at
  least one label on PRs to `main` so release notes are categorised). Their
  triggers differ: calendar checks are path-filtered, the container smoke test
  skips documentation-only PRs, and release publishing runs on version tags or
  manual dispatch. Workflows use Node 24 runtimes (`actions/checkout@v5`,
  `actions/setup-python@v6`, `docker/*@v4/v6/v7`, `actions/upload-artifact@v6`).

Run `pytest tests`, never bare `pytest` — the root scratch scripts break
collection (`SANDBOX.md`).

---

## 8. Container build

`python:3.11-slim`, venv at `/opt/venv`, `fonts-dejavu-core` for overlay text,
`EXPOSE 5679`. The WebUI launches child Python processes via `sys.executable`;
the scheduler's command configuration currently names `/opt/venv/bin/python3`.
`HEALTHCHECK` → `utils/healthcheck.py` (stdlib only; the image has no `curl`).

The Dockerfile copies an explicit list of files rather than the tree — so a new
top-level module is invisible to the image until you add a `COPY` line. The flip
side is that nothing ships by accident: `test_cleanup_utility.py` was never in
the image, contrary to an early review finding.

`.dockerignore` (added v2.5.0) first reduced the measured build context from
1.23 GB to 754 MB by excluding `.git`, tests and local config. Issue #25 reduced
the remaining asset payload from ~753 MiB to ~4 MiB: `assets/external/2025`
was unused and removed, and all byte-identical venue placeholders became
relative links to `assets/motogpPoster.jpg` and `assets/motogpFanart.jpg`.
The calendar filenames and `CLASS_ASSET_DIRS` are unchanged. The links are
*source* artwork inside `/app/assets`; the sorter renders session text and
writes regular JPEGs into the library. Do not link the library outputs — Plex
may run in another container and cannot see `/app/assets`. To replace a venue's
placeholder with custom art, **unlink it before writing** or the shared master
will be changed for every venue. `tests/test_artwork_assets.py` checks the
links, and the container smoke test verifies they survive `COPY assets/`.
`tests/test_build_hygiene.py` checks both directions: development files are
excluded, and no `COPY` source is.

`scripts/` is deliberately not copied. It holds utilities that delete media.

No `USER` directive, deliberately: `entrypoint.sh` drops root to `PUID:PGID`
(default 99:100) with `setpriv` and re-execs itself. A fixed `USER` would bake in
one uid, and on Unraid the right uid is per-installation. `UMASK` is applied and
the script refuses to continue if it is still uid 0.

`utils/permissions.py` then checks write access to `/config`, the library path
and the downloads path before any work starts. Only paths the app writes are
checked, so a read-only media share is legitimate. A failure names the folder,
the uid and the `chown` command, because the alternative symptom — settings that
revert, downloads that never sort — does not look like permissions.

New venue artwork should replace the appropriate symlink with a real JPEG;
keep the other links in place so unused venues do not inflate the image again.

---

## 8a. Activity feed (worth knowing before you touch it)

Events are emitted by the grabber, watcher, sorter and tagger into
`activity_events.json` under a file lock, and read by `/activity/stream` (SSE)
and `/api/activity-events`. Stages: `searching`, `candidates_found`,
`downloading`, `download_complete`, `processing`, `metadata`, `complete`,
`error`.

Four bugs were fixed here historically and the fixes are load-bearing:

1. **Match SABnzbd jobs by episode prefix, not release name.** SAB renames the
   job to `25R02E09 ...` while the release name is entirely different; prefix
   matching is the only stable join. Derive the prefix from the event id
   (`2025_R02_E09` → `25R02E09`).
2. **Keep SAB progress in a separate client-side cache.** Activity polling (3 s)
   and SAB polling (4 s) otherwise overwrite each other and the bar flickers.
3. **Start SAB polling because an event is `downloading`,** not on page load.
4. **No emits from the watcher after the tagger's `complete`.** That produced
   "moved to library" *after* "Complete"; the tagger's `complete` is final.

Session identity is also load-bearing (issue #24). Always emit
`session_type.full_display_name`, never the classless `display_name`, for an
Activity title. `emit_activity()` retains the existing round and venue when a
later stage cannot reconstruct them; the watcher only parses the folder prefix.
`tests/test_activity_feed.py` exercises the watcher, Plex metadata and completed
history for all three race classes.

One known cosmetic gap remains: the SAB progress gradient may not render in the
mobile PWA (fallback text is fine).

## 8b. Background progress vs. container health

Two different questions, deliberately answered separately:

| Question | Answered by | Used for |
|---|---|---|
| Can the WebUI serve a page? | `utils/healthcheck.py` → `/healthz` | Docker `HEALTHCHECK`, so a dead UI no longer hides behind a green "Up" |
| Is the scheduler still looping, and the watcher still scanning? | `background_health.json` (§4) | The home health strip and `/diagnostics` - **not** the Docker probe |

The scheduler records `last_loop` at the top of every iteration, `last_check`
when a scheduled search actually ran, and `started` once at startup; the
watcher records `last_scan` after every scan cycle (including one that found
nothing, or whose downloads path is missing) and `started` when the thread
starts. "Progress" is always the loop field, never `last_check`: off-weekend
the scheduler legitimately runs no searches for days, and a rule keyed on
searches would report a perfectly healthy scheduler as stale for that whole
time.

`utils/background_health.py` holds the question and the rule; the loops only
write. `component_health()` takes an explicit `now` and does no I/O, states are
`live` / `stale` / `never`, and **`never` is never reported as healthy** - a
fresh container has no evidence yet, so it says so. The stale threshold is
twice the scheduler's *own* interval (`motogp_scheduler.CONFIG`, including the
quiet-hours interval via `is_quiet_hours()`) plus an hour of grace; reading the
intervals from the scheduler is what stops a correct overnight run from being
called dead at 03:00.

A stale heartbeat is a **warning, not a restart** (decided in Candidate A):
`/healthz` stays a stateless probe with no file reads, no network calls and no
race-weekend awareness, because a slow-but-legitimate Prowlarr search must not
put the container into a restart loop. Because the record is a shared `/config`
file it goes through `utils/json_store.py` (§4) - one component per write, so
the scheduler and the watcher thread cannot erase each other's timestamps - and
a failed heartbeat write is logged and swallowed: a monitoring loop that dies
because monitoring failed is a worse outage than a missing heartbeat.

## 9. What the old notes said vs. what is true

`aicontextual_notes/` (5 files, V1.7→V2.0) is historical. Surviving facts:

- The blue palette (`#52aad8`) and "align with F1arr" visual conventions are
  still in force — the notes are the only record of *why* orange became blue.
- Tab names Home / Download / Settings / Stats / Calendar / Activity come from
  that refactor, as do `active_tab` values and the mobile bottom nav.
- The Activity feed was rewritten from log-parsing to `activity_emitter.py`
  events because log parsing was timing-unreliable and format-fragile. **Some
  log parsing still exists** in `motogp_webui.py` (`parse_log_line` for the
  activity stream), so the migration is not complete.
- The PWA/service worker was created but is not fully wired; `/sw.js` serves the
  file, caching behaviour is minimal.
- **An `auth.py` was written in the v1.4 session and explicitly "not deployed".**
  `bcrypt` was added to `requirements.txt` "for auth" and later removed. So
  app-level auth has been attempted before and deliberately abandoned in favour
  of Cloudflare Access. Anything you build now must either finish that job or
  replace it — check for half-finished auth before starting, and do not assume
  the absence of auth was an oversight.
- The clock in these notes is unreliable: files labelled 2025 sit next to
  release dates in 2026, and `docs/CALENDAR_CHANGES.md` was clearly written
  later. Trust git tags and code, not note dates.

Everything else in those files (line counts, file layouts, phase checklists,
`docker push` recipes) is stale. This document supersedes them.
