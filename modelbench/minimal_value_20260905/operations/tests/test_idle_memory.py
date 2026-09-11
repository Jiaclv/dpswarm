from pathlib import Path
from types import SimpleNamespace
import pytest

from modelbench.minimal_value_20260905.operations.idle_memory import ensure_idle_headroom


def test_adequate_memory_never_changes_cache(tmp_path):
    calls=[]
    def run(args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='')
    r=ensure_idle_headroom(tmp_path/'r.json',tmp_path/'lease',run=run,available=lambda:8*2**30)
    assert r['status']=='ready' and not r['cache_reclaimed']
    assert all(call==['docker','ps','--quiet'] for call in calls)


def test_running_container_prevents_reclaim(tmp_path):
    calls=[]
    def run(args):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='owned-or-unrelated-container')
    with pytest.raises(RuntimeError,match='running containers'):
        ensure_idle_headroom(tmp_path/'r.json',tmp_path/'lease',run=run,available=lambda:1)
    assert len(calls)==1


def test_low_memory_reclaims_only_idle_cache_and_waits_for_host(tmp_path):
    calls=[]; memory=[2*2**30]
    def run(args):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout='Cached: 12000000 kB\n' if '/proc/meminfo' in args else '')
    def sleep(_):
        memory[0]=12*2**30
    r=ensure_idle_headroom(tmp_path/'r.json',tmp_path/'lease',run=run,available=lambda:memory[0],sleep=sleep)
    assert r['status']=='ready' and r['cache_reclaimed']
    assert sum('sync; echo 3 > /proc/sys/vm/drop_caches' in call for call in calls)==1
    assert not any('rm' in call or '--shutdown' in call for call in calls)


def test_held_lease_prevents_any_command(tmp_path):
    lock=tmp_path/'lease';lock.touch()
    def run(_):
        pytest.fail('must not issue a command while leased')
    with pytest.raises(RuntimeError,match='lease'):
        ensure_idle_headroom(tmp_path/'r.json',lock,run=run)
