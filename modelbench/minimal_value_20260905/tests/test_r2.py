from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from modelbench.minimal_value_20260905.budget import EpisodeBudget, LedgerError
from modelbench.minimal_value_20260905.r2 import R2Run, fallback
from modelbench.minimal_value_20260905.selector import RestrictedSelector, FrozenCandidate, CandidateIntegrityError, sha_bytes


PATCHES = {'candidate_1': b'patch-one\n', 'candidate_2': b'patch-two\n'}


def entry():
    return {'run_id': 'r2-contract', 'episode_id': 'root-episode', 'arm': 'R2',
            'lead_model': 'gpt-5.6-sol', 'expected_candidates': 2,
            'instance': {'instance_id': 'public-task', 'problem_statement': 'Fix public behavior',
                         'repo': 'owner/repo', 'base_commit': 'base', 'version': '1'},
            'public_checks': {'public': 'python -m pytest tests/test_public.py'},
            'limits_override': {'max_calls': 28, 'token_limit': 600000, 'wall_seconds': 1800,
                                'cm_call_allowance': 12},
            'grader_contract': {'hidden_marker': 'DO-NOT-DISCLOSE'}}


class Scratch:
    instances = []
    def __init__(self, instance, directory, **kwargs):
        assert 'grader_contract' not in kwargs
        self.instance, self.directory, self.patch = instance, Path(directory), None
        self.operations = []
        self.__class__.instances.append(self)
    def start(self):
        self.operations.append('start')
    def apply_patch(self, patch):
        self.operations.append('apply')
        self.patch = patch
    def run(self, command, timeout):
        assert command == 'python -m pytest tests/test_public.py'
        assert 0 < timeout <= 120
        self.operations.append(command)
        self.patch = 'scratch may mutate; never export'
        return {'exit_code': 0, 'stdout': 'PUBLIC PASS', 'stderr': '',
                'private_score': 'DO-NOT-DISCLOSE', 'timed_out': False}
    def quiesce(self):
        self.operations.append('quiesce')
        return {'quiesced': True}
    def close(self):
        self.operations.append('close')
        return {'closed': True, 'removed': True, 'errors': []}
    def grade(self, *args, **kwargs):
        raise AssertionError('Official grading was exposed')


class SelectorTransport:
    def __init__(self, directory):
        self.directory, self.ordinal = Path(directory), 0
    def complete(self, model, messages, *, tools, **kwargs):
        assert model == 'gpt-5.6-sol'
        assert kwargs['max_tokens'] == 4096
        assert 'DO-NOT-DISCLOSE' not in json.dumps(messages)
        names = {t['function']['name'] for t in tools}
        assert names == {'read_patch', 'read_public_evidence', 'verify_candidate', 'select_candidate'}
        self.ordinal += 1
        if self.ordinal == 1:
            calls = [{'name': 'verify_candidate', 'arguments': {'candidate_id': key, 'check_id': 'public'}}
                     for key in PATCHES]
        else:
            calls = [{'name': 'select_candidate', 'arguments': {'candidate_id': 'candidate_2',
                      'sha256': sha_bytes(PATCHES['candidate_2']), 'reason': 'Public checks and patch review'}}]
        return {'call_id': kwargs['call_id'], 'role': 'selector', 'total_tokens': 50,
                'action': {'kind': 'tools', 'calls': calls}, 'assistant_message': {'role': 'assistant', 'content': 'selecting'}}


def fake_run_factory(barrier, captured, *, cleanup=True, applicable=None):
    class Candidate:
        def __init__(self, batch_dir, candidate_entry, **kwargs):
            self.entry, self.kwargs = candidate_entry, kwargs
            self.cancel = threading.Event()
            self.folder = Path(batch_dir) / 'results' / candidate_entry['run_id']
            self.folder.mkdir(parents=True)
            captured.append(self)
        def run(self):
            assert self.kwargs['grade_enabled'] is False
            scope = self.entry['scope_id']
            assert self.entry['condition'] == 'solo' and self.entry['worker_specs'] == []
            assert self.entry['limits_override']['token_limit'] == 270000
            budget = self.kwargs['budget']
            budget.reserve(scope, 'lead', 200)
            barrier.wait(timeout=5)
            budget.complete(scope, {'call_id': scope, 'role': 'lead', 'total_tokens': 100})
            budget.reserve(scope + '-cm', 'cm', 100)
            budget.complete(scope + '-cm', {'call_id': scope + '-cm', 'role': 'cm', 'total_tokens': 20})
            patch = PATCHES[scope]
            path = self.folder / 'model.patch'
            path.write_bytes(patch)
            return {'artifact': {'status': 'present', 'path': str(path), 'bytes': len(patch),
                                 'sha256': sha_bytes(patch), 'applicable': (applicable or {}).get(scope, True)},
                    'cleanup_confirmed': cleanup, 'quiesced': cleanup, 'score': None,
                    'public_evidence': {'summary': 'Public candidate evidence'}}
    return Candidate


