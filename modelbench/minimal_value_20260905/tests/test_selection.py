"""Selection contracts use fixed paired outcomes and no model or filesystem IO."""
from copy import deepcopy

import pytest

from modelbench.minimal_value_20260905.contracts import ARMS
from modelbench.minimal_value_20260905.selection import choose


def rows(**overrides):
    specs = {'S': (10, 1.0, 10.0), 'L': (8, .1, 8.0), 'R2': (9, 1.0, 10.0),
             'D': (12, 1.0, 10.0), 'T': (9, 1.0, 10.0)}
    specs.update(overrides)
    return [{'arm': arm, 'instance_id': f'task-{index}', 'repo': f'repo-{index % 8}',
             'end_to_end_success': int(index < specs[arm][0]),
             'api_equivalent_usd': specs[arm][1], 'inference_wall_seconds': specs[arm][2]}
            for arm in ARMS for index in range(16)]


def test_unknown_cost_cannot_rescue_candidate_that_loses_known_quality():
    result = choose(rows(T=(9, None, 10)))
    assert result['C_star'] == 'D' and result['route'] == 'quality'
    assert result['qualifications']['T'] == 'not_qualified'
    assert result['possible_routes']['T'] == []
    assert result['selection_blockers'] == []


def test_potential_economy_cannot_block_already_qualified_quality():
    result = choose(rows(T=(10, None, 10)))
    assert result['C_star'] == 'D' and result['route'] == 'quality'
    assert result['qualifications']['T'] == 'undecidable'
    assert result['possible_routes']['T'] == ['economy']
    assert result['metrics']['T']['mean_cost'] is None


def test_potential_better_quality_blocks_known_quality():
    result = choose(rows(T=(13, None, 10)))
    assert result['status'] == 'undecidable' and result['C_star'] is None
    assert result['selection_blockers'] == ['T']
    assert result['qualifications']['D'] == 'quality'


def test_potential_lower_success_quality_cannot_displace_better_quality():
    result = choose(rows(D=(14, 1, 10), T=(12, None, 10)))
    assert result['C_star'] == 'D'
    assert result['qualifications']['T'] == 'undecidable'
    assert result['selection_blockers'] == []


def test_unknown_latency_cannot_reverse_known_cost_order_when_simplicity_cannot_help():
    result = choose(rows(D=(12, .8, 10), T=(12, 1.1, None)))
    assert result['C_star'] == 'D'
    assert result['qualifications']['T'] == 'undecidable'


def test_tied_quality_with_possible_cheaper_competitor_remains_undecidable():
    result = choose(rows(D=(12, 1, 10), T=(12, None, 10)))
    assert result['status'] == 'undecidable'
    assert result['selection_blockers'] == ['T']


def test_possible_quality_blocks_known_economy_route():
    result = choose(rows(D=(10, .7, 10), T=(12, None, 10)))
    assert result['qualifications']['D'] == 'economy'
    assert result['status'] == 'undecidable' and result['C_star'] is None


def test_known_latency_failure_excludes_unknown_cost_candidate():
    result = choose(rows(T=(13, None, 14)))
    assert result['qualifications']['T'] == 'not_qualified'
    assert result['C_star'] == 'D'


def test_repository_requirement_can_exclude_unknown_quality_cost():
    data = rows(T=(12, None, 10))
    # Both additional successes now lie in one repository, so +2 alone fails.
    for row in data:
        if row['instance_id'] in ('task-10', 'task-11'):
            row['repo'] = 'one-net-positive-repo'
    result = choose(data)
    assert result['positive_net_repositories']['T'] == 1
    assert result['qualifications']['T'] == 'not_qualified'


def test_solo_tie_skips_entire_unknown_cost_layer_and_discloses_it():
    result = choose(rows(S=(10, .1, 10), L=(10, None, 8), R2=(10, 1, 9), D=(9, 1, 10)))
    assert result['S_star'] == 'L'
    assert result['S_star_ranking']['cost_layer_used'] is False
    assert result['S_star_ranking']['wall_layer_used'] is True


def test_all_zero_success_cannot_enter_economy_even_with_missing_cost():
    result = choose(rows(**{arm: (0, None if arm == 'T' else 1, 10) for arm in ARMS}))
    assert result['status'] == 'not_qualified'
    assert result['C_star'] is None and result['possible_routes'] == {'D': [], 'T': []}


def test_qualification_before_selection_preserves_economy_candidate():
    result = choose(rows(D=(10, .7, 10), T=(11, 2, 10)))
    assert result['C_star'] == 'D' and result['route'] == 'economy'


@pytest.mark.parametrize('d_cost,d_wall,expected', [(1.1, 11, 'D'), (1.11, 10, 'T'), (1.1, 11.1, 'T')])
def test_d_simplicity_rule_only_within_both_tolerances(d_cost, d_wall, expected):
    result = choose(rows(D=(12, d_cost, d_wall), T=(12, 1, 10)))
    assert result['C_star'] == expected


def test_known_higher_quality_success_count_precedes_d_simplicity():
    result = choose(rows(D=(12, .8, 10), T=(13, 1, 10)))
    assert result['C_star'] == 'T'


def test_pairs_must_be_unique_same_tasks_and_repositories():
    data = rows()
    data[-1]['instance_id'] = 'task-0'
    assert choose(data)['status'] == 'unpaired_or_duplicate_tasks'
    data = rows()
    data[-1]['repo'] = 'other-repo'
    assert choose(data)['status'] == 'unpaired_or_duplicate_tasks'
    assert choose(rows()[:-1])['status'] == 'incomplete'


@pytest.mark.parametrize('field,value', [('end_to_end_success', None), ('end_to_end_success', 2),
    ('api_equivalent_usd', -1), ('api_equivalent_usd', float('nan')), ('inference_wall_seconds', float('inf'))])
def test_invalid_metrics_cannot_create_winner(field, value):
    data = deepcopy(rows())
    data[0][field] = value
    assert choose(data)['status'] == 'invalid_metrics'
