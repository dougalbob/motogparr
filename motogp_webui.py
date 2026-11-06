#!/usr/bin/env python3
"""
MotoGP Grabber Web UI
======================
Web interface for MotoGP grabber - refactored with templates.
"""

import os
import sys
import subprocess
import threading
import json
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, render_template, request, jsonify, 
    send_from_directory, Response
)

logger = logging.getLogger("motogp.webui")

# Add paths for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from motogp_common import (
    SessionType,
    RaceClass,
    CALENDAR_KEY_TO_SESSION,
    CALENDAR_DAYS,
    parse_prefix,
    load_calendar,
    DEFAULT_DESIRED_SESSIONS,
    get_config_dir,
    get_round_status,
    is_round_active,
    get_round_classes,
    get_weekend_format,
    round_runs_session,
    element_number,
)

from utils.config_loader import (
    SETTINGS_FILE,
    get_motogp_library_path,
    validate,
    reload_settings,
    update_settings,
    has_setting,
    get_max_grabs_per_run,
    get_current_season,
    _load_yaml,
    get_prowlarr_url,
    get_prowlarr_api_key,
    get_sabnzbd_url,
    get_sabnzbd_api_key,
    get_plex_url,
    get_plex_token,
    get_plex_library_name,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

# The interpreter that spawns the grabber/overlay children.
#
# sys.executable rather than a hard-coded /opt/venv path: the WebUI is already
# running under the right interpreter, so asking it is always correct, and it
# keeps the app runnable outside the image (tests, local runs) where /opt/venv
# does not exist. MOTOGPARR_PYTHON overrides both, matching entrypoint.sh.
PYTHON_PATH = os.environ.get("MOTOGPARR_PYTHON") or sys.executable
GRABBER_SCRIPT = SCRIPT_DIR / "motogp_grabber.py"
OVERLAY_SCRIPT = SCRIPT_DIR / "motogp_overlay_utility.py"
CONFIG_DIR = get_config_dir()
WEBUI_CONFIG_FILE = CONFIG_DIR / "webui_settings.json"
LOG_FILE = CONFIG_DIR / "logs" / "motogp.log"
MOTOGP_LIBRARY_PATH = get_motogp_library_path()

def discover_seasons() -> list:
    """
    Seasons that have a calendar file, newest last.

    Derived from config/motogp_<year>.json rather than hard coded, so dropping
    in next season's calendar is enough to make it appear in the UI.
    """
    years = set()
    try:
        for path in CONFIG_DIR.glob("motogp_*.json"):
            suffix = path.stem.split("_")[-1]
            if suffix.isdigit():
                years.add(int(suffix))
    except OSError:
        pass

    if not years:
        years.add(datetime.now().year)

    return sorted(years)


SEASONS = discover_seasons()

SESSION_GROUPS = {
    "MotoGP": [
        "MOTOGP_PRESS_CONFERENCE", "MOTOGP_FP1", "MOTOGP_PRACTICE",
        "MOTOGP_FP2", "MOTOGP_FP3", "MOTOGP_Q1", "MOTOGP_Q2",
        "MOTOGP_SPRINT_QUALIFYING", "MOTOGP_SPRINT",
        "MOTOGP_WARMUP", "MOTOGP_RACE", "MOTOGP_CHEQUERED_FLAG",
    ],
    "Moto2": [
        "MOTO2_FP1", "MOTO2_PRACTICE", "MOTO2_FP2",
        "MOTO2_Q1", "MOTO2_Q2", "MOTO2_RACE",
    ],
    "Moto3": [
        "MOTO3_FP1", "MOTO3_PRACTICE", "MOTO3_FP2",
        "MOTO3_Q1", "MOTO3_Q2", "MOTO3_RACE",
    ],
}

# Build SESSION_TYPES from SessionType enum
SESSION_TYPES = {}
for session_type in SessionType:
    if session_type == SessionType.UNKNOWN:
        continue
    SESSION_TYPES[session_type.name] = {
        "name": session_type.full_display_name,
        "default": DEFAULT_DESIRED_SESSIONS.get(session_type, False)
    }

from grab_stats import get_stats_summary
from activity_emitter import get_all_activity, clear_all_activity

app = Flask(__name__)

# Session cookie hygiene. The cookie only carries the verified identity so the UI
# can show who is signed in; it is not the authentication mechanism (the JWT on
# every request is), but there is no reason to let JavaScript read it.
_secret = os.environ.get("MOTOGPARR_SECRET_KEY") or os.environ.get("AUTH_AUDIENCE") or ""
if not _secret:
    # Ephemeral: a restart invalidates sessions. That is acceptable because the
    # session is only a display concern, and it avoids shipping a fixed default
    # secret that would be the same in every deployment.
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Not Secure-by-default: the app is served over http inside the LAN and
    # Cloudflare terminates TLS outside it, so a Secure cookie would simply not
    # be sent back by the LAN browser. Set MOTOGPARR_COOKIE_SECURE=1 if this
    # container is ONLY ever reached through Cloudflare.
    SESSION_COOKIE_SECURE=os.environ.get("MOTOGPARR_COOKIE_SECURE", "0") == "1",
)

# The UI is same-origin. A wildcard here meant any page in any browser that could
# reach this host could read /app-config (Plex token, API keys) cross-origin, so
# there is deliberately no Access-Control-Allow-Origin header at all.
@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _install_auth(flask_app):
    """Attach the Cloudflare Access guard. Fail loudly, never silently open."""
    from utils.web_auth import install as install_auth_guard

    try:
        return install_auth_guard(flask_app)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not mean no guard
        print("=" * 66)
        print("  ❌ Authentication could not be initialised:")
        print(f"     {exc}")
        print("  The WebUI will refuse every request until this is fixed.")
        print("=" * 66)
        return install_auth_guard(
            flask_app,
            gate=__import__("utils.web_auth", fromlist=["AuthGate"]).AuthGate(
                mode="cloudflare", config_error=str(exc)
            ),
        )


AUTH_GATE = _install_auth(app)


@app.context_processor
def inject_auth_context():
    """Expose who is signed in (and whether auth is off) to every template."""
    from flask import session as _session
    from utils.web_auth import SESSION_EMAIL_KEY

    return {
        "identity_email": _session.get(SESSION_EMAIL_KEY),
        "auth_disabled": getattr(AUTH_GATE, "mode", "none") == "none",
    }
