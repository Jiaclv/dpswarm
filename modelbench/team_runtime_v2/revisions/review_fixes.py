"""Additive fixes for bounded clarification failure and unusable native history.

The frozen runner, protocol, and evidence records are unchanged. This revision
does not synthesize tool IDs, replay tools, or authorize task acceptance.
"""
from copy import deepcopy

from .budget_visibility import BudgetAwareTeamRun
from ..paths import RecoveryRequired
from dpswarm.team_runtime.scheduler import ClarificationError


class ReviewFixedTeamRun(BudgetAwareTeamRun):
    def _review_state(self):
        return self.data.setdefault('review_fixes', {
            'version': 1, 'clarification_failures': [], 'terminal_roles': {},
            'skipped_phases': [],
        })

    def _fail_clarification_admission(self, source, request_id, code):
        """Settle a known admission failure without abandoning a created item."""
        request = self.scheduler.get(request_id)
        if request['state'] == 'RESUMED':
            raise RecoveryRequired('Cannot fail an already resumed clarification')
        if request['state'] != 'FAILED':
            request = self.scheduler.fail(request_id, code)
        created = self.data['current']
        if created is source:
            created = None
        elif (created is None or created.get('phase') != 'clarification'
              or created.get('request_id') != request_id
              or self.data.get('suspended') is not source):
            raise RecoveryRequired('Clarification admission has ambiguous phase ownership')
        failure = {
            'request_id': request_id, 'code': code,
            'failure_reason': request.get('failure_reason'),
            'source_item_id': source['handle']['item_id'],
            'clarifier_item_id': created['handle']['item_id'] if created else None,
        }
        self._review_state()['clarification_failures'].append(failure)
        source['clarification_failure'] = deepcopy(failure)
        self.history(source['role']).append({'role': 'user', 'content':
            'Clarification failed: ' + request_id + ' (' + str(failure['failure_reason'])
            + '). No Planner reply authorized continuation of this phase.'})
        if created is not None:
            # start_phase has already registered the CP handle. Submit its zero
            # call failure evidence through the existing guarded lifecycle; its
            # finish_phase restores the same suspended Executor without reply.
            created['status'] = 'clarification_admission_failed'
            created['admission_error'] = deepcopy(failure)
            self.finish_phase()
        else:
            source['pending_question'] = None
            source['status'] = 'clarification_failed'
            self.data['current'], self.data['suspended'] = source, None
            self.data['inflight'] = None
            self.data['terminal_reason'] = 'CLARIFICATION_FAILED'
            self.checkpoint()
        # run() also calls settle before inspecting a restored pending phase.
        # Keep the failed source current so that inspection is valid; the next
        # ordinary settle submits it exactly once and advances to the verifier.
        # Finishing it here would set current=None before run() reads status.
        return None

    def settle(self):
        source = self.data['current']
        request_id = source.get('pending_question') if source else None
        if not request_id:
            return super().settle()
        self.scheduler.expire()
        request = self.scheduler.get(request_id)
        if request['state'] == 'FAILED':
            code = 'REQUEST_EXPIRED' if request.get('failure_reason') == 'expired' else 'REQUEST_FAILED'
            return self._fail_clarification_admission(source, request_id, code)
        try:
            return super().settle()
        except ClarificationError as exc:
            # Expiry can race the precheck while CP start_role is materializing
            # the new phase. Its registered item must receive failure evidence.
            return self._fail_clarification_admission(source, request_id, exc.code)

    @staticmethod
    def _pairable_ids(assistant):
        calls = assistant.get('tool_calls')
        if not isinstance(calls, list) or not calls:
            return False
        ids = [call.get('id') if isinstance(call, dict) else None for call in calls]
        return (all(isinstance(value, str) and value.strip() for value in ids)
                and len(set(ids)) == len(ids))

    def protocol_feedback(self, phase, assistant, error):
        if not self.models[phase['role']].startswith('glm-') or self._pairable_ids(assistant):
            return super().protocol_feedback(phase, assistant, error)
        # Preserve the original response as evidence but never send this native
        # history again. In particular, do not invent IDs or tool results.
        self.history(phase['role']).append(assistant)
        phase['status'] = 'protocol_terminal'
        phase['role_terminal'] = True
        self._review_state()['terminal_roles'][phase['role']] = {
            'phase': phase['phase'], 'item_id': phase['handle']['item_id'],
            'call_id': phase['call_ids'][-1], 'protocol_error': error.code,
            'reason': 'unpairable_native_history',
        }
        self.data['terminal_reason'] = 'NATIVE_HISTORY_UNPAIRABLE'

    def start_phase(self, role, name, prompt, *, request_id=None):
        if role in self._review_state()['terminal_roles']:
            raise RecoveryRequired('Role has unusable native history and cannot start another phase: ' + role)
        return super().start_phase(role, name, prompt, request_id=request_id)

    def turn(self):
        phase = self.data['current']
        if phase['role'] in self._review_state()['terminal_roles']:
            phase['status'] = 'protocol_terminal'
            return
        return super().turn()

    def next_phase(self):
        stages = {0: ('planner', 'planner'), 1: ('executor', 'executor'),
                  2: ('verifier', 'verifier'), 3: ('executor', 'repair'),
                  4: ('verifier', 'reverify')}
        if self.entry['condition'] != 'solo':
            while self.data['stage'] in stages:
                role, name = stages[self.data['stage']]
                terminal = self._review_state()['terminal_roles'].get(role)
                if terminal is None:
                    break
                self._review_state()['skipped_phases'].append({
                    'role': role, 'phase': name, 'reason': terminal['reason'],
                    'source_call_id': terminal['call_id'],
                })
                self.data['stage'] += 1
                self.checkpoint()
        return super().next_phase()

    def result(self, status):
        result = super().result(status)
        result['review_fixes'] = deepcopy(self._review_state())
        return result
