from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import threading
import time

import pytest

from modelbench.minimal_value_20260905.operations import a2_parallel_prepare as p


def test_original_package_deadline_is_not_reset(tmp_path):
    start = datetime.now(timezone.utc) - timedelta(hours=97)
    (tmp_path / 'state.json').write_text(json.dumps({'started_at': start.isoformat()}))
    assert p.remaining_package(tmp_path) < 0


def test_stop_in_predecessor_blocks_preparation(tmp_path):
    a1 = tmp_path / 'a1'
    a1.mkdir()
    (a1 / 'CANCEL').touch()
    with pytest.raises(RuntimeError, match='STOP/CANCEL'):
        p.check_stop(tmp_path / 'run', a1)


def setup_preparation(monkeypatch, tmp_path, fail_id=None):
    from modelbench.minimal_value_20260905 import cli, data, environment
    from modelbench.minimal_value_20260905.operations import prepare_a2, memory_profile
    a1, run, official = [tmp_path / s for s in ('a1', 'run', 'official')]
    for path in (a1, run, official):
        path.mkdir()
    rows = [{'instance_id': f'task-{i}'} for i in range(16)]
    (official / 'public_checks.json').write_text(json.dumps({r['instance_id']: {} for r in rows}))
    monkeypatch.setattr(p, 'remaining_package', lambda _: 100)
    monkeypatch.setattr(p, 'install_gate', lambda *_: {'passed': True})
    monkeypatch.setattr(p, 'sha', lambda _: 'hash')
    monkeypatch.setattr(prepare_a2, 'stage_a2_inputs', lambda **_: None)
    monkeypatch.setattr(prepare_a2, 'publish_a2_inputs', lambda *a, **kw: {'publication_sha256': 'hash'})
    monkeypatch.setattr(prepare_a2, 'qualification_owned_root', lambda _: official / 'owned')
    monkeypatch.setattr(prepare_a2, '_official', lambda: official)
    monkeypatch.setattr(memory_profile, 'install', lambda *a: None)
    monkeypatch.setattr(cli, 'load_manifest', lambda _: {'resource_lock_path': str(tmp_path / 'lease')})
    monkeypatch.setattr(environment, 'capture_grader_contract', lambda: {'contract': 'frozen'})
    monkeypatch.setattr(data, 'load_public', lambda *a: rows)
    lease = {}
    @contextmanager
    def stage_lease(*_):
        yield lease
    monkeypatch.setattr(cli, 'stage_lease', stage_lease)
    stats = {'active': 0, 'peak': 0, 'seen': []}
    lock = threading.Lock()
    def one(row, **kwargs):
        with lock:
            stats['active'] += 1
            stats['peak'] = max(stats['peak'], stats['active'])
            stats['seen'].append(row['instance_id'])
        time.sleep(.04)
        with lock:
            stats['active'] -= 1
        return {'qualified': row['instance_id'] != fail_id, 'cleanup_confirmed': True}
    monkeypatch.setattr(prepare_a2, '_qualify_one', one)
    return a1, run, stats, lease, official


def test_four_task_cap_all_sixteen_preserved(monkeypatch, tmp_path):
    a1, run, stats, lease, _ = setup_preparation(monkeypatch, tmp_path)
    result = p.qualify(a1, run)
    assert result['all_qualified'] and result['qualified_count'] == 16
    assert stats['peak'] == 4 and stats['active'] == 0
    assert len(set(stats['seen'])) == 16
    assert not lease.get('retain')


def test_failure_stops_new_admissions_but_waits_for_owned_tasks(monkeypatch, tmp_path):
    a1, run, stats, _, _ = setup_preparation(monkeypatch, tmp_path, fail_id='task-0')
    result = p.qualify(a1, run)
    assert result['status'] == 'blocked' and not result['all_qualified']
    assert stats['active'] == 0
    assert len(stats['seen']) == 4


def test_existing_attempt_is_not_reexecuted(monkeypatch, tmp_path):
    a1, run, stats, _, official = setup_preparation(monkeypatch, tmp_path)
    (official / 'owned').mkdir()
    with pytest.raises(ValueError, match='already exists'):
        p.qualify(a1, run)
    assert stats['seen'] == []


def test_failed_qualification_blocks_frozen_paid_batch(monkeypatch, tmp_path):
    (tmp_path / 'preparation-state.json').write_text(json.dumps({'all_qualified': False}))
    with pytest.raises(ValueError, match='16 qualifications'):
        p.freeze_a2(tmp_path / 'a1', tmp_path / 'a2', tmp_path)
    assert not (tmp_path / 'a2').exists()


def test_cancel_joins_tasks_and_retains_lease_if_cleanup_unconfirmed(monkeypatch, tmp_path):
    from modelbench.minimal_value_20260905 import cli
    a1, run, stats, lease, _ = setup_preparation(monkeypatch, tmp_path)
    checks = []
    def stop_after_dispatch(*args, **kwargs):
        checks.append(1)
        if len(checks) == 3:
            raise RuntimeError('cancelled during qualification')
    monkeypatch.setattr(p, 'check_stop', stop_after_dispatch)
    monkeypatch.setattr(cli, 'cleanup_owned_episode', lambda _: {'confirmed': False})
    with pytest.raises(RuntimeError, match='cancelled during'):
        p.qualify(a1, run)
    assert stats['active'] == 0 and len(stats['seen']) == 4
    assert lease['retain'] is True
    assert (run / 'STOP').exists()