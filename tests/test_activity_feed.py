"""Activity events must retain the session identity throughout the pipeline.

The grabber, watcher and Plex tagger are separate processes. They communicate
through activity_events.json, so a later stage must not replace a full session
name with a classless one or erase venue information it cannot reconstruct.
"""

import json
from types import SimpleNamespace

import pytest

from motogp_common import SessionType


ROUND_NUM = 19
VENUE = "Malaysia"
YEAR = 2026


@pytest.fixture
def activity_env(tmp_path, monkeypatch):
    """Isolate the file-backed activity store and use a minimal calendar."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    import activity_emitter
    import motogp_common
    import motogp_plex_tagger
    import motogp_watcher

    monkeypatch.setattr(activity_emitter, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        activity_emitter, "ACTIVITY_FILE", tmp_path / "activity_events.json"
    )
    monkeypatch.setattr(motogp_common, "CONFIG_DIR", tmp_path)

    (tmp_path / f"motogp_{YEAR}.json").write_text(
        json.dumps(
            {
                "season": YEAR,
                "rounds": [
                    {
                        "round": ROUND_NUM,
                        "venue": VENUE,
                        "folder": f"Round {ROUND_NUM:02d} {VENUE}",
                    }
                ],
            }
        )
    )

    return activity_emitter, motogp_plex_tagger, motogp_watcher


def _episode_key(session_type):
    return f"{YEAR}_R{ROUND_NUM:02d}_E{int(session_type):02d}"


def _folder_name(session_type):
    return (
        f"{YEAR % 100:02d}R{ROUND_NUM:02d}E{int(session_type):02d} "
        f"{VENUE} {session_type.full_display_name}"
    )


def _assert_identity(event, session_type):
    assert event["name"] == session_type.full_display_name
    assert event["round"] == ROUND_NUM
    assert event["venue"] == VENUE


@pytest.mark.parametrize(
    "session_type",
    [SessionType.MOTO3_RACE, SessionType.MOTO2_RACE, SessionType.MOTOGP_RACE],
)
def test_watcher_updates_preserve_session_identity(activity_env, session_type):
    """Folder-derived updates must not erase metadata from the grabber."""
    emitter, _, watcher = activity_env
    episode_key = _episode_key(session_type)

    emitter.emit_activity(
        episode_key=episode_key,
        episode_name=session_type.full_display_name,
        stage=emitter.ActivityStage.DOWNLOADING,
        round_info={"round": ROUND_NUM, "venue": VENUE},
    )

    parsed_key, episode_name, round_num, _ = watcher._folder_to_episode_key(
        _folder_name(session_type)
    )
    assert parsed_key == episode_key

    # These are the round_info values supplied by the watcher. Its prefix
    # parser knows the round and session, but not the venue.
    for stage in (emitter.ActivityStage.DOWNLOAD_COMPLETE, emitter.ActivityStage.PROCESSING):
        emitter.emit_activity(
            episode_key=parsed_key,
            episode_name=episode_name,
            stage=stage,
            round_info={"round": round_num, "venue": None},
        )
        _assert_identity(emitter.get_active_events()[episode_key], session_type)


@pytest.mark.parametrize(
    "session_type",
    [SessionType.MOTO3_RACE, SessionType.MOTO2_RACE, SessionType.MOTOGP_RACE],
)
def test_plex_metadata_and_completion_include_race_class(
    activity_env, session_type, monkeypatch
):
    """The tagger and its completion event must both use the full name."""
    emitter, tagger, _ = activity_env
    episode_key = _episode_key(session_type)
    folder_name = _folder_name(session_type)

    emitter.emit_activity(
        episode_key=episode_key,
        episode_name=session_type.full_display_name,
        stage=emitter.ActivityStage.PROCESSING,
        round_info={"round": ROUND_NUM, "venue": VENUE},
    )

    # Exercise the real tagger orchestration without contacting Plex.
    monkeypatch.setattr(tagger, "get_plex_connection", lambda: object())
    monkeypatch.setattr(tagger, "trigger_library_scan", lambda *args: True)
    monkeypatch.setattr(tagger, "translate_path_for_plex", lambda path: path)
    monkeypatch.setattr(tagger, "plex_path_mapping_complete", lambda: True)
    monkeypatch.setattr(
        tagger,
        "get_plex_path_mapping",
        lambda: {"container": "/media", "plex": "/media"},
    )
    monkeypatch.setattr(
        tagger,
        "find_plex_item_by_path",
        lambda *args, **kwargs: SimpleNamespace(title=session_type.full_display_name),
    )
    monkeypatch.setattr(
        tagger, "apply_tags_and_collection", lambda *args, **kwargs: []
    )

    metadata_events = []
    real_mark_complete = tagger.mark_complete

    def capture_metadata_then_complete(episode_key, details="Ready in Plex"):
        metadata_events.append(emitter.get_active_events()[episode_key].copy())
        return real_mark_complete(episode_key=episode_key, details=details)

    monkeypatch.setattr(tagger, "mark_complete", capture_metadata_then_complete)

    file_path = (
        f"/media/MotoGP/{YEAR}/Round {ROUND_NUM:02d} {VENUE}/"
        f"{folder_name}/{folder_name}.mkv"
    )
    assert tagger.tag_single_file(file_path, wait_for_plex=False) is True

    assert len(metadata_events) == 1
    assert metadata_events[0]["stage"] == emitter.ActivityStage.METADATA.value
    _assert_identity(metadata_events[0], session_type)

    history = emitter.get_completed_history()
    assert len(history) == 1
    assert history[0]["stage"] == emitter.ActivityStage.COMPLETE.value
    _assert_identity(history[0], session_type)
    assert emitter.get_active_events() == {}
