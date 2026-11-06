#!/usr/bin/env python3
"""
One-off rebuild of config/motogp_2026.json to the OFFICIAL 2026 calendar.
=========================================================================

Why this exists
---------------
Qatar (originally Round 4, 10-12 April) was postponed to 6-8 November because
of the conflict in the Middle East. The official championship numbering is now:

    1 Thailand ......... 4 Spain ......... 19 Malaysia
    2 Brazil ........... 5 France ........ 20 Qatar   (moved from April)
    3 Americas ......... ...              21 Portugal (moved from 15 Nov)
                                          22 Valencia (moved from 22 Nov)

So everything from Spain (Round 4) onwards shifts down by one versus the file
we shipped earlier, and Qatar moves from the front of the season to the
Australia-Malaysia-Qatar triple header at the end.

The script also stamps every round with the metadata the code now prefers:

    "status"           - "scheduled" | "cancelled" | "postponed" | "replaced"
    "weekend_format"   - key into the top level "weekend_formats" block
    "motogp_sessions"  - which MotoGP sessions this weekend actually runs
    "classes"          - which classes are racing (MotoGP only events exist)

That way a cancellation, a reschedule or a MotoGP-only event never needs
another round of code edits - only data.

Usage:
    python3 utils/build_calendar_2026.py            # writes config/motogp_2026.json
    python3 utils/build_calendar_2026.py --stdout   # dry run, print to screen

Session times for Qatar are the published Lusail timetable, local time
(Asia/Qatar, UTC+3, no DST) and the equivalent UTC used by the rest of the
file.
"""

import argparse
import json
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CALENDAR_PATH = CONFIG_DIR / "motogp_2026.json"

# ---------------------------------------------------------------------------
# Session timetables
# ---------------------------------------------------------------------------
# Standard 2026 weekend, reused by every round except Qatar, which has its own
# (published) timetable because it is a night race in a one-off November slot.
# Format: calendar_key -> (local_time, utc_time, duration_mins)
# The UTC time is derived per-round from the round's utc_offset below.

STANDARD_SESSIONS = {
    "friday": {
        "moto3_fp1":       {"offset": "09:00", "duration_mins": 35},
        "motogp_fp1":      {"offset": "09:45", "duration_mins": 45},
        "moto2_fp1":       {"offset": "10:40", "duration_mins": 35},
        "moto3_practice":  {"offset": "13:15", "duration_mins": 30},
        "motogp_practice": {"offset": "14:00", "duration_mins": 60},
        "moto2_practice":  {"offset": "15:10", "duration_mins": 30},
    },
    "saturday": {
        "moto3_fp2":        {"offset": "09:00", "duration_mins": 30},
        "motogp_fp2":       {"offset": "09:40", "duration_mins": 30},
        "moto2_fp2":        {"offset": "10:20", "duration_mins": 30},
        "moto3_qualifying": {"offset": "10:50", "duration_mins": 30},
        "motogp_qualifying": {"offset": "11:30", "duration_mins": 30},
        "motogp_sprint":    {"offset": "14:00", "duration_mins": 25},
        "moto2_qualifying": {"offset": "14:45", "duration_mins": 30},
    },
    "sunday": {
        "motogp_warmup": {"offset": "09:40", "duration_mins": 10},
        "moto3_race":    {"offset": "11:00", "duration_mins": 40},
        "moto2_race":    {"offset": "12:15", "duration_mins": 45},
        "motogp_race":   {"offset": "14:00", "duration_mins": 50},
    },
}