def run_options(tmp_path, **extra):
    captured = []
    options = {'run_factory': fake_run_factory(threading.Barrier(2), captured),
               'transport_factory': SelectorTransport, 'environment_factory': Scratch,
               'model_slots': threading.Semaphore(2), 'container_slots': threading.Semaphore(2),
               'resource_failure': threading.Event()}
    options.update(extra)
    return options, captured


def candidates(tmp_path, *, eligible=True):
    result = []
    for key, patch in PATCHES.items():
        path = tmp_path / (key + '.patch')
        path.write_bytes(patch)
        result.append(FrozenCandidate(key, path, sha_bytes(patch), len(patch), eligible, {'summary': 'public'}))
    return result


def selector(tmp_path, *, transport=SelectorTransport, env=Scratch, clock=time.monotonic):
    budget = EpisodeBudget(clock=clock)
    return RestrictedSelector(directory=tmp_path / 'selector', entry=entry(), candidates=candidates(tmp_path),
        budget=budget.scope('selector'), deadline=clock() + 1800, transport_factory=transport,
        environment_factory=env, clock=clock), budget


def test_parallel_independent_candidates_select_original_bytes_and_charge_once(tmp_path):
    Scratch.instances = []
    options, captured = run_options(tmp_path)
    run = R2Run(tmp_path, entry(), **options)
    result = run.run()
    assert len(captured) == 2
    assert len({str(c.folder) for c in captured}) == 2
    assert {c.kwargs['deadline'] for c in captured} == {run.deadline}
    assert {c.kwargs['start_clock'] for c in captured} == {run.start_clock}
    assert result['infrastructure_error'] is None
    assert result['selection_source'] == 'selector'
    assert result['selection']['selected'] == 'candidate_2'
    assert Path(result['artifact']['path']).read_bytes() == PATCHES['candidate_2']
    assert result['budget']['call_count'] == 6
    assert result['budget']['cm_call_count'] == 2
    assert result['budget']['known_subtotal'] == 340
    assert result['call_count'] == 4 and result['cm_call_count'] == 2
    assert result['total_tokens'] == 300 and result['cm_usage']['total_tokens'] == 40
    assert sum(v['total_tokens'] for v in result['agent_usage'].values()) == 300
    assert result['budget']['scope_summaries']['selector']['known_subtotal'] == 100
    assert result['score'] is None and result['grading_pending'] is True
    assert result['cleanup_confirmed'] and result['quiesced']
    assert all(c.cancel.is_set() is False for c in captured)
    assert len(Scratch.instances) == 2
    assert all(s.operations[-2:] == ['quiesce', 'close'] for s in Scratch.instances)
    assert all(s.patch.startswith('scratch may mutate') for s in Scratch.instances)


def test_selector_cannot_execute_arbitrary_command_or_use_hidden_grader(tmp_path):
    s, budget = selector(tmp_path)
    with pytest.raises(ValueError, match='Undeclared'):
        s.execute_tool('bash', {'command': 'arbitrary'})
    with pytest.raises(ValueError, match='Unexpected'):
        s.execute_tool('verify_candidate', {'candidate_id': 'candidate_1', 'check_id': 'public', 'command': 'arbitrary'})
    with pytest.raises(ValueError, match='whitelist'):
        s.execute_tool('verify_candidate', {'candidate_id': 'candidate_1', 'check_id': 'official'})
    result = s.execute_tool('verify_candidate', {'candidate_id': 'candidate_1', 'check_id': 'public'})
    assert result['official_grading'] is False
    assert 'private_score' not in result and 'DO-NOT-DISCLOSE' not in json.dumps(result)
    assert s.execute_tool('verify_candidate', {'candidate_id': 'candidate_1', 'check_id': 'public'})['cached']
    assert len(s.verifications) == 1
    assert budget.summary()['call_count'] == 0


