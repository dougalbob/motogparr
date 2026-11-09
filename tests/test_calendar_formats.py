"""
Regression tests for calendar changes and weekend formats.

These cover the two things that broke in 2026 and will break again:

    1. A round being cancelled and rescheduled (Qatar, April -> November),
       which renumbers the championship.
    2. A weekend format change (the 2027 MotoGP schedule), which adds and
       removes sessions without changing anything else.

Run with:  python3 -m pytest tests -q
"""

import json
import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_common as common


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def calendar_2026():
    calendar = common.load_calendar(2026)
    assert calendar, "config/motogp_2026.json should load"
    return calendar


@pytest.fixture
def qatar(calendar_2026):
    return common.get_round_by_number(calendar_2026, 20)


@pytest.fixture
def calendar_2027(calendar_2026):
    """2026 calendar re-stamped with the 2027 MotoGP format."""
    calendar = copy.deepcopy(calendar_2026)
    calendar["season"] = 2027
    # The real config/motogp_2027.json teaches the 2027 release vocabulary in
    # data. The fixture needs the same map for the alias tests to mean
    # anything; without it, SQ is correctly refused.
    calendar["release_aliases"] = {
        "sq": "sprint_qualifying",
        "spq": "sprint_qualifying",
    }

    # 2027 Friday: FP1, FP2 (30m), Sprint Qualifying. No Friday "Practice".
    # 2027 Saturday: FP3 (20m), GP Q1/Q2, Sprint. Sunday: race only.
    friday = {
        "moto3_fp1": {"local": "09:00", "gmt": "03:00", "duration_mins": 35},
        "motogp_fp1": {"local": "09:45", "gmt": "03:45", "duration_mins": 45},
        "moto2_fp1": {"local": "10:40", "gmt": "04:40", "duration_mins": 35},
        "motogp_fp2": {"local": "14:00", "gmt": "08:00", "duration_mins": 30},
        "motogp_sprint_qualifying": {
            "local": "14:45", "gmt": "08:45", "duration_mins": 15,
        },
    }
    saturday = {
        "motogp_fp3": {"local": "09:40", "gmt": "03:40", "duration_mins": 20},
        "moto3_qualifying": {"local": "10:50", "gmt": "04:50", "duration_mins": 30},
        "motogp_qualifying": {"local": "11:30", "gmt": "05:30", "duration_mins": 30},
        "motogp_sprint": {"local": "14:00", "gmt": "08:00", "duration_mins": 25},
    }
    sunday = {
        "moto3_race": {"local": "11:00", "gmt": "05:00", "duration_mins": 40},
        "moto2_race": {"local": "12:15", "gmt": "06:15", "duration_mins": 45},
        "motogp_race": {"local": "14:00", "gmt": "08:00", "duration_mins": 50},
    }

    for entry in calendar["rounds"]:
        entry["weekend_format"] = "motogp_2027"
        if entry["venue"] != "Qatar":
            entry["sessions"] = {
                "friday": copy.deepcopy(friday),
                "saturday": copy.deepcopy(saturday),
                "sunday": copy.deepcopy(sunday),
            }
        entry["motogp_sessions"] = [
            "fp1", "fp2", "sprint_qualifying", "fp3",
            "qualifying", "sprint", "race",
        ]

    return calendar


# ---------------------------------------------------------------------------
# 2026 calendar correctness
# ---------------------------------------------------------------------------

def test_2026_calendar_is_valid(calendar_2026):
    assert common.validate_calendar(calendar_2026) == []


def test_2026_has_22_rounds_in_date_order(calendar_2026):
    rounds = calendar_2026["rounds"]
    assert len(rounds) == 22
    dates = [r["date"] for r in rounds]
    assert dates == sorted(dates), "rounds must be listed in chronological order"
    assert [r["round"] for r in rounds] == list(range(1, 23))


@pytest.mark.parametrize("venue,round_no", [
    ("Spain", 4),
    ("Malaysia", 19),
    ("Qatar", 20),
    ("Portugal", 21),
    ("Valencia", 22),
])
def test_official_2026_numbering(calendar_2026, venue, round_no):
    """Qatar moved from April to November, so everything from Spain shifts."""
    match = [r for r in calendar_2026["rounds"] if r["venue"] == venue]
    assert len(match) == 1
    assert match[0]["round"] == round_no


