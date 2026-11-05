# MotoGParr — Architecture

Single source of truth for how the app fits together at **v2.4.1**. Written after
reading the code, not after reading the old session notes. Where the two
disagreed, the code won.

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
| `motogp_webui.py` | Flask UI + JSON API, 27 routes (§5). Spawns grabber/overlay |
| `motogp_scheduler.py` | Sleep loop. Decides whether it is a race weekend and how often to poll |
| `motogp_watcher.py` | Watches the downloads dir, waits for folder stability, drives sort → tag → Discord |
| `motogp_grabber.py` | Prowlarr search, release filtering, send to SABnzbd. `backfill` / `scheduled` / `status` / `clear` |
| `motogp_sort.py` | Moves a completed download into `Round NN Venue/` structure, writes artwork |
| `motogp_plex_tagger.py` | Labels, genres, collections in Plex. Uses **exact path matching** |
| `motogp_plex_tagger.py` + `motogp_overlay_utility.py` | Artwork overlay/poster generation |
| `motogp_common.py` | Shared spine: session enum, logging, calendar access, release parsing/filtering, prefix build/parse |
| `activity_emitter.py` | File-locked JSON event bus for the Activity tab (`ActivityStage` enum) |
| `grab_stats.py` | Stats derived from `grabber_state.json` |
| `motogp_discord.py` | Webhook notifications, has its own rate-limit file |
| `utils/config_loader.py` | **`ENV_SPEC`**, settings precedence, validation, startup report |
| `utils/calendar_sync.py` | Same-season check + safe replacement of `/config` calendars |
| `utils/validate_calendar.py` | CLI calendar/library checker |
| `utils/healthcheck.py` | stdlib-only `HEALTHCHECK` probe for `/` |

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
| `/config/settings.yml` | entrypoint (template), WebUI `/app-config` + `/settings` | `config_loader` | services, paths, plex mapping, `grabber.*`, `timezone` |
| `/config/webui_settings.json` | WebUI `/settings` | `motogp_common.get_desired_sessions()`, WebUI | desired sessions, `max_grabs_per_run` |

The desired-sessions setting **only** exists in JSON. `max_grabs_per_run` is
written to *both*, and the JSON copy is not what the grabber reads — the grabber
uses `config_loader.get_max_grabs_per_run()`. Known inconsistency; do not assume
`/settings` changes take effect for the grabber without checking.

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
| `webui_settings.json` | Desired sessions, max grabs | No (resets to defaults) |
| `grabber_state.json` | `searched` / `grabbed` maps; also the stats source | No — rewrites history |
| `watcher_state.json` | processed / pending / failed, retry backoff | Loses retry state |
| `activity_events.json` | Activity tab events (file-locked) | Yes |
| `discord_rate_limit.json` | Webhook pacing | Yes |
| `logs/motogp.log` (+ debug) | `RotatingFileHandler`, 50 MB debug cap | Yes |
| `motogp_<year>.json` | Season calendars, refreshed from image | No — re-copied on start |
| `*.bak` | Pre-refresh calendar backup | Yes |

Only the activity bus uses locking. Everything else is plain read/write of a JSON
file from two processes, so a reader can meet a truncated file. Several readers
catch and default; new code must do the same.

---

## 5. WebUI surface

28 routes. Nav tabs: Home, Download, Settings, Stats, Calendar, Activity; a
bottom nav for mobile; `/logs` is reachable but not in the top nav.

| Route | Method | What it does | Risk |
|---|---|---|---|
| `/` | GET | Dashboard | low |
| `/library` | GET | Download/library view | low |
| `/settings-page` | GET | Settings + admin sub-tab | low (data gated client-side only) |
| `/logs` | GET | Log viewer | **exposes log text** |
| `/run` | POST | Spawns grabber/overlay with a client-supplied arg string | **high — unauthenticated action** |
| `/settings` | GET/POST | Reads/writes `webui_settings.json` + `grabber.max_grabs_per_run` | **write** |
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
| `/activity`, `/activity/stream` | GET | Activity feed + SSE | low |
| `/calendar`, `/calendar-data` | GET | Calendar views | low |
| `/api/sabnzbd/queue` | GET | Proxies SABnzbd queue with the API key | medium |
| `/flags/<path>`, `/assets/<path>` | GET | Static files | low (path must be checked) |
| `/manifest.json`, `/sw.js` | GET | PWA — public | — |
| `/healthz` | GET | Liveness for the Docker HEALTHCHECK — public | — |

Every route in the table requires a verified Cloudflare identity except the two
marked *public*. `app.after_request` adds `X-Content-Type-Options: nosniff`,
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

- **151 tests** in `tests/`, all passing at v2.4.1. Pure unit/behavioural; no
  Docker required.
- `tests/test_webui_security.py` is a set of **characterisation tests that assert
  the current insecure behaviour** (secrets returned by `/app-config`, config
  keys dropped on save, wildcard CORS, every route reachable anonymously, the
  route list enumerated from `url_map`). They are expected to fail when auth
  lands — invert them in the fixing commit. Do not "fix" them by weakening the
  assertions.
- `tests/test_container_lifecycle.py` extracts shell blocks from `entrypoint.sh`
  and runs them against a throwaway `/config`. Good, but it is string/block
  extraction — it does not prove the image runs. Hence the smoke-test item.
- `tests/test_unraid_template.py` cross-checks `unraid/motogparr.xml` against
  `ENV_SPEC`.
- `tests/test_config_loader.py` covers precedence and the empty-means-unset rule.
- CI: `docker-publish.yml` (tests → build → push) and `calendar-check.yml`
  (calendar validation, duplicate JSON key rejection). Both run on PRs.

Run `pytest tests`, never bare `pytest` — the root scratch scripts break
collection (`SANDBOX.md`).

---

## 8. Container build

`python:3.11-slim`, venv at `/opt/venv` (hence the hard-coded `PYTHON_PATH` in
the WebUI), `fonts-dejavu-core` for overlay text, `EXPOSE 5679`,
`HEALTHCHECK` → `utils/healthcheck.py` (stdlib only; the image has no `curl`).

The Dockerfile copies an explicit list of files rather than the tree — so a new
top-level module is invisible to the image until you add a `COPY` line. It also
means `test_cleanup_utility.py` shipping in the image was an explicit choice, not
an accident.

No `USER` directive: the container runs as root. No `.dockerignore`: the whole
755 MB `assets/` tree and 373 MB `.git` are sent to the build daemon, and the
build context includes `*.backup*` files (which `COPY` avoids, but the context
still carries).

`assets/` is large because artwork is baked in per class/season; treat it as an
external-data candidate if image size becomes a problem.

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

Two known cosmetic gaps: some stages (tagging, plex) show a generic label
because the session name is not in the event, and the SAB progress gradient may
not render in the mobile PWA (fallback text is fine).

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
