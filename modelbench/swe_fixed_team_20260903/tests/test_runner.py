"""Fixed-team protocol tests: real DPswarm CP, scripted calls and fake containers.

The grade spy proves terminal ordering only; it establishes no SWE task score.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from modelbench.swe_fixed_team_20260903 import runner
from modelbench.swe_verified_20260903.control import SweControl


BASELINE = "diff --git a/base.py b/base.py\n--- a/base.py\n+++ b/base.py\n@@ -1 +1 @@\n-old\n+base\n"
PRODUCTION = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n"
REGRESSION = "diff --git a/test_f.py b/test_f.py\n--- a/test_f.py\n+++ b/test_f.py\n@@ -1 +1 @@\n-old\n+test\n"
LEAD_PATCH = "diff --git a/lead.py b/lead.py\n--- a/lead.py\n+++ b/lead.py\n@@ -1 +1 @@\n-old\n+lead\n"


def action(name, **arguments):
    return {'id': name, 'name': name, 'arguments': arguments}


def done(status='completed'):
    return action('finish', status=status, summary='Offline fixture only')


def settle(worker_id, decision='adopt'):
    return [action('collect', worker_id=worker_id, wait_seconds=5),
            action('review_worker', worker_id=worker_id, decision=decision,
                   reason='Inspected offline delta and evidence')]


@pytest.fixture
def make_run(tmp_path, monkeypatch):
    created = []
    monkeypatch.setattr(runner, 'RESOURCE_FAILURE', threading.Event())
    monkeypatch.setattr(runner, 'MODEL_SLOTS', threading.BoundedSemaphore(4))
    monkeypatch.setattr(runner, 'CONTAINER_SLOTS', threading.BoundedSemaphore(4))

    def create(*, condition='fixed_team', model='glm-5.3', lead=None,
               workers=None, failure=None, baseline='', unknown_worker=None,
               attempt_counts=None):
        references, trace = {}, []
        scripts = {'lead': list(lead if lead is not None else [settle('worker-1') + settle('worker-2') + [done()]]),
                   'worker-1': [[action('bash', command='edit-production'), done()]],
                   'worker-2': [[action('bash', command='edit-regression'), done()]]}
        if workers:
            scripts.update(workers)

        class Transport:
            def __init__(self, folder):
                self.seen, self.records, self.lock = [], [], threading.Lock()

            def complete(self, requested_model, messages, *, role, run_id, task_id, call_id, tools, **kwargs):
                prompt = messages[0]['content']
                actor = 'lead' if role == 'lead' else ('worker-1' if 'Own the production-code' in prompt else 'worker-2')
                with self.lock:
                    run = references['run']
                    if condition == 'fixed_team':
                        assert run.bootstrap_admitted and len(run.workers) == 2
                        assert all(child.future is not None for child in run.workers.values())
                        # Both items must be genuinely in this CP tree before any model runs.
                        assert all(child.handle.item_id in run.control.cp.proj.work_items
                                   for child in run.workers.values())
                    assert scripts[actor], 'Unexpected call for ' + actor
                    response = scripts[actor].pop(0)
                    self.seen.append({'actor': actor, 'model': requested_model, 'messages': deepcopy(messages),
                                      'tools': deepcopy(tools), 'call_id': call_id})
                    trace.append(('model', actor))
                if callable(response):
                    response = response(run)
                error = response.get('error') if isinstance(response, dict) else None
                calls = [] if error else deepcopy(response)
                for ordinal, call in enumerate(calls):
                    call['id'] += '-' + str(ordinal)
                assistant = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': call['id'], 'type': 'function', 'function': {
                        'name': call['name'], 'arguments': json.dumps(call['arguments'])}} for call in calls]}
                unknown = actor == unknown_worker
                record = {'call_id': call_id, 'model_requested': requested_model, 'run_id': run_id,
                          'role': role, 'task_id': task_id, 'input_tokens': None if unknown else 100,
                          'output_tokens': None if unknown else 20, 'total_tokens': None if unknown else 120,
                          'cached_input_tokens': None if unknown else 30, 'reasoning_tokens': None if unknown else 5,
                          'wall_seconds': 0.01, 'error': error, 'assistant_message': assistant,
                          'action': {'kind': 'tools', 'calls': calls}, 'protocol_error': None,
                          'stop_reason': 'fixture',
                          'transport_attempt_count': (attempt_counts or {}).get(actor, 1)}
                with self.lock:
                    self.records.append(deepcopy(record))
                return record

        class Environment:
            instances, grade_calls, guard = [], 0, threading.Lock()

            def __init__(self, instance, run_dir, **kwargs):
                self.instance, self.run_dir = instance, Path(run_dir)
                self.actor = self.run_dir.parent.name if self.run_dir.parent.name.startswith('worker-') else 'lead'
                self.baseline, self.delta = baseline if self.actor == 'lead' else '', ''
                self.started = self.closed = False
                self.applied = []
                with self.guard:
                    self.instances.append(self)

            def start(self):
                self.started = True
                trace.append(('start', self.actor))
                return self

            def run(self, command, timeout):
                assert self.started and not self.closed
                patches = {'edit-production': PRODUCTION, 'edit-regression': REGRESSION, 'edit-lead': LEAD_PATCH}
                self.delta += patches.get(command, '')
                trace.append(('bash', self.actor))
                return {'exit_code': 0, 'stdout': 'offline', 'stderr': ''}

            def export_patch(self, delta=False):
                assert self.started
                return self.delta if delta else self.baseline + self.delta

            def fork(self, run_dir, *, baseline_patch):
                actor = Path(run_dir).parent.name
                if failure == 'fork-' + actor:
                    raise OSError('fixture fork failed for ' + actor)
                child = Environment(self.instance, run_dir)
                child.baseline = baseline_patch
                return child.start()

            def apply_patch(self, patch):
                assert self.started and not self.closed
                self.applied.append(patch)
                self.delta += patch
                return {'exit_code': 0}

            def close(self):
                trace.append(('close', self.actor))
                if failure == 'close-' + self.actor:
                    raise OSError('fixture close failed for ' + self.actor)
                self.closed = True

            def grade(self, patch, model_name):
                run = references['run']
                assert all(env.closed for env in self.instances)
                assert run.control._closed
                assert run.control.cp.proj.active_points == 0
                assert all(child.future.done() for child in run.workers.values())
                assert (run.folder / 'model.patch').read_bytes() == patch.encode()
                trace.append(('grade', 'official-spy'))
                Environment.grade_calls += 1
                return {'completed': True, 'resolved': False, 'test_only': True}

        def control_factory(*args, **kwargs):
            control = SweControl(*args, **kwargs)
            if failure == 'second-admission':
                original, admissions = control.delegate, []
                def delegate(caller, request):
                    admissions.append(request)
                    if len(admissions) == 2:
                        raise RuntimeError('fixture second CP admission failed')
                    return original(caller, request)
                control.delegate = delegate
            if failure == 'adoption-commit':
                def fail_commit(*args, **kwargs):
                    raise OSError('fixture CP adoption commit failed')
                control.decide = fail_commit
            return control

        instance = {'instance_id': 'sympy__sympy-12345', 'repo': 'sympy/sympy',
                    'problem_statement': 'Full original issue with rare constraint and exact requested behavior.'}
        entry = {'run_id': 'fixture-' + str(len(created)), 'condition': condition, 'instance': instance,
                 'arm': 'fixed_' + model if condition == 'fixed_team' else 'solo'}
        if condition == 'fixed_team':
            entry['worker_model'] = model
        run = runner.SweRun(tmp_path, entry, transport_factory=Transport,
                            environment_factory=Environment, control_factory=control_factory)
        references['run'] = run
        created.append(run)
        return run, Environment, trace

    yield create
    for run in created:
        run.cancel.set()
        for child in run.workers.values():
            child.cancel.set()
        run.pool.shutdown(wait=True)
        run.control.close('Offline fixture cleanup')


def events(run):
    return [json.loads(line) for line in (run.folder / 'events.jsonl').read_text(encoding='utf-8').splitlines()]


def assert_clean(run, env, result, count):
    assert result['infrastructure_error'] is None
    assert result['call_count'] == result['budget']['completed_call_count'] == count
    assert result['budget']['pending_call_count'] == 0
    assert result['cp_result']['usage']['total']['calls'] == count
    assert result['cp_result']['official_resolved'] is None
    assert result['score'] == {'completed': True, 'resolved': False, 'test_only': True}
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0
    assert run.control._closed and all(item.closed for item in env.instances)
    assert sum(value['calls'] for value in result['agent_usage'].values()) == count
    assert json.loads((run.folder / 'result.json').read_text(encoding='utf-8')) == result
    calls = [cid for usage in result['agent_usage'].values() for cid in usage['call_ids']]
    assert len(calls) == len(set(calls)) == count
    for usage in result['agent_usage'].values():
        by_node = result['cp_result']['usage']['by_node'][usage['handle']['node_id']]
        assert by_node['calls'] == usage['calls']
        for field in ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens', 'reasoning_tokens'):
            assert by_node[field] == usage[field]


def test_solo_has_no_workers_and_identical_general_environment_hint(make_run):
    run, env, trace = make_run(condition='solo', lead=[[action('bash', command='edit-lead'), done()]])
    result = run.run()
    assert_clean(run, env, result, 1)
    assert result['fixed_team_requested'] is False
    assert result['bootstrap_admitted'] is False and result['activation_source'] is None
    assert result['workers_with_actual_calls'] == result['delegations'] == 0
    assert result['team_execution_status'] == 'not_requested' and result['team_execution_valid'] is None
    assert list(result['agent_usage']) == ['lead'] and len(env.instances) == 1
    assert not any(event['event'] == 'team_activation_requested' for event in events(run))
    assert {t['function']['name'] for t in run.transport.seen[0]['tools']} == {'bash', 'finish'}
    assert 'does not guarantee apply_patch is installed' in run.transport.seen[0]['messages'][0]['content']
    assert trace[-1] == ('grade', 'official-spy')


@pytest.mark.parametrize('model', runner.MODELS)
def test_fixed_team_admits_two_real_derive_workers_before_lead_and_accounts_every_agent(make_run, model):
    run, env, trace = make_run(model=model)
    result = run.run()
    assert_clean(run, env, result, 3)
    assert result['arm'] == 'fixed_' + model and result['worker_model'] == model
    assert result['fixed_team_requested'] is result['bootstrap_admitted'] is True
    assert result['bootstrap_admitted_workers'] == result['workers_with_actual_calls'] == result['delegations'] == 2
    assert result['transport_record_count'] == result['transport_attempt_count'] == result['calls_with_measured_usage'] == 3
    assert result['activation_source'] == 'experiment_protocol'
    assert result['team_execution_status'] == 'workers_completed' and result['team_execution_valid'] is True
    assert result['mechanism_coverage'] == {'derive': 'fixed', 'split': 'not_exposed',
                                           'fission': 'not_exposed', 'cm': 'not_integrated'}
    assert {item['model'] for item in result['workers']} == {model}
    assert result['agent_usage']['worker-1']['total_tokens'] == 120
    assert result['agent_usage']['worker-2']['total_tokens'] == 120
    assert len({value['handle']['node_id'] for value in result['agent_usage'].values()}) == 3
    assert len({value['handle']['session_id'] for value in result['agent_usage'].values()}) == 3
    assert env.instances[0].applied == [PRODUCTION, REGRESSION]
    assert len(env.instances) == 3 and trace[-1] == ('grade', 'official-spy')
    lead = next(record for record in run.transport.seen if record['actor'] == 'lead')
    assert {t['function']['name'] for t in lead['tools']} == {'bash', 'finish', 'collect', 'review_worker', 'reply_worker'}
    assert 'Already-admitted worker assignments' in lead['messages'][0]['content']
    for record in run.transport.seen:
        prompt = record['messages'][0]['content']
        assert run.instance['problem_statement'] in prompt
        assert 'does not guarantee apply_patch is installed' in prompt
    assert 'Do not modify test files' in next(r for r in run.transport.seen if r['actor'] == 'worker-1')['messages'][0]['content']
    assert 'Do not modify production files' in next(r for r in run.transport.seen if r['actor'] == 'worker-2')['messages'][0]['content']
    stream = events(run)
    requested = [e for e in stream if e['event'] == 'team_activation_requested']
    admitted = [e for e in stream if e['event'] == 'worker_admitted']
    activated = [e for e in stream if e['event'] == 'worker_activated']
    actual = [e for e in stream if e['event'] == 'worker_first_call_completed']
    assert len(requested) == 1 and len(admitted) == len(activated) == len(actual) == 2
    assert requested[0]['source'] == 'experiment_protocol'
    assert all(e['source'] == 'experiment_protocol' and e['mechanism'] == 'derive' for e in admitted)
    assert all(e['source'] == 'runtime' and e['activation_source'] == 'experiment_protocol' for e in activated + actual)
    first_model_reservation = next(i for i, e in enumerate(stream) if e['event'] == 'call_reserved')
    assert all(stream.index(e) < first_model_reservation for e in admitted)
    for child in run.workers.values():
        assert run.control.cp.proj.work_items[child.handle.item_id].kind.value == 'derive'


def test_two_workers_keep_independent_same_snapshot_while_lead_changes(make_run):
    lead = [[action('bash', command='edit-lead')] + settle('worker-1') + settle('worker-2') + [done()]]
    run, env, _ = make_run(baseline=BASELINE, lead=lead)
    result = run.run()
    assert_clean(run, env, result, 3)
    children = [item for item in env.instances if item.actor != 'lead']
    assert len(children) == 2 and children[0] is not children[1]
    assert {item.baseline for item in children} == {BASELINE}
    assert {child.baseline_patch for child in run.workers.values()} == {BASELINE}
    assert {worker['baseline_sha256'] for worker in result['workers']} == {hashlib.sha256(BASELINE.encode()).hexdigest()}
    assert (run.folder / 'model.patch').read_bytes() == (BASELINE + LEAD_PATCH + PRODUCTION + REGRESSION).encode()


@pytest.mark.parametrize('condition', ['solo', 'fixed_team'])
def test_extra_delegate_is_absent_and_rejected_even_if_scripted_directly(make_run, condition):
    illegal = action('delegate', model='gpt-5.6-luna', task='Unapproved extra worker')
    lead = [[illegal] + (settle('worker-1') + settle('worker-2') if condition == 'fixed_team' else []) + [done()]]
    run, env, _ = make_run(condition=condition, lead=lead)
    result = run.run()
    assert_clean(run, env, result, 3 if condition == 'fixed_team' else 1)
    assert result['delegations'] == (2 if condition == 'fixed_team' else 0)
    denied = [e for e in events(run) if e['event'] == 'tool_completed' and e['tool'] == 'delegate']
    assert len(denied) == 1 and denied[0]['result']['executed'] is False
    assert 'not available' in denied[0]['result']['message']


def test_worker_transport_failure_is_counted_and_never_masquerades_as_valid_team(make_run):
    run, env, _ = make_run(workers={'worker-1': [{'error': 'fixture transport failure'}]},
        lead=[settle('worker-1', 'discard') + settle('worker-2') + [done()]])
    result = run.run()
    assert_clean(run, env, result, 3)
    assert result['bootstrap_admitted'] and result['workers_with_actual_calls'] == 2
    assert result['team_execution_status'] == 'worker_failure' and result['team_execution_valid'] is False
    assert result['workers'][0]['status'] == 'transport_error'
    assert env.instances[0].applied == [REGRESSION]
    assert result['agent_usage']['worker-1']['calls'] == 1
    failure = next(e for e in events(run) if e['event'] == 'worker_first_call_completed' and e['worker_id'] == 'worker-1')
    assert failure['error'] == 'fixture transport failure'


def test_local_failure_record_without_transport_attempt_is_not_actual_worker_call(make_run):
    run, env, _ = make_run(workers={'worker-1': [{'error': 'fixture missing local key'}]},
        lead=[settle('worker-1', 'discard') + settle('worker-2') + [done()]],
        attempt_counts={'worker-1': 0}, unknown_worker='worker-1')
    result = run.run()
    assert_clean(run, env, result, 3)
    assert result['workers_with_call_records'] == 2 and result['workers_with_actual_calls'] == 1
    assert result['workers_with_measured_usage'] == 1
    assert result['transport_record_count'] == 3 and result['transport_attempt_count'] == 2
    assert result['calls_with_transport_attempts'] == result['calls_with_measured_usage'] == 2
    assert result['team_execution_status'] == 'worker_failure' and result['team_execution_valid'] is False
    usage = result['agent_usage']['worker-1']
    assert usage['calls'] == usage['transport_record_count'] == 1
    assert usage['transport_attempt_count'] == usage['calls_with_transport_attempts'] == usage['calls_with_measured_usage'] == 0
    assert usage['total_tokens'] is None
    assert not any(e['event'] == 'worker_first_call_completed' and e['worker_id'] == 'worker-1' for e in events(run))
    assert 'does not prove' in result['actual_call_definition']


def test_missing_attempt_metadata_is_unknown_and_does_not_imply_an_attempt(make_run):
    run, env, _ = make_run(attempt_counts={'worker-1': None})
    result = run.run()
    assert_clean(run, env, result, 3)
    assert result['workers_with_call_records'] == 2 and result['workers_with_actual_calls'] == 1
    assert result['transport_attempt_count'] is None
    assert result['transport_attempt_count_unknown_records'] == 1
    assert result['transport_attempt_count_known_subtotal'] == 2
    # Completion evidence and transport-attempt coverage are separate facts.
    assert result['team_execution_status'] == 'workers_completed' and result['team_execution_valid'] is True
    assert result['agent_usage']['worker-1']['transport_attempt_count'] is None


def test_worker_fork_failure_records_zero_calls_for_that_identity(make_run):
    run, env, _ = make_run(failure='fork-worker-1',
        lead=[settle('worker-1', 'discard') + settle('worker-2') + [done()]])
    result = run.run()
    assert_clean(run, env, result, 2)
    assert result['bootstrap_admitted'] and result['workers_with_actual_calls'] == 1
    assert result['team_execution_status'] == 'worker_failure' and not result['team_execution_valid']
    assert result['agent_usage']['worker-1']['calls'] == 0
    assert result['workers'][0]['status'] == 'worker_error'


def test_partial_bootstrap_aborts_without_calling_lead_or_silently_reducing_team(make_run):
    run, env, _ = make_run(failure='second-admission')
    result = run.run()
    assert result['fixed_team_requested'] and not result['bootstrap_admitted']
    assert result['bootstrap_admitted_workers'] == 1 and result['workers_with_actual_calls'] == 0
    assert result['team_execution_status'] == 'bootstrap_failed'
    assert result['call_count'] == 0 and result['infrastructure_error'] is not None
    assert result['score'] is None and env.grade_calls == 0
    assert result['workers'][0]['status'] == 'not_started'
    assert run.control._closed and all(item.closed for item in env.instances)
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0


def test_result_is_written_exactly_once_after_final_grading_and_accounting(make_run, monkeypatch):
    run, env, trace = make_run()
    saved, original = [], runner.dump
    def capture(path, value):
        if Path(path).name == 'result.json':
            assert trace[-1] == ('grade', 'official-spy')
            saved.append(deepcopy(value))
        return original(path, value)
    monkeypatch.setattr(runner, 'dump', capture)
    result = run.run()
    assert_clean(run, env, result, 3)
    assert saved == [result]


def test_unknown_usage_is_retained_per_agent_instead_of_becoming_zero(make_run):
    run, env, _ = make_run(unknown_worker='worker-2')
    result = run.run()
    assert_clean(run, env, result, 3)
    assert result['agent_usage']['worker-1']['total_tokens'] == 120
    assert result['agent_usage']['worker-2']['total_tokens'] is None
    assert result['agent_usage']['worker-2']['unknown_counts']['total_tokens'] == 1
    assert result['model_usage']['glm-5.3']['total_tokens'] is None
    assert result['budget']['unknown_call_count'] == 1


@pytest.mark.parametrize('actor', ['lead', 'worker-1'])
def test_failed_environment_cleanup_poisons_resource_admission_and_prohibits_grading(make_run, actor):
    run, env, _ = make_run(failure='close-' + actor)
    result = run.run()
    assert result['infrastructure_error'] is not None and result['cleanup_errors']
    assert result['score'] is None and env.grade_calls == 0
    assert runner.RESOURCE_FAILURE.is_set() and run.control._closed
    assert run.control.cp.proj.active_points == run.control.cp.proj.open_worker_slots_used == 0
    acquired = [runner.CONTAINER_SLOTS.acquire(blocking=False) for _ in range(4)]
    assert all(acquired) and not runner.CONTAINER_SLOTS.acquire(blocking=False)
    for _ in acquired:
        runner.CONTAINER_SLOTS.release()


def test_cp_commit_failure_after_adoption_forbids_grading_and_additional_tools(make_run):
    run, env, _ = make_run(failure='adoption-commit')
    result = run.run()
    assert result['infrastructure_error']['type'] == 'FatalRuntimeError'
    assert env.instances[0].applied == [PRODUCTION]
    assert result['score'] is None and env.grade_calls == 0
    assert run.control._closed and all(item.closed for item in env.instances)


@pytest.mark.parametrize('entry', [
    {'condition': 'dpswarm'},
    {'condition': 'fixed_team'},
    {'condition': 'fixed_team', 'worker_model': 'invented-model'},
    {'condition': 'solo', 'worker_model': 'gpt-5.6-luna'},
])
def test_invalid_condition_or_model_rejected_before_any_run_resources(tmp_path, entry):
    with pytest.raises(ValueError):
        runner.SweRun(tmp_path, entry)
    assert list(tmp_path.iterdir()) == []


def test_worker_closing_call_is_finish_only_and_completes_after_work_limit(make_run):
    scripts = {'worker-1': [[action('bash', command='edit-production')] * 1] * 8 + [[done()]]}
    run, env, _ = make_run(workers=scripts)
    result = run.run()
    assert result['infrastructure_error'] is None
    closing = [event for event in events(run) if event['event'] == 'closing_call_settled']
    assert len(closing) == 1 and closing[0]['worker_id'] == 'worker-1'
    worker1_calls = [seen for seen in run.transport.seen if seen['actor'] == 'worker-1']
    assert len(worker1_calls) == 9
    assert [declaration['function']['name'] for declaration in worker1_calls[-1]['tools']] == ['finish']
    assert run.workers['worker-1'].delivery['status'] == 'completed'


def test_worker_closing_call_without_finish_keeps_local_call_limit(make_run):
    scripts = {'worker-1': [[action('bash', command='edit-production')]] * 8
               + [[action('bash', command='not-declared-here')]]}
    lead = [settle('worker-1', decision='discard') + settle('worker-2') + [done()]]
    run, env, _ = make_run(lead=lead, workers=scripts)
    result = run.run()
    assert result['infrastructure_error'] is None
    assert run.workers['worker-1'].delivery['status'] == 'local_call_limit'
    closing = [event for event in events(run) if event['event'] == 'closing_call_started']
    assert len(closing) == 1
    # The undeclared bash call was never executed; the work-limit delta stands.
    assert run.workers['worker-1'].delivery['patch_sha256'] == hashlib.sha256((PRODUCTION * 8).encode()).hexdigest()


def test_budget_exhaustion_drains_in_flight_worker_and_blocks_new_calls(make_run, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_first_call(run):
        started.set()
        assert release.wait(timeout=60)
        return [action('bash', command='edit-production')]

    scripts = {'worker-1': [slow_first_call, [done()]]}
    run, env, _ = make_run(workers=scripts)
    original_loop = run.loop

    def loop(handle, env, *, worker=None):
        if worker is None:
            assert started.wait(timeout=30)  # worker-1's first call is genuinely in flight
            return {'status': 'budget_exhausted', 'summary': 'fixture Lead reservation failure'}
        return original_loop(handle, env, worker=worker)

    monkeypatch.setattr(run, 'loop', loop)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault('result', run.run()))
    thread.start()
    try:
        assert started.wait(timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if any(event['event'] == 'worker_drain_started' for event in events(run)):
                    break
            except ValueError:
                pass  # events.jsonl may be mid-append; retry
            time.sleep(0.05)
        else:
            pytest.fail('drain never started')
        # New admissions are forbidden while the in-flight call drains.
        assert run.draining is True
        release.set()
        thread.join(timeout=60)
    finally:
        release.set()
        thread.join(timeout=60)
    result = outcome['result']
    assert result['outcome']['status'] == 'budget_exhausted'
    sequence = [event['event'] for event in events(run)]
    assert sequence.index('worker_drain_started') < sequence.index('worker_drain_completed')
    worker1_calls = [seen for seen in run.transport.seen if seen['actor'] == 'worker-1']
    assert len(worker1_calls) == 1  # in-flight call settled; the next call hit DRAIN_IN_PROGRESS
    settled = [record for record in run.calls if record['role'] == 'worker']
    assert any(record['total_tokens'] == 120 for record in settled)
    assert run.workers['worker-1'].delivery['status'] == 'budget_exhausted'