# Store command output
command_output = {
    "running": False,
    "command": "",
    "output": "",
    "timestamp": None,
    "batch_total": 0,
    "batch_current": 0
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

#: Timetable display labels keyed by the suffix of a calendar session key
#: (e.g. "motogp_qualifying" -> "qualifying"). Kept separate from
#: SessionType.display_name because CALENDAR_KEY_TO_SESSION maps "*_qualifying"
#: to the Q2 enum member ("Qualifying 2"), whereas in the timetable it
#: represents the whole Q1+Q2 qualifying slot.
CALENDAR_SESSION_LABELS = {
    "fp1": ("FP1", "Free Practice 1"),
    "practice": ("Practice", "Practice"),
    "fp2": ("FP2", "Free Practice 2"),
    "fp3": ("FP3", "Free Practice 3"),
    "qualifying": ("Qualifying", "Qualifying"),
    "q1": ("Q1", "Qualifying 1"),
    "q2": ("Q2", "Qualifying 2"),
    "sprint_qualifying": ("Sprint Qualifying", "Sprint Qualifying"),
    "sprint": ("Sprint", "Sprint"),
    "warmup": ("Warm Up", "Warm Up"),
    "race": ("Race", "Race"),
    "press_conference": ("Press Conference", "Pre-event Press Conference"),
    "chequered_flag": ("Chequered Flag", "Chequered Flag"),
}

_DAY_NAMES_IN_ORDER = CALENDAR_DAYS


def _parse_hhmm(value: str):
    """Parse 'HH:MM' into (hour, minute), or None on malformed input."""
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_utc_offset_minutes(utc_offset: str):
    """Parse '+03:00' / '-05:00' into signed integer minutes, or None."""
    if not utc_offset or not isinstance(utc_offset, str):
        return None
    match = re.match(r"^([+-])(\d{2}):(\d{2})$", utc_offset.strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * (int(match.group(2)) * 60 + int(match.group(3)))


def _session_utc_bounds(day_date, day_name: str, info: dict, offset_mins):
    """
    Resolve (start_utc, end_utc) for a single calendar session entry.

    Calendars group sessions under track-local days ("friday", "saturday",
    "sunday"), so a morning session in Australia (09:00 +11:00 on Friday) has
    `"gmt": "22:00"` with no `"gmt_day"` field - combining that `"gmt"` with
    Friday's date puts the session 24 hours late on Friday night UTC instead of
    Thursday night UTC. Anchoring on `day_date + local + utc_offset` keeps the
    exact instant across midnight boundaries, falling back to `gmt` only when
    `local`/`utc_offset` are absent or disagree (synthetic test fixtures).
    """
    try:
        duration = int(info.get("duration_mins") or 45)
    except (TypeError, ValueError):
        duration = 45

    local_hm = _parse_hhmm(info.get("local"))
    gmt_hm = _parse_hhmm(info.get("gmt"))

    if local_hm is not None and offset_mins is not None:
        expected_gmt_mins = (local_hm[0] * 60 + local_hm[1] - offset_mins) % 1440
        gmt_matches = (
            gmt_hm is None or (gmt_hm[0] * 60 + gmt_hm[1]) == expected_gmt_mins
        )
        if gmt_matches:
            local_tz = timezone(timedelta(minutes=offset_mins))
            local_dt = datetime(
                day_date.year, day_date.month, day_date.day,
                local_hm[0], local_hm[1], tzinfo=local_tz,
            )
            start_utc = local_dt.astimezone(timezone.utc)
            return start_utc, start_utc + timedelta(minutes=duration)

    if gmt_hm is not None:
        target_date = day_date
        gmt_day = str(info.get("gmt_day") or "").lower()
        if gmt_day in _DAY_NAMES_IN_ORDER and day_name in _DAY_NAMES_IN_ORDER:
            delta = _DAY_NAMES_IN_ORDER.index(gmt_day) - _DAY_NAMES_IN_ORDER.index(day_name)
            target_date = day_date + timedelta(days=delta)
        start_utc = datetime(
            target_date.year, target_date.month, target_date.day,
            gmt_hm[0], gmt_hm[1], tzinfo=timezone.utc,
        )
        return start_utc, start_utc + timedelta(minutes=duration)

    return None, None


def _describe_calendar_session_key(key: str, session_type):
    """Return (class_key, class_label, short_name, full_label) for a calendar key."""
    if session_type is not None and session_type.race_class is not None:
        race_class = session_type.race_class
        class_key = race_class.folder_key
        class_label = race_class.display_name
    else:
        prefix, _, _ = key.partition("_")
        parsed_class = RaceClass.from_string(prefix)
        if parsed_class is not None:
            class_key = parsed_class.folder_key
            class_label = parsed_class.display_name
        else:
            class_key = prefix or "motogp"
            class_label = prefix.upper() if prefix else "MotoGP"

    slug = key.split("_", 1)[1] if "_" in key else key
    if slug in CALENDAR_SESSION_LABELS:
        short_name, name = CALENDAR_SESSION_LABELS[slug]
    elif session_type is not None:
        short_name = name = session_type.display_name
    else:
        short_name = name = slug.replace("_", " ").title()

    return class_key, class_label, short_name, name


def build_round_schedule(round_info: dict, calendar: dict = None) -> dict:
    """
    Build per-day timed session lists with exact UTC instants for the Calendar UI.

    Reads directly from `round_info["sessions"][day]` rather than
    `CALENDAR_KEY_TO_DAY`: from 2027 `motogp_fp2` moves from Saturday to Friday
    and `motogp_warmup` is dropped, so the day structure in the JSON must win
    without code assumptions about which day a key belongs to.
    """
    if not round_info or not is_round_active(round_info):
        return {}

    raw_sessions = round_info.get("sessions")
    if not isinstance(raw_sessions, dict) or not raw_sessions:
        return {}

    race_date_str = round_info.get("date")
    weekend_start_str = round_info.get("weekend_start")
    if not race_date_str:
        return {}

    try:
        race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
        weekend_start = (
            datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            if weekend_start_str
            else race_date - timedelta(days=2)
        )
    except ValueError:
        return {}

    day_dates = {
        "thursday": weekend_start - timedelta(days=1),
        "friday": weekend_start,
        "saturday": weekend_start + timedelta(days=1),
        "sunday": race_date,
    }
    offset_mins = _parse_utc_offset_minutes(round_info.get("utc_offset"))
    allowed_classes = set(get_round_classes(round_info, calendar))

    schedule = {}
    ordered_days = [d for d in _DAY_NAMES_IN_ORDER if d in raw_sessions]
    ordered_days.extend(d for d in raw_sessions if d not in ordered_days)

    for day_name in ordered_days:
        day_entries = raw_sessions.get(day_name)
        day_date = day_dates.get(day_name)
        if not isinstance(day_entries, dict) or day_date is None:
            continue

        built_sessions = []
        for key, info in day_entries.items():
            if not isinstance(info, dict):
                continue

            session_type = CALENDAR_KEY_TO_SESSION.get(key)
            if session_type is not None:
                if not round_runs_session(round_info, session_type, calendar):
                    continue
            else:
                class_prefix, _, _ = key.partition("_")
                if class_prefix in {"motogp", "moto2", "moto3"} and class_prefix not in allowed_classes:
                    continue

            start_utc, end_utc = _session_utc_bounds(day_date, day_name, info, offset_mins)
            if start_utc is None or end_utc is None:
                continue

            class_key, class_label, short_name, full_label = _describe_calendar_session_key(
                key, session_type
            )
            duration_mins = int((end_utc - start_utc).total_seconds() // 60)

            parts_out = []
            for part in info.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                part_start, part_end = _session_utc_bounds(
                    day_date, day_name, part, offset_mins
                )
                if part_start is None or part_end is None:
                    continue
                part_key = str(part.get("key") or "").lower()
                part_short = CALENDAR_SESSION_LABELS.get(
                    part_key, (part_key.upper() or "Part", "")
                )[0]
                parts_out.append({
                    "key": part_key,
                    "label": part_short,
                    "start_utc": part_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_utc": part_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "duration_mins": int((part_end - part_start).total_seconds() // 60),
                })

            entry_out = {
                "key": key,
                "class": class_key,
                "class_label": class_label,
                "short_name": short_name,
                "name": full_label,
                "full_name": f"{class_label} {short_name}",
                "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local": info.get("local"),
                "gmt": info.get("gmt"),
                "duration_mins": duration_mins,
            }
            if parts_out:
                entry_out["parts"] = parts_out

            built_sessions.append(entry_out)

        if built_sessions:
            built_sessions.sort(key=lambda item: item["start_utc"])
            schedule[day_name] = {
                "date": day_date.strftime("%Y-%m-%d"),
                "sessions": built_sessions,
            }

    return schedule


def default_calendar_year(seasons: list = None) -> int:
    """
    Pick the season `/calendar` opens on when no `?year=` query parameter is given.

    Prefers an explicit `grabber.current_season` in settings.yml/env if that
    calendar exists, then the current calendar year, then `get_current_season()`,
    then the newest discovered season - so dropping in next year's calendar file
    never leaves the UI opening on an old season.
    """
    available = list(seasons) if seasons is not None else discover_seasons()
    if not available:
        return get_current_season()

    if has_setting("grabber", "current_season"):
        configured = get_current_season()
        if configured in available:
            return configured

    now_year = datetime.now().year
    if now_year in available:
        return now_year

    fallback = get_current_season()
    if fallback in available:
        return fallback

    return available[-1]


def load_calendar_data(year: int) -> dict:
    """Load calendar for a given year."""
    return load_calendar(year) or {"rounds": []}


def get_rounds_for_season(year: int) -> list:
    """Get list of rounds with venue names and timed schedules for a season."""
    calendar = load_calendar_data(year)
    rounds = []
    for r in calendar.get("rounds", []):
        race_date_str = r.get("date")
        weekend_start_str = r.get("weekend_start")
        weekend_end_str = r.get("weekend_end") or race_date_str
        if not weekend_start_str and race_date_str:
            try:
                weekend_start_str = (
                    datetime.strptime(race_date_str, "%Y-%m-%d").date()
                    - timedelta(days=2)
                ).strftime("%Y-%m-%d")
            except ValueError:
                weekend_start_str = None

        rounds.append({
            "number": r.get("round"),
            "venue": r.get("venue"),
            "date": race_date_str,
            "weekend_start": weekend_start_str,
            "weekend_end": weekend_end_str,
            "status": get_round_status(r),
            "status_note": r.get("status_note"),
            "active": is_round_active(r),
            "classes": get_round_classes(r, calendar),
            "format": get_weekend_format(r, calendar),
            "schedule": build_round_schedule(r, calendar),
        })
    return rounds


def get_next_race_info() -> dict:
    """Get information about the next race weekend."""
    today = datetime.now().date()
    
    for year in [today.year, today.year + 1]:
        calendar = load_calendar_data(year)
        
        for r in calendar.get("rounds", []):
            race_date_str = r.get("date")
            weekend_start_str = r.get("weekend_start")
            
            if not race_date_str:
                continue
            
            try:
                race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
                weekend_start = (
                    datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
                    if weekend_start_str
                    else race_date - timedelta(days=2)
                )
                weekend_end = race_date + timedelta(days=1)
                
                # Format date as DD-MM-YYYY
                formatted_date = race_date.strftime("%d-%m-%Y")
                
                if weekend_start <= today <= weekend_end:
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "now",
                        "countdown": "Race Weekend Now!",
                        "days": 0
                    }
                
                if weekend_start > today:
                    days_until = (weekend_start - today).days
                    
                    if days_until == 1:
                        countdown = "Tomorrow!"
                    elif days_until < 7:
                        countdown = f"{days_until} days"
                    elif days_until < 14:
                        countdown = f"{days_until} days (next week)"
                    else:
                        weeks = days_until // 7
                        countdown = f"{days_until} days (~{weeks} weeks)"
                    
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "upcoming",
                        "countdown": countdown,
                        "days": days_until
                    }
            except ValueError:
                continue
    
    return {
        "round": None,
        "venue": "No upcoming races",
        "date": None,
        "flag": "",
        "status": "none",
        "countdown": "Season ended",
        "days": None
    }



def migrate_max_grabs_per_run() -> None:
    """
    One-time migration of max_grabs_per_run out of webui_settings.json.

    settings.yml owns grabber.* (the grabber reads config_loader, not this
    JSON file), so a value saved only in the JSON was never actually used by
    the grabber while the UI displayed it.

    Idempotent and never loses a value:

      * settings.yml lacks grabber.max_grabs_per_run  -> write the JSON's
        value there once, then drop the key from the JSON;
      * settings.yml already has it                  -> the JSON copy is
        stale; drop it without overwriting (settings.yml wins).
    """
    if not WEBUI_CONFIG_FILE.exists():
        return
    try:
        with open(WEBUI_CONFIG_FILE, "r") as f:
            saved = json.load(f)
    except Exception:
        # Corrupt JSON: nothing to migrate; load_webui_settings warns instead.
        return
    if not isinstance(saved, dict) or "max_grabs_per_run" not in saved:
        return

    value = saved["max_grabs_per_run"]

    if not has_setting("grabber", "max_grabs_per_run"):
        ok = update_settings({"grabber": {"max_grabs_per_run": value}})
        if not ok:
            logger.warning(
                "Could not migrate max_grabs_per_run to %s; the value stays "
                "in %s until /config is writable",
                SETTINGS_FILE, WEBUI_CONFIG_FILE,
            )
            return

    del saved["max_grabs_per_run"]
    try:
        save_webui_settings(saved)
    except OSError as exc:  # noqa: BLE001 - report, never silently keep a duplicate
        logger.warning(
            "Could not drop max_grabs_per_run from %s: %s",
            WEBUI_CONFIG_FILE, exc,
        )


def load_webui_settings() -> dict:
    """Load WebUI settings from config file.

    webui_settings.json owns only the desired sessions. grabber settings
    (max_grabs_per_run and the rest of grabber.*) live in settings.yml and are
    read by the grabber via config_loader.get_max_grabs_per_run(); they are
    merged into the UI presentation at read time, never stored in this file.

    settings.yml may be edited outside this process, so it is re-read on
    every load - there is no stale JSON shadowing it any more.

    A corrupt or missing webui_settings.json logs a warning naming the path
    and falls back to default sessions; sessions still work.
    """
    reload_settings()  # settings.yml can change under this process
    migrate_max_grabs_per_run()

    defaults = {
        "sessions": {k: v["default"] for k, v in SESSION_TYPES.items()}
    }

    if WEBUI_CONFIG_FILE.exists():
        try:
            with open(WEBUI_CONFIG_FILE, "r") as f:
                saved = json.load(f)
        except Exception as exc:  # noqa: BLE001 - never silently default
            logger.warning(
                "Could not read %s: %s; falling back to default sessions",
                WEBUI_CONFIG_FILE, exc,
            )
        else:
            if isinstance(saved, dict):
                defaults.update(saved)
    else:
        logger.warning(
            "%s not found; falling back to default sessions",
            WEBUI_CONFIG_FILE,
        )

    return defaults


def save_webui_settings(settings):
    """Save WebUI settings to config file.

    Callers must not pass max_grabs_per_run: it is a grabber setting and
    belongs in settings.yml, not in this file.
    """
    WEBUI_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEBUI_CONFIG_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# =============================================================================
# LIBRARY SCANNING
# =============================================================================

def scan_library_for_season(year: int) -> dict:
    """Scan the library to find existing sessions for a season."""
    season_path = os.path.join(MOTOGP_LIBRARY_PATH, str(year))
    
    if not os.path.exists(season_path):
        return {}
    
    existing = {}
    
    for round_folder in os.listdir(season_path):
        round_path = os.path.join(season_path, round_folder)
        
        if not os.path.isdir(round_path):
            continue
        
        round_match = re.match(r'Round\s*(\d+)', round_folder)
        if not round_match:
            continue
        
        round_num = int(round_match.group(1))
        existing[round_num] = {}
        
        for session_folder in os.listdir(round_path):
            session_path = os.path.join(round_path, session_folder)
            
            if not os.path.isdir(session_path):
                continue
            
            parsed = parse_prefix(session_folder)
            if parsed:
                folder_year, folder_round, session_type = parsed
                if folder_year == year and folder_round == round_num:
                    existing[round_num][session_type] = session_path
    
    return existing


def get_library_status(year: int, round_filter: int = None) -> dict:
    """Get library status for display."""
    calendar = load_calendar_data(year)
    existing = scan_library_for_season(year)
    settings = load_webui_settings()
    
    # Build desired sessions set from settings
    desired_set = set()
    for key, enabled in settings.get("sessions", {}).items():
        if enabled:
            try:
                desired_set.add(SessionType[key])
            except KeyError:
                pass
    
    if not desired_set:
        desired_set = {st for st, enabled in DEFAULT_DESIRED_SESSIONS.items() if enabled}
    
    result = {
        "rounds": [], 
        "total_grabbed": 0, 
        "total_desired": 0,
        "past_rounds_grabbed": 0,
        "past_rounds_desired": 0,
        "total_in_library": 0
    }
    today = datetime.now().date()
    
    # Count total files in library (all sessions, not just desired)
    for round_num, sessions in existing.items():
        result["total_in_library"] += len(sessions)
    
    for r in calendar.get("rounds", []):
        round_num = r.get("round")
        
        if round_filter and round_num != round_filter:
            continue
        
        # Check if future round (don't skip, just flag it)
        race_date_str = r.get("date", "")
        is_future = False
        if race_date_str:
            try:
                is_future = datetime.strptime(race_date_str, "%Y-%m-%d").date() > today
            except ValueError:
                pass
        
        round_existing = existing.get(round_num, {})
        sessions = []
        grabbed_count = 0
        desired_count = 0
        
        for session_type in sorted(desired_set, key=lambda x: int(x)):
            # Only count sessions this weekend actually runs, otherwise a 2026
            # round would show Sprint Qualifying / FP3 as missing.
            if not round_runs_session(r, session_type, calendar):
                continue

            exists = session_type in round_existing
            sessions.append({
                "type": session_type,
                "name": session_type.full_display_name,
                "exists": exists,
            })
            desired_count += 1
            if exists:
                grabbed_count += 1
        
        result["rounds"].append({
            "number": round_num,
            "venue": r.get("venue", "Unknown"),
            "date": r.get("date", ""),
            "status": get_round_status(r),
            "active": is_round_active(r),
            "is_future": is_future,
            "sessions": sessions,
            "grabbed_count": grabbed_count,
            "desired_count": desired_count,
        })
        
        # Cancelled/postponed rounds do not count towards the season totals.
        if not is_round_active(r):
            continue

        result["total_grabbed"] += grabbed_count
        result["total_desired"] += desired_count
        
        # Only count past rounds for the "preferred" stats
        if not is_future:
            result["past_rounds_grabbed"] += grabbed_count
            result["past_rounds_desired"] += desired_count
    
    return result


# =============================================================================
# COMMAND RUNNERS
# =============================================================================

def run_grabber_command(args: str):
    """Run a grabber command in the background."""
    global command_output
    
    command_output["running"] = True
    command_output["command"] = f"motogp-grab {args}"
    command_output["output"] = "Starting...\n"
    command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        cmd = [PYTHON_PATH, str(GRABBER_SCRIPT)] + args.split()
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        
        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            command_output["output"] = "".join(output_lines)
        
        process.wait()
        command_output["output"] = "".join(output_lines)
        
    except Exception as e:
        command_output["output"] += f"\nError: {e}"
    finally:
        command_output["running"] = False


def run_overlay_command(args: str):
    """Run an overlay command in the background."""
    global command_output
    
    command_output["running"] = True
    command_output["command"] = f"motogp-overlay {args}"
    command_output["output"] = "Starting overlay...\n"
    command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        parts = args.split()
        
        if parts[0] == 'apply-season':
            year = parts[1]
            scale = "1.0"
            dry_run = False
            
            for i, p in enumerate(parts):
                if p == '--scale' and i + 1 < len(parts):
                    scale = parts[i + 1]
                if p == '--dry-run':
                    dry_run = True
            
            cmd = [PYTHON_PATH, str(OVERLAY_SCRIPT), "apply-season", year, "--scale", scale]
            if dry_run:
                cmd.append("--dry-run")
        
        elif parts[0] == 'apply-round':
            year = parts[1]
            round_num = parts[2]
            scale = "1.0"
            dry_run = False
            
            for i, p in enumerate(parts):
                if p == '--scale' and i + 1 < len(parts):
                    scale = parts[i + 1]
                if p == '--dry-run':
                    dry_run = True
            
            calendar = load_calendar_data(int(year))
            round_info = next(
                (r for r in calendar.get("rounds", []) if r.get("round") == int(round_num)),
                None
            )
            
            if round_info:
                # Always use the folder name from the calendar so renumbered
                # rounds (e.g. Qatar moving to Round 20) resolve correctly.
                folder = round_info.get("folder") or (
                    f"Round {int(round_num):02d} {round_info.get('venue', 'Unknown')}"
                )
                round_path = os.path.join(MOTOGP_LIBRARY_PATH, year, folder)
                cmd = [PYTHON_PATH, str(OVERLAY_SCRIPT), "apply-round", round_path, "--scale", scale]
                if dry_run:
                    cmd.append("--dry-run")
            else:
                command_output["output"] = f"Error: Round {round_num} not found in calendar"
                command_output["running"] = False
                return
        else:
            command_output["output"] = f"Error: Unknown overlay command: {parts[0]}"
            command_output["running"] = False
            return
        
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        
        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            command_output["output"] = "".join(output_lines)
        
        process.wait()
        command_output["output"] = "".join(output_lines)
        
    except Exception as e:
        command_output["output"] += f"\nError: {e}"
    finally:
        command_output["running"] = False


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    settings = load_webui_settings()
    session_defaults = {k: v["default"] for k, v in SESSION_TYPES.items()}
    next_race = get_next_race_info()
    
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    return render_template(
        "dashboard.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        session_types=SESSION_TYPES,
        session_groups=SESSION_GROUPS,
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        active_tab="home"
    )


@app.route("/library")
def library():
    settings = load_webui_settings()
    next_race = get_next_race_info()
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    # Build flag data per season
    flag_data = {}
    for year in SEASONS:
        flag_data[str(year)] = {}
        cal = load_calendar_data(year)
        for r in cal.get("rounds", []):
            flag_data[str(year)][r.get("round")] = r.get("flag", "")
    
    return render_template(
        "library.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        next_race=next_race,
        calendar_data=calendar_data,
        flag_data=flag_data,
        active_tab="download"
    )


@app.route("/settings-page")
def settings_page():
    settings = load_webui_settings()
    settings["max_grabs_per_run"] = get_max_grabs_per_run()
    session_defaults = {k: v["default"] for k, v in SESSION_TYPES.items()}
    next_race = get_next_race_info()
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    return render_template(
        "settings.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        session_types=SESSION_TYPES,
        session_groups=SESSION_GROUPS,
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        active_tab="settings"
    )


@app.route("/logs")
def logs_page():
    next_race = get_next_race_info()
    
    return render_template(
        "logs.html",
        status=command_output,
        next_race=next_race,
        active_tab="logs"
    )


@app.route("/run", methods=["POST"])
def run():
    if command_output["running"]:
        return jsonify({"error": "Command already running"}), 400
    
    data = request.json
    cmd = data.get("command", "status")
    
    if cmd.startswith("overlay "):
        thread = threading.Thread(target=run_overlay_command, args=(cmd[8:],))
    else:
        thread = threading.Thread(target=run_grabber_command, args=(cmd,))
    
    thread.start()
    return jsonify({"status": "started", "command": cmd})


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        new_settings = request.get_json(silent=True)
        if not isinstance(new_settings, dict):
            return jsonify({"status": "error", "detail": "Expected a JSON object"}), 400

        # max_grabs_per_run is a grabber setting: it lives in settings.yml,
        # which is what the grabber reads. webui_settings.json keeps sessions
        # only, so pop it before writing the JSON store.
        max_grabs = new_settings.pop("max_grabs_per_run", None)

        # Both writes must succeed before this says "saved". Previously a failed
        # settings.yml write was swallowed and the UI reported success anyway.
        try:
            save_webui_settings(new_settings)
        except OSError as exc:
            return jsonify({
                "status": "error",
                "detail": f"Could not write webui_settings.json: {exc}",
            }), 500

        if max_grabs is not None and not update_settings(
            {"grabber": {"max_grabs_per_run": max_grabs}}
        ):
            return jsonify({
                "status": "error",
                "detail": "Could not write settings.yml; check that /config is writable.",
            }), 500

        return jsonify({"status": "saved"})
    settings = load_webui_settings()
    settings["max_grabs_per_run"] = get_max_grabs_per_run()
    return jsonify(settings)


@app.route("/status")
def status():
    return jsonify(command_output)

@app.route("/api/activity-events")
def api_activity_events():
    """Get all activity events for the activity feed."""
    try:
        activity = get_all_activity()
        return jsonify(activity)
    except Exception as e:
        return jsonify({"error": str(e), "active": [], "history": []}), 500


@app.route("/api/activity-events/clear", methods=["POST"])
def api_clear_activity():
    """Clear all activity events."""
    try:
        clear_all_activity()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/library-status")
def library_status():
    season = request.args.get("season", "2025", type=int)
    round_num = request.args.get("round", "", type=str)
    round_filter = int(round_num) if round_num else None
    
    data = get_library_status(season, round_filter)
    
    for round_data in data["rounds"]:
        for session in round_data["sessions"]:
            # Include episode number for session key building
            session["episode"] = int(session["type"])
            session["type"] = session["type"].name
    
    return jsonify(data)


#: Sections and keys the WebUI is allowed to write through /app-config.
#:
#: This is an allowlist rather than "whatever the client sent" because the client
#: is a browser form, not a trusted editor. Anything not listed here stays in
#: settings.yml untouched - which is the whole point: the previous version
#: rebuilt the file from a fixed dict and silently deleted uiadminpassword,
#: grabber.* and anything added later.
APP_CONFIG_SECTIONS = {
    "prowlarr": ("url", "api_key"),
    "sabnzbd": ("url", "api_key"),
    "plex": ("url", "token", "library_id", "library_name"),
    "library": ("motogp_path", "downloads_path"),
    "discord": ("webhook_url",),
}
APP_CONFIG_NESTED = {
    "plex": {"path_mapping": ("container", "plex")},
}
APP_CONFIG_SCALARS = ("timezone",)
APP_CONFIG_GRABBER = ("max_grabs_per_run", "allowed_groups", "required_quality")


def _build_app_config_updates(payload: dict) -> dict:
    """Translate a WebUI payload into a partial settings.yml update."""
    updates = {}

    for section, keys in APP_CONFIG_SECTIONS.items():
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        section_updates = {
            key: incoming[key] for key in keys if key in incoming
        }
        for nested_name, nested_keys in APP_CONFIG_NESTED.get(section, {}).items():
            nested_incoming = incoming.get(nested_name)
            if isinstance(nested_incoming, dict):
                section_updates[nested_name] = {
                    key: nested_incoming[key]
                    for key in nested_keys
                    if key in nested_incoming
                }
        if section_updates:
            updates[section] = section_updates

    for key in APP_CONFIG_SCALARS:
        if key in payload:
            updates[key] = payload[key]

    grabber = payload.get("grabber")
    grabber_updates = {}
    if isinstance(grabber, dict):
        grabber_updates = {k: grabber[k] for k in APP_CONFIG_GRABBER if k in grabber}
    elif "max_grabs_per_run" in payload:
        # The settings form historically posts this at the top level.
        grabber_updates["max_grabs_per_run"] = payload["max_grabs_per_run"]
    if grabber_updates:
        updates["grabber"] = grabber_updates

    return updates


@app.route("/app-config", methods=["GET", "POST"])
def app_config_route():
    """Get or save app configuration (settings.yml)."""
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "detail": "Expected a JSON object"}), 400

        updates = _build_app_config_updates(payload)
        success = update_settings(updates)
        reload_settings()
        validation = validate()

        return jsonify({
            "status": "saved" if success else "error",
            "applied": sorted(updates),
            "validation": validation,
        })

    reload_settings()
    settings = _load_yaml()
    validation = validate()
    # uiadminpassword is excluded on purpose: it is not needed by the UI any more
    # and one less copy of a credential in a JSON response is one less leak.
    settings = {k: v for k, v in settings.items() if k != "uiadminpassword"}
    return jsonify({**settings, "validation": validation})


