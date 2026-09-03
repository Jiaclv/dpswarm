"""Role capabilities are explicit and independent of provider serialization."""
from copy import deepcopy


def declaration(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


TEXT = {'type': 'string'}
STRINGS = {'type': 'array', 'items': TEXT}
FINISH = declaration('finish_phase', 'Explicitly end this role phase; this does not accept the task.',
    {'status': {'type': 'string', 'enum': ['done', 'blocked']}, 'summary': TEXT,
     'artifacts': STRINGS, 'unresolved': STRINGS}, ['status', 'summary', 'artifacts', 'unresolved'])
MESSAGE = declaration('send_message', 'Send an informational message. For a necessary answer use request_clarification.',
    {'to': {'type': 'string', 'enum': ['planner', 'executor', 'verifier', 'all']},
     'content': TEXT}, ['to', 'content'])
QUESTION = declaration('request_clarification', 'Pause Executor after this tool batch and request a budgeted Planner answer.',
    {'request_id': TEXT, 'question': TEXT, 'missing_fields': STRINGS},
    ['request_id', 'question', 'missing_fields'])
REPLY = declaration('reply_clarification', 'Answer the pending question using authorized full specification.',
    {'reply_to': TEXT, 'answer': TEXT}, ['reply_to', 'answer'])


def role_tools(role, contract, *, clarification=False):
    tools = [declaration('read', 'Read an authorized container file.', {'path': TEXT}, ['path'])]
    if role != 'planner':
        tools += [declaration('run', 'Run a command in the isolated role container.', {'cmd': TEXT}, ['cmd']),
                  declaration('write', 'Write an authorized container file.', {'path': TEXT, 'content': TEXT}, ['path', 'content'])]
    if role != 'oracle':
        tools.append(MESSAGE)
    if role == 'planner':
        tools.append(REPLY if clarification else contract.tool_declaration())
    if role == 'executor':
        tools.append(QUESTION)
    tools.append(FINISH)
    return deepcopy(tools)


TEXT_PROTOCOL = '''Return exactly one JSON object for tool requests:
{"type":"tool_calls","calls":[{"id":"unique_id","name":"tool_name","arguments":{}}]}.
Use only declared tools and put every parameter inside arguments. All effects are
executed by the external runtime, not by your own tools. Use finish_phase to end;
plain prose and DONE are not completion. Ordinary text consumes a NoAction turn.'''


def role_system(role, model, contract, *, clarification=False):
    roles = {
        'planner': 'You are Planner. Executor cannot read the full specification. Transfer exact facts, not instructions to guess them. Submit a complete handoff before finishing.',
        'executor': 'You are Executor. Implement the task from your brief and validated Planner handoff. You cannot read the full spec. Ask request_clarification for missing essential facts; never guess a special mapping. Run checks and deliver required files in the actual workspace.',
        'verifier': 'You are Verifier. Independently compare the current workspace with the ORIGINAL full spec, not only the Planner package. Test actual artifacts. Send useful feedback and write /shared/submission/attestation.json with verdict pass/fail and evidence before finishing.',
        'oracle': 'You are a solo agent. Implement and independently validate the full specification; write /shared/submission/attestation.json with verdict and evidence.',
    }
    text = roles[role]
    if clarification:
        text = 'You are Planner answering one bounded clarification. Your original planning submission is immutable. Read the pending request and answer through reply_clarification, then finish_phase.'
    text += '\nContainer paths: /task/spec.md (P/V/solo only), /task/brief.md, /shared/workspace, /shared/reports, /shared/submission. No network or hidden grader access. Up to eight tools per reply.'
    if role == 'verifier':
        text += '\nWorkspace is read-only. For every writing test use a NEW temporary copy in the same command: checkdir=$(mktemp -d); cp -a /shared/workspace/. "$checkdir/"; cd "$checkdir"; then test. Do not reuse old copies. Required output deliverables must exist in the original workspace, not just your temporary copy.'
    if role == 'planner' and not clarification:
        text += '\n' + contract.prompt_instructions()
    text += '\nfinish_phase is the only completion signal. NoAction is not success. Informational messages do not wait for answers.'
    if model.startswith('gpt-'):
        text += '\n' + TEXT_PROTOCOL
    else:
        text += '\nRequest actions via the supplied native function tools. Preserve tool call/result pairing; finish via finish_phase.'
    return text
