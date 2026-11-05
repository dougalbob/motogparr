"""
Tests for the Plex library check (utils/plexcheck.py).

The bug this catches is invisible by nature: files download, sort and land on
disk correctly, and Plex simply never sees them because its library does not
scan that folder. Nothing in the app is wrong, so nothing in the app's logs looks
wrong either - which is why the check has to exist and why the wording of each
verdict matters.

Plex is stubbed rather than connected: no network, and the interesting cases
(mapping inverted, folder absent, section missing) are not things a real server
would produce on demand.
"""

from utils.plexcheck import (
    MAPPING_MISSING,
    NOT_SCANNED,
    OK,
    UNKNOWN,
    UNREACHABLE,
    check_library,
    is_inside,
)

APP_PATH = "/media/Motorsport/MotoGP"
PLEX_PATH = "/data/media/Motorsport/MotoGP"
GOOD_MAPPING = {"container": "/media", "plex": "/data/media"}


class FakeSection:
    def __init__(self, title, key, locations):
        self.title = title
        self.key = key
        self.locations = locations


class FakeLibrary:
    def __init__(self, sections):
        self._sections = sections

    def section(self, name):
        for section in self._sections:
            if section.title == name:
                return section
        raise LookupError(f"no section {name}")

    def sections(self):
        return self._sections


class FakePlex:
    def __init__(self, sections):
        self.library = FakeLibrary(sections)


def plex_with(*locations, title="Motorsport - MotoGP", key="16"):
    return FakePlex([FakeSection(title, key, list(locations))])


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------

def test_is_inside_requires_a_real_path_boundary():
    """`startswith` alone would call /media/MotoGP2 part of /media/MotoGP."""
    assert is_inside("/media/MotoGP/2026/round", "/media/MotoGP") is True
    assert is_inside("/media/MotoGP2/2026", "/media/MotoGP") is False
    assert is_inside("/media/MotoGP", "/media/MotoGP") is True


def test_is_inside_ignores_trailing_slashes_and_dot_segments():
    assert is_inside("/media/MotoGP/x/", "/media/MotoGP/") is True
    assert is_inside("/media/MotoGP/./x", "/media/MotoGP") is True


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_correct_mapping_and_folder_in_library_is_ok():
    result = check_library(
        plex_with(PLEX_PATH), APP_PATH, GOOD_MAPPING, "Motorsport - MotoGP"
    )
    assert result.status == OK
    assert result.mapping_used is True
    assert PLEX_PATH in result.summary


def test_a_parent_folder_in_the_library_still_counts():
    """Plex scanning .../Motorsport covers .../Motorsport/MotoGP."""
    result = check_library(
        plex_with("/data/media/Motorsport"), APP_PATH, GOOD_MAPPING,
        "Motorsport - MotoGP",
    )
    assert result.status == OK


def test_section_can_be_found_by_id_when_the_name_is_wrong():
    result = check_library(
        plex_with(PLEX_PATH, title="Something Else"), APP_PATH, GOOD_MAPPING,
        "Motorsport - MotoGP", library_id="16",
    )
    assert result.status == OK


# ---------------------------------------------------------------------------
# The failure this exists to catch
# ---------------------------------------------------------------------------

def test_folder_outside_the_library_is_reported_loudly():
    result = check_library(
        plex_with("/data/media/Unrelated"), APP_PATH, GOOD_MAPPING,
        "Motorsport - MotoGP",
    )
    assert result.status == NOT_SCANNED
    assert "NOT scanning" in result.summary
    # The explanation must contain both paths, or it is useless.
    joined = "\n".join(result.detail)
    assert APP_PATH in joined
    assert PLEX_PATH in joined


def test_a_stale_mapping_is_diagnosed_as_inverted():
    """
    Plex scans /media/... but the mapping says Plex sees /data/media/.... The
    detail should point at the mapping rather than at Plex's folder list.
    """
    result = check_library(
        plex_with(APP_PATH), APP_PATH, GOOD_MAPPING, "Motorsport - MotoGP"
    )
    assert result.status == NOT_SCANNED
    assert any("inverted" in line for line in result.detail)


def test_no_mapping_is_reported_as_an_unfair_comparison():
    result = check_library(
        plex_with(PLEX_PATH), APP_PATH, {}, "Motorsport - MotoGP"
    )
    assert result.status == MAPPING_MISSING
    assert "path mapping" in result.summary.lower()


def test_half_a_mapping_is_treated_as_no_mapping():
    result = check_library(
        plex_with(PLEX_PATH), APP_PATH, {"container": "/media"}, "Motorsport - MotoGP"
    )
    assert result.status == MAPPING_MISSING


# ---------------------------------------------------------------------------
# Honest "unknown" rather than a guess
# ---------------------------------------------------------------------------

def test_missing_library_name_is_reported():
    result = check_library(
        plex_with(PLEX_PATH, title="Other"), APP_PATH, GOOD_MAPPING, "MotoGP"
    )
    assert result.status != OK
    assert "not found" in result.summary


def test_library_with_no_locations_reports_unknown():
    """A legacy 'Other' section reports no folders; guessing would be a lie."""
    result = check_library(plex_with(), APP_PATH, GOOD_MAPPING, "Motorsport - MotoGP")
    assert result.status == UNKNOWN


def test_unreachable_plex_is_reported_not_guessed():
    class Broken:
        @property
        def library(self):
            raise ConnectionError("connection refused")

    result = check_library(Broken(), APP_PATH, GOOD_MAPPING, "Motorsport - MotoGP")
    assert result.status == UNREACHABLE
    assert "connection refused" in result.summary


def test_no_configured_path_is_not_an_error():
    assert check_library(plex_with(PLEX_PATH), None, GOOD_MAPPING).status == UNKNOWN


def test_result_is_json_serialisable_for_the_webui():
    result = check_library(plex_with(PLEX_PATH), APP_PATH, GOOD_MAPPING,
                           "Motorsport - MotoGP")
    payload = result.as_dict()
    assert payload["status"] == OK
    assert payload["ok"] is True
    assert isinstance(payload["detail"], list)
