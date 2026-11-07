"""
Tests for grab mode recording (Issue #51).

The home page's "latest scheduled addition" card only counts grabs the
scheduler made while keeping pace with a race weekend. That distinction has to
be written down at the moment of the grab, because a timestamp cannot tell a
Friday-night scheduled grab from a backfill of the same session the next week.
These tests pin both ends: the storage primitive and each command's call into
it, using the real GrabberState on a temporary file rather than a stub, so a
missing `mode` argument fails here instead of silently emptying the card.
"""

import argparse
import json
from datetime import datetime

import pytest

import motogp_common as common
import motogp_grabber as grabber
from motogp_common import SessionType


# ===========================================================================
# Storage primitive
# ===========================================================================

def test_mark_grabbed_persists_the_mode(tmp_path):
    state_file = tmp_path / "grabber_state.json"
    state = grabber.GrabberState(state_file)

    state.mark_grabbed(
        "2026_R20_E22", "26R20E22 MotoGP Practice 1080p",
        nzo_id="nzo1", mode=common.GRAB_MODE_SCHEDULED,
    )
    state.save()

    saved = json.loads(state_file.read_text())
    entry = saved["grabbed"]["2026_R20_E22"]
    assert entry["mode"] == "scheduled"
    assert entry["release"] == "26R20E22 MotoGP Practice 1080p"
    assert entry["nzo_id"] == "nzo1"
    assert "timestamp" in entry


def test_mark_grabbed_without_a_mode_writes_no_mode_field(tmp_path):
    # Callers that do not know the mode must record nothing rather than a
    # default that would later be read as fact.
    state = grabber.GrabberState(tmp_path / "grabber_state.json")
    state.mark_grabbed("2026_R20_E22", "release")
    assert "mode" not in state.state["grabbed"]["2026_R20_E22"]


def test_mode_vocabulary_is_defined_once_in_common():
    assert common.GRAB_MODE_SCHEDULED == "scheduled"
    assert common.GRAB_MODE_BACKFILL == "backfill"
    # The grabber must use the shared constants, not its own strings.
    assert grabber.GRAB_MODE_SCHEDULED is common.GRAB_MODE_SCHEDULED
    assert grabber.GRAB_MODE_BACKFILL is common.GRAB_MODE_BACKFILL


# ===========================================================================
# The scheduled command
# ===========================================================================

def test_cmd_scheduled_records_scheduled_mode(tmp_path, monkeypatch):
    state_file = tmp_path / "grabber_state.json"

    class _State(grabber.GrabberState):
        def __init__(self, *args, **kwargs):
            super().__init__(state_file)

    monkeypatch.setattr(grabber, "GrabberState", _State)
    monkeypatch.setattr(grabber, "reconcile_grabbed_jobs", lambda state: 0)
    monkeypatch.setattr(
        grabber, "load_calendar", lambda season: {"season": season, "rounds": []}
    )
    monkeypatch.setattr(grabber, "has_session_times", lambda calendar: True)
    monkeypatch.setattr(grabber, "get_search_delay_hours", lambda: 1)
    monkeypatch.setattr(grabber, "get_max_grabs_per_run", lambda: 10)
    monkeypatch.setattr(grabber, "get_indexers", lambda: [{"id": 1, "name": "test"}])
    monkeypatch.setattr(grabber, "test_sabnzbd_connection", lambda: True)
    monkeypatch.setattr(grabber, "scan_library_for_season", lambda season: {})
    monkeypatch.setattr(grabber, "emit_activity", lambda **kwargs: None)
    monkeypatch.setattr(grabber, "mark_error", lambda **kwargs: None)
    monkeypatch.setattr(
        grabber, "search_for_session",
        lambda *args, **kwargs: [
            {"title": "MotoGP 2026 Qatar FP2 1080p MWR", "size": 1000, "link": "x"}
        ],
    )
    # grab_release reports (nzo_id, error): the error is what the Download tab
    # shows, so the fake has to supply it too rather than silently returning
    # a bare id that the caller would unpack into two characters.
    monkeypatch.setattr(grabber, "grab_release", lambda *a, **k: ("nzo123", ""))
    monkeypatch.setattr(
        grabber, "get_sessions_ready_for_search",
        lambda calendar, search_delay_hours=None: [{
            "round": 20,
            "round_info": {"round": 20, "venue": "Qatar", "date": "2026-11-08",
                           "sessions": {}},
            "session_type": SessionType.MOTOGP_FP2,
            "ended_at": datetime.now(),
        }],
    )

    args = argparse.Namespace(season=2026, dry_run=False, force=True, search_delay=None)
    assert grabber.cmd_scheduled(args) == 0

    saved = json.loads(state_file.read_text())
    entry = saved["grabbed"][grabber.GrabberState.make_session_key(
        2026, 20, SessionType.MOTOGP_FP2
    )]
    assert entry["mode"] == common.GRAB_MODE_SCHEDULED


# ===========================================================================
# The backfill command
# ===========================================================================

