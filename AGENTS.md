# AGENTS.md — read this first

Kickoff document for any agent (or human) working on MotoGParr. It tells you what
the app is, which document to read next, what has already been fixed, what is
still broken, and the order to fix it in. Keep it current: when you change the
plan or land a priority item, edit this file in the same PR.

**Current release: v2.4.1** · **Tests: 135 passing** · **Single user, self-hosted**

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

1. **Round numbers are permanent identifiers.** They are part of folder names,
   filenames and Plex episode identity. Renumbering mid-season renames files and
   Plex treats them as new items (lost watched state, possible duplicates). See
   `docs/CALENDAR_CHANGES.md`.
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
.venv/bin/python -m pytest tests -q          # expect 135 passed
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

The container listens on port 5679 with **no application-level authentication**.
External access is deliberately via **Cloudflare Zero Trust (Access)** with an
email policy, so Cloudflare is the outer gate — but the app currently trusts
that gate blindly and validates nothing itself. Nothing in the codebase reads
`Cf-Access-Jwt-Assertion`, there is no `AUTH_*` handling, and `CORS` is `*` on
every response.

Target model: Cloudflare remains the outer gate; the app **independently
verifies** the `Cf-Access-Jwt-Assertion` JWT (issuer, audience, signature,
expiry) and requires the email to be in `AUTH_ALLOWED_EMAILS`. A verified email
that is not on the allowlist is denied. There is no self-sign-up and no user
management — one allowlisted email, one user. Requests that bypass Cloudflare
entirely (direct LAN hits, other containers on the same Docker network) must be
denied, not trusted.

Consequences to remember:

- `/app-config` returns the **Plex token, Prowlarr and SABnzbd API keys**.
- `/run` spawns a subprocess from an arbitrary client-supplied string, and
  `/download-selected` triggers downloads. Both are unauthenticated writes.
- `test_cleanup_utility.py` **deletes library folders and rewrites grabber
  state** and currently ships inside the image. It must not.
- `uiadminpassword` is a plaintext value compared with `==` on one route, and is
  not known to `config_loader`, so `validate()` and `get_all_settings()` cannot
  see it. It gates a *tab*, not the API behind the tab.

---

## 6. Priority list and status

Headline issues from the security/design review. Status is authoritative as of
v2.4.1 — update it as items land.

| # | Issue | Status |
|---|---|---|
| 1 | No authentication on the WebUI; secrets exposed | **OPEN — highest risk** |
| 2 | No `.dockerignore`; 6 stale `.backup` files tracked in git | OPEN |
| 3 | Container runs as root (no `USER` directive) | OPEN (needs decision, §7) |
| 4 | `test_cleanup_utility.py` deletes files and ships in the image | OPEN |
| 5 | No schema validation for `settings.yml` | OPEN |
| 6 | Plex library root is never checked | OPEN |
| 7 | Release notes written by hand | OPEN |
| 8 | No container integration test in CI | OPEN |
| + | Cloudflare Access JWT verification (`AUTH_*`) | OPEN — part of item 1 |

### Findings added during the documentation review

These were not in the original list. They are cheap, concrete and in the same
risk family as item 1.

| # | Finding | Where |
|---|---|---|
| A | **`POST /app-config` silently destroys config.** It rebuilds `settings.yml` from a fixed dict of six sections, so any key not in that dict — `grabber.*`, `uiadminpassword`, anything added later — is dropped on every save from the UI. Saving App Config currently wipes the admin password and grabber tuning. | `motogp_webui.py` `app_config_route` |
| B | `Access-Control-Allow-Origin: *` on every response, including `/app-config`. Any page in a browser on the LAN can read secrets cross-origin. | `motogp_webui.py` `add_cors_headers` |
| C | `uiadminpassword` is invisible to `config_loader` (not in `ENV_SPEC`, not in `validate()`), compared in plaintext, and gates only the *client*. | `motogp_webui.py` `verify_admin` |
| D | `/logs/stream` and `/logs/recent` expose log lines; URLs containing API keys have appeared in these logs before. | `motogp_webui.py` |
| E | `grabber.allowed_groups`, `grabber.required_quality`, `grabber.search_delay_hours` are read from config but no UI writes them — YAML/env only, and undocumented in `settings.example.yaml`. Silent-default territory. | `utils/config_loader.py` |

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
This is the priority. Slices:

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

