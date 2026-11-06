"""
Session element numbering (`E<NN>` in yyRNNEee).

The element number is not a sort key bolted on top of the folder name; it *is*
part of the identity Plex and the library scan use. These tests exist because
the old numbering was dense (1-24, no free slots), which forced FP3 and Sprint
Qualifying to be appended as 23 and 24 - after the Race at 21. A 2027 round
therefore listed its sessions out of order in any folder-name or title listing,
and no amount of display code could fix it without renaming files.

SESSION_ELEMENTS replaces it with blocks of ten. Two properties have to hold
forever, and both are asserted here rather than described in a comment:

    1. chronological for every weekend format the app understands;
    2. collision-free, with free slots left for sessions that do not exist yet.

Run with:  python3 -m pytest tests -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import motogp_common as common
from motogp_common import SessionType


# ---------------------------------------------------------------------------
# The weekend formats, as real ordering requirements
# ---------------------------------------------------------------------------

# The MotoGP sessions of a weekend, in the order they actually happen. These
# lists are the specification: the numbers must sort in this order.
MOTOGP_WEEKEND_2026 = [
    SessionType.MOTOGP_FP1,
    SessionType.MOTOGP_PRACTICE,
    SessionType.MOTOGP_FP2,
    SessionType.MOTOGP_Q1,
    SessionType.MOTOGP_Q2,
    SessionType.MOTOGP_SPRINT,
    SessionType.MOTOGP_WARMUP,
    SessionType.MOTOGP_RACE,
]

MOTOGP_WEEKEND_2027 = [
    SessionType.MOTOGP_FP1,
    SessionType.MOTOGP_FP2,
    SessionType.MOTOGP_SPRINT_QUALIFYING,
    SessionType.MOTOGP_FP3,
    SessionType.MOTOGP_Q1,
    SessionType.MOTOGP_Q2,
    SessionType.MOTOGP_SPRINT,
    SessionType.MOTOGP_RACE,
]

SUPPORT_WEEKEND = [
    SessionType.MOTO3_FP1,
    SessionType.MOTO3_PRACTICE,
    SessionType.MOTO3_FP2,
    SessionType.MOTO3_Q1,
    SessionType.MOTO3_Q2,
    SessionType.MOTO3_RACE,
] if False else None  # placeholder replaced below

MOTO3_WEEKEND = [
    SessionType.MOTO3_FP1,
    SessionType.MOTO3_PRACTICE,
    SessionType.MOTO3_FP2,
    SessionType.MOTO3_Q1,
    SessionType.MOTO3_Q2,
    SessionType.MOTO3_RACE,
]

MOTO2_WEEKEND = [
    SessionType.MOTO2_FP1,
    SessionType.MOTO2_PRACTICE,
    SessionType.MOTO2_FP2,
    SessionType.MOTO2_Q1,
    SessionType.MOTO2_Q2,
    SessionType.MOTO2_RACE,
]

ALL_WEEKENDS = {
    "MotoGP 2026": MOTOGP_WEEKEND_2026,
    "MotoGP 2027": MOTOGP_WEEKEND_2027,
    "Moto3": MOTO3_WEEKEND,
    "Moto2": MOTO2_WEEKEND,
}


def _element_block(session_type: SessionType) -> int:
    """Which block of ten a session lives in."""
    return common.SESSION_ELEMENTS[session_type] // 10


# ---------------------------------------------------------------------------
# The two invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weekend,order", ALL_WEEKENDS.items())
def test_numbers_sort_in_chronological_order(weekend, order):
    """The whole point of the scheme: the folder name sorts by session order."""
    numbers = [common.SESSION_ELEMENTS[s] for s in order]
    assert numbers == sorted(numbers), (
        f"{weekend} would list out of order: "
        f"{list(zip([s.name for s in order], numbers))}"
    )
    assert len(set(numbers)) == len(numbers), f"{weekend} has duplicate numbers"


def test_no_duplicate_element_numbers():
    """A shared number would make two sessions indistinguishable on disk."""
    values = list(common.SESSION_ELEMENTS.values())
    assert len(values) == len(set(values))


def test_every_session_type_has_a_number():
    """A member without an entry would silently fall back to its enum value."""
    missing = [s.name for s in SessionType
               if s not in (SessionType.UNKNOWN,) and s not in common.SESSION_ELEMENTS]
    assert missing == []


def test_numbers_fit_the_two_digit_prefix():
    """E<NN> is two digits in yyRNNEee; 100 would produce an unparseable name."""
    assert max(common.SESSION_ELEMENTS.values()) <= 99
    assert min(common.SESSION_ELEMENTS.values()) >= 1


def test_there_is_room_for_a_new_session():
    """
    The old table was full, which is why the 2027 sessions had to be appended
    after the Race. The scheme only works if a future session can be inserted
    between two existing ones without renumbering anything.
    """
    used = set(common.SESSION_ELEMENTS.values())
    free = [n for n in range(1, 100) if n not in used]
    assert len(free) >= 50, f"only {len(free)} free slots left"


def test_naming_does_not_depend_on_enum_declaration_order():
    """
    The old scheme *was* the enum: int(session_type) was the folder number, so
    the order members happen to be declared in silently decided the library
    layout. Naming must come from SESSION_ELEMENTS alone, otherwise reordering
    the enum (a harmless-looking edit) would rename files.
    """
    for session_type in SessionType:
        if session_type is SessionType.UNKNOWN:
            continue
        assert common.element_number(session_type, 2027) == common.SESSION_ELEMENTS[session_type]

    # Sprint Qualifying sits after MotoGP FP2 and before FP3, which is the 2027
    # weekend order - and not the order the enum declares them in (23, 24).
    fp2 = common.SESSION_ELEMENTS[SessionType.MOTOGP_FP2]
    sq = common.SESSION_ELEMENTS[SessionType.MOTOGP_SPRINT_QUALIFYING]
    fp3 = common.SESSION_ELEMENTS[SessionType.MOTOGP_FP3]
    assert fp2 < sq < fp3


# ---------------------------------------------------------------------------
# Round-tripping through the prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("season", [2027, 2028])
def test_prefix_round_trips_for_every_session(season):
    for session_type in common.SESSION_ELEMENTS:
        prefix = common.build_prefix(season, 7, session_type)
        assert common.parse_prefix(prefix) == (season, 7, session_type), prefix


@pytest.mark.parametrize("season", [2025, 2026])
def test_legacy_prefix_round_trips_for_every_session(season):
    for session_type in common.SESSION_ELEMENTS_LEGACY:
        prefix = common.build_prefix(season, 7, session_type)
        assert common.parse_prefix(prefix) == (season, 7, session_type), prefix


def test_the_cutover_season_is_where_the_scheme_changes():
    """Writing must switch exactly once, at LEGACY_ELEMENT_SEASON + 1."""
    race = SessionType.MOTOGP_RACE
    assert common.element_number(race, common.LEGACY_ELEMENT_SEASON) == 21
    assert common.element_number(race, common.LEGACY_ELEMENT_SEASON + 1) == 82


def test_seasons_without_a_number_get_the_new_scheme():
    """Naming must never depend on the season being known."""
    assert common.element_number(SessionType.MOTOGP_RACE, None) == 82


def test_ambiguity_is_reported_not_guessed():
    """
    The two schemes reuse numbers. Where they mean different sessions, a lookup
    with no season must refuse rather than pick one - misreading a leftover Race
    folder as a practice session is silent library corruption.
    """
    clashes = [
        (num, common.get_session_from_element(num), common.get_session_from_element(num, 2027))
        for num in range(1, 100)
        if common.element_is_ambiguous(num)
    ]
    assert clashes, "expected the schemes to share some numbers"

    for num, unknown, defined in clashes:
        assert unknown is None, f"E{num} was guessed as {unknown}"
        assert defined is not None


def test_episode_keys_are_season_scoped_and_unique():
    """
    grabber_state.json and the activity feed key on YYYY_RNN_ENN. Two sessions
    must never produce the same key within a season.
    """
    keys = {
        f"2027_R07_E{common.element_number(s, 2027):02d}"
        for s in common.SESSION_ELEMENTS
    }
    assert len(keys) == len(common.SESSION_ELEMENTS)


# ---------------------------------------------------------------------------
# Unreadable leftovers are reported, not treated as absent
# ---------------------------------------------------------------------------

def test_unreadable_folders_are_detected_rather_than_reported_missing():
    """
    A folder whose number has no meaning in the current scheme would be treated
    as an absent session, so the grabber would fetch it again and the library
    would hold two items meaning the same thing. It must be named instead.

    Note the limit, which is why the media is being wiped rather than migrated:
    detection only works for numbers that do not exist at all. A stale folder
    whose number happens to be valid now (E24 was Sprint Qualifying, is Moto2
    Practice) cannot be told apart from a correct one by name alone.
    """
    season = common.LEGACY_ELEMENT_SEASON + 1  # a season on the new scheme
    leftovers = ["26R04E06 MotoGP Practice", "26R04E02 MotoGP Practice"]
    valid = [f"{season % 100:02d}R04E12 MotoGP FP1"]

    found = common.find_legacy_folders(season, leftovers + valid)
    assert sorted(found) == sorted(leftovers)


def test_a_colliding_legacy_number_cannot_be_detected_by_name():
    """
    Documented limit, asserted so nobody assumes otherwise: E24 parses in both
    schemes, so a leftover folder carrying it is silently misread rather than
    flagged. Only the season folder it sits in distinguishes them, which is why
    pre-cutover media must be removed rather than left in place.
    """
    season = common.LEGACY_ELEMENT_SEASON + 1
    assert common.find_legacy_folders(season, ["26R04E24 MotoGP Sprint Qualifying"]) == []
    assert common.parse_prefix("26R04E24 Sprint Qualifying") == (
        season, 4, SessionType.MOTO2_PRACTICE
    )


def test_current_numbering_is_never_flagged_as_stale():
    """The detector must not cry wolf about folders the app just wrote."""
    season = common.LEGACY_ELEMENT_SEASON + 1
    folders = [
        f"{common.build_prefix(season, 3, s)} {s.full_short_name}"
        for s in common.SESSION_ELEMENTS
    ]
    assert common.find_legacy_folders(season, folders) == []


def test_non_session_folders_are_ignored():
    """Artwork and other stray entries are not session folders."""
    assert common.find_legacy_folders(2027, ["poster.jpg", "Round 04 Spain", ""]) == []