def test_fallback_uses_only_saved_nonempty_applicability_and_stable_order(tmp_path):
    c = candidates(tmp_path)
    ineligible = FrozenCandidate(c[0].candidate_id, c[0].path, c[0].sha256, c[0].size, None, {'claims_success': True})
    assert fallback([c[1], ineligible])['selected'] == 'candidate_2'
    assert fallback([ineligible])['selected'] is None
    assert fallback([])['sha256'] == sha_bytes(b'')
    assert fallback(list(reversed(c)))['selected'] == 'candidate_1'


def test_frozen_hash_and_selection_binding_fail_closed(tmp_path):
    s, _ = selector(tmp_path)
    with pytest.raises(ValueError, match='hash'):
        s.execute_tool('select_candidate', {'candidate_id': 'candidate_1', 'sha256': 'fake'})
    s.candidates['candidate_1'].path.write_bytes(b'tampered')
    with pytest.raises(CandidateIntegrityError, match='changed'):
        s.execute_tool('read_patch', {'candidate_id': 'candidate_1'})


def test_noop_selector_exhausts_exactly_four_calls_then_falls_back(tmp_path):
    class Noop(SelectorTransport):
        def complete(self, model, messages, **kwargs):
            return {'call_id': kwargs['call_id'], 'role': 'selector', 'total_tokens': 10,
                    'action': {'kind': 'final', 'text': 'cannot decide'}}
    options, _ = run_options(tmp_path, transport_factory=Noop)
    result = R2Run(tmp_path, entry(), **options).run()
    assert result['selector']['status'] == 'call_limit'
    assert result['selector']['budget']['call_count'] == 4
    assert result['selection_source'] == 'fallback'
    assert result['selection']['selected'] == 'candidate_1'
    assert result['selector']['output_limit_is_admission_assumption'] is True
    assert result['grading_pending'] is True


def test_unconfirmed_candidate_cleanup_blocks_selector_and_grading(tmp_path):
    captured = []
    options, _ = run_options(tmp_path, run_factory=fake_run_factory(threading.Barrier(2), captured, cleanup=False))
    result = R2Run(tmp_path, entry(), **options).run()
    assert result['selector'] is None
    assert result['infrastructure_error']
    assert result['cleanup_confirmed'] is False and result['grading_pending'] is False
    assert Path(result['artifact']['path']).read_bytes() == b''


def test_selector_environment_failure_is_infrastructure_error_not_successful_fallback(tmp_path):
    class BrokenScratch(Scratch):
        def run(self, command, timeout):
            raise OSError('host scratch failed')
    options, _ = run_options(tmp_path, environment_factory=BrokenScratch)
    result = R2Run(tmp_path, entry(), **options).run()
    assert result['selector']['infrastructure_error']
    assert result['infrastructure_error'] and result['grading_pending'] is False
    assert Path(result['artifact']['path']).read_bytes() == b''


def test_expired_selector_admits_no_call(tmp_path):
    now = [100.0]
    s, budget = selector(tmp_path, clock=lambda: now[0])
    now[0] = 1900
    result = s.run()
    assert result['status'] == 'budget_or_deadline'
    assert result['infrastructure_error'] is None
    assert budget.summary()['call_count'] == 0


def test_wrong_root_allocation_is_rejected_before_episode_creation(tmp_path):
    spec = entry()
    spec['limits_override']['token_limit'] = 700000
    options, _ = run_options(tmp_path)
    with pytest.raises(ValueError, match='allocation is frozen'):
        R2Run(tmp_path, spec, **options)
    assert not (tmp_path / 'results').exists()


def test_selector_bad_hash_is_model_protocol_failure_and_keeps_fallback_eligible(tmp_path):
    class WrongHash(SelectorTransport):
        def complete(self, model, messages, **kwargs):
            return {'call_id': kwargs['call_id'], 'role': 'selector', 'total_tokens': 10,
                    'action': {'kind': 'tools', 'calls': [{'name': 'select_candidate',
                       'arguments': {'candidate_id': 'candidate_2', 'sha256': 'wrong'}}]}}
    options, _ = run_options(tmp_path, transport_factory=WrongHash)
    result = R2Run(tmp_path, entry(), **options).run()
    assert result['selector']['status'] == 'call_limit'
    assert result['infrastructure_error'] is None and result['grading_pending'] is True
    assert result['selection_source'] == 'fallback'
    assert result['selection']['selected'] == 'candidate_1'


