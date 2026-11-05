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
    * rounds with no session times (cannot be scheduled)
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
    CLASS_ASSET_DIRS,
    get_round_status,
    is_round_active,
    validate_calendar,
)

CONFIG_DIR = REPO_ROOT / "config"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _offset_minutes(utc_offset: str) -> int:
    sign = -1 if utc_offset.startswith("-") else 1
    oh, om = map(int, utc_offset.lstrip("+-").split(":"))
    return sign * (oh * 60 + om)


def check_round(entry: dict, season: int) -> list:
    """Per-round checks that validate_calendar() does not cover."""
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
        problems.append(f"{label}: no session times, grabber cannot schedule it")
    else:
        for day, day_sessions in sessions.items():
            if day not in {"friday", "saturday", "sunday"}:
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
    for entry in calendar.get("rounds", []):
        problems.extend(check_round(entry, args.season))

    if args.library:
        problems.extend(check_library_matches(calendar, Path(args.library)))

    rounds = calendar.get("rounds", [])
    active = [r for r in rounds if is_round_active(r)]

    if args.json:
        print(json.dumps({"problems": problems}, indent=2))
        return 1 if problems else 0

    print(f"\n📅 {args.season} calendar: {len(rounds)} rounds "
          f"({len(active)} active, {len(rounds) - len(active)} inactive)")
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