# Qatar 2026 confirmed timetable (local Asia/Qatar = UTC+3).
QATAR_SESSIONS = {
    "friday": {
        "moto3_fp1":       {"local": "13:00", "gmt": "10:00", "duration_mins": 35},
        "moto2_fp1":       {"local": "13:50", "gmt": "10:50", "duration_mins": 40},
        "motogp_fp1":      {"local": "14:45", "gmt": "11:45", "duration_mins": 45},
        "moto3_practice":  {"local": "17:15", "gmt": "14:15", "duration_mins": 35},
        "moto2_practice":  {"local": "18:05", "gmt": "15:05", "duration_mins": 40},
        "motogp_practice": {"local": "19:00", "gmt": "16:00", "duration_mins": 60},
    },
    # Qualifying is stored under the combined key the code already uses
    # (motogp_qualifying), with the Q1/Q2 split recorded in "parts" so it is
    # still possible to tell the two sessions apart.
    "saturday": {
        "moto3_fp2":         {"local": "12:30", "gmt": "09:30", "duration_mins": 30},
        "moto2_fp2":         {"local": "13:15", "gmt": "10:15", "duration_mins": 30},
        "motogp_fp2":        {"local": "14:00", "gmt": "11:00", "duration_mins": 30},
        "motogp_qualifying": {
            "local": "14:40", "gmt": "11:40", "duration_mins": 40,
            "parts": [
                {"key": "q1", "local": "14:40", "duration_mins": 15},
                {"key": "q2", "local": "15:05", "duration_mins": 15},
            ],
        },
        "moto3_qualifying":  {
            "local": "16:50", "gmt": "13:50", "duration_mins": 40,
            "parts": [
                {"key": "q1", "local": "16:50", "duration_mins": 15},
                {"key": "q2", "local": "17:15", "duration_mins": 15},
            ],
        },
        "moto2_qualifying":  {
            "local": "17:45", "gmt": "14:45", "duration_mins": 40,
            "parts": [
                {"key": "q1", "local": "17:45", "duration_mins": 15},
                {"key": "q2", "local": "18:10", "duration_mins": 15},
            ],
        },
        "motogp_sprint":     {"local": "19:00", "gmt": "16:00", "duration_mins": 25},
    },
    "sunday": {
        "motogp_warmup": {"local": "14:40", "gmt": "11:40", "duration_mins": 10},
        "moto3_race":    {"local": "16:00", "gmt": "13:00", "duration_mins": 40},
        "moto2_race":    {"local": "17:15", "gmt": "14:15", "duration_mins": 45},
        "motogp_race":   {"local": "19:00", "gmt": "16:00", "duration_mins": 50},
    },
}

# MotoGP sessions run by the "standard" 2026 weekend format.
STANDARD_MOTOGP_SESSIONS = ["fp1", "practice", "fp2", "qualifying", "sprint", "warmup", "race"]

# Calendar keys that belong to each class (used to know what we can search for).
CLASS_SESSION_PREFIXES = {
    "motogp": "motogp_",
    "moto2": "moto2_",
    "moto3": "moto3_",
}

ASSET_SLUG_OVERRIDES = {
    "Americas": "americas",
    "Czech Republic": "brno",
    "Great Britain": "silverstone",
    "Netherlands": "assen",
    "San Marino": "misano",
    "Valencia": "valencia",
}


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _apply_offset(hhmm: str, utc_offset: str) -> str:
    """Convert local time to UTC using a '+03:00' style offset."""
    sign = -1 if utc_offset.startswith("-") else 1
    oh, om = map(int, utc_offset.lstrip("+-").split(":"))
    return _add_minutes(hhmm, -(sign * (oh * 60 + om)))


def build_standard_sessions(utc_offset: str) -> dict:
    """Build a sessions block for a normal weekend at the given UTC offset."""
    sessions = {}
    for day, day_sessions in STANDARD_SESSIONS.items():
        sessions[day] = {}
        for key, info in day_sessions.items():
            local = info["offset"]
            sessions[day][key] = {
                "local": local,
                "gmt": _apply_offset(local, utc_offset),
                "duration_mins": info["duration_mins"],
            }
    return sessions


# Which calendar keys implement a given MotoGP session name. Used to turn a
# weekend-format definition (which speaks in session names) into the concrete
# keys present in the "sessions" block.
MOTOGP_SESSION_TO_CALENDAR_KEYS = {
    "fp1": ["motogp_fp1"],
    "practice": ["motogp_practice"],
    "fp2": ["motogp_fp2"],
    "fp3": ["motogp_fp3"],
    "qualifying": ["motogp_qualifying"],
    "sprint_qualifying": ["motogp_sprint_qualifying"],
    "sprint": ["motogp_sprint"],
    "warmup": ["motogp_warmup"],
    "race": ["motogp_race"],
}


