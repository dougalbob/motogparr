#!/usr/bin/env python3
"""
Rebuild config/motogp_2027.json from the provisional 2027 calendar.
================================================================

Why this exists
---------------
2027 is the first season with the new MotoGP weekend format: Friday FP1 +
FP2 + Sprint Qualifying, Saturday FP3 + Grand Prix Qualifying + Sprint, and
no Sunday warm-up. Nothing in the repository exercised that format end to end
- the 2026 file was the only real calendar, and the tests only simulated 2027
by re-stamping 2026 data. This script produces a real 2027 calendar so the
validator, the grabber and the WebUI all run against the shape the season will
actually have.

The calendar is the provisional 22-round version published by the FIM and
MotoGP on 25 September 2026:

    * Thailand opens at Buriram on 7 March;
    * Qatar is round 2 at Lusail;
    * Buenos Aires replaces Hungary (round 4, 9-11 April) at the Autódromo
      Oscar y Juan Gálvez;
    * Portimão moves forward to round 17 (1-3 October);
    * Adelaide replaces Phillip Island as round 21 (12-14 November);
    * Valencia closes the season on 28 November.

Session times are NOT yet published for every circuit. The times below follow
the 2027 weekend shape and the standard slot times used for the 2026 season;
they are deliberately marked as provisional in the calendar. Replace them with
the published circuit timetable when it is available - the validator checks
that local and UTC agree, not that the time is the official one.

Usage:
    python3 utils/build_calendar_2027.py            # writes config/motogp_2027.json
    python3 utils/build_calendar_2027.py --stdout   # dry run, print to screen
"""

import argparse
import json
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CALENDAR_PATH = CONFIG_DIR / "motogp_2027.json"

# ---------------------------------------------------------------------------
# Weekend format
# ---------------------------------------------------------------------------
# The 2027 MotoGP format. The session names are the vocabulary used by
# round["motogp_sessions"] and by the grabber's round_runs_session() gate.
WEEKEND_FORMATS = {
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
}

MOTOGP_SESSIONS = [
    "fp1", "fp2", "sprint_qualifying", "fp3", "qualifying", "sprint", "race",
]

# ---------------------------------------------------------------------------
# Provisional session timetable (track-local)
# ---------------------------------------------------------------------------
# Format: calendar_key -> {"offset": "HH:MM", "duration_mins": int}
# The UTC time is derived per round from the round's utc_offset.
STANDARD_SESSIONS = {
    "friday": {
        "moto3_fp1":       {"offset": "09:00", "duration_mins": 35},
        "motogp_fp1":      {"offset": "09:45", "duration_mins": 45},
        "moto2_fp1":       {"offset": "10:40", "duration_mins": 35},
        # The 2027 format only changes MotoGP. The support classes keep their
        # usual Friday Practice sessions; omitting them made the grabber skip
        # Moto3/Moto2 Practice and the WebUI omit them from the day.
        "moto3_practice":  {"offset": "13:15", "duration_mins": 30},
        "motogp_fp2":      {"offset": "14:00", "duration_mins": 30},
        "motogp_sprint_qualifying": {"offset": "14:45", "duration_mins": 15},
        "moto2_practice":  {"offset": "15:10", "duration_mins": 30},
    },
    "saturday": {
        "moto3_fp2":        {"offset": "09:00", "duration_mins": 30},
        "motogp_fp3":       {"offset": "09:40", "duration_mins": 20},
        "moto2_fp2":        {"offset": "10:20", "duration_mins": 30},
        "moto3_qualifying": {"offset": "10:50", "duration_mins": 30},
        "motogp_qualifying": {"offset": "11:30", "duration_mins": 30},
        "motogp_sprint":    {"offset": "14:00", "duration_mins": 25},
        "moto2_qualifying": {"offset": "14:45", "duration_mins": 30},
    },
    "sunday": {
        "moto3_race":  {"offset": "11:00", "duration_mins": 40},
        "moto2_race":  {"offset": "12:15", "duration_mins": 45},
        "motogp_race": {"offset": "14:00", "duration_mins": 50},
    },
}

