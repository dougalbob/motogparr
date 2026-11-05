# MotoGParr v2.5.0 — the WebUI actually verifies who you are

Until now the WebUI had no authentication. Anything that could reach port 5679
got the whole app: your **Plex token**, your **Prowlarr and SABnzbd API keys**,
and the ability to start downloads. Cloudflare Access was assumed to be the only
door, but a LAN host, a phone on the same Wi-Fi, or another container on the
Docker network never goes anywhere near Cloudflare.

This release makes the container check for itself.

## What changed

```
browser ──▶ Cloudflare Access (email policy)
              │  adds Cf-Access-Jwt-Assertion
              ▼
        MotoGParr ──▶ verifies signature, issuer, audience, expiry
                      └─▶ requires the email in AUTH_ALLOWED_EMAILS
```

The assertion is verified against Cloudflare's published signing keys. A
tampered or self-minted header is rejected, and so is a valid token for an
address you have not listed.

```bash
AUTH_ISSUER=https://yourteam.cloudflareaccess.com
AUTH_AUDIENCE=<the Access application's AUD tag>
AUTH_ALLOWED_EMAILS=you@example.com
```

Set **all three or none**. Two out of three is a configuration error, and the
container fails *closed* — it starts, refuses every request, and prints why. It
never assumes "unset" means "open", because a forgotten variable would then
silently expose your API keys again.

**No action is needed if you do not use Cloudflare.** Leave `AUTH_ISSUER` blank
and the WebUI behaves exactly as before — but it now says so at startup and shows
a banner in the UI, because "unauthenticated by default" should not be silent.

## The startup log now tells you

```
  Authentication:
    ⚠️  OFF - the WebUI has no authentication.
       Anyone who can reach port 5679 can read your Plex token and
       API keys and trigger downloads. Set AUTH_ISSUER, AUTH_AUDIENCE
       and AUTH_ALLOWED_EMAILS to require Cloudflare Access.
```

or

```
  Authentication:
    ✅ Cloudflare Access, issuer https://yourteam.cloudflareaccess.com
       1 allowlisted address(es), audience set
```

The number of addresses is shown, not the addresses: they are personal data and
the log is readable from the UI.

## Four bugs fixed along the way

Authentication was the headline, but writing it exposed four worse problems in
the same code path.

**Saving App Config deleted settings.** `POST /app-config` rebuilt `settings.yml`
from the six sections the form knows about, so every save from the UI silently
removed everything else — the entire `grabber` section and the admin password
included. Nothing failed; the settings just reverted. It now merges: the form may
only write the keys it is allowed to write, and everything else in the file is
left alone.

**Any page on your LAN could read the secrets.** Every response carried
`Access-Control-Allow-Origin: *`, so a malicious page in a browser that could
reach the container could read `/app-config` cross-origin and take your Plex
token. The UI is same-origin, so the wildcard bought nothing and is gone.
`X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` were added.

**The admin password was theatre.** `uiadminpassword` was compared with `==` on a
public route, was invisible to the config validation, and locked a *tab* while
the API behind it stayed wide open. It has been removed, along with
`/verify-admin`. Identity is established server-side now; a second, weaker door
would be worse than no door.

**Log routes exposed log text.** `/logs/stream` and `/logs/recent` are behind the
guard like everything else. They still contain whatever gets logged — do not put
secrets in log lines.

## What stays public

`/healthz` (the Docker health probe — a probe that needs credentials would make
every container report unhealthy), plus `/static/*`, `/assets/*`, `/flags/*`,
`/manifest.json` and `/sw.js`.

Everything else requires a verified identity. That is enforced by a single
`before_request` hook rather than a decorator on each route, so a route added
later cannot be born unauthenticated by accident — and a test walks the route
table to prove it.

## If you see "Not authorised"

The page names the actual reason rather than showing a bare 403:

| Message | Meaning |
|---|---|
| `No Cf-Access-Jwt-Assertion header` | The request did not come through Cloudflare — a LAN address, a bookmark to the IP, or another container |
| `Token audience does not match` | `AUTH_AUDIENCE` belongs to a different Access application |
| `Token issuer does not match` | `AUTH_ISSUER` is for a different Cloudflare team |
| `Token has expired` | Reload; Access usually re-issues |
| `not assigned to this installation` | Cloudflare let it through, but the address is not in `AUTH_ALLOWED_EMAILS` |

