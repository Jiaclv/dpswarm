import assert from 'node:assert/strict'
import test from 'node:test'
import { workerDiagnostics, compactWorkerEntry, compactBudgetStatus, compactDiagnosticRecords } from '../lib/worker-diagnostics.js'
import { runSubagentToCompletion } from '../lib/subagent-run.js'

function session(events = [], seedLength = 0) {
  return { id: 'child', header: { id: 'child', origin: 'subagent', parentSession: 'root', delegationDepth: 1, seedLength },
    events: events.map((e, seq) => ({ time: 1000 + seq, seq, ...e })) }
}
const terminal = reason => ({ type: 'turn/end', data: { turn: 1, reason } })
const write = (id, path, options = {}) => [
  { type: 'tool/call', data: { turn: 1, step: 1, name: 'write', callId: id, arguments: JSON.stringify({ file_path: path, content: 'candidate' }) } },
  { type: 'tool/result', surfaceOp: 'append', data: { turn: 1, step: 1,
    message: { source: { kind: 'tool', callId: id }, content: [{ type: 'tool-result', toolCallId: id, isError: false, content: [{ type: 'text', text: 'written' }] }] } }, ...options },
]
const args = value => ({ sessionId: 'child', rootId: 'root', role: 'implementer',
  result: { output: [], stopReason: 'error' }, error: Object.assign(new Error('DSH child ended with error'), { code: 'SUBAGENT_NOT_COMPLETED' }),
  cleanup: { physical_cleanup_confirmed: true }, ...value })

test('native UNKNOWN reservation error survives with real shortfall and no inferred zero usage', async () => {
  const native = { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } }
  const budget = { worker_session_id: 'child', root_session_id: 'root', mode: 'auto', tokenLimit: 6000,
    callLimit: 6, unknown_usage_calls: 0, remaining_tokens: 6000, remaining_calls: 6,
    last_denial: { code: 'WORKER_TOKEN_RESERVATION_DENIED', required_reservation: 9231, remaining_tokens: 6000 },
    closeout: null }
  const result = await workerDiagnostics(args({ session: session([terminal(native)]),
    budget: { diagnosticsForSession: async id => { assert.equal(id, 'child'); return budget } } }))
  assert.equal(result.failure.code, 'WORKER_TOKEN_RESERVATION_DENIED')
  assert.equal(result.failure.raw_code, 'UNKNOWN')
  assert.equal(result.failure.category, 'budget')
  assert.deepEqual(result.native_terminal.reason, native)
  assert.equal(result.budget.last_denial.required_reservation, 9231)
  assert.equal(result.closeout.report_available, false)
  assert.equal(result.closeout.completion, 'failed')
})

for (const [message, category] of [['WORKER_TOKEN_LIMIT_REACHED', 'budget'], ['WORKER_CALL_LIMIT_REACHED', 'call_limit']]) {
  test(`native ${message} keeps the specific limit`, async () => {
    const result = await workerDiagnostics(args({ session: session([terminal({ kind: 'error', error: { code: 'UNKNOWN', message } })]) }))
    assert.equal(result.failure.code, message); assert.equal(result.failure.category, category)
  })
}

test('model prose and older budget denial cannot fabricate the current failure category', async () => {
  const message = 'The worker mentioned WORKER_TOKEN_RESERVATION_DENIED but this is another failure'
  const result = await workerDiagnostics(args({ session: session([terminal({ kind: 'error', error: { code: 'UNKNOWN', message } })]),
    result: { stopReason: 'error', output: [{ type: 'text', text: 'WORKER_CALL_LIMIT_REACHED' }] },
    budget: { diagnosticsForSession: async () => ({ root_session_id: 'root', worker_session_id: 'child', last_denial: { code: 'WORKER_TOKEN_LIMIT_REACHED' } }) } }))
  assert.equal(result.failure.category, 'unknown'); assert.equal(result.failure.raw_message, message)
  assert.equal(result.closeout.completion, 'partial')
})

