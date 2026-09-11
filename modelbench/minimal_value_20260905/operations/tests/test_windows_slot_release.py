"""Owned-slot filesystem retries only; never invokes Docker or a model."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

OPS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('release_test_' + name, OPS / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


r = load('parallel_resources')
p = load('provider_limits')


def fake_clock(slot, monkeypatch, on_sleep=None):
    now = [0.0]
    def sleep(seconds):
        assert slot._held and next(slot.directory.glob('slot-*.lock')).exists()
        now[0] += seconds
        if on_sleep:
            on_sleep()
    monkeypatch.setattr(slot, 'clock', lambda: now[0])
    monkeypatch.setattr(slot, 'sleep', sleep)
    monkeypatch.setattr(r, 'os', SimpleNamespace(name='nt', getpid=os.getpid, fsync=os.fsync))
    return now


def test_normal_release_and_release_without_acquisition(tmp_path):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    with pytest.raises(r.ResourceError, match='without an acquisition'):
        slot.release()
    assert slot.acquire(timeout=0)
    slot.release()
    assert not list(slot.directory.glob('*.lock'))
    assert slot.summary()['release_file_retries'] == 0
    assert slot.summary()['held_by_this_process'] == 0
    with pytest.raises(r.ResourceError, match='without an acquisition'):
        slot.release()


@pytest.mark.parametrize('operation', ['read', 'unlink'])
def test_transient_denial_retries_only_owned_file_and_preserves_capacity(tmp_path, monkeypatch, operation):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    assert slot.acquire(timeout=0)
    path, token = slot._held[threading.get_ident()]
    now = fake_clock(slot, monkeypatch)
    attempts = []
    old_read, old_unlink = r.read, Path.unlink
    def deny_then_read(target):
        if Path(target) == path and operation == 'read':
            attempts.append('read')
            if len(attempts) <= 2:
                raise PermissionError(errno.EACCES, 'test denial', str(path))
        return old_read(target)
    def deny_then_unlink(target, *args, **kwargs):
        if target == path and operation == 'unlink':
            attempts.append('unlink')
            assert old_read(path)['token'] == token
            assert not r.FileSemaphore(slot.directory, 1).acquire(timeout=0)
            if len(attempts) <= 2:
                raise PermissionError(errno.EACCES, 'test denial', str(path))
        return old_unlink(target, *args, **kwargs)
    monkeypatch.setattr(r, 'read', deny_then_read)
    monkeypatch.setattr(Path, 'unlink', deny_then_unlink)
    slot.release()
    assert attempts == [operation] * 3
    assert now[0] == pytest.approx(.05)
    assert slot.summary()['release_file_retries'] == 2
    assert slot.summary()['held_by_this_process'] == 0
    assert slot.acquisitions == 1 and not path.exists()


@pytest.mark.parametrize('changed', ['token', 'pid'])
def test_replacement_between_retries_is_never_deleted(tmp_path, monkeypatch, changed):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    assert slot.acquire(timeout=0)
    path, token = slot._held[threading.get_ident()]
    replacement = r.read(path)
    replacement[changed] = 'replacement-token' if changed == 'token' else -1
    fake_clock(slot, monkeypatch, lambda: path.write_text(json.dumps(replacement), encoding='utf-8'))
    attempts = []
    old = Path.unlink
    def denied(target, *args, **kwargs):
        if target == path:
            attempts.append(1)
            raise PermissionError(errno.EACCES, 'test denial', str(path))
        return old(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    with pytest.raises(r.ResourceError, match='ownership changed'):
        slot.release()
    assert len(attempts) == 1
    assert r.read(path) == replacement
    assert slot._held[threading.get_ident()] == (path, token)


def test_permanent_denial_stops_after_one_second_and_keeps_slot(tmp_path, monkeypatch):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    assert slot.acquire(timeout=0)
    path, token = slot._held[threading.get_ident()]
    now = fake_clock(slot, monkeypatch)
    old = Path.unlink
    attempts = []
    def denied(target, *args, **kwargs):
        if target == path:
            attempts.append(1)
            raise PermissionError(errno.EACCES, 'test denial', str(path))
        return old(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied)
    with pytest.raises(PermissionError):
        slot.release()
    assert now[0] == pytest.approx(1.0)
    assert 2 <= len(attempts) <= 42
    assert r.read(path)['token'] == token and slot._held
    assert not r.FileSemaphore(slot.directory, 1).acquire(timeout=0)


@pytest.mark.parametrize('error', [FileNotFoundError(errno.ENOENT, 'missing'), OSError(errno.EIO, 'I/O failure')])
def test_other_errors_are_not_retried_or_reported_as_release_success(tmp_path, monkeypatch, error):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    assert slot.acquire(timeout=0)
    path, _ = slot._held[threading.get_ident()]
    now = fake_clock(slot, monkeypatch)
    old = r.read
    def fail(target):
        if Path(target) == path:
            raise error
        return old(target)
    monkeypatch.setattr(r, 'read', fail)
    with pytest.raises(type(error)):
        slot.release()
    assert now[0] == 0 and path.exists() and slot._held


@pytest.mark.skipif(os.name != 'nt', reason='Requires real Windows delete-sharing semantics')
def test_real_windows_reader_conflict_releases_without_overadmission(tmp_path):
    slot = r.FileSemaphore(tmp_path / 'slots', 1)
    assert slot.acquire(timeout=0)
    path, token = slot._held[threading.get_ident()]
    options = {'creationflags': subprocess.CREATE_NO_WINDOW}
    reader_code = '''import sys,time
with open(sys.argv[1], 'rb') as stream:
    print('reader_ready', flush=True)
    sys.stdin.readline()
print(time.monotonic(), flush=True)
'''
    reader = subprocess.Popen([sys.executable, '-B', '-c', reader_code, str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)
    contender = timer = None
    try:
        assert reader.stdout.readline().strip() == 'reader_ready'
        # This is the exact un-retried operation from the previous implementation.
        with pytest.raises(PermissionError) as observed:
            path.unlink()
        assert observed.value.winerror in (5, 32, 33)
        contender_code = '''import importlib.util,json,sys,time
s=importlib.util.spec_from_file_location('slot_resources',sys.argv[1]);r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
slot=r.FileSemaphore(sys.argv[2],1)
print('contender_ready',flush=True)
ok=slot.acquire(timeout=5)
assert ok
record=r.read(next(slot.directory.glob('slot-*.lock')))
at=time.monotonic()
slot.release()
print(json.dumps({'acquired_at':at,'token':record['token']}),flush=True)
'''
        contender = subprocess.Popen([sys.executable, '-B', '-c', contender_code,
            str(OPS / 'parallel_resources.py'), str(slot.directory)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **options)
        assert contender.stdout.readline().strip() == 'contender_ready'
        def close_reader():
            reader.stdin.write('close\n')
            reader.stdin.flush()
        timer = threading.Timer(.25, close_reader)
        timer.start()
        slot.release()
        timer.join(timeout=2)
        reader_out, reader_err = reader.communicate(timeout=5)
        contender_out, contender_err = contender.communicate(timeout=5)
        assert reader.returncode == 0, reader_err
        assert contender.returncode == 0, contender_err
        acquired = json.loads(contender_out)
        assert acquired['acquired_at'] >= float(reader_out.strip())
        assert acquired['token'] != token
        assert slot.summary()['release_file_retries'] > 0
        assert slot.summary()['release_retry_seconds_sum'] >= .2
        assert not slot._held and not list(slot.directory.glob('*.lock'))
    finally:
        if timer:
            timer.cancel()
        for child in (reader, contender):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_provider_failure_keeps_stop_and_records_safe_diagnostic_phase(tmp_path, monkeypatch):
    policy = {'provider_limits_version': 1, 'provider_limits_source': 'operator_configured',
              'provider_limits': {'codex_account': 4, 'glm_coding': 1, 'deepseek': 4}}
    pp = tmp_path / 'policy.json'
    p.r.atomic_json(pp, policy)
    group = {'global_lock_dir': str(tmp_path / 'locks'), 'policy_path': str(pp), 'policy_sha256': p.r.sha(pp)}
    p.r.atomic_json(tmp_path / 'group.json', group)
    gate = p.ProviderGate(tmp_path, group, policy, p.r.FileSemaphore(tmp_path / 'locks/models', 8))
    secret = 'DO-NOT-LOG-THIS-FAKE-CREDENTIAL'
    def denied():
        error = PermissionError(errno.EACCES, secret)
        error.winerror = 32
        raise error
    with pytest.raises(PermissionError):
        with gate.route('glm-5.3', run_id='offline-one', role='worker'):
            assert gate.acquire(timeout=0)
            monkeypatch.setattr(gate.buckets['glm_coding'], 'release', denied)
            gate.release()
    trips = [p.r.read(path) for path in tmp_path.glob('provider-trip-*.json')]
    first = next(item for item in trips if item['reason'] == 'provider_release_failed')
    assert first['release_phase'] == 'provider_slot'
    assert first['bucket'] == 'glm_coding' and first['run_id'] == 'offline-one'
    assert first['error_type'] == 'PermissionError' and first['errno'] == errno.EACCES and first['winerror'] == 32
    assert first['message'] == os.strerror(errno.EACCES)
    assert first['traceback'] and first['traceback'][-1]['function'] == 'denied'
    assert secret not in json.dumps(trips)
    assert (tmp_path / 'CANCEL').exists()
    assert list((tmp_path / 'locks/providers/glm_coding').glob('*.lock'))
