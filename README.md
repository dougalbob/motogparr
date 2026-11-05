# MotoGParr

Docker container that automates MotoGP / Moto2 / Moto3 session retrieval into a
Plex library: it watches the race calendar, searches Prowlarr, sends releases to
SABnzbd, sorts the finished download into a Plex-friendly folder structure,
applies artwork and tags, and exposes a small web UI.

**Container image:** `ghcr.io/dougalbob/motogparr`

---

## Releases and updating

Images are built and published by GitHub Actions (`.github/workflows/docker-publish.yml`).
**Nothing needs to be built locally.**

| What you do | What you get |
|---|---|
| Publish a GitHub release tagged `v2.3.0` | `:2.3.0`, `:2.3`, `:latest` |
| Merge a PR to `main` | `:edge` (unreleased, for testing) |
| Run the workflow manually | tags for the ref you pick |

`:latest` only moves when a version tag is published, so a half-finished commit
on `main` never reaches a container that is force-updated.

### One-time GHCR setup (only if a build fails with `permission_denied`)

Making the *repository* public does **not** change the visibility of a package
that already existed - they are separate settings. If the workflow fails with:

```
denied: permission_denied: read_package
```

the existing package is private and not linked to this repository, so the
workflow token cannot read it and therefore cannot push to it. Fix once:

1. Open `https://github.com/users/dougalbob/packages/container/motogparr/settings`
2. **Danger Zone → Change visibility → Public**
3. Recommended: **Manage Actions access → add this repository** with the
   `Write` role, so the workflow owns the package it publishes

Then re-run the failed job. The workflow prints these instructions itself on
that failure.

## Installing on Unraid

There is a container template at [`unraid/motogparr.xml`](unraid/motogparr.xml).
It is not in Community Applications, so add it by URL:

**Docker → Add Container**, then paste this into the **Template** field:

```
https://raw.githubusercontent.com/dougalbob/motogparr/main/unraid/motogparr.xml
```

Everything else can be left at its default until you know your own paths. The
template asks for four things that matter:

| Setting | What it is |
|---|---|
| `WebUI Port` | 5679. Open `http://<server>:5679` to finish setup. |
| `/config` | Appdata folder. Holds `settings.yml` and your calendars - keep it. |
| `/media` | Your media share, so the library can be read and written. |
| `/downloads` | Your downloads share, so completed downloads can be picked up. |

Prowlarr, SABnzbd and Plex details can be typed into the WebUI instead of the
template, which is why the template marks them optional - but if you do set them
there, they take priority over `settings.yml` (that is how the app is designed,
and it is easy to blame the wrong one when a setting "will not save").

The template's path fields ship **blank** on purpose. A non-blank default would
silently beat `settings.yml`, so editing a path in one place could be overridden
by the other. Blank means "use `settings.yml`"; fill them in only to override.

