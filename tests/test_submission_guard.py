import json
import threading
from pathlib import Path

import pytest

import motogp_grabber as grabber
from motogp_common import SessionType
from utils.submission_guard import intent_path, load_intents, find_sab_job

ITEM = {'round': 20, 'round_info': {'venue': 'Qatar'}, 'session_type': SessionType.MOTO2_RACE}
KEY = '2026_R20_E82'
RELEASE = {'title': 'Moto2 2026 Qatar Race', 'link': 'https://example.invalid/a'}


def test_interruption_reserves_before_network_and_prevents_duplicate(tmp_path, monkeypatch):
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    def crash(release, item, season):
        pending = load_intents(intent_path(tmp_path))
        assert item['_submission_token'] in pending
        raise RuntimeError('process died')
    monkeypatch.setattr(grabber, 'grab_release', crash)
    with pytest.raises(RuntimeError):
        grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: pytest.fail('must not submit again'))
    result, error = grabber.guarded_grab(grabber.GrabberState(tmp_path / 'grabber_state.json'), RELEASE, ITEM, 2026, [KEY], 'backfill', True)
    assert result is None and 'uncertain' in error


def test_definite_refusal_removes_intent_but_timeout_does_not(tmp_path, monkeypatch):
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: (None, 'SABnzbd refused the release: rejected'))
    grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    assert load_intents(intent_path(tmp_path)) == {}
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: (None, 'SABnzbd timed out accepting the release'))
    grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    assert len(load_intents(intent_path(tmp_path))) == 1


def test_concurrent_stale_processes_submit_once(tmp_path, monkeypatch):
    path = tmp_path / 'grabber_state.json'
    states = [grabber.GrabberState(path), grabber.GrabberState(path)]
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    entered = threading.Event()
    proceed = threading.Event()
    sent = []
    def submit(*args):
        sent.append(1)
        entered.set()
        assert proceed.wait(5)
        return 'nzo', ''
    monkeypatch.setattr(grabber, 'grab_release', submit)
    results = []
    def run(state):
        results.append(grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled'))
    first = threading.Thread(target=run, args=(states[0],))
    second = threading.Thread(target=run, args=(states[1],))
    first.start()
    assert entered.wait(5)
    second.start()
    proceed.set()
    first.join(5); second.join(5)
    assert len(sent) == 1
    assert sum(result[0] == 'nzo' for result in results) == 1


def test_recover_both_combined_sessions_using_unique_token(tmp_path, monkeypatch):
    path = tmp_path / 'grabber_state.json'
    state = grabber.GrabberState(path)
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: (None, 'timeout'))
    grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY, 'other'], 'backfill')
    monkeypatch.setattr(grabber, 'find_sab_job', lambda *a: 'nzo')
    grabber.reconcile_submission_intents(grabber.GrabberState(path))
    result = grabber.GrabberState(path).state['grabbed']
    assert result[KEY]['nzo_id'] == result['other']['nzo_id'] == 'nzo'
    assert load_intents(intent_path(tmp_path)) == {}


def test_corrupt_intent_store_fails_closed(tmp_path, monkeypatch):
    intent_path(tmp_path).write_text('{')
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: pytest.fail('must not submit'))
    with pytest.raises(ValueError):
        grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')


def test_recovery_finds_exact_unique_marker_and_fails_closed():
    class Response:
        status_code = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body
    def get(url, params, timeout):
        slots = [{'filename': '26R20E82 Qatar mgp-abc', 'nzo_id': 'nzo'}] if params['mode'] == 'queue' else []
        return Response({params['mode']: {'slots': slots}})
    assert find_sab_job('abc', 'http://sab', 'key', get) == 'nzo'
    assert find_sab_job('other', 'http://sab', 'key', get) is None


def test_crash_after_saved_grab_is_cleared_without_sab_history(tmp_path, monkeypatch):
    path = tmp_path / 'grabber_state.json'
    state = grabber.GrabberState(path)
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: ('nzo', ''))
    real_write = grabber.save_intents
    def crash_before_deleting_intent(path_arg, value):
        if Path(path_arg) == intent_path(tmp_path) and not value:
            raise RuntimeError('crash')
        return real_write(path_arg, value)
    monkeypatch.setattr(grabber, 'save_intents', crash_before_deleting_intent)
    with pytest.raises(RuntimeError):
        grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    monkeypatch.setattr(grabber, 'save_intents', real_write)
    monkeypatch.setattr(grabber, 'find_sab_job', lambda *a: pytest.fail('already committed'))
    grabber.reconcile_submission_intents(grabber.GrabberState(path))
    assert load_intents(intent_path(tmp_path)) == {}


def test_force_does_not_bypass_an_active_or_unknown_job(tmp_path, monkeypatch):
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    state.mark_grabbed(KEY, 'previous', nzo_id='old')
    state.save()
    monkeypatch.setattr(grabber, 'get_sabnzbd_job_status', lambda job: 'unknown')
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: pytest.fail('must not duplicate'))
    result, error = grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'backfill', True)
    assert result is None and 'status is unknown' in error
    assert not intent_path(tmp_path).exists()


def test_combined_reservation_blocks_either_half(tmp_path, monkeypatch):
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: (None, 'timeout'))
    grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY, 'other'], 'backfill')
    result, error = grabber.guarded_grab(state, RELEASE, ITEM, 2026, ['other'], 'backfill', True)
    assert result is None and 'uncertain' in error