# ---------------------------------------------------------------------------
# Round definitions - official 2026 championship numbering
# ---------------------------------------------------------------------------
# (round, venue, official_name, circuit, aliases, race_date, weekend_start,
#  weekend_end, local_timezone, utc_offset, asset_slug, flag, status)
ROUNDS = [
    (1, "Thailand", "Thai Grand Prix", "Buriram International Circuit",
     ["thailand", "thai", "buriram", "chang"], "2026-03-01", "2026-02-27", "2026-03-01",
     "Asia/Bangkok", "+07:00", "thailand", "th.svg", "scheduled"),
    (2, "Brazil", "Brazilian Grand Prix", "Ayrton Senna Circuit",
     ["brazil", "brazilian", "ayrton", "senna", "goiania"], "2026-03-22", "2026-03-20",
     "2026-03-22", "America/Sao_Paulo", "-03:00", "brazil", "br.svg", "scheduled"),
    (3, "Americas", "Grand Prix of the Americas", "Circuit of the Americas",
     ["americas", "austin", "cota", "texas", "usa"], "2026-03-29", "2026-03-27",
     "2026-03-29", "America/Chicago", "-05:00", "americas", "us.svg", "scheduled"),
    (4, "Spain", "Spanish Grand Prix", "Circuito de Jerez - Angel Nieto",
     ["spain", "spanish", "jerez"], "2026-04-26", "2026-04-24",
     "2026-04-26", "Europe/Madrid", "+02:00", "jerez", "es.svg", "scheduled"),
    (5, "France", "French Grand Prix", "Circuit Bugatti (Le Mans)",
     ["france", "french", "le mans", "lemans", "bugatti"], "2026-05-10", "2026-05-08",
     "2026-05-10", "Europe/Paris", "+02:00", "france", "fr.svg", "scheduled"),
    (6, "Catalunya", "Catalan Grand Prix", "Circuit de Barcelona-Catalunya",
     ["catalunya", "catalonia", "catalan", "barcelona", "montmelo"], "2026-05-17",
     "2026-05-15", "2026-05-17", "Europe/Madrid", "+02:00", "catalunya", "es.svg", "scheduled"),
    (7, "Italy", "Italian Grand Prix", "Autodromo Internazionale del Mugello",
     ["italy", "italian", "mugello", "tuscany"], "2026-05-31", "2026-05-29",
     "2026-05-31", "Europe/Rome", "+02:00", "mugello", "it.svg", "scheduled"),
    (8, "Hungary", "Hungarian Grand Prix", "Balaton Park Circuit",
     ["hungary", "hungarian", "balaton"], "2026-06-07", "2026-06-05",
     "2026-06-07", "Europe/Budapest", "+02:00", "hungary", "hu.svg", "scheduled"),
    (9, "Czech Republic", "Czech Grand Prix", "Automotodrom Brno",
     ["czech", "czechia", "brno"], "2026-06-21", "2026-06-19",
     "2026-06-21", "Europe/Prague", "+02:00", "brno", "cz.svg", "scheduled"),
    (10, "Netherlands", "Dutch TT", "TT Circuit Assen",
     ["netherlands", "dutch", "assen", "tt"], "2026-06-28", "2026-06-26",
     "2026-06-28", "Europe/Amsterdam", "+02:00", "assen", "nl.svg", "scheduled"),
    (11, "Germany", "German Grand Prix", "Sachsenring",
     ["germany", "german", "sachsenring"], "2026-07-12", "2026-07-10",
     "2026-07-12", "Europe/Berlin", "+02:00", "sachsenring", "de.svg", "scheduled"),
    (12, "Great Britain", "British Grand Prix", "Silverstone Circuit",
     ["great britain", "british", "silverstone", "uk"], "2026-08-09", "2026-08-07",
     "2026-08-09", "Europe/London", "+01:00", "silverstone", "gb.svg", "scheduled"),
    (13, "Aragon", "Aragon Grand Prix", "MotorLand Aragon",
     ["aragon", "motorland", "alcaniz"], "2026-08-30", "2026-08-28",
     "2026-08-30", "Europe/Madrid", "+02:00", "aragon", "es.svg", "scheduled"),
    (14, "San Marino", "San Marino Grand Prix", "Misano World Circuit Marco Simoncelli",
     ["san marino", "misano", "rimini"], "2026-09-13", "2026-09-11",
     "2026-09-13", "Europe/Rome", "+02:00", "misano", "sm.svg", "scheduled"),
    (15, "Austria", "Austrian Grand Prix", "Red Bull Ring",
     ["austria", "austrian", "spielberg", "red bull ring"], "2026-09-20", "2026-09-18",
     "2026-09-20", "Europe/Vienna", "+02:00", "austria", "at.svg", "scheduled"),
    (16, "Japan", "Japanese Grand Prix", "Mobility Resort Motegi",
     ["japan", "japanese", "motegi"], "2026-10-04", "2026-10-02",
     "2026-10-04", "Asia/Tokyo", "+09:00", "motegi", "jp.svg", "scheduled"),
    (17, "Indonesia", "Indonesian Grand Prix", "Pertamina Mandalika Circuit",
     ["indonesia", "indonesian", "mandalika"], "2026-10-11", "2026-10-09",
     "2026-10-11", "Asia/Makassar", "+08:00", "mandalika", "id.svg", "scheduled"),
    (18, "Australia", "Australian Grand Prix", "Phillip Island Grand Prix Circuit",
     ["australia", "australian", "phillip island", "phillip"], "2026-10-25", "2026-10-23",
     "2026-10-25", "Australia/Melbourne", "+11:00", "phillip_island", "au.svg", "scheduled"),
    (19, "Malaysia", "Malaysian Grand Prix", "Sepang International Circuit",
     ["malaysia", "malaysian", "sepang"], "2026-11-01", "2026-10-30",
     "2026-11-01", "Asia/Kuala_Lumpur", "+08:00", "sepang", "my.svg", "scheduled"),
    # --- Qatar: originally Round 4 (10-12 April), postponed to 6-8 November ---
    (20, "Qatar", "Qatar Grand Prix", "Lusail International Circuit",
     ["qatar", "lusail", "losail", "doha"], "2026-11-08", "2026-11-06",
     "2026-11-08", "Asia/Qatar", "+03:00", "qatar", "qa.svg", "scheduled"),
    (21, "Portugal", "Portuguese Grand Prix", "Autodromo Internacional do Algarve",
     ["portugal", "portuguese", "portimao", "algarve"], "2026-11-22", "2026-11-20",
     "2026-11-22", "Europe/Lisbon", "+00:00", "portimao", "pt.svg", "scheduled"),
    (22, "Valencia", "Valencian Community Grand Prix", "Circuit Ricardo Tormo",
     ["valencia", "valencian", "ricardo tormo", "cheste", "comunitat"], "2026-11-29",
     "2026-11-27", "2026-11-29", "Europe/Madrid", "+01:00", "valencia", "es.svg", "scheduled"),
]

