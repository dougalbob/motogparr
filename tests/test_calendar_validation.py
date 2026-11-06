"""
Tests for calendar format validation (issue #33, P3).

The old validator did not look at weekend_format at all. Three realistic 2027
misconfigurations returned zero problems, so a broken season passed the gate
and then silently never searched for the affected sessions. These tests pin
the cross-checks: format <-> motogp_sessions <-> timetable, in both
directions and per day.

Run with:  python3 -m pytest tests -q
"""

import copy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_common as common
from utils.validate_calendar import check_round


@pytest.fixture(scope="module")
def calendar_2027():
    calendar = common.load_calendar(2027)
    assert calendar, "config/motogp_2027.json should load"
    return calendar


@pytest.fixture(scope="module")
def calendar_2026():
    calendar = common.load_calendar(2026)
    assert calendar, "config/motogp_2026.json should load"
    return calendar


@pytest.fixture
def round_2027(calendar_2027):
    return copy.deepcopy(calendar_2027["rounds"][0])


# ---------------------------------------------------------------------------
# The real 2027 calendar
# ---------------------------------------------------------------------------

def test_the_2027_calendar_exists_and_has_22_rounds(calendar_2027):
    assert len(calendar_2027["rounds"]) == 22
    assert [r["round"] for r in calendar_2027["rounds"]] == list(range(1, 23))
    assert [r["date"] for r in calendar_2027["rounds"]] == sorted(
        r["date"] for r in calendar_2027["rounds"]
    )


def test_the_2027_calendar_uses_the_2027_format(calendar_2027):
    for entry in calendar_2027["rounds"]:
        assert entry["weekend_format"] == "motogp_2027"
        assert entry["motogp_sessions"] == [
            "fp1", "fp2", "sprint_qualifying", "fp3",
            "qualifying", "sprint", "race",
        ]


def test_the_2027_calendar_has_no_warmup(calendar_2027):
    for entry in calendar_2027["rounds"]:
        assert "motogp_warmup" not in entry["sessions"].get("sunday", {})
        assert "warmup" not in entry["motogp_sessions"]


def test_the_2027_calendar_passes_the_validator(calendar_2027):
    for entry in calendar_2027["rounds"]:
        assert check_round(entry, 2027, True, calendar_2027) == [], entry["venue"]


def test_the_2027_calendar_passes_the_common_validation(calendar_2027):
    assert common.validate_calendar(calendar_2027) == []


def test_the_2026_calendar_still_passes(calendar_2026):
    for entry in calendar_2026["rounds"]:
        assert check_round(entry, 2026, True, calendar_2026) == [], entry["venue"]


def test_the_published_2027_rounds_are_the_expected_venues(calendar_2027):
    by_number = {r["round"]: r for r in calendar_2027["rounds"]}
    assert by_number[4]["venue"] == "Argentina"
    assert by_number[4]["date"] == "2027-04-11"
    assert by_number[17]["venue"] == "Portugal"
    assert by_number[17]["date"] == "2027-10-03"
    assert by_number[21]["venue"] == "Australia"
    assert by_number[21]["date"] == "2027-11-14"
    assert by_number[22]["venue"] == "Valencia"
    assert by_number[22]["date"] == "2027-11-28"


# ---------------------------------------------------------------------------
# The three misconfigurations that used to pass
# ---------------------------------------------------------------------------

def test_a_typo_in_weekend_format_is_rejected(round_2027, calendar_2027):
    """motogp_2028 resolves to {} and the warm-up would be hunted all season."""
    entry = copy.deepcopy(round_2027)
    entry["weekend_format"] = "motogp_2028"
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("motogp_2028" in p and "weekend_formats" in p for p in problems)


def test_stale_motogp_sessions_are_rejected(round_2027, calendar_2027):
    """A 2027 format with the 2026 session list hunts sessions that never run."""
    entry = copy.deepcopy(round_2027)
    entry["motogp_sessions"] = [
        "fp1", "practice", "fp2", "sprint_qualifying", "fp3",
        "qualifying", "sprint", "warmup", "race",
    ]
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("'practice'" in p for p in problems)
    assert any("'warmup'" in p for p in problems)


def test_fp3_declared_without_times_is_rejected(round_2027, calendar_2027):
    """The round passes the gate and the session is silently never searched."""
    entry = copy.deepcopy(round_2027)
    entry["sessions"]["saturday"].pop("motogp_fp3")
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("motogp_fp3" in p for p in problems)


