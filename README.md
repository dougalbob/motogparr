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

The two path variables are the easy ones to get wrong, because they are paths
*inside the container*:

- `MOTOGP_LIBRARY_PATH` should match what Plex sees for the same library
- `MOTOGP_DOWNLOADS_PATH` should match the completed-downloads folder you gave
  SABnzbd

On first start the container creates `/config/settings.yml` from a commented
template and prints which settings are still missing.

In Unraid: **Docker → your container → Force Update** after a new release.

```bash
docker pull ghcr.io/dougalbob/motogparr:latest
```

The repository is public, so the package is pullable without credentials.

To confirm what is actually running, the container prints its version at
startup:

```
  MotoGParr - MotoGP Automated Recording
  Version: 2.3.0
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
