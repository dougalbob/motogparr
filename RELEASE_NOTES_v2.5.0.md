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

## Under the hood

- `utils/auth.py` — verification only, no Flask import. Signature, issuer,
  audience, expiry, email claim, allowlist. Keys are cached and refetched on an
  unknown `kid`, so Cloudflare key rotation needs no restart.
- `utils/web_auth.py` — mode resolution, the guard, the denial page.
- `AUTH_*` are environment variables only. Putting an allowlist in a file the UI
  can rewrite is not a security control.
- `AUTH_MODE=none` exists for local development; it warns in the log and in the UI.
- 173 tests (was 135), including 21 that sign real RS256 tokens and verify them
  against a local key set — no network in the suite.
- The denial page deliberately does *not* extend `base.html`: it renders from
  `before_request`, where no view context exists, and the first version 500'd
  instead of returning 401.

## Upgrading

Pull and Force Update. If you already run behind Cloudflare Access, add the three
`AUTH_*` variables to the container (they are in the Unraid template under
*advanced*) and restart. Until you do, the app keeps working unauthenticated and
says so at startup.

Same calendar, same paths, same configuration keys. No files are migrated.
