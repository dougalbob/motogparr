"""
Tests for the scheduled grabber's activity lifecycle.

The backfill path emitted SEARCHING / CANDIDATES_FOUND / DOWNLOADING; the
scheduled path did not. On a live weekend the scheduler downloaded the session
correctly, but the activity tab's first event was the watcher's
DOWNLOAD_COMPLETE, so it looked like the session came from nowhere. The
downloads were never broken - only the reporting.

Run with:  python3 -m pytest tests -q
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_grabber as grabber


class FakeState:
    """Minimal GrabberState so the test does not touch /config."""

    def __init__(self, *args, **kwargs):
        self.state = {"searched": {}, "grabbed": {}, "failed": {}}

    def make_session_key(self, season, round_num, session_type):
        return (
            f"{season}_R{round_num:02d}_"
            f"E{grabber.element_number(session_type, season):02d}"
        )

    def was_grabbed(self, key):
        return key in self.state["grabbed"]

    def mark_searched(self, key):
        self.state["searched"][key] = datetime.now().isoformat()

    def mark_failed(self, key, reason):
        self.state["failed"][key] = {"reason": reason}

    def mark_grabbed(self, key, title, nzo_id=None):
        self.state["grabbed"][key] = {"title": title, "nzo_id": nzo_id}

    def update_scheduled_check(self):
        pass

    def save(self):
        pass


def test_scheduled_path_emits_the_grabber_activity_lifecycle(monkeypatch):
    events = []
    monkeypatch.setattr(
        grabber, "emit_activity", lambda **kwargs: events.append(kwargs)
    )
    monkeypatch.setattr(
        grabber,
        "mark_error",
        lambda **kwargs: events.append({"stage": "error", **kwargs}),
    )
    monkeypatch.setattr(grabber, "GrabberState", FakeState)
    monkeypatch.setattr(grabber, "reconcile_grabbed_jobs", lambda state: 0)
    monkeypatch.setattr(
        grabber, "load_calendar", lambda season: {"season": season, "rounds": []}
    )
    monkeypatch.setattr(grabber, "has_session_times", lambda calendar: True)
    monkeypatch.setattr(grabber, "get_search_delay_hours", lambda: 1)
    monkeypatch.setattr(grabber, "get_max_grabs_per_run", lambda: 10)
    monkeypatch.setattr(
        grabber, "get_indexers", lambda: [{"id": 1, "name": "test"}]
    )
    monkeypatch.setattr(grabber, "test_sabnzbd_connection", lambda: True)
    monkeypatch.setattr(grabber, "scan_library_for_season", lambda season: {})
    monkeypatch.setattr(
        grabber,
        "search_for_session",
        lambda *args, **kwargs: [
            {"title": "Moto3 2026 Malaysia FP1 1080p MWR",
             "size": 1000, "link": "x"}
        ],
    )
    monkeypatch.setattr(grabber, "grab_release", lambda *args, **kwargs: "nzo123")
    monkeypatch.setattr(
        grabber,
        "get_sessions_ready_for_search",
        lambda calendar, search_delay_hours=None: [
            {
                "round": 19,
                "round_info": {
                    "round": 19,
                    "venue": "Malaysia",
                    "aliases": ["malaysia"],
                    "date": "2026-11-01",
                    "weekend_start": "2026-10-30",
                    "sessions": {},
                },
                "session_type": grabber.SessionType.MOTO3_FP1,
                "ended_at": datetime.now(),
            }
        ],
    )

    args = argparse.Namespace(
        season=2026, dry_run=False, force=True, search_delay=None
    )
    result = grabber.cmd_scheduled(args)
    assert result == 0

    stages = [event.get("stage") for event in events]
    assert grabber.ActivityStage.SEARCHING in stages, stages
    assert grabber.ActivityStage.CANDIDATES_FOUND in stages, stages
    assert grabber.ActivityStage.DOWNLOADING in stages, stages

    searching = [
        event for event in events
        if event.get("stage") == grabber.ActivityStage.SEARCHING
    ][0]
    assert searching["episode_name"] == "Moto3 Free Practice 1"
    assert searching["round_info"]["round"] == 19
    assert searching["round_info"]["venue"] == "Malaysia"