## Repository hygiene, and one thing you must do

**A live Prowlarr API key was committed to this public repository.**

`test_prowlarr.py` at the repo root contained a working Prowlarr URL and API key.
It arrived in the `cac8dc5` merge, so it has been world-readable since then. The
file is deleted in this release, but **deleting it does not un-publish it** — it
is still in git history and the branch was pushed.

> **Action: rotate the Prowlarr API key** (Settings → General → API Key → Reset)
> and update it in the container. Assume it is compromised.

The same file also exposed the LAN address `192.168.1.241`.

That file is gone, and `tests/test_build_hygiene.py` now scans the working tree
for credential literals — it cannot scan history, so the rule is: if a key may
have leaked, rotate it. Rewriting a public repository's history does not recall
the clones.

### Other cleanup

- **`.dockerignore` added.** Every build was sending the daemon 1.23 GB: `.git`
  (372 MB), `assets/` (753 MB), plus tests, docs and local config. It now sends
  754 MB, almost all of it artwork the image genuinely needs. The image itself
  was always small — the Dockerfile copies an explicit file list — but the
  transfer was not. (Shrinking `assets/` further is a separate job: 753 MB of
  posters and fanart baked into the image.)
- **Six `*.backup` files deleted.** They had drifted out of date, and git already
  holds every previous version. Now gitignored so they cannot return.
- **The cleanup utility is no longer a test, and no longer in your way.**
  `test_cleanup_utility.py` deleted media from the library, sat at the repo root
  where `pytest` could collect it, and was named like a test while asserting
  nothing (its imports had been broken for months unnoticed). It is now
  `scripts/cleanup_utility.py`: same job, requires `--confirm`, refuses to run
  outside a real session folder, and its default library path now matches the
  documented layout instead of a tree that did not exist.
- `MOTOGPARR_COOKIE_SECURE` is now a registered setting (it was read but
  undocumented). Set it to `1` only if the container is reached *solely* through
  Cloudflare.

## Two silent failures now have a voice

Both of these are the same shape of problem: nothing errors, so nothing gets
investigated.

### A misspelled setting is no longer ignored in silence

An unrecognised key in `settings.yml` used to be dropped without a word — the
app fell back to its default and behaved as if the line had never been written.
Now it is reported at startup and in the WebUI:

```
  Settings file:
    ⚠️  prowlarr.api_kye: unknown key; nothing reads it (did you mean prowlarr.api_key?)
    ⚠️  grabber.required_quality: '1440p' is not one of 720p, 1080p, 2160p
    These keys are ignored by the app. Correct or remove them.
```

It never refuses to start over one. A warning is not worth an outage.

Check on demand:

```bash
docker exec -it motogparr /opt/venv/bin/python3 -m utils.settings_schema /config/settings.yml
```

### "Plex isn't scanning this folder"

This is the one that cost you the missing sessions. If the Plex library does not
contain the folder MotoGParr writes to, everything succeeds — the download, the
sort, the file on disk — and the session simply never appears. Nothing in the app
is wrong, so nothing in its logs looks wrong.

**Settings → Admin → 🩺 Diagnostics** now answers the question directly:

```
✅ Plex is scanning this folder (via /data/media/Motorsport/MotoGP).
```

or, when it is not:

```
⚠️ Plex is NOT scanning this folder. Sessions will download, sort correctly,
   and then never appear in Plex.
   App path:        /media/Motorsport/MotoGP
   As Plex sees it: /data/media/Motorsport/MotoGP
   Plex folders:    /data/media/Unrelated
```

It names the usual causes — an **inverted** path mapping, a `library_name` that
matches no section — and says "unknown" rather than guessing when Plex is
unreachable. It never triggers a scan.

The same panel shows **where each credential came from** (`env` or
`settings.yml`), never the value. That is there because of a real trip-up: a key
typed into the WebUI is saved and looks like it took effect, while an environment
variable in the Unraid template quietly wins — so the app keeps failing
authentication with a key you have already replaced.

## A silent regression, caught before release

While building the schema check I found `get_allowed_groups()` defined **twice**
in `config_loader.py`, the second silently shadowing the first and returning an
empty list when nothing was configured. `filter_releases()` rejects any release
whose group is not in that list, so the grabber would have matched **nothing**,
for a whole season, with no error — an empty candidate list looks exactly like
"no releases published yet".

