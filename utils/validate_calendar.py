#!/usr/bin/env python3
"""
Calendar validator
==================

Run this after editing any config/motogp_<season>.json - especially after a
cancellation, postponement or reschedule, which is exactly when hand editing
goes wrong.

It checks the things that silently break the grabber or the Plex library:

    * duplicate / missing round numbers
    * folder names that disagree with the round number
    * unparseable or out-of-order dates
    * rounds with no session times (2026+ calendars only)
    * session keys the code does not understand
    * asset files referenced but not present on disk
    * local/GMT times that do not agree with the round's utc_offset
    * round numbers that no longer match what is already on disk

Usage:
    python3 utils/validate_calendar.py --season 2026
    python3 utils/validate_calendar.py --season 2026 --library /path/to/MotoGP
    python3 utils/validate_calendar.py --season 2026 --library ... --strict
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from motogp_common import (  # noqa: E402
    CALENDAR_KEY_TO_SESSION,
    CALENDAR_DAYS,
    MOTOGP_SESSION_NAME_TO_TYPE,
    RaceClass,
    has_session_times,
    CLASS_ASSET_DIRS,
    get_round_status,
    is_round_active,
    validate_calendar,
)

CONFIG_DIR = REPO_ROOT / "config"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# The calendar key that carries each weekend-format session name. This is the
# bridge between the format definition (which speaks in session names) and the
# round's sessions block (which speaks in class-prefixed keys).
MOTOGP_SESSION_NAME_TO_CALENDAR_KEY = {
    "fp1": "motogp_fp1",
    "practice": "motogp_practice",
    "fp2": "motogp_fp2",
    "fp3": "motogp_fp3",
    "qualifying": "motogp_qualifying",
    "sprint_qualifying": "motogp_sprint_qualifying",
    "sprint": "motogp_sprint",
    "warmup": "motogp_warmup",
    "race": "motogp_race",
    "chequered_flag": "motogp_chequered_flag",
}


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _offset_minutes(utc_offset: str) -> int:
    sign = -1 if utc_offset.startswith("-") else 1
    oh, om = map(int, utc_offset.lstrip("+-").split(":"))
    return sign * (oh * 60 + om)


def check_round_format(entry: dict, calendar: dict) -> list:
    """
    Cross-check a round against the weekend format it claims to use.

    The three 2027 misconfigurations this exists to catch all passed the old
    validator with zero problems:

      * a typo in weekend_format ("motogp_2028") resolves to {} and the warm-up
        is hunted all season;
      * a 2027 format with a stale motogp_sessions list still containing
        "practice"/"warmup" hunts sessions that never exist;
      * motogp_sessions declares "fp3" but the sessions block has no
        motogp_fp3 times, so the round passes the gate and the session is
        silently never searched.

    The format is data, so the check is data-vs-data: format <-> declared
    sessions <-> timetable, in both directions and per day.
    """
    problems = []
    formats = calendar.get("weekend_formats") or {}
    if not formats:
        return problems

    round_num = entry.get("round")
    venue = entry.get("venue", "?")
    label = f"Round {round_num} {venue}"

    fmt_name = entry.get("weekend_format")
    if not fmt_name:
        problems.append(f"{label}: no weekend_format, but the calendar has weekend_formats")
        return problems

    fmt = formats.get(fmt_name)
    if fmt is None:
        problems.append(
            f"{label}: weekend_format {fmt_name!r} is not in weekend_formats "
            f"(known: {', '.join(sorted(formats))})"
        )
        return problems

    fmt_days = {
        day: {item.get("session") for item in (fmt.get(day) or [])
              if isinstance(item, dict) and item.get("session")}
        for day in CALENDAR_DAYS
    }
    fmt_sessions = {name for names in fmt_days.values() for name in names}

    declared = entry.get("motogp_sessions")
    if declared is None:
        problems.append(f"{label}: no motogp_sessions, cannot check against {fmt_name!r}")
    elif not isinstance(declared, list):
        problems.append(f"{label}: motogp_sessions must be a list")
    elif not all(isinstance(name, str) for name in declared):
        problems.append(f"{label}: motogp_sessions must contain only strings")
    else:
        declared_set = set(declared)
        for name in sorted(fmt_sessions - declared_set):
            problems.append(
                f"{label}: format {fmt_name!r} runs {name!r} but motogp_sessions "
                f"does not list it"
            )
        for name in sorted(declared_set - fmt_sessions):
            problems.append(
                f"{label}: motogp_sessions lists {name!r} but format "
                f"{fmt_name!r} does not run it"
            )
        if len(declared_set) != len(declared):
            problems.append(f"{label}: motogp_sessions has duplicate entries")

    sessions = entry.get("sessions") or {}
    for day in CALENDAR_DAYS:
        day_sessions = sessions.get(day) or {}
        if not isinstance(day_sessions, dict):
            problems.append(f"{label}: {day} sessions must be an object")
            continue

        # A MotoGP key in a day block the format does not run it on: the
        # "stale sessions block" mistake. Support classes are not described by
        # the MotoGP format, so they are left alone.
        for key, info in day_sessions.items():
            session_type = CALENDAR_KEY_TO_SESSION.get(key)
            if session_type is None or session_type.race_class != RaceClass.MOTOGP:
                continue
            name = session_type.motogp_session_name
            if name not in fmt_days[day]:
                problems.append(
                    f"{label}: {day} has MotoGP key {key!r} ({name}) but format "
                    f"{fmt_name!r} does not run it on {day}"
                )

        # A session the format runs on this day with no times: the
                # "declared but never searched" mistake.
        for name in sorted(fmt_days[day]):
            key = MOTOGP_SESSION_NAME_TO_CALENDAR_KEY.get(name)
            if key and key not in day_sessions:
                problems.append(
                    f"{label}: format {fmt_name!r} runs {name!r} on {day} but "
                    f"the sessions block has no {key!r} times"
                )

    # Release aliases are per-season vocabulary. Validate the shape so a typo
    # cannot silently make a whole season's releases unclassifiable.
    aliases = calendar.get("release_aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            problems.append("release_aliases must be an object mapping token -> session name")
        else:
            for token, session_name in aliases.items():
                if not isinstance(token, str) or not token.strip():
                    problems.append(f"release_aliases: bad token {token!r}")
                if not isinstance(session_name, str) or session_name not in MOTOGP_SESSION_NAME_TO_TYPE:
                    problems.append(
                        f"release_aliases: {token!r} -> {session_name!r} is not "
                        f"a known MotoGP session name"
                    )

    return problems


def check_round(entry: dict, season: int, require_sessions: bool = True,
                calendar: dict = None) -> list:
    """
    Per-round checks that validate_calendar() does not cover.

    require_sessions is False for legacy calendars (pre-2026) that carry no
    session times; those seasons are handled by backfill by date, so a missing
    sessions block is expected rather than a problem.
    """
    problems = []
    round_num = entry.get("round")
    venue = entry.get("venue", "?")
    label = f"Round {round_num} {venue}"

    status = get_round_status(entry)
    if status not in {"scheduled", "cancelled", "canceled", "postponed", "replaced"}:
        problems.append(f"{label}: unknown status {status!r}")

    # Sessions
    sessions = entry.get("sessions") or {}
    if not sessions:
        if require_sessions:
            problems.append(f"{label}: no session times, grabber cannot schedule it")
    else:
        for day, day_sessions in sessions.items():
            if day not in CALENDAR_DAYS:
                problems.append(f"{label}: unexpected day {day!r}")
            for key, info in (day_sessions or {}).items():
                if key not in CALENDAR_KEY_TO_SESSION:
                    problems.append(f"{label}: unknown session key {key!r}")
                local = info.get("local")
                gmt = info.get("gmt")
                if local and not TIME_RE.match(local):
                    problems.append(f"{label}/{key}: bad local time {local!r}")
                if gmt and not TIME_RE.match(gmt):
                    problems.append(f"{label}/{key}: bad gmt time {gmt!r}")

                utc_offset = entry.get("utc_offset")
                if local and gmt and utc_offset:
                    expected = _add_minutes(local, -_offset_minutes(utc_offset))
                    if expected != gmt:
                        problems.append(
                            f"{label}/{key}: local {local} {utc_offset} should be "
                            f"{expected} UTC, not {gmt}"
                        )

    # Assets
    assets = entry.get("assets") or {}
    for class_key, files in assets.items():
        dirs = CLASS_ASSET_DIRS.get(class_key)
        if not dirs:
            problems.append(f"{label}: unknown class in assets {class_key!r}")
            continue
        for asset_kind, filename in (files or {}).items():
            directory = dirs.get(asset_kind)
            if not directory:
                problems.append(f"{label}: unknown asset kind {asset_kind!r}")
                continue
            if not (directory / filename).exists():
                problems.append(f"{label}: missing asset {filename}")

    # Round aliases (other numbers this event has been known by)
    aliases = entry.get("round_aliases") or []
    for alias in aliases:
        if not isinstance(alias, int) or alias == round_num:
            problems.append(f"{label}: bad round_aliases entry {alias!r}")

    flag = entry.get("flag")
    if flag and not (REPO_ROOT / "assets" / "flag_assets_svg" / flag).exists():
        problems.append(f"{label}: missing flag {flag}")

    if calendar is not None:
        problems.extend(check_round_format(entry, calendar))

    return problems


def check_library_matches(calendar: dict, library_root: Path) -> list:
    """
    Compare the calendar against the season folder on disk.

    With official numbering, a round that moved (e.g. Qatar 4 -> 20) leaves the
    old folder behind. That folder is picked up by the library scan as the round
    number in its own name, so the grabber thinks the new round is complete.
    """
    problems = []
    season = calendar.get("season")
    season_path = library_root / str(season)
    if not season_path.is_dir():
        return problems

    calendar_by_number = {
        r.get("round"): r for r in calendar.get("rounds", []) if r.get("round")
    }

    for folder in sorted(season_path.iterdir()):
        if not folder.is_dir():
            continue
        match = re.match(r"Round\s*(\d+)", folder.name)
        if not match:
            continue
        round_num = int(match.group(1))
        entry = calendar_by_number.get(round_num)

        if entry is None:
            problems.append(
                f"Library: '{folder.name}' has no matching round in the calendar"
            )
            continue

        expected = entry.get("folder")
        if expected and folder.name != expected:
            problems.append(
                f"Library: folder '{folder.name}' should be '{expected}' "
                f"(round {round_num} is now {entry.get('venue')})"
            )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a MotoGP season calendar")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--library", type=str, default=None,
                        help="MotoGP library root to compare against")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as failures")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    path = CONFIG_DIR / f"motogp_{args.season}.json"
    if not path.exists():
        print(f"❌ Calendar not found: {path}")
        return 1

    with open(path, "r", encoding="utf-8") as f:
        calendar = json.load(f)

    problems = validate_calendar(calendar)
    requires_sessions = has_session_times(calendar)
    for entry in calendar.get("rounds", []):
        problems.extend(check_round(entry, args.season, requires_sessions, calendar))

    if args.library:
        problems.extend(check_library_matches(calendar, Path(args.library)))

    rounds = calendar.get("rounds", [])
    active = [r for r in rounds if is_round_active(r)]

    if args.json:
        print(json.dumps({"problems": problems}, indent=2))
        return 1 if problems else 0

    print(f"\n📅 {args.season} calendar: {len(rounds)} rounds "
          f"({len(active)} active, {len(rounds) - len(active)} inactive)")
    if not requires_sessions:
        print("   Legacy format: no session times (backfill by date only)")
    print(f"   File: {path}")

    inactive = [r for r in rounds if not is_round_active(r)]
    for entry in inactive:
        print(f"   ⏸  Round {entry.get('round')} {entry.get('venue')}: "
              f"{get_round_status(entry)}")

    if problems:
        print(f"\n❌ {len(problems)} problem(s):")
        for problem in problems:
            print(f"   - {problem}")
        return 1

    print("\n✅ No problems found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