def test_qatar_is_the_rescheduled_november_round(qatar):
    assert qatar["date"] == "2026-11-08"
    assert qatar["weekend_start"] == "2026-11-06"
    assert qatar["folder"] == "Round 20 Qatar"
    assert qatar["local_timezone"] == "Asia/Qatar"


def test_qatar_session_times_match_utc(qatar):
    """The published timetable must convert to the stored UTC times."""
    from utils.validate_calendar import check_round
    assert check_round(qatar, 2026) == []


def test_qatar_grand_prix_is_a_night_race(qatar):
    """Local 19:00 (16:00 UTC) Sunday start."""
    race = qatar["sessions"]["sunday"]["motogp_race"]
    assert race["local"] == "19:00"
    assert race["gmt"] == "16:00"


def test_portugal_and_valencia_dates(calendar_2026):
    portugal = common.get_round_by_number(calendar_2026, 21)
    valencia = common.get_round_by_number(calendar_2026, 22)
    assert portugal["venue"] == "Portugal"
    assert portugal["weekend_start"] == "2026-11-20"
    assert valencia["venue"] == "Valencia"
    assert valencia["weekend_end"] == "2026-11-29"


def test_no_round_remains_in_april_2026(calendar_2026):
    """The cancelled April slot must not reappear with Qatar's old dates."""
    april = [r for r in calendar_2026["rounds"] if r["date"].startswith("2026-04")]
    assert [r["venue"] for r in april] == ["Spain"]


# ---------------------------------------------------------------------------
# Session scheduling
# ---------------------------------------------------------------------------

def test_session_end_times_are_derived_from_calendar(calendar_2026, qatar):
    from motogp_grabber import get_session_end_time

    assert str(get_session_end_time(qatar, common.SessionType.MOTOGP_FP1)) \
        == "2026-11-06 12:30:00"
    assert str(get_session_end_time(qatar, common.SessionType.MOTOGP_RACE)) \
        == "2026-11-08 16:50:00"


def test_session_start_times_match_published_timetable(qatar):
    """Start times must come from the calendar, not be guessed from the end."""
    from motogp_grabber import get_session_start_time, get_session_times

    assert str(get_session_start_time(qatar, common.SessionType.MOTOGP_FP1)) \
        == "2026-11-06 11:45:00"
    assert str(get_session_start_time(qatar, common.SessionType.MOTOGP_PRACTICE)) \
        == "2026-11-06 16:00:00"
    assert str(get_session_start_time(qatar, common.SessionType.MOTOGP_QUALIFYING
                                      if hasattr(common.SessionType, "MOTOGP_QUALIFYING")
                                      else common.SessionType.MOTOGP_Q2)) \
        == "2026-11-07 11:40:00"

    start, end = get_session_times(qatar, common.SessionType.MOTOGP_RACE)
    assert (end - start).total_seconds() == 50 * 60


def test_upcoming_sessions_use_real_start_times(calendar_2026, qatar, monkeypatch):
    import motogp_grabber
    from motogp_grabber import get_upcoming_sessions

    class RaceMorning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 8, 8, 0)

    monkeypatch.setattr(motogp_grabber, "datetime", RaceMorning)

    session = common.SessionType.MOTOGP_RACE
    start = datetime(2026, 11, 8, 16, 0)
    # A calendar containing only Qatar, evaluated as if "now" were race morning.
    calendar = {"rounds": [qatar]}
    upcoming = get_upcoming_sessions(calendar, hours_ahead=24 * 30)
    entries = [u for u in upcoming if u["session_type"] == session]
    assert entries, "race should be listed as upcoming"
    assert entries[0]["starts_at"] == start
    assert entries[0]["ends_at"] == start + timedelta(minutes=50)


def test_2026_motogp_runs_the_warmup(calendar_2026, qatar):
    assert common.round_runs_session(
        qatar, common.SessionType.MOTOGP_WARMUP, calendar_2026
    )


def test_2027_grabber_does_not_look_for_warmup(calendar_2027):
    """Warm-up is dropped from 2027: the grabber must stop hunting for it."""
    entry = common.get_round_by_number(calendar_2027, 20)
    assert not common.round_runs_session(
        entry, common.SessionType.MOTOGP_WARMUP, calendar_2027
    )
    assert common.round_runs_session(
        entry, common.SessionType.MOTOGP_RACE, calendar_2027
    )


def test_2027_adds_fp3_and_sprint_qualifying(calendar_2027):
    entry = common.get_round_by_number(calendar_2027, 20)
    for session in (common.SessionType.MOTOGP_FP3,
                    common.SessionType.MOTOGP_SPRINT_QUALIFYING,
                    common.SessionType.MOTOGP_FP2):
        assert common.round_runs_session(entry, session, calendar_2027), session.name