It was introduced by my own authentication change and is fixed here, with a test
that also asserts this default agrees with the copy inside
`motogp_common.filter_releases()`.

## The container no longer runs as root

It starts as root only long enough to drop privileges, then everything — the
WebUI, the scheduler, the watcher, every subprocess — runs as **`PUID`:`PGID`,
default 99:100** (Unraid's `nobody:users`).

| Variable | Default | |
|---|---|---|
| `PUID` | `99` | user id the app runs as |
| `PGID` | `100` | group id |
| `UMASK` | `0022` | permission mask for new files |

These are runtime variables rather than a `USER` line in the image, because the
right id is per-installation and a baked-in uid cannot be changed without a
rebuild.

**The failure mode this prevents** is not a permissions error. A wrong owner
looks like settings that save and then revert, downloads that complete and are
never sorted, or a `PermissionError` in a subprocess at 3am. So the container
now checks before it starts:

```
  Permissions (uid=99 gid=100):
    ✅ config (root) /config
    ✅ library      /media/Motorsport/MotoGP
    ❌ config (logs) /config/logs [NOT WRITABLE]

     This is almost always ownership. The container runs as 99:100:
       chown -R 99:100 '/config/logs'
     or set PUID/PGID to the owner of those folders.
```

Only folders the app writes to are checked, so a read-only media share is fine.
`MOTOGPARR_SKIP_PATH_CHECK=1` starts anyway.

> **Unraid users:** this is the update most likely to need a setting changed.
> If your appdata or media folders are not owned by 99:100, set `PUID`/`PGID` to
> match. The startup log tells you which folder is wrong, so you do not have to
> guess.

## Release notes are generated now, and the image is tested before it ships

Three releases this session were written by hand — an hour each, and they drift
from what the commits actually did. Notes now come from the merged pull requests,
categorised by label (`.github/release.yml`), with the tag format enforced
(`V.2.2.1` previously produced no `:2.2` image tag).

A new workflow, `container-smoke.yml`, builds the real image and runs it on every
PR that touches code. It asserts what a user would notice, and things a
syntax check cannot:

- no `*.backup`, no tests, and no media-deleting utility inside `/app`
- `setpriv` is present, and **no python process in the container runs as root** —
  not assumed from the Dockerfile, but read out of `/proc`, with the startup
  log's `Running as: 99:100` line required as corroboration
- `/healthz` answers and Docker reports `healthy`
- calendars and a non-empty `settings.yml` land in `/config`
- with `AUTH_*` set, every route returns 401 — including with a forged token —
  while `/healthz` stays public

That non-root assertion took five attempts, and each failure was a test bug that
would have hidden a real regression: `docker exec` starts a *new* process as the
image's user and so reports root however well the drop worked; `docker top` needs
`ps`, which `python:3.11-slim` does not have; a `/proc` scan matched the shell
running it. It only earns its keep now that its failure paths have been shown to
fail, which they have.

## Under the hood

- `utils/auth.py` — verification only, no Flask import. Signature, issuer,
  audience, expiry, email claim, allowlist. Keys are cached and refetched on an
  unknown `kid`, so Cloudflare key rotation needs no restart.
- `utils/web_auth.py` — mode resolution, the guard, the denial page.
- `AUTH_*` are environment variables only. Putting an allowlist in a file the UI
  can rewrite is not a security control.
- `AUTH_MODE=none` exists for local development; it warns in the log and in the UI.
- 252 tests (was 135), including 21 that sign real RS256 tokens and verify them
  against a local key set — no network in the suite.
- The denial page deliberately does *not* extend `base.html`: it renders from
  `before_request`, where no view context exists, and the first version 500'd
  instead of returning 401.

## Upgrading

Pull and Force Update.

1. **Update the Unraid template** so the new variables appear (`PUID`, `PGID`,
   `UMASK`, and the five `AUTH_*`). The container works without them — defaults
   are 99:100 and auth stays off — but the startup log will tell you what is
   unset.
2. If you use Cloudflare Access, set `AUTH_ISSUER`, `AUTH_AUDIENCE` and
   `AUTH_ALLOWED_EMAILS`. Until you do, the WebUI keeps working unauthenticated
   and says so, loudly, at startup and in a UI banner.
3. Watch the first startup for the **Permissions** block. If it refuses to
   start, it names the folder and the `chown` command.

Same calendar, same paths, same configuration keys. No files are migrated —
except that `uiadminpassword` is no longer read and can be deleted.
