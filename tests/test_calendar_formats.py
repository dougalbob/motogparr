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


def test_upcoming_sessions_use_real_start_times(calendar_2026, qatar):
    from motogp_grabber import get_upcoming_sessions

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
# Prefixes / folder names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("element", [21, 23, 24])
def test_new_session_numbers_are_stable(element):
    """
    Session numbers are baked into folder names as E<NN>. Appending new members
    is safe; changing an existing one would orphan files already on disk.
    """
    session = common.get_session_from_element(element)
    assert session is not None
    prefix = common.build_prefix(2027, 20, session)
    assert common.parse_prefix(prefix) == (2027, 20, session)


def test_historical_session_numbers_have_not_moved():
    assert int(common.SessionType.MOTOGP_RACE) == 21
    assert int(common.SessionType.MOTOGP_WARMUP) == 18
    assert int(common.SessionType.MOTOGP_SPRINT) == 15
    assert int(common.SessionType.MOTO3_FP1) == 2


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
    calendar["rounds"][3]["folder"] = "Round 4 Qatar"
    problems = common.validate_calendar(calendar)
    assert any("does not match round" in p for p in problems)


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
