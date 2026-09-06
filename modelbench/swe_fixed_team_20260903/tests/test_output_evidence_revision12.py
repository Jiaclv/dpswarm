"""Environment-to-runner display evidence; all process output is synthetic."""
import json
from types import SimpleNamespace

import pytest

from modelbench.swe_fixed_team_20260903.runner import LIMITS, SweRun
from modelbench.swe_verified_20260903.environment import SWEEnvironment


def bash_result(environment):
    run = object.__new__(SweRun)
    run.fixed_team_requested = False
    run.limits = dict(LIMITS)
    run.remaining_time = lambda: 60
    return run.execute_tool('bash', {'command': 'synthetic command'}, None, environment, None)


def test_two_display_limits_preserve_capture_and_existing_truncation(tmp_path, monkeypatch):
    environment = SWEEnvironment({'instance_id': 'pallets__flask-5014',
                                 'base_commit': 'a' * 40}, tmp_path)
    captured = {'stdout': '\u6c49' * 45000, 'stderr': 'e' * 13000,
                'exit_code': 1, 'timed_out': False, 'duration_seconds': 0.01}
    monkeypatch.setattr(environment, '_exec', lambda *args, **kwargs: dict(captured))
    result = bash_result(environment)
    assert result['stdout'] == '\u6c49' * 12000 + '\n[output truncated]\n' + '\u6c49' * 6000
    assert result['stdout_truncated'] is True
    assert result['stdout_captured_chars'] == 45000
    assert result['stdout_returned_chars'] == len(result['stdout'])
    assert result['stderr'] == 'e' * 12000
    assert result['stderr_truncated'] is True  # Environment clipped it before the runner.
    assert result['stderr_captured_chars'] == 13000
    assert result['stderr_returned_chars'] == 12000
    assert result['exit_code'] == 1 and result['timed_out'] is False
    saved = json.loads((tmp_path / 'commands/00001.json').read_text(encoding='utf-8'))
    assert saved['stdout'] == captured['stdout'] and saved['stderr'] == captured['stderr']


@pytest.mark.parametrize('size,truncated', [(18000, False), (18001, True)])
def test_legacy_environment_metadata_is_added_without_changing_command_status(size, truncated):
    environment = SimpleNamespace(run=lambda *args, **kwargs: {
        'stdout': 'x' * size, 'stderr': '', 'exit_code': 0, 'timed_out': False})
    result = bash_result(environment)
    assert result['stdout_captured_chars'] == size
    assert result['stdout_truncated'] is truncated
    assert result['stdout_returned_chars'] == len(result['stdout'])
    assert result['stderr_captured_chars'] == result['stderr_returned_chars'] == 0
    assert result['stderr_truncated'] is False
    assert result['exit_code'] == 0
