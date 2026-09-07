"""One read-only Coding Plan quota check per idle wave, never per model call.

The controller owns finite waits and admission order. This is a local planning
bound, not a reservation at the provider: other applications can spend the same
account quota. No model calls, retry, fallback, credential persistence or logging.
Official reference: https://docs.bigmodel.cn/cn/coding-plan/overview and the
zai-org/zai-coding-plugins glm-plan-usage query-usage.mjs implementation.
"""
from __future__ import annotations

import http.client
import json
import math
import time
from decimal import Decimal, ROUND_CEILING

QUOTA_ENDPOINT = 'https://open.bigmodel.cn/api/monitor/usage/quota/limit'
QUOTA_HOST = 'open.bigmodel.cn'
QUOTA_PATH = '/api/monitor/usage/quota/limit'
PROTOCOL = 'coding_plan_idle_wave_quota_v1'
MAX_BODY_BYTES = 65536
MAX_OBSERVATION_AGE_MS = 120000
GLM_CREDIT_WEIGHTS = {'glm-5.3': 24, 'glm-5.3-flash': 8}
MAPPING_BASIS = ('observed CREDIT_LIMIT unit3/number5 and unit6/number1 matched '
                 'official personal-plan five-hour and weekly credit table; '
                 'unit enum is not a published API contract')


def _now_ms():
    return int(time.time() * 1000)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _business_code(payload):
    code = payload.get('code') if isinstance(payload, dict) else None
    value = str(code)
    return value if value.isascii() and value.isdigit() and len(value) <= 8 else None


