"""Offline fixtures only: these tests never use a key or reach an API."""
from copy import deepcopy
import json
import pytest
from modelbench.big_budget_team_20260906 import coding_quota as q

NOW = 1800000000000
FAKE_KEY = 'fixture-secret-not-a-live-key'


def payload():
    return {'code': 200, 'success': True, 'data': {'limits': [
        {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'usage': 28000,
         'currentValue': 0, 'remaining': 27999, 'percentage': 1,
         'nextResetTime': NOW + 3600000},
        {'type': 'CREDIT_LIMIT', 'unit': 6, 'number': 1, 'usage': 140000,
         'currentValue': 59821, 'remaining': 80178, 'percentage': 42,
         'nextResetTime': NOW + 2 * 86400000}]}}


def snapshot(value=None):
    return q.inspect_quota(FAKE_KEY, now_ms=NOW,
        request=lambda key, timeout: (200, json.dumps(payload() if value is None else value)))


def episode(model='glm-5.3', tokens=2400000, workers=()):
    return {'lead_model': model, 'worker_specs': [{'model': m} for m in workers],
            'limits_override': {'token_limit': tokens}}


def test_current_credit_response_retains_provider_rounding_without_leaking_fields():
    value = payload()
    value['data']['api_key'] = FAKE_KEY
    value['data']['limits'][0]['message'] = FAKE_KEY
    result = snapshot(value)
    assert result['available'] is True
    assert result['windows']['five_hour']['remaining'] == 27999
    assert result['windows']['five_hour']['currentValue'] == 0
    assert result['windows']['five_hour']['percentage'] == 1
    assert result['windows']['weekly']['remaining'] == 80178
    assert FAKE_KEY not in json.dumps(result)


def test_official_raw_authorization_and_fixed_endpoint_without_model_request(monkeypatch):
    seen = []
    class Response:
        status = 200
        def __init__(self): self.chunks = [json.dumps(payload()).encode(), b'']
        def read1(self, limit): return self.chunks.pop(0)
    class Connection:
        sock = None
        def __init__(self, host, timeout): seen.append(('host', host, timeout))
        def request(self, method, path, headers): seen.append((method, path, headers))
        def getresponse(self): return Response()
        def close(self): seen.append(('closed',))
    monkeypatch.setattr(q.http.client, 'HTTPSConnection', Connection)
    result = q.inspect_quota(FAKE_KEY, now_ms=NOW)
    assert result['available']
    assert seen[0] == ('host', 'open.bigmodel.cn', 20)
    assert seen[1][0:2] == ('GET', '/api/monitor/usage/quota/limit')
    assert seen[1][2]['Authorization'] == FAKE_KEY
    assert 'Cookie' not in seen[1][2]
    assert len([x for x in seen if x[0] == 'GET']) == 1
    assert seen[-1] == ('closed',)


@pytest.mark.parametrize('status,code', [(401, 1002), (200, 401), (429, 1308), (302, 302)])
def test_http_and_business_errors_close_admission_without_body_or_key(status, code):
    called = []
    def request(key, timeout):
        called.append(1)
        return status, json.dumps({'code': code, 'msg': FAKE_KEY, 'data': {'api_key': FAKE_KEY}})
    result = q.inspect_quota(FAKE_KEY, request=request, now_ms=NOW)
    assert result['available'] is False and called == [1]
    assert result['provider_business_code'] == str(code)
    assert FAKE_KEY not in json.dumps(result)
    assert q.assess_wave(result, [episode()], now_ms=NOW)['status'] == 'unavailable'


def test_timeout_never_retries_or_reports_exception_payload():
    calls = []
    def request(key, timeout):
        calls.append(1)
        raise TimeoutError(FAKE_KEY)
    result = q.inspect_quota(FAKE_KEY, request=request, now_ms=NOW)
    assert result['reason'] == 'quota_network_error' and calls == [1]
    assert FAKE_KEY not in json.dumps(result)