Paths are the easy thing to get wrong, and they come in two halves: the volume
mapping (`/media` in the table above) and the path *inside* it
(`library.motogp_path`). The second must exist beneath the first. Set it in
`settings.yml` and leave the template's path fields blank; see
[Configuring paths](#configuring-paths), which also covers the Plex path mapping
you will need if Plex runs as its own container.

On first start the container creates `/config/settings.yml` from a commented
template and prints which settings are still missing.

In Unraid: **Docker → your container → Force Update** after a new release.

```bash
docker pull ghcr.io/dougalbob/motogparr:latest
```

The repository is public, so once the package itself is public it pulls without
credentials.

To confirm what is actually running, the container prints its version at
startup:

```
  MotoGParr - MotoGP Automated Recording
  Version: 2.3.0
```

## Configuring paths

Environment variables are the canonical configuration, because that is what a
container launcher can set. `settings.yml` is the fallback, and the precedence is:

```
environment variable  >  settings.yml  >  built-in default
```

An environment variable that is **set but empty** counts as not set. That is
deliberate: the Unraid template ships its path fields blank, so `settings.yml`
stays authoritative and there is one place to change a path.

| Variable | Overrides |
|---|---|
| `MOTOGP_LIBRARY_PATH` | `library.motogp_path` |
| `MOTOGP_DOWNLOADS_PATH` | `library.downloads_path` |
| `PLEX_PATH_CONTAINER` | `plex.path_mapping.container` |
| `PLEX_PATH_PLEX` | `plex.path_mapping.plex` |
| `MOTOGP_PLEX_LIBRARY_ID`, `MOTOGP_PLEX_LIBRARY` | `plex.library_id`, `plex.library_name` |
| `PROWLARR_URL`, `PROWLARR_API_KEY` | `prowlarr.*` |
| `SABNZBD_URL`, `SABNZBD_API_KEY` | `sabnzbd.*` |
| `PLEX_URL`, `PLEX_TOKEN` | `plex.*` |
| `DISCORD_WEBHOOK_URL` | `discord.webhook_url` |
| `TZ` | `timezone` |

A few variables are read by `entrypoint.sh` rather than by the Python code —
`MOTOGPARR_CALENDAR_UPDATE` and `MOTOGPARR_SKIP_PATH_CHECK`. They are listed
separately, because claiming the app reads them would be untrue.

The full list is generated from the code, so it cannot drift:

```bash
python -m utils.config_loader --env-help
```

### The Plex path mapping matters more than it looks

Matching a file in Plex is an exact string comparison against what Plex reports.
If Plex runs as its own container, it sees `/data/media/Motorsport/MotoGP/...`
while MotoGParr sees `/media/Motorsport/MotoGP/...`, and those will never be
equal. Without a mapping the tagger retries, fails, and files arrive in Plex with
no labels, genres or collections — looking like "tagging stopped working".

```yaml
plex:
  path_mapping:
    container: /media      # how MotoGParr sees it
    plex: /data/media      # how Plex sees it
```

or `PLEX_PATH_CONTAINER=/media` and `PLEX_PATH_PLEX=/data/media` on the
container. Both halves are required; one on its own does nothing.

### Startup checks

At every start the container prints each path, says **where the value came from**
(an env var set by the template beats `settings.yml`, which surprises people),
and whether the path exists:

```
  Paths:
    Library path    /media/Motorsport/MotoGP  [settings.yml, ok]
    Downloads path  /downloads/complete/motogp  [env via MOTOGP_DOWNLOADS_PATH, ok]
    Plex mapping    /media -> /data/media
```

A missing **library** path stops the container: nothing can be sorted, and the
alternative is a container that looks healthy while doing nothing for hours. A
missing **downloads** path is only a warning — that folder is often absent on a
fresh install or after a share is reorganised, and refusing to boot the WebUI
over it would remove the very thing you would use to fix it.

Override the library check with `MOTOGPARR_SKIP_PATH_CHECK=1` if you really mean
it.

### If the WebUI dies

The WebUI runs under supervision. If it exits, it is restarted (up to five times)
and the death is logged:

```
  ❌ WebUI exited (code 1) after 42s.
     Restarting it (attempt 1/5);
     the scheduler is unaffected.
```

A process that dies within seconds of starting is treated as a startup fault
rather than a crash and is **not** retried — the usual cause is port 5679 still
being held by an older container, and the log says so. The scheduler keeps
running in that case, so the container still does its job.

If the scheduler itself exits, the container stops rather than pretending to
work, so Docker's restart policy (or Unraid's) can act.

The image also declares a `HEALTHCHECK` that requests `/` on 5679, so a container
whose UI has gone reports **unhealthy** instead of "Up". Check it with:

```bash
docker inspect MotoGParr --format '{{.State.Health.Status}}'
```

## Calendars

Season calendars live in `config/motogp_<year>.json` and are baked into the
image. On first start they are copied into `/config`, which is the volume the
app actually reads.

**Because `/config` is persistent, a new image does not by itself update a
calendar you already have.** That matters when a race is cancelled, postponed or
rescheduled. At startup the container therefore compares each calendar in
`/config` with the one in the image and either refreshes it or warns:

```
  Calendars (auto):
    = motogp_2025.json: up to date
    ^ motogp_2026.json: updated from image
        dates changed: Qatar 2026-04-12 -> 2026-11-08; 19 rounds renumbered
        previous copy saved as motogp_2026.json.bak
```

Control this with `MOTOGPARR_CALENDAR_UPDATE`:

| Value | Behaviour |
|---|---|
| `auto` (default) | Refresh the `/config` copy when it is provably the same season, keeping a `.bak`. |
| `warn` | Never write, just report differences and how to fix them. |
| `off` | Stay silent unless the files differ. |

A calendar is only replaced automatically when the set of venues matches, so a
file for a different championship is never overwritten - but note that `auto`
*will* roll back a local edit to a season calendar, which is why the `.bak` is
kept. Use `warn` if you hand-edit calendars.

Check a calendar - and the library it points at - at any time:

```bash
docker exec -it motogparr /opt/venv/bin/python3 /app/utils/validate_calendar.py \
    --season 2026 --library /media/MotoGP
```

See [docs/CALENDAR_CHANGES.md](docs/CALENDAR_CHANGES.md) for what to do when
MotoGP changes the calendar, and for the 2027 weekend format.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

CI runs the same tests plus `utils/validate_calendar.py` on every season on each
push and pull request; a pull request that edits a calendar gets a dedicated
check that also rejects duplicate JSON keys.
