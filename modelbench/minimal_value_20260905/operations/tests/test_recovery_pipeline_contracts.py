"""Recovery must preserve the original deadline and Windows Unicode output."""
from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys

from modelbench.minimal_value_20260905.operations import pipeline as p


def test_pipeline_children_emit_unicode_under_forced_gbk_parent(monkeypatch):
    monkeypatch.setenv('PYTHONIOENCODING', 'gbk')
    result = subprocess.run([sys.executable, '-B', '-c', "print(chr(0x2713))"],
                            env=p.child_environment(), capture_output=True, timeout=15)
    assert result.returncode == 0
    assert result.stdout.decode('utf-8').strip() == '\u2713'


def test_expired_original_package_cannot_dispatch_any_operation(tmp_path, monkeypatch):
    a1, a2, destination = tmp_path / 'a1', tmp_path / 'a2', tmp_path / 'pipeline'
    a1.mkdir()
    p.write(a1 / 'launch.json', {'pid': 999999})
    p.write(a1 / 'manifest.json', {'resource_lock_path': str(tmp_path / 'shared.lock')})
    p.write(a1 / 'state.json', {'completed_at': 'done', 'completed_episodes': 10})
    monkeypatch.setattr(p, 'OPERATIONS', tmp_path / 'operations')
    def forbidden(*args, **kwargs):
        raise AssertionError('Expired package dispatched work')
    result = p.run(a1, a2, destination,
                   package_started_at=(datetime.now(timezone.utc) - timedelta(hours=97)).isoformat(),
                   runtime=lambda _: ({'stage': 'a1', 'scheduled_episodes': 10}, {}, {}),
                   alive=lambda _: False, operation=forbidden, sleep=forbidden)
    assert result['status'] == 'stopped'
    assert result['reason'] == 'pipeline_total_deadline'
    assert result['elapsed_before_start_seconds'] >= 97 * 3600
    assert not result['operations']