def test_a_motogp_key_the_format_does_not_run_is_rejected(round_2027, calendar_2027):
    """A stale warm-up block left in the 2027 sessions must be named."""
    entry = copy.deepcopy(round_2027)
    entry["sessions"]["sunday"]["motogp_warmup"] = {
        "local": "09:40", "gmt": "02:40", "duration_mins": 10,
    }
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("motogp_warmup" in p for p in problems)


def test_a_session_missing_from_motogp_sessions_is_rejected(round_2027, calendar_2027):
    entry = copy.deepcopy(round_2027)
    entry["motogp_sessions"] = [
        "fp1", "fp2", "fp3", "qualifying", "sprint", "race",
    ]
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("sprint_qualifying" in p for p in problems)


# ---------------------------------------------------------------------------
# Thursday and the shared day list
# ---------------------------------------------------------------------------

def test_thursday_is_an_accepted_day(round_2027, calendar_2027):
    """
    Flyaway weekends can run on Thursday. The validator used to reject the day
    outright while the WebUI supported it; both now read CALENDAR_DAYS.
    """
    entry = copy.deepcopy(round_2027)
    entry["sessions"]["thursday"] = {
        "motogp_fp1": {"local": "09:00", "gmt": "02:00", "duration_mins": 45},
    }
    fmt = copy.deepcopy(calendar_2027["weekend_formats"]["motogp_2027"])
    fmt["thursday"] = [{"session": "fp1", "label": "FP1", "duration_mins": 45}]
    calendar = copy.deepcopy(calendar_2027)
    calendar["weekend_formats"]["motogp_2027"] = fmt

    problems = check_round(entry, 2027, True, calendar)
    assert not any("unexpected day" in p for p in problems)


def test_a_thursday_session_absent_from_the_format_is_rejected(round_2027, calendar_2027):
    entry = copy.deepcopy(round_2027)
    entry["sessions"]["thursday"] = {
        "motogp_fp1": {"local": "09:00", "gmt": "02:00", "duration_mins": 45},
    }
    problems = check_round(entry, 2027, True, calendar_2027)
    assert any("thursday" in p and "motogp_fp1" in p for p in problems)


# ---------------------------------------------------------------------------
# Release aliases
# ---------------------------------------------------------------------------

def test_an_unknown_release_alias_session_is_rejected(calendar_2027):
    calendar = copy.deepcopy(calendar_2027)
    calendar["release_aliases"] = {"sq": "not_a_session"}
    entry = calendar["rounds"][0]
    problems = check_round(entry, 2027, True, calendar)
    assert any("release_aliases" in p and "not_a_session" in p for p in problems)


def test_the_2027_release_aliases_are_valid(calendar_2027):
    entry = calendar_2027["rounds"][0]
    assert check_round(entry, 2027, True, calendar_2027) == []
    assert calendar_2027["release_aliases"]["sq"] == "sprint_qualifying"
    assert calendar_2027["release_aliases"]["spq"] == "sprint_qualifying"


def test_the_2027_calendar_keeps_support_class_sessions(calendar_2027):
    """
    The 2027 format only changes MotoGP. The support classes keep their usual
    Friday Practice and Saturday FP2 sessions; omitting them made the grabber
    skip Moto3/Moto2 Practice and Moto2 FP2, and the WebUI omit them.
    """
    for entry in calendar_2027["rounds"]:
        sessions = entry["sessions"]
        assert "moto3_practice" in sessions["friday"], entry["venue"]
        assert "moto2_practice" in sessions["friday"], entry["venue"]
        assert "moto2_fp2" in sessions["saturday"], entry["venue"]
        assert sessions["friday"]["moto3_practice"]["duration_mins"] == 30
        assert sessions["friday"]["moto2_practice"]["duration_mins"] == 30
        assert sessions["saturday"]["moto2_fp2"]["duration_mins"] == 30


def test_2027_support_classes_are_not_gated_by_the_motogp_format(calendar_2027):
    """round_runs_session() must keep the support classes on the 2027 format."""
    entry = calendar_2027["rounds"][0]
    for session in (
        common.SessionType.MOTO3_PRACTICE,
        common.SessionType.MOTO2_PRACTICE,
        common.SessionType.MOTO2_FP2,
        common.SessionType.MOTO3_FP2,
        common.SessionType.MOTO3_RACE,
        common.SessionType.MOTO2_RACE,
    ):
        assert common.round_runs_session(entry, session, calendar_2027), session.name