def test_selector_false_cleanup_report_fails_closed(tmp_path):
    class Unconfirmed(Scratch):
        def close(self):
            return {'closed': False, 'removed': False, 'errors': []}
    options, _ = run_options(tmp_path, environment_factory=Unconfirmed)
    result = R2Run(tmp_path, entry(), **options).run()
    assert result['cleanup_confirmed'] is False
    assert result['grading_pending'] is False and result['infrastructure_error']


def test_actual_value_run_integrates_with_shared_r2_ledger_without_scoring(tmp_path, monkeypatch):
    from modelbench.minimal_value_20260905 import runner
    from modelbench.minimal_value_20260905.tests.test_strategies import PRODUCTION
    monkeypatch.setattr(runner, 'MODEL_SLOTS', threading.BoundedSemaphore(2))
    monkeypatch.setattr(runner, 'CONTAINER_SLOTS', threading.BoundedSemaphore(3))
    monkeypatch.setattr(runner, 'RESOURCE_FAILURE', threading.Event())
    barrier = threading.Barrier(2)
    trace = []
    class FullEnvironment(Scratch):
        def __init__(self, instance, directory, **kwargs):
            kwargs.pop('grader_contract', None)
            super().__init__(instance, directory, **kwargs)
            self.patch = ''
            self.quiet = False
        def run(self, command, timeout):
            if command == 'edit-production':
                self.patch += PRODUCTION
                return {'exit_code': 0, 'stdout': 'edited', 'stderr': ''}
            return super().run(command, timeout)
        def observe_worktree(self):
            return {'nonempty_delta': bool(self.patch), 'state_sha256': sha_bytes(self.patch.encode()),
                    'baseline_sha256': sha_bytes(b''), 'changed_files': {}, 'measurement': 'offline'}
        def snapshot_patch(self, delta=False):
            return self.patch
        def quiesce(self):
            self.quiet = True
            trace.append(('quiet', str(self.directory)))
            return super().quiesce()
        def export_patch(self, delta=False):
            assert self.quiet
            return self.patch
        def check_frozen_patch(self, patch):
            assert self.quiet
            return {'applicable': True, 'patch_sha256': sha_bytes(patch.encode()), 'baseline_commit': 'base'}
    class FullTransport(SelectorTransport):
        def complete(self, model, messages, *, role, call_id, **kwargs):
            if role == 'lead':
                barrier.wait(timeout=5)
                calls = [{'id': 'edit', 'name': 'bash', 'arguments': {'command': 'edit-production'}},
                         {'id': 'finish', 'name': 'finish', 'arguments': {'status': 'completed', 'summary': 'fixture'}}]
            else:
                assert role == 'selector'
                assert len([kind for kind, _ in trace if kind == 'quiet']) == 2
                calls = [{'id': 'choose', 'name': 'select_candidate', 'arguments': {
                    'candidate_id': 'candidate_2', 'sha256': sha_bytes(PRODUCTION.encode())}}]
            return {'call_id': call_id, 'role': role, 'model_requested': model,
                    'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                    'cached_input_tokens': 30, 'reasoning_tokens': 5, 'transport_attempt_count': 1,
                    'wall_seconds': .01, 'error': None, 'protocol_error': None,
                    'assistant_message': {'role': 'assistant', 'content': 'fixture'},
                    'action': {'kind': 'tools', 'calls': calls}}
    result = R2Run(tmp_path, entry(), run_factory=runner.ValueRun,
                   transport_factory=FullTransport, environment_factory=FullEnvironment).run()
    assert result['infrastructure_error'] is None, result['infrastructure_error']
    assert result['grading_pending'] is True and result['score'] is None
    assert result['candidate_count'] == 2 and result['call_count'] == 3
    assert result['budget']['call_count'] == 3 and result['budget']['known_subtotal'] == 360
    assert result['transport_attempt_count'] == 3
    assert all(value['score'] is None for value in result['candidates'].values())
    assert Path(result['artifact']['path']).read_text() == PRODUCTION
