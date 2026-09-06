"""Bounded local persistence; these helpers never retry a provider operation."""
import json
import os
from pathlib import Path
import tempfile
import time


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # A retained unique diagnostic file cannot overwrite another writer.


def resolve_limits(defaults, override=None):
    """Validate effective runtime declarations before allocating any resources."""
    import math
    if override is None:
        override = {}
    if not isinstance(override, dict) or not set(override) <= set(defaults):
        raise ValueError('limits_override contains unknown limit keys')
    values = {**defaults, **override}
    zero_allowed = {'lead_reserve_calls', 'cm_call_allowance', 'cm_reservation_slack', 'active_workers', 'delegations'}
    for key, value in values.items():
        default = defaults[key]
        if key == 'cm_call_allowance' and value is None:
            continue
        if isinstance(default, bool):
            valid = type(value) is bool
        elif isinstance(default, (int, float)) or key == 'cm_call_allowance':
            numeric = type(value) in (int, float) and math.isfinite(value)
            valid = numeric and value >= (0 if key in zero_allowed else 0.000001)
            if key not in {'wall_seconds', 'call_timeout', 'command_timeout', 'question_timeout',
                           'cm_socket_timeout', 'cpus'}:
                valid = valid and type(value) is int
        elif isinstance(default, str):
            valid = isinstance(value, str) and bool(value.strip())
        else:
            valid = False
        if not valid:
            raise ValueError('Invalid effective limit: ' + key)
    for key in ('model_concurrency', 'container_concurrency', 'active_workers', 'delegations'):
        if values[key] != defaults[key]:
            raise ValueError(key + ' is fixed by the process or team protocol; per-entry override is unsupported')
    if values['lead_reserve_calls'] > values['max_calls']:
        raise ValueError('lead_reserve_calls exceeds max_calls')
    assembly = any(values[key] for key in ('cm_team_memory', 'cm_scout_distill', 'cm_bootstrap_package'))
    if assembly and not values['cm_enabled']:
        raise ValueError('CM assembly requires cm_enabled')
    if (values['cm_scout_distill'] or values['cm_bootstrap_package']) and not values['cm_team_memory']:
        raise ValueError('Scout distillation and bootstrap packages require cm_team_memory')
    return values


def strategy_entry(entry, defaults):
    """Validate explicit A0 topology before allocating files or resources."""
    from copy import deepcopy
    value = deepcopy(entry)
    arm = value.get('arm') or value.get('strategy_id')
    arm = {'R2_candidate': 'R2-candidate'}.get(arm, arm)
    if arm not in {'S', 'L', 'D', 'T', 'R2-candidate'}:
        if arm is not None:
            raise ValueError('Unknown minimal-value strategy')
        if value.get('condition') == 'solo':
            arm = 'L' if value.get('lead_model') == 'gpt-5.6-luna' else 'S'
        elif value.get('condition') in {'fixed_team', 'hetero_team'}:
            arm = 'T'
        else:
            raise ValueError('Unknown minimal-value strategy')
    lead = 'gpt-5.6-luna' if arm == 'L' else 'gpt-5.6-sol'
    if value.get('lead_model', lead) != lead:
        raise ValueError('Lead model disagrees with frozen strategy')
    roles = ([] if arm in {'S', 'L', 'R2-candidate'} else
             [('test', 'glm-5.3')] if arm == 'D' else
             [('implementation', 'gpt-5.6-terra'), ('test', 'glm-5.3')])
    specs = [{'worker_id': f'worker-{i + 1}', 'role': role, 'model': model}
             for i, (role, model) in enumerate(roles)]
    if 'worker_specs' in value and value['worker_specs'] != specs:
        raise ValueError('worker_specs disagrees with frozen strategy')
    models = [spec['model'] for spec in specs]
    if 'worker_models' in value and value['worker_models'] != models:
        raise ValueError('worker_models disagrees with frozen strategy')
    if value.get('worker_model') is not None:
        raise ValueError('Use ordered worker_specs rather than worker_model')
    if value.get('expected_workers', len(specs)) != len(specs):
        raise ValueError('expected_workers disagrees with worker_specs')
    condition = 'value_team' if specs else 'solo'
    allowed = {'value_team', 'fixed_team', 'hetero_team'} if specs else {'solo'}
    if value.get('condition', condition) not in allowed:
        raise ValueError('condition disagrees with strategy topology')
    limits = resolve_limits({**defaults, 'active_workers': len(specs), 'delegations': len(specs)},
                            value.get('limits_override'))
    fixed = {'cm_edit_curfew': False, 'edit_status_banner': False,
             'cm_team_memory': False, 'cm_scout_distill': False, 'cm_bootstrap_package': False,
             'closing_call_reserve_exempt': True}
    if any(limits[key] != setting for key, setting in fixed.items()):
        raise ValueError('A0 freezes F1/F5/assembly OFF and closing exemption ON')
    if value.get('effective_limits') is not None and value['effective_limits'] != limits:
        raise ValueError('effective_limits disagrees with frozen strategy')
    value.update(arm=arm, strategy_id=value.get('strategy_id', arm), condition=condition,
                 lead_model=lead, worker_models=models, worker_model=None,
                 worker_specs=specs, expected_workers=len(specs), effective_limits=limits)
    return value