@pytest.mark.parametrize('change', ['missing_week', 'duplicate', 'unknown_window',
    'tokens_instead_of_credits', 'negative', 'nan', 'missing_reset', 'string_credits'])
def test_unrecognized_quota_never_becomes_a_zero_or_fabricated_balance(change):
    value = payload(); limits = value['data']['limits']
    if change == 'missing_week': limits.pop()
    elif change == 'duplicate': limits.append(deepcopy(limits[0]))
    elif change == 'unknown_window': limits[0]['unit'] = 42
    elif change == 'tokens_instead_of_credits': limits[0]['type'] = 'TOKENS_LIMIT'
    elif change == 'negative': limits[0]['remaining'] = -1
    elif change == 'nan': limits[0]['remaining'] = float('nan')
    elif change == 'missing_reset': limits[0].pop('nextResetTime')
    else: limits[0]['remaining'] = '27999'
    result = snapshot(value)
    assert result['available'] is False and result['windows'] == {}


def test_one_shared_episode_cap_is_not_multiplied_by_worker_count():
    flash = episode('glm-5.3-flash', workers=('glm-5.3-flash', 'glm-5.3-flash'))
    assert q.wave_credit_requirement([flash]) == 1920
    assert q.wave_credit_requirement([episode('gpt-5.6-sol', workers=('glm-5.3',))]) == 5760
    assert q.wave_credit_requirement([episode('gpt-5.6-sol')]) == 0
    assert q.wave_credit_requirement([episode(tokens=1)]) == 1


def test_saved_full_condition_block_fits_official_max_capacity_without_discount():
    from modelbench.big_budget_team_20260906.tests.test_execution import make_entries
    entries = make_entries()
    assert q.wave_credit_requirement(entries[:10]) == 27360
    assert q.wave_credit_requirement(entries[7:10]) == 9120
    assert q.assess_wave(snapshot(), entries[:10], now_ms=NOW)['status'] == 'ready'


def test_insufficient_balance_waits_and_does_not_assume_reset_fully_replenishes():
    observed = snapshot(); observed['windows']['five_hour']['remaining'] = 100
    result = q.assess_wave(observed, [episode()], now_ms=NOW)
    assert result['status'] == 'wait' and result['required_credits'] == 5760
    assert result['next_reset_ms'] == NOW + 3600000
    assert result['insufficient_windows'] == ['five_hour']
    observed['windows']['five_hour']['nextResetTime'] = NOW - 1
    assert q.assess_wave(observed, [episode()], now_ms=NOW)['next_reset_ms'] == NOW + 60000


def test_weekly_limit_independently_blocks_admission():
    observed = snapshot(); observed['windows']['weekly']['remaining'] = 100
    result = q.assess_wave(observed, [episode()], now_ms=NOW)
    assert result['status'] == 'wait' and result['insufficient_windows'] == ['weekly']
    assert result['next_reset_ms'] == NOW + 2 * 86400000


def test_impossible_wave_or_stale_observation_does_not_wait_forever():
    result = q.assess_wave(snapshot(), [episode(tokens=20000000)], now_ms=NOW)
    assert result['status'] == 'unavailable' and result['reason'] == 'wave_exceeds_window_capacity'
    result = q.assess_wave(snapshot(), [episode()], now_ms=NOW + 120001)
    assert result['status'] == 'unavailable' and result['reason'] == 'quota_observation_stale_or_invalid'


def test_non_glm_wave_does_not_require_quota():
    result = q.assess_wave(None, [episode('gpt-5.6-sol')], now_ms=NOW)
    assert result['status'] == 'ready' and result['required_credits'] == 0


@pytest.mark.parametrize('key', ['', None, 'fixture\nheader'])
def test_missing_or_invalid_key_does_not_make_request(key):
    result = q.inspect_quota(key, request=lambda *a: pytest.fail('unexpected request'), now_ms=NOW)
    assert result['available'] is False
