"""Explicit phase loop with source-bound handoff and budgeted clarification.

This is the external TeamBench adapter, not the production Orchestrator. The
ControlPlane alone accepts work after the unchanged, isolated final grader.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import traceback
from uuid import uuid4

from .paths import LEGACY, RecoveryRequired
from .tools import role_tools, role_system
from .task_contracts import build_contract
from .transport import V2Transport
from dpswarm.team_runtime.protocol import ProtocolError, parse_native_response, parse_text_response
from dpswarm.team_runtime.ledger import ExecutionStore, RunBudget, LedgerError
from dpswarm.team_runtime.scheduler import ClarificationScheduler, ClarificationError
from control_bridge import ExperimentControl, RoleHandle
from sandbox import RoleSandbox
from dpswarm.control import ControlPlaneError, AdmissionError

LIMITS = {'planner': 2, 'executor': 6, 'verifier': 4, 'repair': 3,
          'reverify': 3, 'clarification': 2, 'solo': 20}
MAX_CALLS = 20
TOKEN_LIMIT = 600_000
RUN_DEADLINE = 14_400
CLARIFICATION_DEADLINE = 1_200


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def tree_hashes(root):
    return {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file() and not p.is_symlink()}


class TeamRun:
    def __init__(self, root, entry, manifest, *, transport=None, sandbox_factory=RoleSandbox,
                 control_factory=ExperimentControl):
        self.root, self.entry, self.manifest = Path(root).resolve(), deepcopy(entry), manifest
        self.folder = self.root / 'results' / entry['run_id']
        self.transport = transport or V2Transport(self.root)
        self.sandbox_factory, self.control_factory = sandbox_factory, control_factory
        self.models = {'planner': 'gpt-5.6-sol', 'executor': entry['executor'],
                       'verifier': 'gpt-5.6-terra', 'oracle': 'gpt-5.6-sol'}
        self.box = self.control = None

    def checkpoint(self):
        self.data['budget'] = self.budget.snapshot()
        self.data['scheduler'] = self.scheduler.snapshot()
        self.store.save_snapshot(self.data)

    def context(self, phase):
        handle = RoleHandle(**phase['handle'])
        self.control._check_handle(handle)
        item = self.control.cp.proj.work_items[handle.item_id]
        return {'run_id': self.entry['run_id'], 'role': handle.role, 'item_id': handle.item_id,
                'attempt': item.attempt, 'node_id': handle.node_id, 'session_id': handle.session_id,
                'context_epoch': handle.context_epoch}

    def setup(self, resume=False):
        if resume:
            from .recovery import RecoverableControl, reattach_sandbox, cleanup_finished_sandbox
            self.store = ExecutionStore(self.folder / 'execution')
            snapshot = self.store.load_snapshot()
            if snapshot is None or snapshot['pending_events'] or snapshot['state'].get('inflight') or snapshot['state'].get('unprocessed_response'):
                raise RecoveryRequired('Unsettled model/tool/control outcome; inspect journal before recovery')
            self.data = snapshot['state']
            if self.data['entry'] != self.entry or self.data['manifest_hash'] != self.manifest_hash():
                raise RecoveryRequired('Run identity or frozen manifest changed')
            self.budget = RunBudget.from_snapshot(self.data['budget'])
            self.scheduler = ClarificationScheduler.from_snapshot(self.data['scheduler'])
            if self.data.get('finished'):
                if not isinstance(self.data.get('final_result'), dict):
                    raise RecoveryRequired('Finished checkpoint has no final result evidence')
                # The grader and CP acceptance already finished. Only reconstruct
                # the output and remove verified owned containers, never regrade.
                self.box = cleanup_finished_sandbox(self.folder, self.manifest['image_id'])
                return
            self.control = RecoverableControl.restore(self.folder, self.models, self.entry['task_id'])
            try:
                self.box = reattach_sandbox(self.folder, self.manifest['image_id'])
            except BaseException:
                self.control.cp.close()
                raise
        else:
            if self.folder.exists():
                raise RecoveryRequired('Run directory exists; use explicit --resume after checking its state')
            shutil.copytree(self.root / 'instances' / self.entry['task_id'], self.folder)
            self.store = ExecutionStore(self.folder / 'execution')
            self.budget = RunBudget(MAX_CALLS, TOKEN_LIMIT, RUN_DEADLINE)
            self.scheduler = ClarificationScheduler(self.entry['run_id'], deadline_seconds=CLARIFICATION_DEADLINE)
            self.data = {'entry': self.entry, 'manifest_hash': self.manifest_hash(), 'started_at': utc(),
                         'started_clock': time.time(), 'stage': 0, 'current': None, 'suspended': None,
                         'histories': {}, 'seen': {}, 'dialogue': [], 'handoff': None, 'revision': 0,
                         'phases': [], 'calls': [], 'inflight': 'setup', 'terminal_reason': None}
            self.checkpoint()
            self.control = self.control_factory(self.folder, self.models, self.entry['task_id'])
            self.box = self.sandbox_factory(self.folder / 'task', self.folder, image=self.manifest['image_id'])
            self.box.start()
            self.data['inflight'] = None
        self.spec = (self.folder / 'task' / 'spec.md').read_text(encoding='utf-8')
        self.brief = (self.folder / 'task' / 'brief.md').read_text(encoding='utf-8')
        self.contract = build_contract(self.entry['task_id'], self.spec)
        if self.data['handoff'] is not None:
            # Check the saved contract against the actual public source again.
            payload = {key: self.data['handoff'][key] for key in self.contract._FIELDS}
            self.contract.accepted_package(payload)
        self.checkpoint()

    def manifest_hash(self):
        return hashlib.sha256(json.dumps(self.manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def history(self, role):
        return self.data['histories'].setdefault(role, [])

    def start_phase(self, role, name, prompt, *, request_id=None):
        self.data['inflight'] = 'control.start_role:' + name
        self.checkpoint()
        handle = self.control.start_role(role, name)
        phase = {'role': role, 'phase': name, 'handle': asdict(handle), 'request_id': request_id,
                 'calls': 0, 'call_ids': [], 'tool_calls': 0, 'protocol_errors': 0, 'no_actions': 0,
                 'status': 'running', 'started_at': utc(), 'started_clock': time.time(),
                 'pending_question': None, 'finish': None}
        history = self.history(role)
        system = {'role': 'system', 'content': role_system(role, self.models[role], self.contract,
                                                        clarification=name == 'clarification')}
        if history:
            history[0] = system
        else:
            history.append(system)
        declarations = role_tools(role, self.contract, clarification=name == 'clarification')
        if self.models[role].startswith('gpt-'):
            prompt += '\nDeclared tools (JSON schemas):\n' + json.dumps(declarations, ensure_ascii=False)
        history.append({'role': 'user', 'content': prompt})
        self.data['current'] = phase
        if request_id:
            self.scheduler.admit(request_id, self.context(phase))
        self.data['inflight'] = None
        self.checkpoint()

    def notifications(self, role):
        seen = self.data['seen'].get(role, 0)
        messages = [v for v in self.data['dialogue'][seen:] if v['to'] in (role, 'all')]
        self.data['seen'][role] = len(self.data['dialogue'])
        if messages:
            self.history(role).append({'role': 'user', 'content': 'Informational messages:\n' + json.dumps(messages, ensure_ascii=False)})

    def tool(self, phase, call, index, count):
        self.context(phase)
        name, args, role = call['name'], call['arguments'], phase['role']
        if name in ('read', 'write', 'run'):
            return self.box.tool(role, name, args)
        if name == 'send_message':
            self.data['dialogue'].append({'from': role, 'to': args['to'], 'content': args['content'], 'at': utc()})
            return {'ok': True, 'notification_only': True}
        if name == 'submit_handoff':
            if self.data['handoff'] is not None:
                return {'ok': False, 'error': 'HANDOFF_IMMUTABLE'}
            validation = self.contract.validate(args)
            if validation['ok']:
                self.data['handoff'] = self.contract.accepted_package(args)
                self.data['revision'] = 1
                dump(self.folder / 'handoff.json', self.data['handoff'])
            return validation
        if name == 'request_clarification':
            if phase['calls'] >= LIMITS[phase['phase']]:
                return {'ok': False, 'error': 'NO_EXECUTOR_CONTINUATION_BUDGET'}
            request = self.scheduler.request(self.context(phase), 'planner', args['question'],
                args['missing_fields'], args['request_id'], self.data['revision'])
            if request['state'] == 'RESUMED':
                return {'ok': True, 'already_resumed': True}
            if request['state'] == 'FAILED':
                return {'ok': False, 'error': request['failure_reason']}
            phase['pending_question'] = request['request_id']
            return {'ok': True, 'state': 'E_WAITING_REPLY', 'request_id': request['request_id']}
        if name == 'reply_clarification':
            source = self.data['suspended']
            if source is None or args['reply_to'] != phase['request_id']:
                return {'ok': False, 'error': 'WRONG_REPLY_TARGET'}
            reply = self.scheduler.reply(args['reply_to'], self.context(phase), self.context(source),
                args['answer'], contract_revision=self.data['revision'])
            return {'ok': True, 'state': reply['state']}
        if name == 'finish_phase':
            if index != count - 1:
                return {'ok': False, 'error': 'FINISH_MUST_BE_LAST_IN_BATCH'}
            if phase['pending_question']:
                return {'ok': False, 'error': 'WAITING_FOR_CLARIFICATION'}
            if role == 'planner' and phase['phase'] == 'planner' and self.data['handoff'] is None and args['status'] == 'done':
                return {'ok': False, 'error': 'HANDOFF_REQUIRED'}
            if phase['phase'] == 'clarification' and self.scheduler.get(phase['request_id'])['state'] != 'REPLY_READY' and args['status'] == 'done':
                return {'ok': False, 'error': 'REPLY_REQUIRED'}
            phase['finish'] = deepcopy(args)
            phase['status'] = 'finished' if args['status'] == 'done' else 'blocked'
            return {'ok': True, 'task_accepted': False}
        raise ValueError('Tool not implemented: ' + name)

    @staticmethod
    def visible(result):
        value = deepcopy(result)
        # Bounded file/command output; complete output stays in the tool ledger.
        for key, limit in (('stdout', 4000), ('stderr', 2000)):
            if isinstance(value.get(key), str) and len(value[key]) > limit:
                value[key] = value[key][:limit]
                value[key + '_truncated'] = True
        return value

    def protocol_feedback(self, phase, assistant, exc):
        history = self.history(phase['role'])
        history.append(assistant)
        calls = assistant.get('tool_calls')
        # Even rejected native calls require one result per call ID. Never repair
        # malformed IDs, split prose to find actions, or execute a partial batch.
        if calls:
            ids = [c.get('id') if isinstance(c, dict) else None for c in calls]
            if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
                phase['status'] = 'protocol_terminal'
                return
            for ident in ids:
                history.append({'role': 'tool', 'tool_call_id': ident,
                                'content': json.dumps({'ok': False, 'protocol_error': exc.code, 'message': str(exc)})})
        else:
            history.append({'role': 'user', 'content': 'Protocol error; call consumed: ' + str(exc)})

    def turn(self):
        phase = self.data['current']
        role = phase['role']
        self.context(phase)
        self.notifications(role)
        ticket = uuid4().hex
        scheduler_before = self.scheduler.snapshot()
        try:
            if phase['request_id']:
                # Check expiry/admission before consuming a run reservation.
                self.scheduler.expire()
                if self.scheduler.get(phase['request_id'])['state'] not in ('ADMITTED', 'P_REPLYING', 'REPLY_READY'):
                    phase['status'] = 'clarification_unavailable'
                    return
                self.scheduler.mark_reply_started(phase['request_id'], ticket)
            self.budget.reserve(ticket, role)
        except LedgerError as exc:
            if phase['request_id']:
                # Neither operation has made an external call yet. Roll back a
                # tentative scheduler mark if global admission was rejected.
                self.scheduler = ClarificationScheduler.from_snapshot(scheduler_before)
                self.scheduler.fail(phase['request_id'], exc.code)
            phase['status'] = exc.code.lower()
            self.data['terminal_reason'] = exc.code
            return
        phase['calls'] += 1
        self.data['inflight'] = 'model:' + ticket
        self.checkpoint()
        declarations = role_tools(role, self.contract, clarification=phase['phase'] == 'clarification')
        record = self.transport.complete(self.models[role], deepcopy(self.history(role)), tools=declarations,
            run_id=self.entry['run_id'], role=role, task_id=self.entry['task_id'])
        self.budget.complete(ticket, record)
        self.control.record_call(RoleHandle(**phase['handle']), record)
        self.data['calls'].append(record)
        phase['call_ids'].append(record['call_id'])
        self.data['inflight'] = None
        # This checkpoint holds the raw returned response before interpreting it.
        self.data['unprocessed_response'] = record['call_id']
        self.checkpoint()
        step = {'record': record, 'tool_results': []}
        if record.get('error'):
            phase['status'] = 'transport_error'
            step['transport_error'] = record['error']
        else:
            assistant = deepcopy(record['assistant_message'])
            native = self.models[role].startswith('glm-')
            try:
                action = parse_native_response(assistant, declarations) if native else parse_text_response(record['text'], declarations)
            except ProtocolError as exc:
                phase['protocol_errors'] += 1
                step['protocol_error'] = {'code': exc.code, 'message': str(exc)}
                self.protocol_feedback(phase, assistant, exc)
            else:
                if action['kind'] == 'no_action':
                    phase['no_actions'] += 1
                    self.history(role).extend([assistant, {'role': 'user', 'content':
                        'NoAction: no declared tool was called. This turn was consumed. Use finish_phase explicitly when done.'}])
                else:
                    calls = action['calls']
                    if not native:
                        # A canonical tool history for both transports; raw CLI
                        # output remains intact in the immutable call record.
                        assistant = {'role': 'assistant', 'content': None, 'tool_calls': [
                            {'id': c['id'], 'type': 'function', 'function': {'name': c['name'],
                             'arguments': json.dumps(c['arguments'], ensure_ascii=False)}} for c in calls]}
                    self.history(role).append(assistant)
                    for index, call in enumerate(calls):
                        operation = record['call_id'] + ':' + call['id']
                        request = {'context': self.context(phase), 'call': call}
                        prior = self.store.plan_tool(operation, request)
                        self.data['inflight'] = 'tool:' + operation
                        self.checkpoint()
                        if prior['status'] == 'completed':
                            result = self.store.replay_completed(operation)
                        else:
                            self.store.start_tool(operation)
                            try:
                                result = self.tool(phase, call, index, len(calls))
                            except (ValueError, ClarificationError) as exc:
                                result = {'ok': False, 'error': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}
                            self.store.complete_tool(operation, result)
                        phase['tool_calls'] += 1
                        step['tool_results'].append({'call': call, 'result': result})
                        self.history(role).append({'role': 'tool', 'tool_call_id': call['id'],
                                                  'content': json.dumps(self.visible(result), ensure_ascii=False)})
                        self.data['inflight'] = None
                        self.checkpoint()
        self.data.pop('unprocessed_response', None)
        dump(self.folder / 'phases' / phase['phase'] / f"turn_{phase['calls']:03d}.json", step)
        self.checkpoint()

    def finish_phase(self):
        phase = self.data['current']
        phase.update(completed_at=utc(), wall_seconds=time.time() - phase['started_clock'])
        dump(self.folder / 'phases' / phase['phase'] / 'phase.json', phase)
        dump(self.folder / 'phases' / phase['phase'] / 'conversation.json', self.history(phase['role']))
        self.data['inflight'] = 'control.submit:' + phase['phase']
        self.checkpoint()
        self.control.submit_role(RoleHandle(**phase['handle']), phase)
        self.data['phases'].append(deepcopy(phase))
        self.data['current'] = None
        self.data['inflight'] = None
        if phase['phase'] == 'clarification':
            source, request_id = self.data['suspended'], phase['request_id']
            try:
                if phase['status'] != 'finished' or (phase.get('finish') or {}).get('status') != 'done':
                    raise ClarificationError('CLARIFIER_NOT_FINISHED', 'Planner did not explicitly finish its reply phase')
                reply = self.scheduler.resume(request_id, self.context(source))
                self.history('executor').append({'role': 'user', 'content':
                    'Context-validated Planner reply to ' + request_id + ':\n' + reply['reply']['answer']})
                source['pending_question'] = None
                source['status'] = 'running'
            except ClarificationError as exc:
                current = self.scheduler.get(request_id)
                if current['state'] != 'FAILED':
                    self.scheduler.fail(request_id, exc.code)
                source['status'] = 'clarification_failed'
                source['pending_question'] = None
                self.data['terminal_reason'] = 'CLARIFICATION_FAILED'
            self.data['current'], self.data['suspended'] = source, None
        else:
            self.data['stage'] += 1
            if phase['phase'] == 'planner' and self.data['handoff'] is None:
                self.data['stage'], self.data['terminal_reason'] = 5, 'HANDOFF_FAILED'
            if phase['phase'] == 'verifier':
                first = self.attestation()
                dump(self.folder / 'first_attestation.json', first)
                if first.get('verdict') == 'pass':
                    self.data['stage'] = 5
        self.checkpoint()

    def next_phase(self):
        if self.entry['condition'] == 'solo':
            if self.data['stage']:
                return False
            self.start_phase('oracle', 'solo', 'Complete and validate this full specification:\n' + self.spec)
            return True
        stage = self.data['stage']
        if stage == 0:
            self.start_phase('planner', 'planner', 'Relay the required facts from this full specification.\n' + self.spec)
        elif stage == 1:
            if not self.data['handoff']:
                raise RuntimeError('Executor dispatch without accepted handoff')
            self.start_phase('executor', 'executor', 'Task brief:\n' + self.brief + '\nValidated handoff:\n' + json.dumps(self.data['handoff'], ensure_ascii=False))
        elif stage == 2:
            self.start_phase('verifier', 'verifier', 'Independently check ORIGINAL specification and write attestation.json:\n' + self.spec)
        elif stage == 3:
            self.start_phase('executor', 'repair', 'Repair verifier findings:\n' + json.dumps(self.attestation(), ensure_ascii=False))
        elif stage == 4:
            prior = self.folder / 'submission' / 'attestation.json'
            if prior.is_file() and not prior.is_symlink():
                prior.replace(prior.with_name('attestation.previous.json'))
            self.start_phase('verifier', 'reverify', 'Recheck CURRENT original workspace; use a NEW temporary copy. Write a NEW attestation.json.')
        else:
            return False
        return True

    def settle(self):
        phase = self.data['current']
        if phase['pending_question']:
            request_id = phase['pending_question']
            phase['status'] = 'waiting_reply'
            self.data['suspended'] = phase
            request = self.scheduler.get(request_id)
            try:
                self.start_phase('planner', 'clarification',
                    'Pending request (answer using the original public spec):\n' + json.dumps(request, ensure_ascii=False), request_id=request_id)
            except (ControlPlaneError, AdmissionError) as exc:
                self.scheduler.fail(request_id, 'admission_failed:' + getattr(exc, 'code', type(exc).__name__))
                phase['pending_question'], phase['status'] = None, 'clarification_admission_failed'
                self.data['current'], self.data['suspended'] = phase, None
                self.data['inflight'] = None
                self.data['terminal_reason'] = 'CLARIFICATION_ADMISSION_FAILED'
                self.checkpoint()
        elif phase['status'] != 'running' or phase['calls'] >= LIMITS[phase['phase']]:
            if phase['status'] == 'running':
                phase['status'] = 'phase_budget_exhausted'
            self.finish_phase()

    def attestation(self):
        path = self.folder / 'submission' / 'attestation.json'
        try:
            if path.is_symlink() or not path.resolve().is_relative_to((self.folder / 'submission').resolve()):
                raise ValueError('Attestation path escaped submission')
            if path.stat().st_size > 1_048_576:
                raise ValueError('Attestation exceeds 1 MiB')
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {'invalid': value}
        except (ValueError, OSError) as exc:
            return {'error': str(exc)}

    def grade(self):
        self.scheduler.freeze()
        self.budget.freeze()
        self.data['inflight'] = 'freeze_and_grade'
        self.checkpoint()
        self.box.freeze()
        dump(self.folder / 'workspace_before_grade.sha256.json', tree_hashes(self.folder / 'workspace'))
        operation = 'final-grader'
        self.store.plan_tool(operation, {'run_id': self.entry['run_id'], 'image_id': self.manifest['image_id']})
        self.store.start_tool(operation)
        grading = self.box.grade()
        self.store.complete_tool(operation, grading)
        self.data['grading'] = grading
        self.data['inflight'] = None
        self.checkpoint()
        result = self.result('scored' if grading.get('exit_code') == 0 and not grading.get('timed_out') and isinstance(grading.get('raw_score'), dict) else 'grader_error')
        result['grading'], result['score'], result['attestation'] = grading, grading.get('raw_score'), self.attestation()
        # No finish tool or model attestation can replace this evidence gate.
        self.data['inflight'] = 'control.finish'
        self.checkpoint()
        result['control'] = self.control.finish(grading, result)
        self.data['inflight'] = None
        self.data['finished'] = True
        self.data['final_result'] = deepcopy(result)
        self.checkpoint()
        return result

    def result(self, status):
        calls = self.data['calls']
        totals = {}
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            values = [c.get(field) for c in calls]
            totals[field] = sum(values) if all(v is not None for v in values) else None
            totals[field + '_known_subtotal'] = sum(v for v in values if v is not None)
        return {**self.entry, 'status': status, 'started_at': self.data['started_at'], 'completed_at': utc(),
                'wall_seconds': time.time() - self.data['started_clock'], 'phases': self.data['phases'],
                'call_ids': [c['call_id'] for c in calls], 'call_count': len(calls), **totals,
                'usage_complete': all(c.get('input_tokens') is not None and c.get('output_tokens') is not None for c in calls),
                'budget': self.budget.summary(), 'clarifications': self.scheduler.snapshot(),
                'terminal_reason': self.data['terminal_reason'], 'native_orchestrator_exercised': False,
                'handoff_validated': self.data['handoff'] is not None}

    def run(self, *, resume=False, pause_after_calls=None):
        target = self.folder / 'result.json'
        if target.exists():
            return json.loads(target.read_text(encoding='utf-8'))
        paused, result = False, None
        try:
            self.setup(resume)
            if self.data.get('finished'):
                result = deepcopy(self.data['final_result'])
                return result
            if self.data.get('unprocessed_response'):
                raise RecoveryRequired('Returned response not fully settled; explicit reconciliation required')
            while self.data['current'] is not None or self.next_phase():
                phase = self.data['current']
                if phase['pending_question']:
                    # A crash can leave the settled E batch checkpointed before
                    # its scheduler transition. Never call E again at that point.
                    self.settle()
                    phase = self.data['current']
                if phase['status'] == 'running' and phase['calls'] < LIMITS[phase['phase']]:
                    self.turn()
                self.settle()
                if pause_after_calls is not None and self.budget.summary()['call_count'] >= pause_after_calls:
                    self.checkpoint()
                    paused = True
                    return self.result('paused')
            result = self.grade()
        except RecoveryRequired:
            paused = True
            raise
        except (KeyboardInterrupt, SystemExit):
            paused = True
            raise
        except Exception as exc:
            if not hasattr(self, 'data'):
                raise
            result = self.result('infrastructure_error')
            result['error'] = {'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}
        finally:
            if self.control is not None:
                try:
                    self.control.cp.close() if paused else self.control.close()
                except Exception as exc:
                    if result is not None:
                        result['control_cleanup_error'] = str(exc)
            if self.box is not None and not paused:
                try:
                    self.box.close()
                except Exception as exc:
                    if result is not None:
                        result['sandbox_cleanup_error'] = str(exc)
            if result is not None:
                dump(target, result)
        return result