# Per-round overrides for weekends that do not use the standard timetable.
SESSION_OVERRIDES = {
    "Qatar": QATAR_SESSIONS,
}

# Rounds that only run some of the classes (none in 2026, kept for future use).
CLASS_OVERRIDES = {}

# Why a round is not at its original slot. Free text, shown nowhere critical.
STATUS_NOTES = {
    "Qatar": "Originally round 4 (10-12 April 2026); postponed to 6-8 November "
             "because of the conflict in the Middle East.",
}

# Other round numbers this event has been known by, so release searches still
# match uploads named after the original calendar. Qatar was round 4 until the
# April date was postponed.
ROUND_ALIASES = {
    "Qatar": [4],
}

# MotoGP-only session overrides, e.g. a sprint-less weekend:
#   MOTOGP_SESSION_OVERRIDES = {1: ["fp1", "practice", "fp2", "qualifying", "race"]}
MOTOGP_SESSION_OVERRIDES = {}


def build_assets(slug: str) -> dict:
    return {
        cls: {"fanart": f"fanart_{slug}.jpg", "poster": f"poster_{slug}.jpg"}
        for cls in ("motogp", "moto2", "moto3")
    }


def build_calendar() -> dict:
    rounds = []

    for (round_num, venue, official_name, circuit, aliases, date, weekend_start,
         weekend_end, tz, utc_offset, slug, flag, status) in ROUNDS:

        sessions = SESSION_OVERRIDES.get(venue) or build_standard_sessions(utc_offset)
        classes = CLASS_OVERRIDES.get(venue, ["motogp", "moto2", "moto3"])

        # Which MotoGP sessions does this weekend actually run? Derived from the
        # sessions block so a format change or a dropped session is data-only.
        available = {key for day in sessions.values() for key in day}
        wanted_sessions = MOTOGP_SESSION_OVERRIDES.get(
            round_num, STANDARD_MOTOGP_SESSIONS
        )
        motogp_sessions = [
            session for session in wanted_sessions
            if any(key in available
                   for key in MOTOGP_SESSION_TO_CALENDAR_KEYS.get(session, []))
        ]

        rounds.append({
            "round": round_num,
            "venue": venue,
            "official_name": official_name,
            "circuit": circuit,
            "aliases": aliases,
            "date": date,
            "weekend_start": weekend_start,
            "weekend_end": weekend_end,
            "status": status,
            "status_note": STATUS_NOTES.get(venue),
            "round_aliases": ROUND_ALIASES.get(venue, []),
            "classes": classes,
            "weekend_format": "motogp_standard",
            "motogp_sessions": motogp_sessions,
            "folder": f"Round {round_num:02d} {venue}",
            "local_timezone": tz,
            "utc_offset": utc_offset,
            "sessions": sessions,
            "search_delay_hours": 1,
            "assets": build_assets(slug),
            "flag": flag,
        })

    return {
        "season": 2026,
        "series": "MotoGP World Championship",
        "classes": ["motogp", "moto2", "moto3"],
        "timezone_note": "All session times are in GMT/UTC. Local times vary by venue.",
        "your_timezone": "GMT+0",
        "weekend_formats": {
            "motogp_standard": {
                "friday": [
                    {"session": "fp1", "label": "FP1", "duration_mins": 45,
                     "note": "Same as 2026"},
                    {"session": "practice", "label": "Practice", "duration_mins": 60,
                     "note": "Top 10 get direct Q2 access"},
                ],
                "saturday": [
                    {"session": "fp2", "label": "FP2", "duration_mins": 30},
                    {"session": "qualifying", "label": "Qualifying", "duration_mins": 30},
                    {"session": "sprint", "label": "Sprint", "duration_mins": 25,
                     "note": "Grid from grand prix qualifying"},
                ],
                "sunday": [
                    {"session": "warmup", "label": "Warm-up", "duration_mins": 10},
                    {"session": "race", "label": "Race", "duration_mins": 50,
                     "note": "Grid from grand prix qualifying"},
                ],
            },
            # 2027 MotoGP-only format. Schedule keys deliberately mirror the
            # 2026 names so existing search/classification rules keep working:
            #   practice  -> Friday's one-hour Practice is replaced by the
            #                30 minute FP2
            #   sprint_qualifying -> NEW Friday session before FP2, sets the
            #                Sprint grid (it is not a Saturday session)
            "motogp_2027": {
                "friday": [
                    {"session": "fp1", "label": "FP1", "duration_mins": 45,
                     "note": "Same as 2026"},
                    {"session": "fp2", "label": "FP2", "duration_mins": 30,
                     "note": "Was 1 hour; no longer decides Q2 places"},
                    {"session": "sprint_qualifying", "label": "Sprint Qualifying",
                     "duration_mins": 15, "note": "NEW - sets the Sprint grid"},
                ],
                "saturday": [
                    {"session": "fp3", "label": "FP3", "duration_mins": 20,
                     "note": "Was 30 minutes"},
                    {"session": "qualifying", "label": "Grand Prix Qualifying",
                     "duration_mins": 30,
                     "note": "Q1 15 mins all riders; Q2 15 mins top 12 from Q1"},
                    {"session": "sprint", "label": "Sprint", "duration_mins": 25,
                     "note": "Grid from Sprint Qualifying"},
                ],
                "sunday": [
                    {"session": "race", "label": "Race", "duration_mins": 50,
                     "note": "Warm-up removed; grid from Grand Prix qualifying"},
                ],
            },
        },
        "rounds": rounds,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true",
                        help="Print the calendar instead of writing it")
    args = parser.parse_args()

    calendar = build_calendar()

    if args.stdout:
        json.dump(calendar, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    with open(CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(calendar, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {CALENDAR_PATH} ({len(calendar['rounds'])} rounds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