def test_cmd_backfill_records_backfill_mode(tmp_path, monkeypatch):
    state_file = tmp_path / "grabber_state.json"

    class _State(grabber.GrabberState):
        def __init__(self, *args, **kwargs):
            super().__init__(state_file)

    monkeypatch.setattr(grabber, "GrabberState", _State)
    monkeypatch.setattr(grabber, "reconcile_grabbed_jobs", lambda state: 0)
    monkeypatch.setattr(
        grabber, "load_calendar", lambda season: {"season": season, "rounds": []}
    )
    monkeypatch.setattr(grabber, "get_max_grabs_per_run", lambda: 10)
    monkeypatch.setattr(grabber, "get_indexers", lambda: [{"id": 1, "name": "test"}])
    monkeypatch.setattr(grabber, "test_sabnzbd_connection", lambda: True)
    monkeypatch.setattr(grabber, "scan_library_for_season", lambda season: {})
    monkeypatch.setattr(grabber, "emit_activity", lambda **kwargs: None)
    monkeypatch.setattr(grabber, "mark_error", lambda **kwargs: None)
    monkeypatch.setattr(grabber, "print_missing_summary", lambda missing: None)
    monkeypatch.setattr(
        grabber, "identify_missing_sessions",
        lambda season, calendar, existing, round_filter=None: [{
            "round": 20,
            "round_info": {"round": 20, "venue": "Qatar", "date": "2026-11-08",
                           "sessions": {}},
            "session_type": SessionType.MOTOGP_FP1,
        }],
    )
    monkeypatch.setattr(
        grabber, "search_for_session",
        lambda *args, **kwargs: [
            {"title": "MotoGP 2026 Qatar FP1 1080p MWR", "size": 1000, "link": "x"}
        ],
    )
    # grab_release reports (nzo_id, error): the error is what the Download tab
    # shows, so the fake has to supply it too rather than silently returning
    # a bare id that the caller would unpack into two characters.
    monkeypatch.setattr(grabber, "grab_release", lambda *a, **k: ("nzo123", ""))

    args = argparse.Namespace(
        season=2026, dry_run=False, round=None, force=True, sessions=None
    )
    assert grabber.cmd_backfill(args) == 0

    saved = json.loads(state_file.read_text())
    entry = saved["grabbed"]["2026_R20_E12"]
    assert entry["mode"] == common.GRAB_MODE_BACKFILL


def test_backfill_never_records_the_scheduled_mode(tmp_path, monkeypatch):
    """The card must not be fed by a correction run, even one run on a race day."""
    state_file = tmp_path / "grabber_state.json"

    class _State(grabber.GrabberState):
        def __init__(self, *args, **kwargs):
            super().__init__(state_file)

    monkeypatch.setattr(grabber, "GrabberState", _State)
    monkeypatch.setattr(grabber, "reconcile_grabbed_jobs", lambda state: 0)
    monkeypatch.setattr(grabber, "load_calendar", lambda season: {"rounds": []})
    monkeypatch.setattr(grabber, "get_max_grabs_per_run", lambda: 10)
    monkeypatch.setattr(grabber, "get_indexers", lambda: [{"id": 1}])
    monkeypatch.setattr(grabber, "test_sabnzbd_connection", lambda: True)
    monkeypatch.setattr(grabber, "scan_library_for_season", lambda season: {})
    monkeypatch.setattr(grabber, "emit_activity", lambda **kwargs: None)
    monkeypatch.setattr(grabber, "mark_error", lambda **kwargs: None)
    monkeypatch.setattr(grabber, "print_missing_summary", lambda missing: None)
    monkeypatch.setattr(
        grabber, "identify_missing_sessions",
        lambda season, calendar, existing, round_filter=None: [{
            "round": 20,
            "round_info": {"round": 20, "venue": "Qatar", "date": "2026-11-08",
                           "sessions": {}},
            "session_type": SessionType.MOTOGP_RACE,
        }],
    )
    monkeypatch.setattr(
        grabber, "search_for_session",
        lambda *args, **kwargs: [{"title": "MotoGP Race", "size": 1, "link": "x"}],
    )
    monkeypatch.setattr(grabber, "grab_release", lambda *a, **k: ("nzo", ""))

    args = argparse.Namespace(
        season=2026, dry_run=False, round=None, force=True, sessions=None
    )
    grabber.cmd_backfill(args)

    saved = json.loads(state_file.read_text())
    assert all(
        entry.get("mode") != common.GRAB_MODE_SCHEDULED
        for entry in saved["grabbed"].values()
    )


# ===========================================================================
# The operational window (shared with the home page)
# ===========================================================================

def test_weekend_window_constants_are_used_by_the_ready_search():
    # The home page imports these numbers to decide when to show the weekend
    # block; if the grabber stopped using them the two would drift apart.
    import inspect

    source = inspect.getsource(grabber.get_sessions_ready_for_search)
    assert "WINDOW_DAYS_BEFORE_WEEKEND" in source
    assert "WINDOW_DAYS_AFTER_RACE" in source
    assert grabber.WINDOW_DAYS_BEFORE_WEEKEND == 1
    assert grabber.WINDOW_DAYS_AFTER_RACE == 2
