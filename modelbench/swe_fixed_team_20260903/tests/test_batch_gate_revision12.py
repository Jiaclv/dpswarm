import pytest

from modelbench.swe_fixed_team_20260903.cli import result_stop


@pytest.mark.parametrize('condition', ['fixed_team', 'hetero_team'])
@pytest.mark.parametrize('coverage', [None, 0, 1, 2])
def test_team_batch_gate_requires_both_worker_attempts(condition, coverage):
    result = {'run_id': 'fixture', 'condition': condition,
              'score': {'completed': True, 'resolved': False},
              'budget': {'unknown_call_count': 0, 'pending_call_count': 0},
              'workers_with_actual_calls': coverage}
    assert (result_stop(result) is None) is (coverage == 2)


def test_execution_health_stops_even_if_legacy_infrastructure_field_is_empty():
    result = {'run_id': 'fixture', 'condition': 'solo',
              'score': {'completed': True, 'resolved': True},
              'budget': {'unknown_call_count': 0, 'pending_call_count': 0},
              'infrastructure_error': None, 'execution_health': {'status': 'host_error'}}
    stop = result_stop(result)
    assert stop['execution_health']['status'] == 'host_error'


def test_ordinary_unresolved_and_known_empty_patch_do_not_stop_batch():
    result = {'run_id': 'fixture', 'condition': 'solo',
              'score': {'completed': True, 'resolved': False},
              'budget': {'unknown_call_count': 0, 'pending_call_count': 0}}
    assert result_stop(result) is None
    result['score'] = {'completed': False, 'resolved': False, 'failure_kind': 'candidate_empty_patch'}
    assert result_stop(result) is None
    result['budget']['pending_call_count'] = 1
    assert result_stop(result)['pending_calls'] == 1