# ---------------------------------------------------------------------------
# Round definitions - provisional 2027 championship numbering
# ---------------------------------------------------------------------------
# (round, venue, official_name, circuit, aliases, race_date, weekend_start,
#  weekend_end, local_timezone, utc_offset, asset_slug, flag)
ROUNDS = [
    (1, "Thailand", "Thai Grand Prix", "Buriram International Circuit",
     ["thailand", "thai", "buriram", "chang"], "2027-03-07", "2027-03-05",
     "2027-03-07", "Asia/Bangkok", "+07:00", "thailand", "th.svg"),
    (2, "Qatar", "Qatar Grand Prix", "Lusail International Circuit",
     ["qatar", "lusail"], "2027-03-14", "2027-03-12",
     "2027-03-14", "Asia/Qatar", "+03:00", "qatar", "qa.svg"),
    (3, "Brazil", "Brazilian Grand Prix", "Ayrton Senna Circuit",
     ["brazil", "brazilian", "ayrton", "senna", "goiania"], "2027-04-04",
     "2027-04-02", "2027-04-04", "America/Sao_Paulo", "-03:00", "brazil", "br.svg"),
    (4, "Argentina", "Argentine Grand Prix",
     "Autódromo Oscar y Juan Gálvez",
     ["argentina", "argentine", "buenos aires", "galvez"], "2027-04-11",
     "2027-04-09", "2027-04-11", "America/Argentina/Buenos_Aires", "-03:00",
     "argentina", "ar.svg"),
    (5, "Americas", "Grand Prix of the Americas", "Circuit of the Americas",
     ["americas", "austin", "cota", "texas", "usa"], "2027-04-25",
     "2027-04-23", "2027-04-25", "America/Chicago", "-05:00", "americas", "us.svg"),
    (6, "Spain", "Spanish Grand Prix", "Circuito de Jerez - Angel Nieto",
     ["spain", "spanish", "jerez"], "2027-05-09", "2027-05-07",
     "2027-05-09", "Europe/Madrid", "+02:00", "jerez", "es.svg"),
    (7, "France", "French Grand Prix", "Circuit Bugatti (Le Mans)",
     ["france", "french", "le mans", "lemans", "bugatti"], "2027-05-16",
     "2027-05-14", "2027-05-16", "Europe/Paris", "+02:00", "france", "fr.svg"),
    (8, "Italy", "Italian Grand Prix",
     "Autodromo Internazionale del Mugello",
     ["italy", "italian", "mugello", "tuscany"], "2027-05-30",
     "2027-05-28", "2027-05-30", "Europe/Rome", "+02:00", "mugello", "it.svg"),
    (9, "Catalunya", "Catalan Grand Prix", "Circuit de Barcelona-Catalunya",
     ["catalunya", "catalonia", "catalan", "barcelona", "montmelo"],
     "2027-06-13", "2027-06-11", "2027-06-13", "Europe/Madrid", "+02:00",
     "catalunya", "es.svg"),
    (10, "Netherlands", "Dutch Grand Prix", "TT Circuit Assen",
     ["netherlands", "dutch", "assen", "holland"], "2027-06-27",
     "2027-06-25", "2027-06-27", "Europe/Amsterdam", "+02:00", "assen", "nl.svg"),
    (11, "Germany", "German Grand Prix", "Sachsenring",
     ["germany", "german", "sachsenring"], "2027-07-11", "2027-07-09",
     "2027-07-11", "Europe/Berlin", "+02:00", "sachsenring", "de.svg"),
    (12, "Czech Republic", "Czech Grand Prix", "Automotodrom Brno",
     ["czech", "czech republic", "brno"], "2027-07-18", "2027-07-16",
     "2027-07-18", "Europe/Prague", "+02:00", "brno", "cz.svg"),
    (13, "Great Britain", "British Grand Prix", "Silverstone Circuit",
     ["great britain", "british", "silverstone", "uk"], "2027-08-15",
     "2027-08-13", "2027-08-15", "Europe/London", "+01:00", "silverstone", "gb.svg"),
    (14, "Austria", "Austrian Grand Prix", "Red Bull Ring",
     ["austria", "austrian", "spielberg", "red bull ring"], "2027-08-29",
     "2027-08-27", "2027-08-29", "Europe/Vienna", "+02:00", "austria", "at.svg"),
    (15, "San Marino", "San Marino Grand Prix", "Misano World Circuit",
     ["san marino", "misano"], "2027-09-12", "2027-09-10",
     "2027-09-12", "Europe/Rome", "+02:00", "misano", "sm.svg"),
    (16, "Aragon", "Aragon Grand Prix", "MotorLand Aragón",
     ["aragon", "aragón", "motorland"], "2027-09-19", "2027-09-17",
     "2027-09-19", "Europe/Madrid", "+02:00", "aragon", "es.svg"),
    (17, "Portugal", "Portuguese Grand Prix", "Algarve International Circuit",
     ["portugal", "portuguese", "portimao", "portimão", "algarve"],
     "2027-10-03", "2027-10-01", "2027-10-03", "Europe/Lisbon", "+01:00",
     "portimao", "pt.svg"),
    (18, "Japan", "Japanese Grand Prix", "Twin Ring Motegi",
     ["japan", "japanese", "motegi"], "2027-10-17", "2027-10-15",
     "2027-10-17", "Asia/Tokyo", "+09:00", "motegi", "jp.svg"),
    (19, "Malaysia", "Malaysian Grand Prix", "Sepang International Circuit",
     ["malaysia", "malaysian", "sepang"], "2027-10-24", "2027-10-22",
     "2027-10-24", "Asia/Kuala_Lumpur", "+08:00", "sepang", "my.svg"),
    (20, "Indonesia", "Indonesian Grand Prix", "Mandalika International Circuit",
     ["indonesia", "indonesian", "mandalika"], "2027-10-31", "2027-10-29",
     "2027-10-31", "Asia/Makassar", "+08:00", "mandalika", "id.svg"),
    (21, "Australia", "Australian Grand Prix", "Adelaide Street Circuit",
     ["australia", "australian", "adelaide"], "2027-11-14", "2027-11-12",
     "2027-11-14", "Australia/Adelaide", "+10:30", "adelaide", "au.svg"),
    (22, "Valencia", "Valencian Grand Prix", "Circuit Ricardo Tormo",
     ["valencia", "valencian", "cheste"], "2027-11-28", "2027-11-26",
     "2027-11-28", "Europe/Madrid", "+01:00", "valencia", "es.svg"),
]