### PR 3 — Hygiene *(items 2, 4)*
- `.dockerignore`: `.git`, `*.backup*`, `aicontextual_notes`, `.venv*`, tests,
  `docs`, `AGENTS.md`, `SANDBOX.md`, caches. Watch the build context: `assets/`
  is ~755 MB and `.git` 373 MB, all of it currently sent to the daemon.
- Delete the six tracked `*.backup*` files (`git rm`), and the `.backup` files
  under `templates/`. They are pre-refactor snapshots with no value now that git
  is the history.
- Move `test_cleanup_utility.py` out of the image — `scripts/` (not copied by
  the Dockerfile) or delete it. It rewrites library state and is a foot-gun if
  reachable; it must not be in `/app`. Its imports were broken for months
  without CI noticing, which is the argument for it not being a test at all.
- `test_prowlarr.py` in the repo root has the same problem (collection error).

### PR 4 — Correctness *(items 5, 6, finding E)*
- **`settings.yml` schema** (`utils/schema.py` or a table in `config_loader`):
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

### PR 5 — Release engineering *(items 7, 8)*
- **Generated release notes**: `.github/workflows/release.yml` using
  `gh release create --generate-notes` (categorised labels), triggered by a
  `v*` tag. Write the notes into `RELEASE_NOTES_vX.Y.Z.md` as an artifact and
  commit/link it, so the file the user asked for exists alongside the GitHub
  release. Tags must stay `vX.Y.Z` semver for the existing `metadata-action`
  patterns (the `V.2.2.1` tag did not produce `2.2` tags).
- **Container smoke test** in CI: `docker build`, run with a temp `/config`,
  assert exit code, `/` returns 200, `/healthz`-equivalent healthy, startup
  banner prints the version, and that the image contains no `*.backup*`,
  no `test_*.py`, and no `.git`. This is the only place item 3 and the
  entrypoint can be validated — there is no Docker daemon in the agent sandbox.

### PR 6 — Non-root container *(item 3, needs your decision)*
Not in the original order because it interacts with Unraid. On Unraid the
media/appdata shares are owned by `nobody:users` (99:100), so a blind
`USER nobody` produces permission errors on `/config` and `/media` writes. The
safe shape is a build-arg/`PUID`+`PGID` pair defaulting to `99:100`, plus an
optional `UMASK`, plus a startup check that says which path is unwritable rather
than failing with `EACCES`. Decide: (a) `99:100` default with `PUID`/`PGID`
overrides, or (b) keep root and document it. Recommend (a), shipped with the
smoke test asserting the process is not UID 0.

---

## 8. Known traps (learned the hard way)

- The WebUI and scheduler are separate processes that share `/config` JSON
  state by **writing files with no locking** (activity events use a lock; most
  others do not). Assume a reader can see a half-written file and handle
  `JSONDecodeError` — several places already do, silently.
- `discover_seasons()` reads `/config`, not the image, so a season only appears
  in the UI once the entrypoint has copied the calendar into `/config`.
- `WEBUI_CONFIG_FILE` (`webui_settings.json`) is the *real* store for desired
  sessions and `max_grabs_per_run` at runtime; `settings.yml` is only written by
  the WebUI for `grabber.max_grabs_per_run`. Two stores, one visible setting —
  keep that in mind before "fixing" either.
- `grab_stats.py` and friends derive stats from `grabber_state.json`; deleting
  that file (as the cleanup utility does) rewrites the app's history.
- Flask dev server is used in production (`app.run`). Fine on a LAN behind
  Cloudflare, but do not increase worker count or add threading without
  revisiting the shared-state assumption above.