def _request(api_key, timeout_seconds):
    # Fixed HTTPS host/path and raw Authorization exactly as in the official
    # plugin. http.client does not follow redirects. Error bodies never escape.
    connection = http.client.HTTPSConnection(QUOTA_HOST, timeout=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    try:
        connection.request('GET', QUOTA_PATH, headers={
            'Authorization': api_key, 'Accept-Language': 'en-US,en',
            'Accept': 'application/json', 'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(4096, MAX_BODY_BYTES + 1 - len(body)))
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ValueError('response_size_limit')
            if not chunk:
                break
        return response.status, bytes(body)
    finally:
        connection.close()


def _window(item):
    if not isinstance(item, dict) or item.get('type') != 'CREDIT_LIMIT':
        raise ValueError('unsupported_limit')
    if type(item.get('unit')) is not int or type(item.get('number')) is not int:
        raise ValueError('invalid_window_identity')
    identity = (item['unit'], item['number'])
    if identity not in ((3, 5), (6, 1)):
        raise ValueError('unknown_credit_window')
    for name in ('usage', 'remaining', 'currentValue', 'percentage'):
        if not _number(item.get(name)) or item[name] < 0:
            raise ValueError('invalid_credit_value')
    if (item['usage'] <= 0 or item['remaining'] > item['usage']
            or item['currentValue'] > item['usage'] or item['percentage'] > 100):
        raise ValueError('invalid_credit_range')
    if type(item.get('nextResetTime')) is not int or item['nextResetTime'] <= 0:
        raise ValueError('missing_reset_time')
    # Preserve provider rounding exactly, including its occasional one-credit
    # difference between usage and currentValue + remaining.
    fields = ('type', 'unit', 'number', 'usage', 'currentValue', 'remaining',
              'percentage', 'nextResetTime')
    return ('five_hour' if identity == (3, 5) else 'weekly'), {k: item[k] for k in fields}


def inspect_quota(api_key, timeout_seconds=20, *, request=None, now_ms=None):
    """Return only allowlisted fields; errors never include a body/key/message.

    request is an offline-test seam returning (HTTP status, bytes), and receives
    the same key and timeout as the fixed-host request. There is one HTTP attempt.
    """
    observed = _now_ms() if now_ms is None else now_ms
    result = {'protocol': PROTOCOL, 'endpoint': QUOTA_ENDPOINT,
              'observed_at_ms': observed, 'available': False, 'windows': {},
              'http_status': None, 'provider_business_code': None,
              'reason': 'quota_query_unavailable', 'mapping_basis': MAPPING_BASIS}
    if not isinstance(api_key, str) or not api_key.strip() or '\r' in api_key or '\n' in api_key:
        result['reason'] = 'missing_or_invalid_coding_api_key'
        return result
    if not _number(timeout_seconds) or not 0 < timeout_seconds <= 30:
        result['reason'] = 'invalid_quota_timeout'
        return result
    try:
        status, body = (request or _request)(api_key, timeout_seconds)
        result['http_status'] = status if type(status) is int else None
        if not isinstance(body, (str, bytes)) or len(body) > MAX_BODY_BYTES:
            result['reason'] = 'invalid_quota_body'
            return result
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            result['reason'] = 'invalid_quota_json'
            return result
        result['provider_business_code'] = _business_code(payload)
        if status != 200 or result['provider_business_code'] != '200':
            result['reason'] = 'quota_http_or_business_error'
            return result
        if not isinstance(payload, dict) or payload.get('success') is False:
            result['reason'] = 'quota_business_error'
            return result
        data = payload.get('data')
        limits = data.get('limits') if isinstance(data, dict) else None
        if not isinstance(limits, list) or not limits:
            result['reason'] = 'missing_quota_limits'
            return result
        windows = {}
        for item in limits:
            # Legacy MCP-only meter is not used by these coding experiments.
            if isinstance(item, dict) and item.get('type') == 'TIME_LIMIT':
                continue
            name, value = _window(item)
            if name in windows:
                raise ValueError('duplicate_credit_window')
            windows[name] = value
        if set(windows) != {'five_hour', 'weekly'}:
            raise ValueError('missing_credit_window')
        result.update(available=True, reason='quota_observed', windows=windows)
    except (OSError, http.client.HTTPException):
        result['reason'] = 'quota_network_error'
    except (ValueError, TypeError, KeyError, OverflowError):
        result['reason'] = 'unsupported_or_invalid_quota_schema'
    return result


def wave_credit_requirement(wave):
    """Conservative whole-episode token cap times highest GLM credit weight.

    The episode cap is shared across roles/models, so multiplying the same cap
    once per GLM worker would double-count it. Output is the largest published
    coefficient for either GLM model; no off-peak discount is assumed.
    """
    credits = Decimal(0)
    for entry in wave:
        models = [entry.get('lead_model')]
        models.extend(spec.get('model') for spec in entry.get('worker_specs', []))
        models.extend(entry.get('worker_models') or [])
        limits = entry.get('limits_override') or {}
        models.append(limits.get('cm_model'))
        weights = [GLM_CREDIT_WEIGHTS[model] for model in models if model in GLM_CREDIT_WEIGHTS]
        if not weights:
            continue
        cap = limits.get('token_limit')
        if type(cap) is not int or cap <= 0:
            raise ValueError('GLM episode requires a positive integer token admission cap')
        credits += Decimal(cap) * max(weights) / Decimal(10000)
    return int(credits.to_integral_value(rounding=ROUND_CEILING))


def assess_wave(snapshot, wave, *, now_ms=None):
    required = wave_credit_requirement(wave)
    result = {'status': 'unavailable', 'required_credits': required,
              'next_reset_ms': None, 'insufficient_windows': [],
              'reason': 'quota_query_unavailable'}
    if required == 0:
        return {**result, 'status': 'ready', 'reason': 'wave_has_no_glm'}
    if not isinstance(snapshot, dict) or snapshot.get('available') is not True:
        return result
    now = _now_ms() if now_ms is None else now_ms
    observed = snapshot.get('observed_at_ms')
    if type(observed) is not int or not 0 <= now - observed <= MAX_OBSERVATION_AGE_MS:
        return {**result, 'reason': 'quota_observation_stale_or_invalid'}
    windows = snapshot.get('windows')
    if not isinstance(windows, dict) or set(windows) != {'five_hour', 'weekly'}:
        return {**result, 'reason': 'unsupported_or_invalid_quota_schema'}
    insufficient = []
    try:
        for name in ('five_hour', 'weekly'):
            actual, window = _window(windows[name])
            if actual != name:
                raise ValueError('window_identity_mismatch')
            if required > window['usage']:
                return {**result, 'reason': 'wave_exceeds_window_capacity',
                        'insufficient_windows': [name]}
            if required > window['remaining']:
                insufficient.append(name)
    except (ValueError, TypeError, KeyError):
        return {**result, 'reason': 'unsupported_or_invalid_quota_schema'}
    if not insufficient:
        return {**result, 'status': 'ready', 'reason': 'credit_upper_bound_covered'}
    # Recheck at the earliest deficient window reset; do not infer that a
    # rolling reset will replenish the full five-hour allowance.
    reset = min(windows[name]['nextResetTime'] for name in insufficient)
    return {**result, 'status': 'wait', 'reason': 'insufficient_coding_credits',
            'next_reset_ms': max(reset, now + 60000), 'insufficient_windows': insufficient}
