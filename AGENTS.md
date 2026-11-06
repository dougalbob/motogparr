# AGENTS.md — read this first

Kickoff document for any agent (or human) working on MotoGParr. It tells you what
the app is, which document to read next, what has already been fixed, what is
still broken, and the order to fix it in. Keep it current: when you change the
plan or land a priority item, edit this file in the same PR.

**Current release: v2.6.0** · **Tests: 345 passing** · **Single user, self-hosted**

---

## 1. What this app is

A single-container Docker app that automates MotoGP/Moto2/Moto3 session retrieval
into a Plex TV library. It is not a general-purpose arr app: it exists for one
household and one library.

```
calendar (JSON)  ->  Prowlarr search  ->  SABnzbd download
                                              |
                                     watcher detects completed folder
                                              |
                       sort into Round NN Venue/ + artwork/tags
                                              |
                                    Plex library scan
```

Full detail — runtime topology, configuration precedence, naming rules, the WebUI
route map and which routes are dangerous — is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Read it before touching
routing, config or the library layout.

Sandbox/tooling problems already solved are in **[SANDBOX.md](SANDBOX.md)**.
Check it before debugging the environment; it will save you an hour.

Release history: GitHub Releases (`gh release list`). Per-release notes:
`RELEASE_NOTES_vX.Y.Z.md` at the repo root, starting with v2.4.2.

---

## 2. Read in this order

| Order | Document | Why |
|---|---|---|
| 1 | `AGENTS.md` (this file) | Status and plan |
| 2 | `docs/ARCHITECTURE.md` | How the app actually fits together |
| 3 | `SANDBOX.md` | Environment gotchas |
| 4 | `README.md` | User-facing behaviour, install, env vars |
| 5 | `docs/CALENDAR_CHANGES.md` | The numbering rules you must not break |

`aicontextual_notes/` is **historical and superseded**. Do not treat it as
current; it described work up to v2.0 and surviving facts were folded into
`docs/ARCHITECTURE.md`.

---

## 3. Non-negotiable rules

These come from real outages. Breaking one causes silent data problems, not
visible errors.

1. **Round numbers and session numbers are permanent identifiers** — for any
   season that still has files on disk. They are part of folder names, filenames
   and Plex identity, so renumbering a live season renames files and Plex treats
   them as new items (lost watched state, possible duplicates). See
   `docs/CALENDAR_CHANGES.md`.
   Session numbers come from `SESSION_ELEMENTS`, **not** from the enum value, and
   are allocated in blocks of ten so a new session takes a free number instead of
   being appended after the Race. Never renumber an existing value; allocate into
   a gap. `LEGACY_ELEMENT_SEASON` marks the last season written with the old dense
   numbering (`SESSION_ELEMENTS_LEGACY`, read-only). A number that means different
   sessions in the two schemes must never be resolved by guessing — the season
   decides, and `tests/test_session_numbering.py` enforces both invariants.
2. **Env var > `settings.yml` > default.** An env var set but *empty* counts as
   unset. Never add a non-blank default for a path in the Unraid template.
3. **`ENV_SPEC` in `utils/config_loader.py` is the single source of truth** for
   which env vars are read. A variable not in that table is not read by
   anything — that class of bug (documented but dead `PLEX_PATH_CONTAINER`) has
   happened and is what the table exists to prevent. Adding an env var means
   adding it to `ENV_SPEC`, and the Unraid template test will check it.
4. **Fail visibly, never silently.** The recurring theme of this codebase is
   things dying quietly: a dead WebUI behind a healthy container, a path mapping
   that never matched, a calendar that was never refreshed. Prefer refusing to
   start, or a loud warning, over doing nothing.
5. **One user.** Do not build user accounts, roles, sign-up, or a household
   registry. Identity is Cloudflare Access' job; see §5.

---