@app.route("/diagnostics")
def diagnostics():
    """
    Configuration problems that are invisible in normal use.

    Two things live here because neither shows up as an error anywhere else:

      * unknown or malformed settings.yml keys - the loader silently falls back
        to defaults, so the app behaves as if the line were never written;
      * whether Plex actually scans the folder this app writes to - if it does
        not, sessions download and sort correctly and then simply never appear.

    Also reports where the credentials came from (never their values), because
    an environment variable in the container template silently beats a key typed
    into this page, which makes "I already changed that" fail in a confusing way.
    """
    from utils.config_loader import credential_sources
    from utils.plexcheck import check_from_settings
    from utils.plexlabels import probe_section

    return jsonify({
        "validation": validate(),
        "credentials": credential_sources(),
        "plex_library": check_from_settings().as_dict(),
        "plex_labels": _probe_plex_labels(probe_section),
    })


def _probe_plex_labels(probe):
    """
    What does this Plex library hand back for a video item?

    Session titles and sort titles are only writable if the section returns an
    object type that supports field edits - and an "Other Videos" section is the
    case where it may not. This is reported rather than assumed: the answer
    cannot be known without a live server, and a diagnostic that guesses is worse
    than one that says "unknown".
    """
    try:
        from motogp_plex_tagger import get_plex_connection

        plex = get_plex_connection()
    except Exception as exc:  # noqa: BLE001 - unreachable Plex is a normal state
        return {"note": f"Plex is not reachable: {exc}"}

    try:
        return probe(plex, get_plex_library_name())
    except Exception as exc:  # noqa: BLE001
        return {"note": f"Could not inspect the library: {exc}"}