test('fixed timer, user cancellation, parent cancellation and provider failure remain distinct', async () => {
  const controller = new AbortController()
  controller.abort(Object.assign(new Error('wall-time elapsed'), { code: 'WORKER_TIMEOUT' }))
  const timeout = await workerDiagnostics(args({ signal: controller.signal, session: session([terminal({ kind: 'aborted', reason: { kind: 'parent' } })]) }))
  assert.equal(timeout.failure.category, 'timeout'); assert.equal(timeout.failure.source, 'fixed-role-timer')
  for (const [kind, code] of [['user', 'WORKER_USER_CANCELLED'], ['parent', 'WORKER_PARENT_CANCELLED']]) {
    const r = await workerDiagnostics(args({ session: session([terminal({ kind: 'aborted', reason: { kind } })]) }))
    assert.equal(r.failure.code, code); assert.equal(r.failure.category, 'cancelled')
  }
  const provider = await workerDiagnostics(args({ session: session([terminal({ kind: 'error', error: { code: 'SERVICE_UNAVAILABLE', message: 'provider service failure' } })]) }))
  assert.equal(provider.failure.code, 'SERVICE_UNAVAILABLE'); assert.equal(provider.failure.category, 'provider')
  const outputLimit = await workerDiagnostics(args({ session: session([terminal({ kind: 'max-tokens' })]), result: { output: [], stopReason: 'max-tokens' } }))
  assert.equal(outputLimit.failure.category, 'output_limit'); assert.equal(outputLimit.failure.code, 'WORKER_OUTPUT_LIMIT_REACHED')
})

test('failed child preserves successful write/edit candidates and interrupted text without marking complete', async () => {
  const events = [...write('saved', 'dev/candidate.html'),
    { type: 'assistant/message', surfaceOp: 'append', data: { turn: 1, step: 2, interrupted: true,
      message: { content: [{ type: 'reasoning', text: 'private reasoning' }, { type: 'text', text: 'Saved candidate; validation unfinished.' }] } } },
    terminal({ kind: 'aborted', reason: { kind: 'parent' } })]
  const r = await workerDiagnostics(args({ session: session(events) }))
  assert.equal(r.closeout.completion, 'partial'); assert.equal(r.closeout.requires_lead_verification, true)
  assert.equal(r.closeout.report.text, 'Saved candidate; validation unfinished.')
  assert.equal(r.closeout.report.interrupted, true)
  assert.deepEqual(r.closeout.candidates.map(c => [c.path, c.result_seq, c.at]), [['dev/candidate.html', 1, 1001]])
  assert.equal(r.closeout.candidates[0].verification, 'unverified')
  assert.equal(r.closeout.candidates[0].current_file_state, 'unknown')
})

test('inherited writes, replacement results, unmatched and failed calls cannot become candidates', async () => {
  const failed = write('bad', 'dev/bad.html'); failed[1].data.message.content[0].isError = true
  const replacement = write('replace', 'dev/replacement.html', { surfaceOp: { start: 2, end: 3 } })
  const inherited = write('inherited', 'dev/old.html')
  const events = [...inherited, ...replacement, ...failed,
    { type: 'tool/result', data: { turn: 1, step: 1, message: { callId: 'missing', isError: false } } },
    { type: 'tool/code-dispatch', data: { name: 'write', arguments: { file_path: 'dev/nested.html' } } },
    terminal({ kind: 'error', error: { code: 'UNKNOWN', message: 'failed' } })]
  const r = await workerDiagnostics(args({ session: session(events, inherited.length) }))
  assert.deepEqual(r.closeout.candidates, []); assert.equal(r.closeout.candidate_evidence_complete, false)
})

test('wrong native root and wrong budget identity are rejected as evidence, not silently relabelled', async () => {
  const child = session([...write('wrong', 'dev/other.html')]); child.header.parentSession = 'other-root'
  const r = await workerDiagnostics(args({ session: child, budget: { diagnosticsForSession: async () => ({ root_session_id: 'other', worker_session_id: 'child' }) } }))
  assert.equal(r.native_terminal, null); assert.equal(r.budget, null)
  assert.equal(r.evidence.error, 'NATIVE_SESSION_IDENTITY_MISMATCH')
  assert.equal(r.evidence.budget_error, 'WORKER_BUDGET_IDENTITY_MISMATCH')
  assert.deepEqual(r.closeout.candidates, [])
})

test('native cancellation terminal is read after dispose; cleanup failure does not erase original denial', async () => {
  const native = session([...write('saved', 'dev/partial.html')]), controller = new AbortController()
  const handle = { id: 'child', localAgent: { session: native }, result: Promise.resolve({ output: [], stopReason: 'error' }),
    async dispose() {
      native.events.push({ type: 'turn/end', seq: 2, time: 1234, data: { turn: 1,
        reason: { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } } } })
      throw new Error('native worker may still be alive')
    } }
  await assert.rejects(runSubagentToCompletion({ start: async () => handle }, 'spawn',
    { parent: { id: 'root' }, signal: controller.signal }, {
      onTerminal: info => workerDiagnostics({ ...info, session: native, sessionId: 'child', rootId: 'root', role: 'tester' }),
    }), error => {
      assert.equal(error.code, 'WORKER_TOKEN_RESERVATION_DENIED')
      assert.equal(error.details.physicalCleanupConfirmed, false)
      assert.equal(error.details.worker_diagnostic.cleanup.code, 'SUBAGENT_DISPOSAL_FAILED')
      assert.equal(error.details.worker_diagnostic.closeout.completion, 'partial')
      assert.equal(error.details.worker_diagnostic.closeout.candidates.length, 1)
      return true
    })
})