## 4. Change workflow

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q          # expect 345 passed
```

Always use `pytest tests` — **not** bare `pytest`, which also collects the
root-level `test_*.py` scratch scripts and errors out. See `SANDBOX.md`.

- CI (`.github/workflows/`) must stay green: `pytest tests`, calendar validation
  on every season, and a build of the image on every PR.
- Keep the style of the existing code: docstrings explain *why* a rule exists,
  quoting the failure it prevents. Comments that restate the code are not useful.
- One concern per PR. The priority list in §6 is deliberately sliced that way.
- Never commit `*.backup*` files. They are being removed; see §6 item 2.

---

## 5. Security model (the one paragraph that matters)

External access is via **Cloudflare Zero Trust (Access)** with an email policy.
Since v2.5.0 the app **independently verifies** the `Cf-Access-Jwt-Assertion`
JWT — signature against Cloudflare's published keys, issuer, audience, expiry —
and requires the email to be in `AUTH_ALLOWED_EMAILS`. A verified email that is
not on the allowlist is denied. Cloudflare is the outer gate; the app is the
inner one, because a LAN host or another container on the Docker network never
goes through Cloudflare at all.

**There is one user and there will only ever be one.** No accounts, no sign-up,
no roster, no roles. The allowlist is the entire permission model.

Mechanics you must not break:

- The guard is a **`before_request` hook** (`utils/web_auth.py`), not a
  per-route decorator, so a new route cannot be born unauthenticated. Anything
  that must stay public is named in `PUBLIC_ENDPOINTS`, and two tests enforce
  that: one enumerates `url_map`, one forbids data routes in the allowlist.
- `utils/auth.py` never imports Flask and never echoes the attempted email into
  an exception or log line.
- `AUTH_*` live in environment variables only, never `settings.yml`: an
  allowlist in a file the UI can rewrite is not a security control.
- `/healthz` must stay public or every container reports unhealthy.
- A **partial** `AUTH_*` config fails *closed* (starts, refuses everything,
  says why). It must never fall back to open.

Still true, and still worth knowing:

- `/app-config` returns the **Plex token and Prowlarr/SABnzbd API keys** — now
  behind auth, so it matters that the auth cannot be bypassed.
- `/run` spawns a subprocess from a client-supplied string and
  `/download-selected` triggers downloads. Both are authenticated writes now,
  but the argument handling is still string-splitting: treat as untrusted.
- `scripts/cleanup_utility.py` **deletes library folders and rewrites grabber
  state**. It is no longer a test, no longer at the repo root, no longer in the
  image, and requires `--confirm`. Keep it that way: `tests/test_build_hygiene.py`
  asserts it cannot be copied in.

---

## 6. Priority list and status

Headline issues from the security/design review. Status is authoritative as of
v2.4.1 — update it as items land.

| # | Issue | Status |
|---|---|---|
| 1 | No authentication on the WebUI; secrets exposed | **DONE (v2.5.0)** — Cloudflare Access JWT verification |
| 2 | No `.dockerignore`; 6 stale `.backup` files tracked in git | **DONE (v2.5.0)** — `.dockerignore` added (context 1.23 GB → 754 MB); backups removed and gitignored |
| 3 | Container runs as root (no `USER` directive) | **DONE (v2.5.0)** — drops to PUID:PGID (99:100) via setpriv, checked at startup |
| 4 | `test_cleanup_utility.py` deletes files and ships in the image | **DONE (v2.5.0)** — moved to `scripts/`, `--confirm` gate, not in the image |
| 5 | No schema validation for `settings.yml` | **DONE (v2.5.0)** — `utils/settings_schema.py`, reported at startup and in the UI |
| 6 | Plex library root is never checked | **DONE (v2.5.0)** — `utils/plexcheck.py`, surfaced at startup and in `/diagnostics` |
| 7 | Release notes written by hand | **DONE (v2.5.0)** — generated from merged PRs, categorised by label |
| 8 | No container integration test in CI | **DONE (v2.5.0)** — `container-smoke.yml` builds, runs, and asserts non-root; green on PR #11 |
| + | Cloudflare Access JWT verification (`AUTH_*`) | **DONE (v2.5.0)** |

### Findings added during the documentation review

These were not in the original list. They are cheap, concrete and in the same
risk family as item 1.

| # | Finding | Where |
|---|---|---|
| A | **`POST /app-config` silently destroys config.** It rebuilt `settings.yml` from a fixed dict of six sections, so any key not in that dict — `grabber.*`, `uiadminpassword`, anything added later — was dropped on every save from the UI. **Fixed:** `update_settings()` deep-merges, and `_build_app_config_updates()` allowlists what the form may write. | `motogp_webui.py`, `utils/config_loader.py` |
| B | `Access-Control-Allow-Origin: *` on every response, including `/app-config`. **Fixed:** wildcard removed (the UI is same-origin); `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` added. | `motogp_webui.py` |
| C | `uiadminpassword` was plaintext, `==`-compared, invisible to `config_loader`, and gated only the client. **Fixed:** route and key removed; identity is server-side. | — |
| D | `/logs/stream` and `/logs/recent` expose log lines; URLs containing API keys have appeared in these logs before. **Mitigated:** both now require a verified identity. Logs are still log lines — do not add secrets to them. | `motogp_webui.py` |
| E | `grabber.allowed_groups`, `grabber.required_quality`, `grabber.search_delay_hours` were read from config but unreachable and undocumented. **Fixed:** documented in `settings.example.yaml`, with getters; still YAML/env only. | `utils/config_loader.py` |
| J1 | **Two settings stores shadow each other for `max_grabs_per_run`.** The UI displayed the JSON copy while the grabber read `settings.yml`. **Fixed:** `settings.yml` owns `grabber.*`; `webui_settings.json` holds sessions only; `/settings` reads `max_grabs_per_run` from `config_loader` at presentation time; a legacy JSON value is migrated once into `settings.yml` (settings.yml wins on disagreement); a corrupt or missing JSON file logs a warning naming the path and falls back to defaults. | `motogp_webui.py`, `utils/config_loader.py`, `tests/test_settings_store.py` |
| J2 | **The permission hint printed container paths as host-runnable chown commands.** **Fixed:** `utils/permissions.hint()` names the container path and tells the user to chown whatever they mounted on the host; `MOTOGPARR_HOST_MAP` prints the exact host command when known; CLI and entrypoint print identical text. | `utils/permissions.py`, `utils/config_loader.py`, `unraid/motogparr.xml`, `tests/test_permissions.py`, `README.md` |
| J3 | **Unraid TemplateURL floated on `main`, so "Apply" could silently replace a user's edited template.** **Decision (b):** pinned `TemplateURL` to the release tag (`.../v2.5.0/unraid/motogparr.xml`) and added a rot test (`tests/test_unraid_template.py`) asserting the pinned tag equals the newest tag in the repo. Trade-off: old installs don't receive template updates until the user re-points it, so **"bump TemplateURL"** is a release-checklist item on every release. | `unraid/motogparr.xml`, `tests/test_unraid_template.py` |
| J5 | **Release notes were generated as an artifact but never reached the repo.** **Fixed:** the release workflow now commits `RELEASE_NOTES_vX.Y.Z.md` to a `release-notes/vX.Y.Z` branch and opens (or updates) a pull request to `main`, so the notes reach the repository without pushing to a protected branch directly. Adds `pull-requests: write`; idempotent on re-runs. Verification requires a real tag push (maintainer's action) — stated in the PR rather than claimed. | `.github/workflows/release.yml`, `tests/test_workflows.py` |
| J4 | **GitHub Actions Node 20 deprecation.** Every run warned \"Node.js 20 is deprecated… forced to run on Node.js 24\" for 7 actions. **Fixed:** bumped each to its node24 major — `actions/checkout@v5`, `actions/setup-python@v6`, `docker/setup-buildx-action@v4`, `docker/build-push-action@v7`, `docker/login-action@v4`, `docker/metadata-action@v6`, `actions/upload-artifact@v6` — keeping permissions minimal (`contents: read`, `packages: write`, plus `pull-requests: write` for release). No version-pinning test (rots); existing workflow invariant tests cover tag patterns, non-root and auth assertions. | `.github/workflows/*.yml`, `tests/test_workflows.py` |
| L1 | **PRs to main could be merged with no labels, producing a single \"Other changes\" line in release notes.** **Fixed:** cheap CI check `.github/workflows/pr-label-check.yml` fails when a PR to main has zero labels (exempts `dependabot[bot]`), printing the categorisation map. Pure shell + `jq`, no checkout or JS action, so no Node deprecation surface. | `.github/workflows/pr-label-check.yml`, `tests/test_workflows.py` |

### Issue #25 — artwork bloat (v2.5.4)

The 23 venue placeholders for each of three classes were byte-identical copies
of two master JPEGs: 69 posters and 69 fanarts. They are now **relative symlinks**
to `assets/motogpPoster.jpg` and `assets/motogpFanart.jpg`. The calendar still
names each venue file, so custom artwork can replace any link without changing
calendar numbering or runtime code. The unused `assets/external/2025/` tree
(314 files, ~535 MiB) was removed after confirming it has no users. Together
these changes shrink `assets/` from ~753 MiB to ~4 MiB in the checkout/build
context. Tests cover every calendar reference and check that the sorter still
writes *real* images to the library; CI must also verify the links inside the
built image (no Docker daemon in the sandbox).

**Do not write through a venue symlink** when adding custom art: that overwrites
the shared master for every venue. Unlink it first, then install a real JPEG at
the same filename.

### Issue #30 — calendar popup per-day session times (v2.5.6)

`/calendar` defaults to the current season (`default_calendar_year()`) rather
than hard-coded 2025, and `build_round_schedule()` emits per-day session blocks
with exact UTC timestamps derived from track-local date + `local` + `utc_offset`
(preserving midnight-crossing flyaway times such as Phillip Island Friday morning
landing on Thursday UTC, and reading directly from `round["sessions"][day]` so
the 2027 format — Friday FP2 + Sprint Qualifying, Saturday FP3, no Sunday
warm-up — works from JSON data alone). In the modal (`templates/calendar.html`),
clicking a date opens that specific day's timetable converted to the browser's
local timezone (with `Fri`/`Sat`/`Sun` switcher pills), replaces `"days to go"`
on a live weekend with **`NOW`** (when a session is underway) or the **next
session's start time**, and keeps past rounds focused solely on library status
and missing-session downloads.

### Issue #33 — 2027 race weekend format (in progress)

The 2027 format (Friday FP1/FP2/Sprint Qualifying, Saturday FP3/Q1/Q2/Sprint, no
Sunday warm-up) is data, not code: `weekend_formats` in the calendar, with each
round carrying `weekend_format` and `motogp_sessions`, and `round_runs_session()`
gating every search. That part was already in place. What was not:

**Done**

- **Session numbering reworked.** `SESSION_ELEMENTS` allocates in blocks of ten
  instead of using the enum value. The old table was dense (1-24, full), so FP3
  and Sprint Qualifying had to be appended as 23/24 — *after* the Race — and a
  2027 round listed out of order in every folder, title and CLI listing. Ordering
  is now a property of the identifier, not something patched in the UI.
- **Cutover is immediate** (`LEGACY_ELEMENT_SEASON = 2025`): 2025/2026 media is
  deleted and re-fetched under the new scheme, so one numbering applies from 2026
  onwards. `SESSION_ELEMENTS_LEGACY` is frozen and read-only. A number that means
  different sessions in the two schemes is refused rather than guessed, and
  `find_legacy_folders()` makes the library scan warn about leftovers it cannot
  read instead of silently re-downloading them.
- **Short canonical names.** `SessionType.full_short_name` gives `MotoGP FP2`
  rather than `MotoGP Free Practice 2`; it is used for the on-disk folder name
  and written to Plex as the item title. `utils/plexlabels.py` writes `title` and
  `titleSort` together (title alone would clump every same-named session), and
  reuses the `YYRREE` prefix as the sort key. On-disk and Plex names are kept
  identical so a library re-scan is a no-op rather than a silent revert.

**Outstanding**

- `get_session_times()` still resolves a session's day from `CALENDAR_KEY_TO_DAY`
  (hard-coded "saturday" for `motogp_fp2`), so a 2027 round with Friday FP2 gets
  no end time and is skipped at DEBUG level — the grabber would never search for
  it. This is the next fix.
- Release-vocabulary ambiguity: `SQ`/`SPQ`/bare `FP` fall through to the Race
  fallback, so an unrecognised title is filed as the Grand Prix. Needs a
  refuse-to-guess rule plus a per-season alias map.
- The validator does not cross-check `weekend_format` ↔ `motogp_sessions` ↔ the
  sessions block; all three realistic 2027 misconfigurations currently pass.
- No `config/motogp_2027.json` yet, so nothing exercises the format end to end.
- Plex title/sort writing depends on the object type an "Other Videos" section
  returns; `/diagnostics` → `plex_labels` reports what a live server actually
  gives back. Unverified outside a real deployment.

---

## 7. Implementation plan

Sliced into PRs that can each be reviewed and reverted on their own.
**Order is deliberate**: item A/1 before hygiene, because auth is the only one
that stops a live exposure; hygiene is cheap but does not reduce risk on its own.

### PR 1 — Documentation baseline (this PR)
`AGENTS.md`, `docs/ARCHITECTURE.md`, `SANDBOX.md`, plus
`tests/test_webui_security.py`: 29 characterisation tests that turn findings
A–D and the missing-auth claim into executable documentation. They assert the
current *bad* behaviour, so they will fail — deliberately — the moment the fix
lands, which is what stops the fix from being forgotten or half-done. No
production behaviour change.

### PR 2 — Auth: Cloudflare Access verification  *(item 1 + `AUTH_*` + findings A–D)*
**DONE (v2.5.0).** Shipped as designed: `utils/auth.py` (verification),
`utils/web_auth.py` (guard + modes + error page), `AUTH_*` in `ENV_SPEC`, the
`/app-config` merge fix, wildcard CORS removed, `uiadminpassword` deleted, plus
`/healthz` moved out of the guarded set. 45 new tests.

The remaining sub-items below are kept as the rationale, not as outstanding work.

1. **Verification module** `utils/auth.py`:
   - Verify `Cf-Access-Jwt-Assertion` against `AUTH_ISSUER` JWKS
     (`$AUTH_ISSUER/cdn-cgi/access/certs`, overridable via `AUTH_CERTS_URL`).
   - Check `aud` == `AUTH_AUDIENCE`, `iss` == `AUTH_ISSUER`, expiry, and
     signature. Cache JWKS with a TTL and refetch on unknown `kid`.
   - Require `email` ∈ `AUTH_ALLOWED_EMAILS` (comma-separated, exact match).
   - Deny by default when `AUTH_*` is configured but anything fails.
2. **Enforcement**: a Flask `before_request` guard covering *all* routes rather
   than per-route decorators, so a new route cannot be born unauthenticated.
   Decide and document the exceptions: `/manifest.json`, `/sw.js`, icons,
   `/assets/*`, and `utils/healthcheck.py`'s probe. Keep `EXPORTED`/static
   allowlist explicit and tested.
3. **`AUTH_*` into `ENV_SPEC`** (not secrets; `AUTH_ALLOWED_EMAILS` is
   personal data — do not echo it in startup output).
4. **Local/LAN development escape hatch**: an explicit `AUTH_MODE=cloudflare|none`
   (default `cloudflare` when `AUTH_*` is set, `none` otherwise), so a direct
   `docker run` on a laptop still works — but `none` must log a loud warning and
   the docs must say the container should not be exposed without CF.
5. **Fix finding A**: read-modify-write of `settings.yml` (load, apply only the
   keys the client is allowed to set, write back) instead of a fixed dict.
6. **Fix finding B**: drop `Access-Control-Allow-Origin: *`, or restrict to an
   explicit same-origin policy. The UI is same-origin; the wildcard buys nothing.
7. **Fix finding C**: retain `uiadminpassword` only as a secondary client-side
   tab lock (or remove the modal entirely now that identity is server-side).
   Do not treat it as security. If retained, compare with `hmac.compare_digest`
   and keep it out of `get_all_settings()`.
8. **Tests**: forged/expired/wrong-audience/wrong-issuer/unknown-kid tokens
   rejected; allowed email accepted; non-allowlisted email denied; guard covers
   every route (enumerate `app.url_map` and assert each rule is either
   guarded or on the public allowlist — this is the test that stops regressions).

### PR 3 — Hygiene *(items 2, 4)* — **DONE (v2.5.0)**
- `.dockerignore` added. Measured context was 1.23 GB, of which `.git` was
  372 MB; tests, docs and local config made up most of the rest. Now 754 MB.
  A test now asserts the two directions that matter: development-only paths are
  excluded, and **nothing the Dockerfile COPYs is excluded**.
- The six tracked `*.backup*` files deleted; `*.backup*`/`*.bak`/`*.orig`
  gitignored so they cannot come back.
- `test_cleanup_utility.py` → `scripts/cleanup_utility.py`: renamed (it is not a
  test — it asserts nothing and `pytest` could collect it), moved out of the root,
  out of the image, and given a `--confirm` gate plus a "is this actually a
  session folder" check before `rmtree`. Its library default also did not match
  the documented layout, so it had been searching the wrong tree.
- `test_prowlarr.py` deleted: it held a live Prowlarr API key (see §7a), had a
  collection error, and was a scratch script.
- `MOTOGPARR_COOKIE_SECURE` added to `ENV_SPEC` and the Unraid template. It was
  read by the WebUI but unregistered, which is the "documented but dead" class
  this repo already got burned by.

**Correction to the original finding:** item 4 said the cleanup utility "ships in
the image". It did not — the Dockerfile copies an explicit file list and never
included it. The exposure was real but narrower: it was collectable by `pytest`
and sitting in a public repo. Verifying that cost two minutes and is worth
repeating before recording a finding as fact.

### PR 4 — Correctness *(items 5, 6, finding E)* — **DONE (v2.5.0)**

What shipped:

- `utils/settings_schema.py` — the permitted shape of settings.yml, with
  `unknown-key` / `wrong-type` / `bad-value` issues and "did you mean"
  suggestions. Reported through `validate()` (so the WebUI shows it), at startup,
  and via `python -m utils.settings_schema`. Deliberately never fatal: the app
  has a default for everything and an outage is worse than a warning.
  Two design rules, both enforced by tests: an empty value is *not* a type error
  (the loader treats empty as unset), and `library_id: 16` is fine unquoted.
- `utils/plexcheck.py` — compares the configured library path with the Plex
  library's real `locations`, translated through `plex.path_mapping` first.
  Distinguishes `ok` / `not_scanned` / `mapping_missing` / `unreachable` /
  `unknown`, names the inverted-mapping case explicitly, and never guesses.
  Uses a real path-boundary check, not `startswith`, so `/media/MotoGP2` is not
  mistaken for `/media/MotoGP`.
- Both surface at startup and in **Settings → Admin → 🩺 Diagnostics**, together
  with `credential_sources()` — which reports `env` vs `settings.yml` per
  credential (never the value), because an env var in the Unraid template
  silently beats a key typed into the UI.

**Regression found while doing this:** `get_allowed_groups()` had been defined
*twice*, the second definition shadowing the first and returning `[]` when
unset. `filter_releases()` rejects any release not from an allowed group, so the
grabber would have matched **nothing** — silently, because an empty candidate
list looks identical to "no releases published yet". Introduced by the PR 2 edit,
caught by review, now covered by a test that also asserts the default agrees
with the copy in `motogp_common.filter_releases()`. Lesson: a duplicated `def`
is invisible; check for shadows before assuming a helper is what you wrote.

- **`settings.yml` schema** (`utils/settings_schema.py`):
  known sections/keys with types and defaults, mirroring `ENV_SPEC`. Unknown key
  → warning in `validate()` and at startup, naming the key and the nearest known
  one. Reported via `/app-config` validation so the UI can show it. Add the
  missing documented keys from finding E to `settings.example.yaml`.
- **Plex library-root check**: read the section's `locations` from Plex
  (`library.section(...)` / `/library/sections`), translate through
  `plex.path_mapping`, and compare with `library.motogp_path`. If the configured
  library path is not inside a Plex-scanned location, say so loudly at startup
  and in the UI — "Plex is not scanning this folder" is the actual cause of the
  missing-sessions mystery, and it is checkable.

### PR 5 — Release engineering *(items 7, 8)* — **DONE (v2.5.0)**

- `.github/workflows/release.yml` — on a `v*` tag, generates notes from merged
  PRs via the `releases/generate-notes` API (POST, JSON body — `gh api` defaults
  to GET and would 404), publishes or updates the release, and uploads
  `RELEASE_NOTES_v<version>.md` as an artifact. It does **not** push to `main`:
  the notes file is an artifact, and keeping it in the repo is a follow-up PR.
- `.github/release.yml` — label → category. Unlabelled PRs land in "Other
  changes" so nothing is ever omitted, only uncategorised.
- Tag format is enforced (`vX.Y.Z`) because `V.2.2.1` produced no `:2.2` tag.
- `.github/workflows/container-smoke.yml` — builds the real image, runs it, and
  asserts: no dev files in `/app`, `setpriv` present, `/healthz` answers, Docker
  reports healthy, **no python process in the container runs as uid 0** (plus the
  startup log's `Running as: 99:100` line), calendars and a non-empty
  `settings.yml` land in `/config`, the schema check runs, and with `AUTH_*` set
  every route except `/healthz` returns 401 including with a forged token.

**This is the only place the image is exercised.** There is no Docker daemon in
the agent sandbox, so no container change can be verified locally. The runtime
behaviour of `entrypoint.sh` *can* be verified here by running it as root with
`sudo` (SANDBOX.md §10) — but the image itself cannot. Push, then read the
annotations, because job logs are unreachable from here (SANDBOX.md §11).

### PR 6 — Non-root container *(item 3)* — **DONE (v2.5.0)**

`entrypoint.sh` drops root → `PUID:PGID` (default 99:100) with `setpriv` and
re-execs itself; `UMASK` is applied; it refuses to continue if still uid 0.
`utils/permissions.py` then checks write access to `/config`, the library and the
downloads path — only paths the app writes, so a read-only media share is fine —
and refuses to start naming the folder, the uid, and the `chown` command.

Deliberately *not* a Dockerfile `USER`: the correct uid is per-installation and
an image with a fixed one cannot be adjusted on Unraid without a rebuild. A test
asserts there is no `USER` line.

Side effects worth knowing: `SCRIPT_DIR` is now derived from the script's own
location, `PYTHON` is overridable via `MOTOGPARR_PYTHON`, and the WebUI uses
`sys.executable` instead of a hard-coded `/opt/venv/bin/python3` — all three so
the entrypoint and app can be run from a checkout, which is how the privilege
drop was actually tested (as root, then asserting uid 99).

One bug this caught before release: a missing `/config/logs` was reported as NOT
WRITABLE, which would have refused to start *every* new install. `/config` belongs
to the app, so its own subfolders are created; the library and downloads paths
never are, because creating them would hide a wrong volume mapping.

`.github/workflows/container-smoke.yml` asserts the drop in CI. Five attempts got
it wrong before it worked; the traps are recorded in `SANDBOX.md` §12, and the
one-line summary is: `docker exec` starts a *new* process as the image's user (so
it reports root regardless), `docker top` has no `ps` to work with, and a `/proc`
scan matches the shell running it. It works because the scan filters on `argv[0]`
being a python interpreter and judges outside the container. It now passes, and
each of its failure paths was verified to fail for the right reason.

## 7a. Incidents

Record security incidents here with dates and required actions, so a later
session does not re-discover them or assume they were handled.

### 2026-11-05 — live Prowlarr API key committed to a public repository

`test_prowlarr.py` at the repository root contained a working Prowlarr URL and
API key:

```python
PROWLARR_URL = "http://192.168.1.241:9696"
PROWLARR_API_KEY = "<redacted>"
```

**The repository is public** (`gh repo view --json isPrivate` → `false`), and the
file arrived in the `cac8dc5` merge, so the key was world-readable from then
until it was deleted. It is still recoverable from git history
(`git log --all -S<key>`), and this branch has been pushed, so deleting the file
does not un-publish it.

**Required action (cannot be done from the sandbox — no Prowlarr access):**

1. Rotate the Prowlarr API key: Prowlarr → Settings → General → API Key → Reset.
2. Update `PROWLARR_API_KEY` wherever it is set (Unraid template, `settings.yml`).
3. Rotate any other key that was stored alongside it if they share a values file.

The file was deleted in v2.5.0 and `tests/test_build_hygiene.py` now scans the
working tree for credential literals. It cannot scan history; if a key is ever
suspected, assume it is compromised and rotate rather than rewriting history —
rewriting a public repo does not recall clones.

The same file also leaked the LAN address `192.168.1.241`; treat the layout of
your network as public knowledge from that date.

## 8. Known traps (learned the hard way)

- The WebUI and scheduler are separate processes that share `/config` JSON
  state by **writing files with no locking** (activity events use a lock; most
  others do not). Assume a reader can see a half-written file and handle
  `JSONDecodeError` — several places already do, silently.
- `discover_seasons()` reads `/config`, not the image, so a season only appears
  in the UI once the entrypoint has copied the calendar into `/config`.
- `WEBUI_CONFIG_FILE` (`webui_settings.json`) holds desired sessions only.
  `max_grabs_per_run` is a `grabber.*` setting owned by `settings.yml` and read
  by the grabber via `config_loader.get_max_grabs_per_run()`; the UI reads it
  from there at presentation time. A legacy value left in the JSON is migrated
  once on first load, and `settings.yml` wins on disagreement — never assume the
  two stores agree.
- `grab_stats.py` and friends derive stats from `grabber_state.json`; deleting
  that file (as the cleanup utility does) rewrites the app's history.
- Flask dev server is used in production (`app.run`). Fine on a LAN behind
  Cloudflare, but do not increase worker count or add threading without
  revisiting the shared-state assumption above.
