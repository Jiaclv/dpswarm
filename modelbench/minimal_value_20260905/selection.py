"""Finite development selection; missing evidence blocks only possible winners."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import statistics

from .contracts import ARMS


ROUTES = ('quality', 'economy')
TEAM_ARMS = ('D', 'T')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _conjunction(checks):
    """A known failure excludes a route even when another condition is unknown."""
    if any(value is False for value in checks):
        return False
    return None if any(value is None for value in checks) else True


def _within(value, baseline, multiplier):
    # Preserve the protocol's undefined zero-baseline resource comparison.
    if value is None or baseline is None or baseline == 0:
        return None
    return value <= multiplier * baseline


def _winner(eligible, metrics):
    """Apply the frozen success, D simplicity, cost, latency, arm-ID order."""
    def rank(arm):
        value = metrics[arm]
        return (-value['successes'], value['mean_cost'], value['median_wall'], ARMS.index(arm))
    selected = min(eligible, key=rank)
    if set(eligible) == set(TEAM_ARMS):
        d, t = metrics['D'], metrics['T']
        if (d['successes'] == t['successes'] and d['mean_cost'] <= 1.1 * t['mean_cost']
                and d['median_wall'] <= 1.1 * t['median_wall']):
            selected = 'D'
    return selected


def _can_displace(challenger, incumbent, metrics):
    """For either D or T, reducing its cost/latency cannot harm its rank.

    Zero is the nonnegative lower bound for an unknown aggregate. If even this
    optimistic completion loses the frozen ordering, its missing evidence
    cannot change the winner. This is deliberately conservative: it does not
    guess the unobserved fee or latency or report zero as a measurement.
    """
    optimistic = {arm: dict(value) for arm, value in metrics.items()}
    for key in ('mean_cost', 'median_wall'):
        if optimistic[challenger][key] is None:
            optimistic[challenger][key] = 0.0
    return _winner((challenger, incumbent), optimistic) == challenger


def choose(entries):
    """Qualify each candidate, then select only when unresolved routes cannot win."""
    grouped = defaultdict(list)
    for row in entries:
        grouped[row['arm']].append(row)
    if set(grouped) != set(ARMS) or any(len(grouped[arm]) != 16 for arm in ARMS):
        return {'status': 'incomplete'}
    tasks = {row['instance_id']: row['repo'] for row in grouped['S']}
    if len(tasks) != 16 or any(len({row['instance_id'] for row in grouped[arm]}) != 16
            or {row['instance_id']: row['repo'] for row in grouped[arm]} != tasks for arm in ARMS):
        return {'status': 'unpaired_or_duplicate_tasks'}
    for arm in ARMS:
        for row in grouped[arm]:
            if type(row.get('end_to_end_success')) not in (int, bool) or row['end_to_end_success'] not in (0, 1):
                return {'status': 'invalid_metrics', 'arm': arm, 'field': 'end_to_end_success'}
            for key in ('api_equivalent_usd', 'inference_wall_seconds'):
                if row.get(key) is not None and not _number(row[key]):
                    return {'status': 'invalid_metrics', 'arm': arm, 'field': key}

    def summarize(arm):
        rows = grouped[arm]
        costs = [row.get('api_equivalent_usd') for row in rows]
        walls = [row.get('inference_wall_seconds') for row in rows]
        return {'successes': sum(row['end_to_end_success'] for row in rows),
                'mean_cost': statistics.mean(costs) if all(value is not None for value in costs) else None,
                'median_wall': statistics.median(walls) if all(value is not None for value in walls) else None}
    metrics = {arm: summarize(arm) for arm in ARMS}
    solo_arms = ('S', 'L', 'R2')
    best_successes = max(metrics[arm]['successes'] for arm in solo_arms)
    tied = [arm for arm in solo_arms if metrics[arm]['successes'] == best_successes]
    use_cost = all(metrics[arm]['mean_cost'] is not None for arm in tied)
    use_wall = all(metrics[arm]['median_wall'] is not None for arm in tied)
    solo = min(tied, key=lambda arm: ((metrics[arm]['mean_cost'] if use_cost else 0),
               (metrics[arm]['median_wall'] if use_wall else 0), ARMS.index(arm)))
    base = metrics[solo]
    baseline_by_task = {row['instance_id']: row for row in grouped[solo]}
    states, qualifications, positive_repos = {}, {}, {}
    for arm in TEAM_ARMS:
        value = metrics[arm]
        deltas = Counter()
        for row in grouped[arm]:
            deltas[row['repo']] += row['end_to_end_success'] - baseline_by_task[row['instance_id']]['end_to_end_success']
        positive_repos[arm] = sum(delta > 0 for delta in deltas.values())
        states[arm] = {
            'quality': _conjunction((value['successes'] >= base['successes'] + 2,
                positive_repos[arm] >= 2, _within(value['mean_cost'], base['mean_cost'], 1.25),
                _within(value['median_wall'], base['median_wall'], 1.3))),
            'economy': _conjunction((base['successes'] > 0, value['successes'] == base['successes'],
                _within(value['mean_cost'], base['mean_cost'], .8),
                _within(value['median_wall'], base['median_wall'], 1.3))),
        }
        qualifications[arm] = next((route for route in ROUTES if states[arm][route] is True),
            'undecidable' if any(state is None for state in states[arm].values()) else 'not_qualified')

    selected, selected_route, status, blockers = None, None, 'not_qualified', []
    for route in ROUTES:
        qualified = [arm for arm in TEAM_ARMS if states[arm][route] is True]
        unresolved = [arm for arm in TEAM_ARMS if states[arm][route] is None]
        if qualified:
            incumbent = _winner(qualified, metrics)
            blockers = [arm for arm in unresolved if _can_displace(arm, incumbent, metrics)]
            if blockers:
                status = 'undecidable'
            else:
                selected, selected_route, status = incumbent, route, 'development_candidate_selected'
            break
        if unresolved:
            # An unresolved quality route can supersede a fully known economy
            # result. A later, lower-priority route must therefore not win yet.
            blockers, status = unresolved, 'undecidable'
            break
    return {'status': status, 'S_star': solo, 'C_star': selected, 'route': selected_route,
            'qualifications': qualifications, 'metrics': metrics, 'confirmatory': False,
            'possible_routes': {arm: [route for route in ROUTES if states[arm][route] is not False]
                                for arm in TEAM_ARMS},
            'route_evidence': {arm: {route: ('qualified' if value is True else 'undecidable' if value is None
                                  else 'not_qualified') for route, value in states[arm].items()} for arm in TEAM_ARMS},
            'selection_blockers': blockers, 'positive_net_repositories': positive_repos,
            'S_star_ranking': {'success_tied_arms': tied, 'cost_layer_used': use_cost,
                              'wall_layer_used': use_wall}}