def test_2027_has_no_friday_practice_for_motogp(calendar_2027):
    entry = common.get_round_by_number(calendar_2027, 20)
    assert not common.round_runs_session(
        entry, common.SessionType.MOTOGP_PRACTICE, calendar_2027
    )


def test_2027_friday_fp2_has_a_start_and_end_time(calendar_2027):
    """
    The regression this test exists for: the 2027 fixture puts FP2 in the
    Friday block, but get_session_times() used to resolve motogp_fp2 through
    CALENDAR_KEY_TO_DAY ("saturday"), find nothing there, and return
    (None, None). The grabber then logged "Could not determine end time" at
    DEBUG and never searched for FP2 - while every session-gate test passed.
    """
    from motogp_grabber import get_session_times

    entry = common.get_round_by_number(calendar_2027, 1)
    start, end = get_session_times(entry, common.SessionType.MOTOGP_FP2)
    assert start is not None, "2027 Friday FP2 must have a start time"
    assert end is not None, "2027 Friday FP2 must have an end time"
    assert str(start) == "2026-02-27 08:00:00"
    assert str(end) == "2026-02-27 08:30:00"


def test_2026_saturday_fp2_still_resolves(calendar_2026):
    """The legacy lookup must keep working for the 2026 calendar."""
    from motogp_grabber import get_session_times

    qatar = common.get_round_by_number(calendar_2026, 20)
    start, end = get_session_times(qatar, common.SessionType.MOTOGP_FP2)
    assert str(start) == "2026-11-07 11:00:00"
    assert str(end) == "2026-11-07 11:30:00"


def test_flyaway_friday_morning_anchors_to_the_previous_utc_day(calendar_2026):
    """
    Phillip Island Friday 09:45 (+11:00) is Thursday 22:45 UTC, not Friday
    22:45. The grabber used to combine the stored gmt time with the local day
    and put the session 24 hours late; the WebUI already anchored on
    local + utc_offset.
    """
    from motogp_grabber import get_session_times

    australia = common.get_round_by_number(calendar_2026, 18)
    start, end = get_session_times(australia, common.SessionType.MOTOGP_FP1)
    assert str(start) == "2026-10-22 22:45:00"
    assert str(end) == "2026-10-22 23:30:00"


def test_a_thursday_session_is_supported():
    """
    Flyaway weekends can run on Thursday. The day list is shared with the
    WebUI, so a Thursday block must produce a real time rather than being
    rejected as an unknown day.
    """
    from motogp_grabber import get_session_times

    entry = {
        "weekend_start": "2026-02-27",
        "date": "2026-03-01",
        "sessions": {
            "thursday": {
                "motogp_fp1": {"local": "09:00", "gmt": "02:00", "duration_mins": 45},
            }
        },
    }
    start, end = get_session_times(entry, common.SessionType.MOTOGP_FP1)
    assert str(start) == "2026-02-26 02:00:00"
    assert str(end) == "2026-02-26 02:45:00"


def test_support_classes_keep_their_sessions_in_2027(calendar_2027):
    """The 2027 format change only applies to MotoGP."""
    entry = common.get_round_by_number(calendar_2027, 20)
    assert common.round_runs_session(
        entry, common.SessionType.MOTO3_RACE, calendar_2027
    )
    assert common.round_runs_session(
        entry, common.SessionType.MOTO2_Q2, calendar_2027
    )
    assert common.round_runs_session(
        entry, common.SessionType.MOTO3_FP1, calendar_2027
    )


# ---------------------------------------------------------------------------
# Classification of 2027 release names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("MotoGP 2027 Qatar Sprint Qualifying 1080p", "MOTOGP_SPRINT_QUALIFYING"),
    ("MotoGP 2027 Qatar Sprint 1080p", "MOTOGP_SPRINT"),
    ("MotoGP 2027 Qatar FP3 1080p", "MOTOGP_FP3"),
    ("MotoGP 2027 Qatar FP2 1080p", "MOTOGP_FP2"),
    ("MotoGP 2027 Qatar FP1 1080p", "MOTOGP_FP1"),
    ("MotoGP 2027 Qatar Practice 1080p", "MOTOGP_PRACTICE"),
    ("MotoGP 2027 Qatar Qualifying 1080p", "MOTOGP_Q2"),
    ("MotoGP 2027 Qatar Q1 1080p", "MOTOGP_Q1"),
    ("MotoGP 2027 Qatar Race 1080p", "MOTOGP_RACE"),
    ("Moto2 2027 Qatar Qualifying 1080p", "MOTO2_Q2"),
    ("Moto3 2027 Qatar Race 1080p", "MOTO3_RACE"),
])
def test_classify_2027_release_names(title, expected):
    assert common.classify_session(title).name == expected


