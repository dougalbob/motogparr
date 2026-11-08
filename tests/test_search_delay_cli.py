"""Regression tests for the scheduler-to-grabber search-delay interface."""
import argparse
import subprocess
import sys

import pytest

import motogp_grabber as grabber
import motogp_scheduler as scheduler


@pytest.mark.parametrize("delay", [1.0, 0.5, 0.0, 2])
def test_scheduler_delay_is_accepted_by_grabber_cli(monkeypatch, delay):
    received = []
    monkeypatch.setattr(scheduler, "get_search_delay_hours", lambda: delay)
    monkeypatch.setattr(grabber, "init_logging", lambda verbose: None)
    monkeypatch.setattr(grabber, "cmd_scheduled", lambda args: received.append(args.search_delay) or 0)

    def run_grabber(cmd, **kwargs):
        monkeypatch.setattr(sys, "argv", cmd[1:])
        return subprocess.CompletedProcess(cmd, grabber.main(), stdout="", stderr="")

    monkeypatch.setattr(scheduler.subprocess, "run", run_grabber)
    assert scheduler.run_grabber_scheduled()["success"] is True
    assert received == [delay]


def test_zero_delay_override_is_displayed_and_passed_through(monkeypatch, capsys):
    monkeypatch.setattr(grabber, "get_search_delay_hours", lambda: 4)
    class FakeState:
        def update_scheduled_check(self):
            pass

        def save(self):
            pass

    monkeypatch.setattr(grabber, "GrabberState", FakeState)
    monkeypatch.setattr(grabber, "scan_library_for_season", lambda season: {})
    monkeypatch.setattr(grabber, "load_calendar", lambda season: {"rounds": []})
    monkeypatch.setattr(grabber, "has_session_times", lambda calendar: True)
    received = []
    monkeypatch.setattr(grabber, "get_sessions_ready_for_search",
                        lambda calendar, search_delay_hours: received.append(search_delay_hours) or [])
    grabber.cmd_scheduled(argparse.Namespace(season=2026, dry_run=True, force=False, search_delay=0.0))
    assert "Search Delay: 0.0 hours" in capsys.readouterr().out
    assert received == [0.0]
