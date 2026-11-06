"""
Plex titles and sort titles.

Two requirements pull in opposite directions and must be satisfied together:

    the visible name has to say what the session IS   ->  "MotoGP FP2"
    the item still has to sort in running order       ->  27R02E32

Setting only the title is actively worse than setting neither: every "MotoGP
FP2" in a season would sort together and a round would no longer read in order.
So `apply_labels` writes the pair, and these tests hold that contract - including
the case where Plex hands back an object type that cannot be edited at all, which
is what an "Other Videos" section may do.

Run with:  python3 -m pytest tests -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_common as common
from motogp_common import SessionType
from utils import plexlabels


class FakeVideo:
    """Stands in for a plexapi Movie: records edits instead of calling Plex."""

    type = "movie"

    def __init__(self, title="", sort_title=""):
        self.title = title
        self.titleSort = sort_title
        self.edits = {}

    def editField(self, field, value, locked=True):
        self.edits[field] = value
        setattr(self, field, value)


class UneditableVideo:
    """Stands in for a plexapi Video, which has no edit mixins."""

    type = "video"

    def __init__(self, title="27R02E32 MotoGP FP2"):
        self.title = title
        self.titleSort = title


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session_type,expected", [
    (SessionType.MOTOGP_FP1, "MotoGP FP1"),
    (SessionType.MOTOGP_FP2, "MotoGP FP2"),
    (SessionType.MOTOGP_FP3, "MotoGP FP3"),
    (SessionType.MOTOGP_SPRINT_QUALIFYING, "MotoGP Sprint Qualifying"),
    (SessionType.MOTOGP_Q1, "MotoGP Q1"),
    (SessionType.MOTO3_FP2, "Moto3 FP2"),
    (SessionType.MOTO2_RACE, "Moto2 Race"),
])
def test_title_is_the_short_canonical_label(session_type, expected):
    assert plexlabels.labels_for(session_type, 2027, 2)["title"] == expected


def test_2027_friday_session_is_never_called_practice():
    """
    The whole reason for the change: from 2027 Friday runs FP2 plus Sprint
    Qualifying, and there is no Practice session. A user shown "Practice" is
    looking at something that does not exist.
    """
    title = plexlabels.labels_for(SessionType.MOTOGP_FP2, 2027, 2)["title"]
    assert "Practice" not in title
    assert title == "MotoGP FP2"


def test_2026_keeps_the_practice_name_because_2026_really_runs_it():
    """The relabel is a format fact, not a cosmetic preference."""
    assert plexlabels.labels_for(
        SessionType.MOTOGP_PRACTICE, 2026, 20
    )["title"] == "MotoGP Practice"


def test_sort_title_is_the_folder_prefix():
    """
    The sort key is the same token as the folder name: derivable, visible on
    disk, and it cannot drift from the numbering scheme.
    """
    labels = plexlabels.labels_for(SessionType.MOTOGP_RACE, 2027, 2)
    assert labels["titleSort"] == "27R02E82"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weekend,season", [
    ([SessionType.MOTOGP_FP1, SessionType.MOTOGP_PRACTICE, SessionType.MOTOGP_FP2,
      SessionType.MOTOGP_Q1, SessionType.MOTOGP_Q2, SessionType.MOTOGP_SPRINT,
      SessionType.MOTOGP_WARMUP, SessionType.MOTOGP_RACE], 2026),
    ([SessionType.MOTOGP_FP1, SessionType.MOTOGP_FP2,
      SessionType.MOTOGP_SPRINT_QUALIFYING, SessionType.MOTOGP_FP3,
      SessionType.MOTOGP_Q1, SessionType.MOTOGP_Q2, SessionType.MOTOGP_SPRINT,
      SessionType.MOTOGP_RACE], 2027),
])
def test_a_round_sorts_in_running_order(weekend, season):
    """Sorting the items by titleSort must reproduce the weekend order."""
    sort_keys = [plexlabels.labels_for(s, season, 2)["titleSort"] for s in weekend]
    assert sort_keys == sorted(sort_keys)


def test_sort_titles_do_not_collide_across_classes():
    """Moto3/Moto2/MotoGP interleave on track but must stay distinguishable."""
    keys = {
        plexlabels.labels_for(s, 2027, 2)["titleSort"]
        for s in common.SESSION_ELEMENTS
    }
    assert len(keys) == len(common.SESSION_ELEMENTS)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_apply_sets_both_fields():
    """
    Title without titleSort would clump every same-named session together, so
    the pair is the unit of work and this is the test that stops a future edit
    from writing one and not the other.
    """
    video = FakeVideo("27R02E32 MotoGP Free Practice 2", "27R02E32 MotoGP Free Practice 2")
    changes = plexlabels.apply_labels(video, SessionType.MOTOGP_FP2, 2027, 2)

    assert video.title == "MotoGP FP2"
    assert video.titleSort == "27R02E32"
    assert len(changes) == 2


def test_apply_is_idempotent():
    video = FakeVideo("MotoGP FP2", "27R02E32")
    assert plexlabels.apply_labels(video, SessionType.MOTOGP_FP2, 2027, 2) == []
    assert video.edits == {}


def test_dry_run_writes_nothing():
    video = FakeVideo("27R02E32 MotoGP Free Practice 2", "27R02E32 MotoGP Free Practice 2")
    changes = plexlabels.apply_labels(
        video, SessionType.MOTOGP_FP2, 2027, 2, dry_run=True
    )
    assert changes and all(c.startswith("[DRY-RUN]") for c in changes)
    assert video.edits == {}


def test_an_uneditable_item_is_reported_not_half_applied():
    """
    An "Other Videos" section may hand back a base Video with no edit methods.
    Silently doing nothing is not acceptable, and neither is raising in the middle
    of a tagging run, so it warns and returns no changes.
    """
    assert plexlabels.supports_field_edits(UneditableVideo()) is False
    assert plexlabels.apply_labels(UneditableVideo(), SessionType.MOTOGP_FP2, 2027, 2) == []


def test_a_plex_failure_does_not_break_the_tagging_run():
    """Tagging must never abort the sort/tag pipeline."""
    class Broken(FakeVideo):
        def editField(self, *args, **kwargs):
            raise RuntimeError("Plex said no")

    assert plexlabels.apply_labels(Broken(), SessionType.MOTOGP_FP2, 2027, 2) == []


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def test_collection_sort_title_is_zero_padded():
    """"2027-02" must sort before "2027-10"."""
    keys = [plexlabels.collection_sort_title(2027, r) for r in (2, 10, 22)]
    assert keys == ["2027-02", "2027-10", "2027-22"]
    assert keys == sorted(keys)


def test_collection_sort_is_skipped_when_plex_is_unavailable():
    """A missing collection must not fail a tagging run."""
    class NoPlex:
        library = None

    assert plexlabels.ensure_collection_sort(
        NoPlex(), "MotoGP", "2027 Round 02 Qatar", 2027, 2
    ) is None


# ---------------------------------------------------------------------------
# The on-disk name must match, or a rescan undoes the title
# ---------------------------------------------------------------------------

def test_folder_name_and_plex_title_agree():
    """
    Plex re-derives a title from the filename whenever the item is re-scanned.
    If the folder said "MotoGP Free Practice 2" while Plex said "MotoGP FP2",
    the next scan would quietly revert it - so the two are kept identical.
    """
    for season in (2026, 2027):
        for session_type in common.SESSION_ELEMENTS:
            prefix = common.build_prefix(season, 2, session_type)
            folder_name = f"{prefix} {session_type.full_short_name}"
            labels = plexlabels.labels_for(session_type, season, 2)

            assert folder_name == f"{labels['titleSort']} {labels['title']}"