def test_quality_token_is_not_mistaken_for_a_qualifying_part():
    """"Qualifying 1080p" used to match the "qualifying 1" pattern."""
    assert common.matches_qualifying_part("Qualifying 1080p", 1) is False
    assert common.matches_qualifying_part("Qualifying 2160p", 2) is False
    assert common.matches_qualifying_part("Qualifying 1 1080p", 1) is True
    assert common.matches_qualifying_part("Q2 1080p", 2) is True


# ---------------------------------------------------------------------------
# Refuse-to-guess release classification (issue #33, P2)
# ---------------------------------------------------------------------------

def test_sq_release_resolves_through_the_calendar_alias(calendar_2027):
    """2027 teaches the release vocabulary in data, not in a token table."""
    entry = common.get_round_by_number(calendar_2027, 1)
    title = "MotoGP 2027 Thailand SQ 1080p MWR"
    assert common.classify_session(
        title, round_info=entry, calendar=calendar_2027
    ) is common.SessionType.MOTOGP_SPRINT_QUALIFYING


def test_spq_release_resolves_through_the_calendar_alias(calendar_2027):
    entry = common.get_round_by_number(calendar_2027, 1)
    title = "MotoGP 2027 Thailand SPQ 1080p MWR"
    assert common.classify_session(
        title, round_info=entry, calendar=calendar_2027
    ) is common.SessionType.MOTOGP_SPRINT_QUALIFYING


def test_sq_release_is_refused_without_the_alias_map(calendar_2027):
    """
    Without the season's alias map, SQ is not a known session. The old code
    fell through to MOTOGP_RACE; the fix refuses.
    """
    entry = common.get_round_by_number(calendar_2027, 1)
    calendar = copy.deepcopy(calendar_2027)
    calendar.pop("release_aliases", None)
    assert common.classify_session(
        "MotoGP 2027 Thailand SQ 1080p MWR", round_info=entry, calendar=calendar
    ) is common.SessionType.UNKNOWN


def test_sq_release_is_not_accepted_as_the_race(calendar_2027):
    """
    The regression that mattered: filter_releases() accepted an SQ title as
    the Race target, which would have written a wrong folder name and a wrong
    Plex identity. It must be refused for the Race, and accepted for the
    session it actually is.
    """
    from motogp_common import filter_releases

    entry = common.get_round_by_number(calendar_2027, 1)
    release = {"title": "MotoGP 2027 Thailand SQ 1080p MWR",
               "size": 1000, "link": "x"}

    as_race = filter_releases(
        [release], common.SessionType.MOTOGP_RACE, 2027, entry,
        "1080p", ["MWR"], calendar_2027,
    )
    assert as_race == [], "an SQ title must not be filed as the Grand Prix"

    as_sprint_quali = filter_releases(
        [release], common.SessionType.MOTOGP_SPRINT_QUALIFYING, 2027, entry,
        "1080p", ["MWR"], calendar_2027,
    )
    assert len(as_sprint_quali) == 1


def test_bare_fp_is_refused_rather_than_guessed(calendar_2027):
    entry = common.get_round_by_number(calendar_2027, 1)
    assert common.classify_session(
        "MotoGP 2027 Thailand FP 1080p MWR", round_info=entry, calendar=calendar_2027
    ) is common.SessionType.UNKNOWN


def test_2027_practice_title_resolves_to_fp2(calendar_2027):
    """
    The counter-example that must keep working: the 2027 Friday session that
    replaced Practice is FP2, so the calendar narrows the title. The same
    title in 2026 is still MOTOGP_PRACTICE.
    """
    entry = common.get_round_by_number(calendar_2027, 1)
    assert common.classify_session(
        "MotoGP 2027 Thailand Practice 1080p", round_info=entry, calendar=calendar_2027
    ) is common.SessionType.MOTOGP_FP2


def test_2026_practice_title_resolves_to_practice(calendar_2026):
    entry = common.get_round_by_number(calendar_2026, 1)
    assert common.classify_session(
        "MotoGP 2026 Thailand Practice 1080p", round_info=entry, calendar=calendar_2026
    ) is common.SessionType.MOTOGP_PRACTICE