test('successful textual completion has no implied file delivery; cleanup failure makes it partial', async () => {
  const base = { session: session([terminal({ kind: 'completed' })]), error: null,
    result: { output: [{ type: 'text', text: 'report only' }], stopReason: 'completed' } }
  const completed = await workerDiagnostics(args(base))
  assert.equal(completed.closeout.completion, 'completed'); assert.deepEqual(completed.closeout.candidates, [])
  const partial = await workerDiagnostics(args({ ...base, error: Object.assign(new Error('cleanup failed'), { code: 'SUBAGENT_DISPOSAL_FAILED' }),
    cleanup: { physical_cleanup_confirmed: false, code: 'SUBAGENT_DISPOSAL_FAILED' } }))
  assert.equal(partial.closeout.completion, 'partial'); assert.equal(partial.failure.category, 'cleanup')
})


test('bounded model view removes repeated receipts and reports without mutating complete evidence', () => {
  const report = 'report '.repeat(20000), message = 'failure '.repeat(20000)
  const diagnostic = { version: 'worker-diagnostic-v1', role: 'implementer', worker_session_id: 'child',
    native_stop_reason: 'error', native_terminal: { reason: { kind: 'error', error: { code: 'UNKNOWN', message } } },
    failure: { code: 'WORKER_TOKEN_RESERVATION_DENIED', category: 'budget', raw_code: 'UNKNOWN', message },
    budget: { worker_session_id: 'child', root_session_id: 'root', mode: 'manual', tokenLimit: 1,
      remaining_tokens: 1, remaining_calls: 2, unknown_usage_calls: 1, recent: [{ payload: report }],
      last_denial: { code: 'WORKER_TOKEN_RESERVATION_DENIED', required_reservation: 17000, remaining_tokens: 1 } },
    closeout: { completion: 'partial', report_available: true, report: { text: report },
      candidates: Array.from({ length: 12 }, () => ({ path: 'candidate/'.repeat(1000), source: 'native-successful-tool' })), candidate_evidence_complete: false },
    cleanup: { physical_cleanup_confirmed: true } }
  const original = structuredClone(diagnostic)
  const full = { item_id: 'item', code: diagnostic.failure.code, error: message, diagnostic,
    budget: diagnostic.budget, closeout: diagnostic.closeout, details: { worker_diagnostic: diagnostic, physicalCleanupConfirmed: true },
    control_settlement: { ok: true, outcome: 'terminated', failure: { diagnostic } } }
  const value = compactWorkerEntry(full)
  assert.ok(JSON.stringify(value, null, 2).length < 7000)
  assert.equal(value.diagnostic.failure.code, 'WORKER_TOKEN_RESERVATION_DENIED')
  assert.equal(value.diagnostic.budget.unknown_usage_calls, 1)
  assert.equal(value.diagnostic.budget.last_denial.required_reservation, 17000)
  assert.equal(value.diagnostic.closeout.report.truncated, true)
  assert.equal(value.diagnostic.closeout.candidate_count, 12)
  assert.equal(value.diagnostic.closeout.candidates.length, 3)
  assert.equal(value.diagnostic.closeout.candidates[0].path_truncated, true)
  assert.equal(value.details.physicalCleanupConfirmed, true)
  assert.equal(value.control_settlement.ok, true)
  assert.equal(value.details.worker_diagnostic, undefined)
  assert.equal(value.control_settlement.failure, undefined)
  assert.deepEqual(diagnostic, original)
  const root = compactBudgetStatus({ scope: 'per worker', lead_limited: false,
    configured: { mode: 'manual', tokenLimit: 1, callLimit: 2 }, children: Array(20).fill(diagnostic.budget) })
  assert.equal(root.children.length, 6); assert.equal(root.children_omitted, 14)
  assert.ok(JSON.stringify(root, null, 2).length < 6500)
  assert.equal(compactDiagnosticRecords(Array(20).fill({ diagnostic })).length, 6)
})
