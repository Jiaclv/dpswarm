from copy import deepcopy

import pytest

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_fixed_team_20260903.runtime_integrity import resolve_limits


@pytest.mark.parametrize('override', [
    {'cm_edit_curfew': 'false'}, {'max_calls': 1.5}, {'token_limit': -1},
    {'wall_seconds': float('nan')}, {'call_timeout': 0}, {'model_concurrency': 8},
    {'container_concurrency': 2}, {'active_workers': 3}, {'delegations': 3},
    {'cm_scout_distill': True}, {'cm_bootstrap_package': True},
    {'cm_enabled': False, 'cm_team_memory': True}, {'unknown_field': 1},
])
def test_invalid_or_ineffective_limits_fail_before_resources(tmp_path, override):
    entry = {'run_id': 'fixture', 'instance': {}, 'condition': 'solo', 'limits_override': override}
    with pytest.raises(ValueError):
        runner.SweRun(tmp_path, entry)
    assert not list(tmp_path.iterdir())


def test_effective_configuration_cannot_disagree_with_override(tmp_path):
    effective = deepcopy(runner.LIMITS)
    effective['token_limit'] += 1
    entry = {'run_id': 'fixture', 'instance': {}, 'condition': 'solo', 'effective_limits': effective}
    with pytest.raises(ValueError, match='effective_limits'):
        runner.SweRun(tmp_path, entry)
    assert not list(tmp_path.iterdir())


def test_fixed_team_cannot_hide_an_ordered_route_change(tmp_path):
    entry = {'run_id': 'fixture', 'instance': {}, 'condition': 'fixed_team',
             'worker_model': 'glm-5.3', 'worker_models': ['gpt-5.6-terra'] * 2}
    with pytest.raises(ValueError, match='worker_models'):
        runner.SweRun(tmp_path, entry)
    assert not list(tmp_path.iterdir())


def test_zero_reserves_and_legacy_shared_cm_pool_are_explicitly_supported():
    limits = resolve_limits(runner.LIMITS, {'lead_reserve_calls': 0, 'cm_call_allowance': None,
                                         'cm_reservation_slack': 0})
    assert limits['cm_call_allowance'] is None and limits['lead_reserve_calls'] == 0
