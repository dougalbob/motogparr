"""The WebUI must pick up season calendars without code changes."""

import pytest

pytest.importorskip("flask")

import motogp_webui


def test_seasons_discovered_from_config():
    seasons = motogp_webui.discover_seasons()
    assert 2026 in seasons, "2026 calendar exists in config/"
    assert seasons == sorted(seasons)


def test_2026_rounds_expose_status_and_format():
    rounds = motogp_webui.get_rounds_for_season(2026)
    assert len(rounds) == 22

    qatar = [r for r in rounds if r["number"] == 20][0]
    assert qatar["venue"] == "Qatar"
    assert qatar["status"] == "scheduled"
    assert qatar["active"] is True
    assert "friday" in qatar["format"]


def test_library_status_ignores_sessions_not_run():
    """2026 rounds must not report FP3 / Sprint Qualifying as missing."""
    status = motogp_webui.get_library_status(2026)
    rounds = {r["number"]: r for r in status["rounds"]}
    names = {s["name"] for s in rounds[20]["sessions"]}
    assert "MotoGP Free Practice 3" not in names
    assert "MotoGP Sprint Qualifying" not in names
    assert "MotoGP Race" in names