def test_unrecognised_title_is_refused_not_filed_as_the_race(calendar_2027):
    entry = common.get_round_by_number(calendar_2027, 1)
    assert common.classify_session(
        "MotoGP 2027 Thailand 1080p MWR", round_info=entry, calendar=calendar_2027
    ) is common.SessionType.UNKNOWN


def test_classify_release_explains_a_refusal(calendar_2027):
    result = common.classify_release(
        "MotoGP 2027 Thailand FP 1080p MWR", calendar_2027, 2027
    )
    assert result.session_type is common.SessionType.UNKNOWN
    assert "ambiguous 'FP'" in (result.reason or "")


def test_report_unknown_cli_prints_the_reason(calendar_2027):
    """The --report-unknown dry run is what an operator uses before a weekend."""
    import subprocess
    import sys
    import os

    env = dict(os.environ)
    env["CONFIG_DIR"] = str(REPO_ROOT / "config")
    proc = subprocess.run(
        [sys.executable, "-m", "motogp_common", "--report-unknown", "--season", "2027"],
        input="MotoGP 2027 Thailand FP 1080p MWR\n",
        text=True, capture_output=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "REFUSED" in proc.stdout
    assert "ambiguous 'FP'" in proc.stdout


# ---------------------------------------------------------------------------
# Prefixes / folder names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session", [
    common.SessionType.MOTOGP_RACE,
    common.SessionType.MOTOGP_FP3,
    common.SessionType.MOTOGP_SPRINT_QUALIFYING,
])
def test_new_session_numbers_are_stable(session):
    """
    Session numbers are baked into folder names as E<NN>, so a prefix must round
    trip. The number itself now comes from SESSION_ELEMENTS (see
    tests/test_session_numbering.py for the scheme and its guarantees).
    """
    assert session is not None
    prefix = common.build_prefix(2027, 20, session)
    assert common.parse_prefix(prefix) == (2027, 20, session)


def test_pre_cutover_folders_are_still_readable():
    """
    Seasons at or before the cutover were written with the dense numbering and
    must still parse, so their contents are understood even while they are being
    cleared out.
    """
    for session_type, legacy_num in common.SESSION_ELEMENTS_LEGACY.items():
        season = common.LEGACY_ELEMENT_SEASON
        prefix = f"{season % 100:02d}R05E{legacy_num:02d}"
        assert common.parse_prefix(prefix) == (season, 5, session_type)


def test_the_cutover_uses_the_spaced_numbering_from_the_next_season():
    """2026 onwards is written with SESSION_ELEMENTS, not the dense table."""
    season = common.LEGACY_ELEMENT_SEASON + 1
    assert common.element_number(common.SessionType.MOTOGP_RACE, season) == 82
    assert common.element_number(common.SessionType.MOTOGP_RACE, season - 1) == 21


def test_ambiguous_element_without_a_season_is_refused():
    """
    A bare E-number can mean two different sessions. Guessing would let a
    leftover older folder be read as a different session, so with no season the
    answer is "unknown", not a coin flip.
    """
    legacy, new = common.LEGACY_ELEMENT_SEASON, common.LEGACY_ELEMENT_SEASON + 1
    assert common.get_session_from_element(24, season=legacy) is common.SessionType.MOTOGP_SPRINT_QUALIFYING
    assert common.get_session_from_element(24, season=new) is common.SessionType.MOTO2_PRACTICE
    assert common.get_session_from_element(24) is None
    assert common.element_is_ambiguous(24) is True


def test_round_folder_names_are_zero_padded(calendar_2026):
    for entry in calendar_2026["rounds"]:
        assert entry["folder"] == f"Round {entry['round']:02d} {entry['venue']}"


# ---------------------------------------------------------------------------
# Cancellations / postponements
# ---------------------------------------------------------------------------

def test_cancelled_round_is_not_active(calendar_2026):
    entry = copy.deepcopy(common.get_round_by_number(calendar_2026, 20))
    entry["status"] = "cancelled"
    assert not common.is_round_active(entry)
    assert not common.round_runs_session(
        entry, common.SessionType.MOTOGP_RACE, calendar_2026
    )


