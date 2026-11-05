"""
Tests for the /config calendar reconciliation done at container start.

The calendars in /config live on a persistent volume, so a new image does not
update them by itself. These tests pin down when it is safe to replace the
config copy (same season, e.g. a postponed race after an image update) and when
it is not (different venues / different championship).
"""

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from utils.calendar_sync import compare


@pytest.fixture
def calendar_2026():
    with open(REPO_ROOT / "config" / "motogp_2026.json") as f:
        return json.load(f)


def write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2))
    return path


def test_identical_calendars(tmp_path, calendar_2026):
    image = write(tmp_path / "image.json", calendar_2026)
    config = write(tmp_path / "config.json", calendar_2026)

    result = compare(image, config)
    assert result["identical"] is True
    assert result["same_season"] is True


def test_stale_qatar_calendar_is_the_same_season(tmp_path, calendar_2026):
    """
    The real case: config holds the April Qatar calendar, the image ships the
    November one. Same venues, different numbering - safe to refresh.
    """
    image = write(tmp_path / "image.json", calendar_2026)

    stale = copy.deepcopy(calendar_2026)
    qatar = next(r for r in stale["rounds"] if r["venue"] == "Qatar")
    qatar["round"] = 4
    qatar["date"] = "2026-04-12"
    for entry in stale["rounds"]:
        if entry["venue"] not in ("Thailand", "Brazil", "Americas", "Qatar"):
            entry["round"] += 1
    stale["rounds"].sort(key=lambda r: r["round"])
    config = write(tmp_path / "config.json", stale)

    result = compare(image, config)

    assert result["identical"] is False
    assert result["same_season"] is True, "same venues means the same season"
    assert "Qatar" in result["change_summary"]
    assert "renumbered" in result["change_summary"]
    # The summary must stay short rather than listing all 22 rounds.
    assert len(result["change_summary"]) < 200


def test_renumbering_is_summarised_not_enumerated(tmp_path, calendar_2026):
    image = write(tmp_path / "image.json", calendar_2026)

    renumbered = copy.deepcopy(calendar_2026)
    for entry in renumbered["rounds"]:
        entry["round"] += 10
    config = write(tmp_path / "config.json", renumbered)

    result = compare(image, config)
    assert result["same_season"] is True
    assert "22 rounds renumbered" in result["change_summary"]


def test_cancelled_round_is_reported(tmp_path, calendar_2026):
    image = copy.deepcopy(calendar_2026)
    qatar = next(r for r in image["rounds"] if r["venue"] == "Qatar")
    qatar["status"] = "cancelled"
    image_path = write(tmp_path / "image.json", image)
    config = write(tmp_path / "config.json", calendar_2026)

    result = compare(image_path, config)
    assert result["same_season"] is True
    assert "status changed: Qatar" in result["change_summary"]


def test_different_championship_is_not_the_same_season(tmp_path, calendar_2026):
    image = write(tmp_path / "image.json", calendar_2026)

    other = copy.deepcopy(calendar_2026)
    other["rounds"] = [
        {"round": 1, "venue": "Somewhere Else", "date": "2026-03-01"},
    ]
    config = write(tmp_path / "config.json", other)

    result = compare(image, config)
    assert result["same_season"] is False


def test_2025_and_2026_are_not_the_same_season(tmp_path, calendar_2026):
    with open(REPO_ROOT / "config" / "motogp_2025.json") as f:
        calendar_2025 = json.load(f)

    image = write(tmp_path / "image.json", calendar_2026)
    config = write(tmp_path / "config.json", calendar_2025)

    assert compare(image, config)["same_season"] is False


def test_added_and_removed_rounds(tmp_path, calendar_2026):
    image = copy.deepcopy(calendar_2026)
    image["rounds"] = [r for r in image["rounds"] if r["venue"] != "Qatar"]

    image_path = write(tmp_path / "image.json", image)
    config = write(tmp_path / "config.json", calendar_2026)

    result = compare(image_path, config)
    assert result["same_season"] is False
    assert any("Qatar" in d for d in result["differences"])