def _add_minutes(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _apply_offset(hhmm: str, utc_offset: str) -> str:
    """Convert track-local time to UTC using a '+03:00' style offset."""
    sign = -1 if utc_offset.startswith("-") else 1
    oh, om = map(int, utc_offset.lstrip("+-").split(":"))
    return _add_minutes(hhmm, -(sign * (oh * 60 + om)))


def build_sessions(utc_offset: str) -> dict:
    """Build the sessions block for a normal 2027 weekend at the given offset."""
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


def build_calendar() -> dict:
    rounds = []
    for (round_num, venue, official_name, circuit, aliases, race_date,
         weekend_start, weekend_end, local_timezone, utc_offset,
         asset_slug, flag) in ROUNDS:
        assets = {
            class_key: {
                "fanart": f"fanart_{asset_slug}.jpg",
                "poster": f"poster_{asset_slug}.jpg",
            }
            for class_key in ("motogp", "moto2", "moto3")
        }
        rounds.append({
            "round": round_num,
            "venue": venue,
            "official_name": official_name,
            "circuit": circuit,
            "aliases": aliases,
            "date": race_date,
            "weekend_start": weekend_start,
            "weekend_end": weekend_end,
            "status": "scheduled",
            "status_note": None,
            "round_aliases": [],
            "classes": ["motogp", "moto2", "moto3"],
            "weekend_format": "motogp_2027",
            "motogp_sessions": list(MOTOGP_SESSIONS),
            "folder": f"Round {round_num:02d} {venue}",
            "local_timezone": local_timezone,
            "utc_offset": utc_offset,
            "sessions": build_sessions(utc_offset),
            "search_delay_hours": 1,
            "assets": assets,
            "flag": flag,
        })

    return {
        "season": 2027,
        "series": "MotoGP World Championship",
        "classes": ["motogp", "moto2", "moto3"],
        "timezone_note": "All session times are in GMT/UTC. Local times vary by venue.",
        "your_timezone": "GMT+0",
        "session_times_note": (
            "Provisional 2027 timetable: session times follow the 2027 weekend "
            "shape and the standard slot times. Replace with the published "
            "circuit timetable when it is released."
        ),
        "weekend_formats": WEEKEND_FORMATS,
        # Release vocabulary is data, not code: a group that shortens the
        # session name before the season starts can be taught here without a
        # release. The round still has to run the session.
        "release_aliases": {
            "sq": "sprint_qualifying",
            "spq": "sprint_qualifying",
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