def test_queue_match_is_not_enough_when_history_request_fails():
    class Response:
        def __init__(self, status_code, data): self.status_code, self.data = status_code, data
        def json(self): return self.data
    def get(url, params, timeout):
        if params['mode'] == 'queue':
            return Response(200, {'queue': {'slots': [{'filename': 'mgp-abc', 'nzo_id': 'x'}]}})
        return Response(500, {})
    assert find_sab_job('abc', 'http://sab', 'key', get) is None


def test_corrupt_grabber_state_cannot_authorise_duplicate(tmp_path, monkeypatch):
    path = tmp_path / 'grabber_state.json'
    path.write_text('{')
    state = grabber.GrabberState(path)
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: pytest.fail('must not submit'))
    with pytest.raises(ValueError):
        grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    assert path.read_text() == '{'


def test_missing_status_response_is_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(grabber, 'get_sabnzbd_url', lambda: 'http://sab')
    monkeypatch.setattr(grabber, 'get_sabnzbd_api_key', lambda: 'key')
    class Response:
        status_code = 200
        def json(self): return {'not_status': True}
    monkeypatch.setattr(grabber.requests, 'get', lambda *a, **kw: Response())
    nzo_id, error = grabber.grab_release(RELEASE, ITEM, 2026)
    assert nzo_id is None and 'ambiguous' in error


def test_success_without_job_id_remains_pending(tmp_path, monkeypatch):
    state = grabber.GrabberState(tmp_path / 'grabber_state.json')
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    monkeypatch.setattr(grabber, 'grab_release', lambda *a: ('', ''))
    result, error = grabber.guarded_grab(state, RELEASE, ITEM, 2026, [KEY], 'scheduled')
    assert result is None and 'without a job ID' in error
    assert len(load_intents(intent_path(tmp_path))) == 1
    assert KEY not in grabber.GrabberState(state.state_file).state['grabbed']


def test_sab_name_has_marker_but_keeps_parser_prefix(monkeypatch):
    captured = {}
    monkeypatch.setattr(grabber, 'get_sabnzbd_url', lambda: 'http://sab')
    monkeypatch.setattr(grabber, 'get_sabnzbd_api_key', lambda: 'key')
    class Response:
        status_code = 200
        def json(self): return {'status': True, 'nzo_ids': ['nzo']}
    def get(url, params, timeout):
        captured.update(params)
        return Response()
    monkeypatch.setattr(grabber.requests, 'get', get)
    token = 'a' * 32
    assert grabber.grab_release(RELEASE, dict(ITEM, _submission_token=token), 2026) == ('nzo', '')
    assert 'mgp-' + token in captured['nzbname']
    assert grabber.parse_prefix(captured['nzbname']) == (2026, 20, SessionType.MOTO2_RACE)


def test_process_killed_after_submit_preserves_reservation(tmp_path, monkeypatch):
    import multiprocessing
    context = multiprocessing.get_context('fork')
    entered = context.Event()
    block = context.Event()
    path = tmp_path / 'grabber_state.json'
    monkeypatch.setattr(grabber, 'scan_library_for_season', lambda season: {})
    def accepting(*args):
        entered.set()  # represents SAB accepting immediately before interruption
        block.wait(10)
        return 'nzo', ''
    monkeypatch.setattr(grabber, 'grab_release', accepting)
    def worker():
        grabber.guarded_grab(grabber.GrabberState(path), RELEASE, ITEM, 2026, [KEY], 'scheduled')
    process = context.Process(target=worker)
    process.start()
    try:
        assert entered.wait(5)
        process.kill()
        process.join(5)
        assert not process.is_alive()
        monkeypatch.setattr(grabber, 'grab_release', lambda *a: pytest.fail('duplicate submission'))
        result, error = grabber.guarded_grab(grabber.GrabberState(path), RELEASE, ITEM, 2026, [KEY], 'scheduled')
        assert result is None and 'uncertain' in error
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_run_lock_rejects_other_process_then_recovers(tmp_path):
    import multiprocessing
    from utils.submission_guard import try_run_lock
    context = multiprocessing.get_context('fork')
    entered, release = context.Event(), context.Event()
    def worker():
        with try_run_lock(tmp_path) as acquired:
            assert acquired
            entered.set()
            release.wait(5)
    process = context.Process(target=worker)
    process.start()
    try:
        assert entered.wait(5)
        with try_run_lock(tmp_path) as acquired:
            assert not acquired
        release.set()
        process.join(5)
        assert process.exitcode == 0
        with try_run_lock(tmp_path) as acquired:
            assert acquired
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_cli_reports_busy_without_starting_an_overlapping_run(tmp_path, monkeypatch, capsys):
    import sys
    from utils.submission_guard import try_run_lock
    monkeypatch.setattr(grabber, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(grabber, 'init_logging', lambda verbose: None)
    monkeypatch.setattr(grabber, 'cmd_scheduled', lambda args: pytest.fail('should not start'))
    monkeypatch.setattr(sys, 'argv', ['motogp_grabber.py', 'scheduled'])
    with try_run_lock(tmp_path) as acquired:
        assert acquired
        assert grabber.main() == 2
    assert 'Another grabber is running' in capsys.readouterr().out