def test_cancelled_round_is_excluded_from_missing_sessions(calendar_2026):
    from motogp_grabber import identify_missing_sessions

    calendar = copy.deepcopy(calendar_2026)
    for entry in calendar["rounds"]:
        if entry["venue"] != "Qatar":
            entry["date"] = "2020-01-01"
            entry["weekend_start"] = "2019-12-30"
    qatar = common.get_round_by_number(calendar, 20)
    qatar["status"] = "cancelled"

    missing = identify_missing_sessions(2026, calendar, {}, only_past=True)
    assert all(item["round"] != 20 for item in missing)


def test_postponed_round_is_not_active():
    assert not common.is_round_active({"status": "postponed"})
    assert common.is_round_active({"status": "scheduled"})
    assert common.is_round_active({}), "missing status defaults to scheduled"


def test_active_rounds_helper(calendar_2026):
    calendar = copy.deepcopy(calendar_2026)
    common.get_round_by_number(calendar, 20)["status"] = "cancelled"
    active = common.get_active_rounds(calendar)
    assert len(active) == 21
    assert 20 not in [r["round"] for r in active]


# ---------------------------------------------------------------------------
# Validator catches hand-editing mistakes
# ---------------------------------------------------------------------------

def test_validator_flags_duplicate_round_numbers(calendar_2026):
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][4]["round"] = 4
    problems = common.validate_calendar(calendar)
    assert any("Duplicate round number" in p for p in problems)


def test_validator_flags_folder_mismatch(calendar_2026):
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][3]["folder"] = "Round 5 Qatar"      # wrong number
    problems = common.validate_calendar(calendar)
    assert any("but the entry is round" in p for p in problems)


def test_validator_flags_folder_venue_mismatch(calendar_2026):
    """A folder that keeps the old round but the new venue is the 2026 trap."""
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][19]["folder"] = "Round 20 Malaysia"  # Qatar is round 20
    problems = common.validate_calendar(calendar)
    assert any("Malaysia" in p for p in problems)


def test_validator_accepts_unpadded_legacy_folders(calendar_2026):
    """2025 uses 'Round 1 Thailand'; padding must not be treated as an error."""
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][0]["folder"] = "Round 1 Thailand"
    assert common.validate_calendar(calendar) == []


def test_validator_flags_non_round_folder(calendar_2026):
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][0]["folder"] = "Thailand Round One"
    problems = common.validate_calendar(calendar)
    assert any("is not 'Round" in p for p in problems)


def test_validator_flags_out_of_order_dates(calendar_2026):
    calendar = copy.deepcopy(calendar_2026)
    calendar["rounds"][20]["date"] = "2026-01-01"
    problems = common.validate_calendar(calendar)
    assert any("earlier than the previous round" in p for p in problems)


def test_validator_flags_bad_utc_conversion(calendar_2026):
    from utils.validate_calendar import check_round

    entry = copy.deepcopy(common.get_round_by_number(calendar_2026, 20))
    entry["sessions"]["sunday"]["motogp_race"]["gmt"] = "17:00"
    problems = check_round(entry, 2026)
    assert any("should be 16:00 UTC" in p for p in problems)


def test_2026_calendar_json_has_status_and_format_fields(calendar_2026):
    for entry in calendar_2026["rounds"]:
        assert entry["status"] == "scheduled"
        assert entry["weekend_format"] == "motogp_standard"
        assert entry["motogp_sessions"]
    assert "motogp_2027" in calendar_2026["weekend_formats"]


def test_backfill_session_filter_uses_element_numbers():
    """
    The backfill --sessions filter must compare element numbers, not enum
    values. Moto3 FP1 is enum 2 / element 10, and Moto2 FP2 is enum 10;
    comparing the enum value to the element number sent a Moto3 FP1 request
    to the Moto2 FP2 search. Moto3 Practice (enum 5 / element 20) was sent to
    the Moto2 Race search (enum 20).
    """
    from motogp_grabber import filter_missing_by_session_numbers

    missing = [
        {"round": 19, "session_type": common.SessionType.MOTO3_FP1},
        {"round": 19, "session_type": common.SessionType.MOTO2_FP2},
        {"round": 19, "session_type": common.SessionType.MOTO3_PRACTICE},
        {"round": 19, "session_type": common.SessionType.MOTO2_RACE},
    ]

    fp1 = filter_missing_by_session_numbers(missing, [10], 2026)
    assert [m["session_type"] for m in fp1] == [common.SessionType.MOTO3_FP1]

    practice = filter_missing_by_session_numbers(missing, [20], 2026)
    assert [m["session_type"] for m in practice] == [common.SessionType.MOTO3_PRACTICE]
