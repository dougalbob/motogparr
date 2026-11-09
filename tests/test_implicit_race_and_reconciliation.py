import argparse
from datetime import datetime

import pytest

import motogp_common as common
import motogp_grabber as grabber


@pytest.mark.parametrize('cls,expected', [
    ('MotoGP', common.SessionType.MOTOGP_RACE),
    ('Moto2', common.SessionType.MOTO2_RACE),
    ('Moto3', common.SessionType.MOTO3_RACE),
])
def test_billie_implicit_race_is_accepted_only_for_race(cls, expected):
    round_info = {'round': 20, 'venue': 'Qatar', 'aliases': ['Lusail']}
    title = f'{cls}.2026.Qatar.1080p.WEB.h264-BILLIE'
    release = {'title': title, 'size': 1000, 'link': 'x'}
    assert common.classify_session(title, round_info=round_info) == expected
    assert common.filter_releases([release], expected, 2026, round_info, '1080p', ['BILLIE'])
    practice = {'MotoGP': common.SessionType.MOTOGP_FP1, 'Moto2': common.SessionType.MOTO2_FP1, 'Moto3': common.SessionType.MOTO3_FP1}[cls]
    assert not common.filter_releases([release], practice, 2026, round_info, '1080p', ['BILLIE'])


@pytest.mark.parametrize('title', [
    'Moto2.2026.Qatar.SQ.1080p.WEB.h264-BILLIE',
    'Moto2.2026.Qatar.FP.1080p.WEB.h264-BILLIE',
    'Moto2.2026.Qatar.1080p.WEB.h264-OTHER',
    'Moto2.2026.Qatar.1080p.WEB.h264-BILLIE-extra',
    'Moto2.2026.Qatar.Other.1080p.WEB.h264-BILLIE',
])
def test_unknown_titles_are_not_implicit_races(title):
    assert common.classify_session(title, round_info={'venue': 'Qatar'}) == common.SessionType.UNKNOWN


def test_reconciliation_caches_only_completed_jobs_and_persists(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    state = grabber.GrabberState(path)
    for key in ('complete', 'active', 'unknown', 'failed'):
        state.mark_grabbed(key, key, nzo_id=key)
    state.save()
    calls = []
    statuses = {'complete': 'completed', 'active': 'inprogress', 'unknown': 'unknown', 'failed': 'failed'}
    def status(job):
        calls.append(job)
        return statuses[job]
    monkeypatch.setattr(grabber, 'get_sabnzbd_job_status', status)
    assert grabber.reconcile_grabbed_jobs(state) == 1
    reloaded = grabber.GrabberState(path)
    assert reloaded.state['grabbed']['complete']['sab_status'] == 'completed'
    assert 'failed' not in reloaded.state['grabbed']
    assert 'failed' in reloaded.state['failed']
    calls.clear()
    assert grabber.reconcile_grabbed_jobs(reloaded) == 0
    assert set(calls) == {'active', 'unknown'}


def test_scheduled_grab_persisted_before_next_search(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    original = grabber.GrabberState
    monkeypatch.setattr(grabber, 'GrabberState', lambda: original(path))
    monkeypatch.setattr(grabber, 'reconcile_grabbed_jobs', lambda state: 0)
    monkeypatch.setattr(grabber, 'load_calendar', lambda season: {'rounds': []})
    monkeypatch.setattr(grabber, 'has_session_times', lambda calendar: True)
    monkeypatch.setattr(grabber, 'get_search_delay_hours', lambda: 1)
    monkeypatch.setattr(grabber, 'get_max_grabs_per_run', lambda: 10)
    monkeypatch.setattr(grabber, 'get_indexers', lambda: [{'id': 1}])
    monkeypatch.setattr(grabber, 'test_sabnzbd_connection', lambda: True)
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'emit_activity', lambda **kw: None)
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: ('nzo1', ''))
    items = [{'round': 20, 'round_info': {'venue': 'Qatar'}, 'session_type': session, 'ended_at': datetime.now()} for session in (common.SessionType.MOTO2_RACE, common.SessionType.MOTO3_RACE)]
    monkeypatch.setattr(grabber, 'get_sessions_ready_for_search', lambda *a, **kw: items)
    calls = []
    def search(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('interrupted')
        return [{'title': 'release', 'size': 1}]
    monkeypatch.setattr(grabber, 'search_for_session', search)
    with pytest.raises(RuntimeError, match='interrupted'):
        grabber.cmd_scheduled(argparse.Namespace(season=2026, dry_run=False, force=False, search_delay=None))
    saved = original(path)
    key = original.make_session_key(2026, 20, common.SessionType.MOTO2_RACE)
    assert saved.state['grabbed'][key]['nzo_id'] == 'nzo1'


def test_backfill_combined_grab_persisted_before_next_search(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    original = grabber.GrabberState
    class State(original):
        def __init__(self):
            super().__init__(path)
    monkeypatch.setattr(grabber, 'GrabberState', State)
    monkeypatch.setattr(grabber, 'reconcile_grabbed_jobs', lambda state: 0)
    monkeypatch.setattr(grabber, 'load_calendar', lambda season: {'rounds': []})
    monkeypatch.setattr(grabber, 'get_max_grabs_per_run', lambda: 10)
    monkeypatch.setattr(grabber, 'get_indexers', lambda: [{'id': 1}])
    monkeypatch.setattr(grabber, 'test_sabnzbd_connection', lambda: True)
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'print_missing_summary', lambda missing: None)
    monkeypatch.setattr(grabber, 'emit_activity', lambda **kw: None)
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: ('nzo1', ''))
    items = [{'round': 20, 'round_info': {'venue': 'Qatar'}, 'session_type': session} for session in (common.SessionType.MOTO2_Q1, common.SessionType.MOTO3_RACE)]
    monkeypatch.setattr(grabber, 'identify_missing_sessions', lambda *a: items)
    calls = []
    def search(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('interrupted')
        return [{'title': 'qualifying', 'size': 1, '_combined_quali': True}]
    monkeypatch.setattr(grabber, 'search_for_session', search)
    with pytest.raises(RuntimeError, match='interrupted'):
        grabber.cmd_backfill(argparse.Namespace(season=2026, dry_run=False, round=None, force=False, sessions=None))
    saved = original(path)
    for session in (common.SessionType.MOTO2_Q1, common.SessionType.MOTO2_Q2):
        assert original.make_session_key(2026, 20, session) in saved.state['grabbed']


def test_implicit_race_requires_calendar_context_and_exact_venue():
    title = 'MotoGP.2026.Great.Britain.1080p.WEB.H.264-BILLIE'
    assert common.classify_session(title) == common.SessionType.UNKNOWN
    assert common.classify_session(title, round_info={'venue': 'Great Britain'}) == common.SessionType.MOTOGP_RACE
    assert common.classify_session(title, round_info={'venue': 'Britain'}) == common.SessionType.UNKNOWN