@app.route("/test-connections")
def test_connections():
    """Test connections to Prowlarr, SABnzbd, and Plex."""
    import requests as req
    
    results = {"prowlarr": False, "sabnzbd": False, "plex": False, "errors": []}
    
    # Prowlarr
    try:
        url, key = get_prowlarr_url(), get_prowlarr_api_key()
        if url and key:
            r = req.get(f"{url}/api/v1/health", headers={"X-Api-Key": key}, timeout=5)
            results["prowlarr"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Prowlarr: {str(e)}")
    
    # SABnzbd
    try:
        url, key = get_sabnzbd_url(), get_sabnzbd_api_key()
        if url and key:
            r = req.get(f"{url}/api?mode=version&apikey={key}&output=json", timeout=5)
            results["sabnzbd"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"SABnzbd: {str(e)}")
    
    # Plex
    try:
        url, token = get_plex_url(), get_plex_token()
        if url and token:
            r = req.get(f"{url}/?X-Plex-Token={token}", timeout=5)
            results["plex"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Plex: {str(e)}")
    
    return jsonify(results)


@app.route("/logs/stream")
def stream_logs():
    """Stream log file using Server-Sent Events."""
    def generate():
        try:
            if not LOG_FILE.exists():
                yield f"data: Log file not found: {LOG_FILE}\n\n"
                return
            
            # Send last 100 lines initially
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                for line in lines[-100:]:
                    yield f"data: {line.rstrip()}\n\n"
            
            # Then tail the file
            with open(LOG_FILE, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield f"data: Error reading log: {e}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


@app.route("/logs/recent")
def recent_logs():
    """Get recent log lines."""
    lines = request.args.get("lines", 100, type=int)
    
    if not LOG_FILE.exists():
        return jsonify({"error": "Log file not found", "lines": []})
    
    try:
        with open(LOG_FILE, 'r') as f:
            all_lines = f.readlines()
            return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as e:
        return jsonify({"error": str(e), "lines": []})




@app.route("/stats")
def stats():
    """Statistics page."""
    next_race = get_next_race_info()
    stats_data = get_stats_summary()
    
    # Calculate max values for chart scaling
    max_daily = max([d["count"] for d in stats_data.get("by_day", [])], default=1) or 1
    max_monthly = max([m["count"] for m in stats_data.get("by_month", [])], default=1) or 1
    
    return render_template(
        "stats.html",
        status=command_output,
        next_race=next_race,
        stats=stats_data,
        max_daily=max_daily,
        max_monthly=max_monthly,
        active_tab="stats"
    )



@app.route("/failed-sessions")
def failed_sessions():
    """Get recently failed session searches."""
    state_file = CONFIG_DIR / "grabber_state.json"
    failed = {}
    
    if state_file.exists():
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                failed = state.get("failed", {})
        except Exception:
            pass
    
    return jsonify({"failed": failed})


@app.route("/download-selected", methods=["POST"])
def download_selected():
    """Download selected missing sessions."""
    data = request.json
    season = data.get("season")
    selections = data.get("selections", [])

    if not selections:
        return jsonify({"error": "No sessions selected"}), 400

    # The Library page sends the season from a <select>, so it arrives as a
    # string; the Calendar page sends an int. element_number() compares the
    # season to an int, so a string raises TypeError and the whole request
    # 500s - the UI then shows "Failed to start download" even though every
    # connection test passes. Coerce once, here, so both callers work.
    try:
        season = int(season)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid season"}), 400
    
    # Group by round for efficiency
    by_round = {}
    for sel in selections:
        round_num = sel.get("round")
        if round_num not in by_round:
            by_round[round_num] = []
        
        # Convert session type name to episode number
        session_name = sel.get("session", "")
        try:
            # SessionType is an IntEnum, so we can get its value
            session_type = SessionType[session_name]
            episode_num = f"E{element_number(session_type, season):02d}"
            by_round[round_num].append(episode_num)
        except (KeyError, ValueError, TypeError) as e:
            # If conversion fails, skip this session
            print(f"Could not convert session: {session_name} - {e}")
            continue
    
    # Build commands with specific sessions
    commands = []
    for round_num, episodes in by_round.items():
        if episodes:  # Only if we have valid episodes
            sessions_str = ",".join(episodes)
            cmd = f"backfill --season {season} --round {round_num} --sessions {sessions_str} --force"
            commands.append(cmd)
    
    # Run commands sequentially in background, keeping running=True until all complete
    def run_all_commands(cmds, season, round_info):
        global command_output
        total = len(cmds)
        command_output["batch_total"] = total
        
        for i, (cmd, info) in enumerate(zip(cmds, round_info)):
            # Update command display with clear search status
            command_output["running"] = True
            command_output["batch_current"] = i + 1
            round_num = info["round"]
            episodes = info["episodes"]
            command_output["command"] = f"Searching ({i+1}/{total}): Season {season} R{round_num} {episodes}"
            command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if i == 0:
                command_output["output"] = f"Starting batch download of {total} job(s)...\n\n"
            else:
                command_output["output"] += f"\n{'='*60}\n"
            
            command_output["output"] += f">>> Search {i+1}/{total}: Season {season} Round {round_num} - {episodes}\n\n"
            
            try:
                cmd_parts = [PYTHON_PATH, str(GRABBER_SCRIPT)] + cmd.split()
                process = subprocess.Popen(
                    cmd_parts, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1
                )
                
                for line in process.stdout:
                    command_output["output"] += line
                
                process.wait()
                
            except Exception as e:
                command_output["output"] += f"\nError: {e}\n"
        
        # Only set running=False after ALL commands complete
        command_output["running"] = False
        command_output["batch_total"] = 0
        command_output["batch_current"] = 0
        command_output["output"] += f"\n{'='*60}\n✅ All {total} searches completed.\n"
        command_output["output"] += f"\n{'='*60}\nAll {len(cmds)} commands completed.\n"
    
    # Build round info for status display
    round_info = []
    for round_num, episodes in by_round.items():
        if episodes:
            round_info.append({"round": round_num, "episodes": ",".join(episodes)})
    
    if commands:
        thread = threading.Thread(target=run_all_commands, args=(commands, season, round_info))
        thread.start()
    
    return jsonify({
        "status": "started",
        "rounds": list(by_round.keys()),
        "total_sessions": len(selections),
        "commands": commands
    })



@app.route("/activity")
def activity():
    """Activity feed page."""
    next_race = get_next_race_info()
    
    return render_template(
        "activity.html",
        status=command_output,
        next_race=next_race,
        active_tab="activity"
    )


@app.route("/activity/stream")
def activity_stream():
    """Stream activity events parsed from log file."""
    import re
    
    def parse_log_line(line):
        """Parse a log line into an activity event."""
        # Pattern: 2026-01-31 19:24:30 [INFO] motogp.grabber: message
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] ([\w.]+): (.+)', line)
        if not match:
            return None
        
        timestamp, level, source, message = match.groups()
        event = {'timestamp': timestamp, 'level': level, 'source': source, 'raw': message}
        
        # Parse specific event types
        if '🔍 Searching:' in message:
            # 🔍 Searching: Round 3 Americas - MotoGP Free Practice 2
            m = re.search(r'Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'searching'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"  # "Americas MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
        
        elif '📋 Found' in message:
            # 📋 Found 6 candidates for Round 4 Qatar - MotoGP Free Practice 2
            m = re.search(r'Found (\d+) candidates for Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'found'
                event['candidates'] = int(m.group(1))
                event['round'] = m.group(2)
                event['venue'] = m.group(3)
                event['name'] = f"{m.group(3)} {m.group(4)}"  # "Qatar MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(2)}_{m.group(4).replace(' ', '_')}"
            else:
                # Fallback for old format without Round/Venue
                m = re.search(r'Found (\d+) candidates for (.+)', message)
                if m:
                    event['stage'] = 'found'
                    event['candidates'] = int(m.group(1))
                    event['name'] = m.group(2)
                    event['session_key'] = f"search_{m.group(2).replace(' ', '_')}"
        
        elif '⬇️ Sending to SABnzbd:' in message:
            # ⬇️ Sending to SABnzbd: MotoGP.2025.The.Americas...
            m = re.search(r'Sending to SABnzbd: (.+)', message)
            if m:
                event['stage'] = 'sending'
                event['release'] = m.group(1)
        
        elif '✅ Grabbed:' in message:
            # ✅ Grabbed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Grabbed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'grabbed'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '📦 Release:' in message:
            # 📦 Release: MotoGP.2025.The.Americas...
            m = re.search(r'Release: (.+)', message)
            if m:
                event['release'] = m.group(1)
        
        elif '📊 Score:' in message:
            # 📊 Score: 155
            m = re.search(r'Score: (\d+)', message)
            if m:
                event['score'] = int(m.group(1))
        
        elif '📂 New download ready:' in message:
            # 📂 New download ready: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'New download ready: (\S+) (.+)', message)
            if m:
                event['stage'] = 'ready'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Processing:' in message and 'motogp.sort' in source:
            # Processing: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Processing: (\S+) (.+)', message)
            if m:
                event['stage'] = 'sorting'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Applying Plex tags' in message:
            event['stage'] = 'tagging'
        
        elif 'Triggering Plex refresh' in message:
            event['stage'] = 'plex'
        
        elif '✅ Successfully processed:' in message:
            # ✅ Successfully processed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Successfully processed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '✅ Sort completed:' in message:
            # ✅ Sort completed: 25R08E07 Aragon Moto2 Practice
            m = re.search(r'Sort completed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
                
        elif '✅ Plex refresh triggered' in message:
            # This one doesn't have session info in the message
            # Skip it - let "Successfully processed" be the complete trigger
            return None

        elif '❌ No candidates:' in message:
            m = re.search(r'No candidates: Round (\d+) (.+?) - (.+)', message)
            if m:
                event['stage'] = 'error'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
                event['detail'] = 'No matching releases found'
                event['candidates'] = 0
                event['error_message'] = 'Episode unavailable - no matching releases after filtering'

           
        elif '❌' in message or 'Error' in message or 'error' in message.lower():
            event['stage'] = 'error'
            event['detail'] = message
        
        return event if event.get('stage') or event.get('release') or event.get('score') else None
        
    def generate():
        try:
            log_file = CONFIG_DIR / "logs" / "motogp.log"
            
            if not log_file.exists():
                yield "data: " + json.dumps({"error": "Log file not found"}) + "\n\n"
                return
            
            # Send last 50 lines initially
            with open(log_file, 'r') as f:
                lines = f.readlines()[-50:]
                for line in lines:
                    event = parse_log_line(line.strip())
                    if event:
                        yield "data: " + json.dumps(event) + "\n\n"
            
            # Then tail the file
            with open(log_file, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        event = parse_log_line(line.strip())
                        if event:
                            yield "data: " + json.dumps(event) + "\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


def _serialize_calendar_library_status(year: int) -> dict:
    """
    Build per-round library status payload for the Calendar modal.

    `get_library_status()` stores `type` as a `SessionType(IntEnum)`. Converting
    it to `.name` and recording `episode = int(...)` keeps `/calendar` and
    `/calendar-data` aligned with what `POST /download-selected` expects
    (`SessionType[session_name]`).
    """
    library_status = {}
    lib_data = get_library_status(year)
    for r in lib_data.get("rounds", []):
        serialized_sessions = []
        for s in r["sessions"]:
            raw_type = s["type"]
            type_name = raw_type.name if hasattr(raw_type, "name") else str(raw_type)
            episode = s.get("episode") or (int(raw_type) if isinstance(raw_type, int) else 0)
            serialized_sessions.append({
                "name": s["name"],
                "exists": s["exists"],
                "type": type_name,
                "episode": episode,
            })
        library_status[r["number"]] = {
            "grabbed": r["grabbed_count"],
            "total": r["desired_count"],
            "sessions": serialized_sessions,
        }
    return library_status


@app.route("/calendar")
def calendar():
    """Calendar view of race schedule."""
    seasons = discover_seasons() or SEASONS
    year = request.args.get("year", type=int)
    if year is None:
        year = default_calendar_year(seasons)
    next_race = get_next_race_info()

    # Get calendar data for all seasons
    calendar_data = {}
    for y in seasons:
        calendar_data[y] = get_rounds_for_season(y)
    if year not in calendar_data:
        calendar_data[year] = get_rounds_for_season(year)

    library_status = _serialize_calendar_library_status(year)

    # Build flag data per season
    flag_data = {}
    for y in calendar_data:
        flag_data[y] = {}
        cal = load_calendar_data(y)
        for r in cal.get("rounds", []):
            flag_data[y][r.get("round")] = r.get("flag", "")

    return render_template(
        "calendar.html",
        status=command_output,
        next_race=next_race,
        year=year,
        calendar_data=calendar_data,
        library_status=library_status,
        flag_data=flag_data,
        active_tab="calendar"
    )


@app.route("/calendar-data")
def calendar_data_api():
    """Get calendar and library data for a specific year."""
    seasons = discover_seasons() or SEASONS
    year = request.args.get("year", type=int)
    if year is None:
        year = default_calendar_year(seasons)

    return jsonify({
        "year": year,
        "library_status": _serialize_calendar_library_status(year),
    })

@app.route("/api/sabnzbd/queue")
def sabnzbd_queue():
    """Get SABnzbd queue status for motogp category."""
    import requests
    
    try:
        # Load SABnzbd settings
        settings_file = CONFIG_DIR / "settings.yml"
        if not settings_file.exists():
            return jsonify({"error": "Settings file not found"}), 404
        
        with open(settings_file, 'r') as f:
            import yaml
            settings = yaml.safe_load(f)
        
        sab_config = settings.get('sabnzbd', {})
        sab_url = sab_config.get('url', '').rstrip('/')
        sab_api_key = sab_config.get('api_key', '')
        
        if not sab_url or not sab_api_key:
            return jsonify({"error": "SABnzbd not configured"}), 400
        
        # Call SABnzbd API
        api_url = f"{sab_url}/api"
        params = {
            'apikey': sab_api_key,
            'mode': 'queue',
            'output': 'json'
        }
        
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        queue = data.get('queue', {})
        slots = queue.get('slots', [])
        
        # Filter for motogp category
        motogp_downloads = []
        for slot in slots:
            if slot.get('cat', '').lower() == 'motogp':
                motogp_downloads.append({
                    'filename': slot.get('filename', 'Unknown'),
                    'size': slot.get('size', '0 MB'),
                    'sizeleft': slot.get('sizeleft', '0 MB'),
                    'percentage': slot.get('percentage', '0'),
                    'timeleft': slot.get('timeleft', 'Unknown'),
                    'status': slot.get('status', 'Unknown')
                })
        
        # Get total queue info
        result = {
            'motogp_downloads': motogp_downloads,
            'motogp_count': len(motogp_downloads),
            'total_size': queue.get('size', '0 MB'),
            'total_sizeleft': queue.get('sizeleft', '0 MB'),
            'speed': queue.get('speed', '0 KB/s'),
            'timeleft': queue.get('timeleft', 'Unknown'),
            'paused': queue.get('paused', False),
            'total_slots': len(slots)
        }
        
        return jsonify(result)
    
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to connect to SABnzbd: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/flags/<path:filename>")
def flag_files(filename):
    """Serve flag SVG files."""
    return send_from_directory(str(SCRIPT_DIR / "assets" / "flag_assets_svg"), filename)


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "MotoGParr",
        "short_name": "MotoGParr",
        "description": "MotoGP Automated Recording & Retrieval",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#52aad8 ",
        "orientation": "any",
        "icons": [
            {
                "src": "/assets/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/assets/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    })

@app.route("/sw.js")
def service_worker():
    return send_from_directory(str(SCRIPT_DIR / "static" / "js"), "sw.js", mimetype='application/javascript')


@app.route("/assets/<path:filename>")
def static_files(filename):
    return send_from_directory(str(SCRIPT_DIR / "assets"), filename)


if __name__ == "__main__":
    print("=" * 50)
    print("  MotoGParr Web UI")
    print("  Open http://your-server-ip:5679")
    print("=" * 50)

    try:
        app.run(host="0.0.0.0", port=5679, debug=False)
    except OSError as exc:
        # Flask/Werkzeug would otherwise dump a traceback whose most useful
        # line is the errno. Say the actual thing that is wrong: something else
        # already owns the port (on Unraid, almost always an older container
        # that was not fully removed).
        import errno as _errno

        if exc.errno == _errno.EADDRINUSE:
            print("")
            print("  ❌ Port 5679 is already in use - the WebUI cannot start.")
            print("     Another container is almost certainly still running and")
            print("     holding the port. Check with:")
            print("       docker ps --format '{{.Names}} {{.Ports}}'")
            print("     Remove or stop it, or give this container a different")
            print("     host port.")
            print("=" * 50)
            raise SystemExit(1)

        print(f"  ❌ WebUI could not start: {exc}")
        raise SystemExit(1)