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
    zero_allowed = {'lead_reserve_calls', 'cm_call_allowance', 'cm_reservation_slack'}
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
