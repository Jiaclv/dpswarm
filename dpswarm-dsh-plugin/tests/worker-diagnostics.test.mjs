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
  assert.equal(r.closeout.report_status, 'truncated')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.report, null)
  assert.equal(r.closeout.progress.text, 'Saved candidate; validation unfinished.')
  assert.equal(r.closeout.progress.interrupted, true)
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
    closeout: { completion: 'partial', report_status: 'progress', report_available: false, report: null, progress: { text: report },
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
  assert.equal(value.diagnostic.closeout.report_available, false)
  assert.equal(value.diagnostic.closeout.progress.truncated, true)
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

const assistant = (value, step = 1, extra = {}) => ({ type: 'assistant/message', surfaceOp: 'append',
  data: { turn: 1, step, message: { role: 'assistant', content: value }, ...extra } })
const completedArgs = events => args({ session: session(events), error: null,
  result: { output: [], stopReason: 'completed' } })

test('error after progress and tool requests does not turn repeated native output into a report', async () => {
  // Intentionally sounds like a finished report: classification must not read prose.
  const progress = 'Final report: every check passed.'
  const events = [assistant([{ type: 'text', text: progress }, { type: 'tool-call', id: 'probe', name: 'pwsh', arguments: '{}' }]),
    { type: 'tool/call', data: { turn: 1, step: 1, callId: 'probe', name: 'pwsh', arguments: '{}' } },
    assistant([{ type: 'tool-call', id: 'retry', name: 'grep', arguments: '{}' }], 2),
    { type: 'step/start', data: { turn: 1, step: 3 } },
    terminal({ kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } })]
  const r = await workerDiagnostics(args({ session: session(events), role: 'reviewer',
    result: { stopReason: 'error', output: [{ type: 'text', text: progress }] } }))
  assert.equal(r.closeout.report_status, 'progress')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.report, null)
  assert.equal(r.closeout.progress.text, progress)
  assert.equal(r.closeout.progress.event_seq, 0)
  assert.equal(r.closeout.progress.reference.field, 'closeout.progress')
  assert.equal(r.closeout.progress.reference.worker_session_id, 'child')
  const compact = compactDiagnosticRecords([{ role: 'reviewer', diagnostic: r }])[0].diagnostic
  assert.equal(compact.closeout.report_status, 'progress')
  assert.equal(compact.closeout.report_available, false)
  assert.deepEqual(compact.closeout.progress.reference, r.closeout.progress.reference)
})

for (const role of ['implementer', 'tester']) {
  test(`${role} clean native final keeps complete report and event provenance`, async () => {
    // A future-tense sentence is still final if the native execution says so.
    const final = 'Next I will explain the saved file.\n' + 'Complete evidence. '.repeat(100)
    const events = [...write('saved', 'dev/candidate.html'), assistant([{ type: 'text', text: final }], 2), terminal({ kind: 'completed' })]
    const r = await workerDiagnostics({ ...completedArgs(events), role,
      result: { stopReason: 'completed', output: [{ type: 'text', text: final }] } })
    assert.equal(r.closeout.completion, 'completed')
    assert.equal(r.closeout.report_status, 'final')
    assert.equal(r.closeout.report_available, true)
    assert.equal(r.closeout.report.text, final)
    assert.equal(r.closeout.report_source, 'native-result')
    assert.equal(r.closeout.report.reference.event_seq, 2)
    assert.equal(r.closeout.progress, null)
    const compact = compactWorkerEntry({ output: final, diagnostic: r })
    assert.equal(compact.output_status, 'final')
    assert.equal(compact.diagnostic.closeout.report_reference.event_seq, 2)
    assert.equal(compact.output_truncated, true)
    assert.equal(r.closeout.report.text, final)
  })
}

test('empty final message never promotes an earlier progress message or repeated result', async () => {
  const prior = 'Saved a candidate; checking next.'
  const events = [assistant([{ type: 'text', text: prior }]), assistant([{ type: 'text', text: '  ' }], 2), terminal({ kind: 'completed' })]
  const r = await workerDiagnostics({ ...completedArgs(events),
    result: { stopReason: 'completed', output: [{ type: 'text', text: prior }] } })
  assert.equal(r.closeout.report_status, 'progress')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.progress.text, prior)
  assert.equal(r.closeout.report_basis, 'newer-assistant-message-without-final-text')
  const empty = await workerDiagnostics(completedArgs([assistant([{ type: 'text', text: '  ' }]), terminal({ kind: 'completed' })]))
  assert.equal(empty.closeout.report_status, 'missing')
  assert.equal(empty.closeout.report_available, false)
  assert.equal(empty.closeout.progress_available, false)
})

test('native final text can be recovered when the result wrapper omits its output', async () => {
  const r = await workerDiagnostics(completedArgs([assistant([{ type: 'text', text: 'Complete final evidence.' }]), terminal({ kind: 'completed' })]))
  assert.equal(r.closeout.report_status, 'final')
  assert.equal(r.closeout.report_source, 'native-assistant-message')
  assert.equal(r.closeout.report.text, 'Complete final evidence.')
})

test('a completed terminal cannot promote a message followed by a tool call', async () => {
  const events = [assistant([{ type: 'text', text: 'Checking.' }]),
    { type: 'tool/call', data: { turn: 1, step: 1, callId: 'pending', name: 'read', arguments: '{}' } },
    terminal({ kind: 'completed' })]
  const r = await workerDiagnostics(completedArgs(events))
  assert.equal(r.closeout.report_status, 'progress')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.report_basis, 'assistant-tool-continuation')
})

test('output-limited visible text is recoverable but not a final report', async () => {
  const r = await workerDiagnostics(args({ error: null,
    session: session([assistant([{ type: 'text', text: 'Partial verification report' }]), terminal({ kind: 'max-tokens' })]),
    result: { output: [{ type: 'text', text: 'Partial verification report' }], stopReason: 'max-tokens' } }))
  assert.equal(r.closeout.report_status, 'truncated')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.progress.text, 'Partial verification report')
  assert.equal(r.closeout.completion, 'partial')
})

test('a later native turn cannot inherit a prior final report or terminal', async () => {
  const events = [assistant([{ type: 'text', text: 'Old final report' }]), terminal({ kind: 'completed' }),
    { type: 'turn/start', data: { turn: 2 } },
    { type: 'step/start', data: { turn: 2, step: 1 } }]
  const r = await workerDiagnostics(args({ session: session(events), result: { output: [], stopReason: 'error' } }))
  assert.equal(r.native_terminal, null)
  assert.equal(r.closeout.report_status, 'missing')
  assert.equal(r.closeout.report_available, false)
})

const DSML_FINAL = `Now the decisive kinematic check plus a real browser run.

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem .</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`

test('DSML pseudo-call final text is classified as never-executed markup, not as evidence', async () => {
  const events = [assistant([{ type: 'text', text: DSML_FINAL }], 4), terminal({ kind: 'completed' })]
  const budget = { worker_session_id: 'child', root_session_id: 'root', mode: 'manual',
    closeout: { mode: 'final_only', trigger: 'budget_rail' } }
  const r = await workerDiagnostics({ ...completedArgs(events), role: 'tester',
    result: { stopReason: 'completed', output: [{ type: 'text', text: DSML_FINAL }] },
    budget: { diagnosticsForSession: async () => budget } })
  // The native classification still records a completed final text…
  assert.equal(r.closeout.report_status, 'final')
  // …but the output nature says the "calls" in it are markup that never ran.
  assert.deepEqual(r.closeout.output_nature.pseudo_tool_markup.families, ['dsml-markup'])
  assert.deepEqual(r.closeout.output_nature.pseudo_tool_markup.tool_names, ['pwsh'])
  assert.equal(r.closeout.output_nature.final_step_tool_calls, false)
  assert.equal(r.closeout.output_nature.tools_removed_by_budget_rail, true)
  const compact = compactWorkerEntry({ output: DSML_FINAL, diagnostic: r })
  assert.equal(compact.output_status, 'final')
  assert.deepEqual(compact.diagnostic.closeout.output_nature.pseudo_tool_markup.tool_names, ['pwsh'])
  assert.match(compact.diagnostic.closeout.lead_note, /never executed/)
  assert.match(compact.diagnostic.closeout.lead_note, /dpswarm_repair_report/)
})

test('clean final text has no markup flag and no lead note', async () => {
  const final = 'Saved pelican.html; static checks done; browser check unavailable.'
  const events = [assistant([{ type: 'text', text: final }], 2), terminal({ kind: 'completed' })]
  const r = await workerDiagnostics({ ...completedArgs(events),
    result: { stopReason: 'completed', output: [{ type: 'text', text: final }] } })
  assert.equal(r.closeout.report_status, 'final')
  assert.equal(r.closeout.output_nature.pseudo_tool_markup, null)
  assert.equal(r.closeout.output_nature.tools_removed_by_budget_rail, false)
  const compact = compactWorkerEntry({ output: final, diagnostic: r })
  assert.equal(compact.diagnostic.closeout.output_nature.pseudo_tool_markup, null)
  assert.equal(compact.diagnostic.closeout.lead_note, undefined)
})

test('legacy diagnostics without output_nature get a recomputed advisory flag from their text', () => {
  const diagnostic = { version: 'worker-diagnostic-v1', root_session_id: 'root', worker_session_id: 'child',
    native_stop_reason: 'completed', closeout: { completion: 'completed', report_status: 'final', report_available: true,
      report: { text: DSML_FINAL, source: 'native-result' }, candidates: [] } }
  const compact = compactDiagnosticRecords([{ role: 'tester', diagnostic }])[0].diagnostic
  assert.equal(compact.closeout.report_status, 'final')
  assert.deepEqual(compact.closeout.output_nature.pseudo_tool_markup.tool_names, ['pwsh'])
  assert.equal(compact.closeout.output_nature.recomputed_from_text, true)
  assert.match(compact.closeout.lead_note, /never executed/)
  // The stored diagnostic itself is not mutated.
  assert.equal(diagnostic.closeout.output_nature, undefined)
})

test('legacy unclassified report text remains readable as progress without a final claim', () => {
  const diagnostic = { version: 'worker-diagnostic-v1', root_session_id: 'root', worker_session_id: 'child',
    native_stop_reason: 'error', closeout: { completion: 'partial', report_available: true,
      report: { text: 'Older last visible progress', source: 'native-result' }, candidates: [] } }
  const r = compactDiagnosticRecords([{ diagnostic }])[0].diagnostic
  assert.equal(r.closeout.report_status, 'unclassified')
  assert.equal(r.closeout.report_available, false)
  assert.equal(r.closeout.report, null)
  assert.equal(r.closeout.progress.text, 'Older last visible progress')
  assert.equal(diagnostic.closeout.report_available, true)
})


test('worker summary includes omitted workers, preserves unknown reservations and does not invent a team pool', () => {
  const children = Array.from({ length: 7 }, (_, i) => ({ worker_session_id: `child-${i}`, root_session_id: 'root',
    calls: 1, observed_tokens_lower_bound: 100, committed_tokens: 100, unknown_usage_calls: 0, active_calls: 0,
    remaining_tokens: i === 6 ? null : 900, remaining_calls: i === 6 ? null : 9 }))
  children.unshift({ worker_session_id: '98f6', root_session_id: 'root', calls: 5,
    observed_tokens_lower_bound: 251680, committed_tokens: 576693, unknown_usage_calls: 1, active_calls: 0,
    remaining_tokens: 23307, remaining_calls: 23 })
  const original = structuredClone(children)
  const result = compactBudgetStatus({ children, lead_limited: false })
  assert.equal(result.children.length, 6)
  assert.equal(result.children_omitted, 2)
  assert.equal(result.summary.workers, 8)
  assert.equal(result.summary.calls, 12)
  assert.equal(result.summary.observed_tokens_lower_bound, 252380)
  assert.equal(result.summary.committed_tokens, 577393)
  assert.equal(result.summary.unobserved_reserved_tokens, 325013)
  assert.equal(result.summary.unknown_usage_calls, 1)
  assert.equal(result.summary.active_calls, 0)
  assert.equal(result.summary.remaining_tokens, undefined)
  assert.match(result.summary.scope, /Lead usage excluded/)
  assert.match(result.summary.cost_note, /cache.*different prices/)
  assert.equal(result.children.at(-1).remaining_tokens, null)
  assert.deepEqual(children, original)
})

test('worker summary deduplicates identities and refuses ambiguous or incomplete totals', () => {
  const row = { worker_session_id: 'child', calls: 2, observed_tokens_lower_bound: 100,
    committed_tokens: 500, unknown_usage_calls: 1, active_calls: 1 }
  const summary = children => compactBudgetStatus({ children }).summary
  const repeated = summary([row, { ...row }])
  assert.equal(repeated.workers, 1)
  assert.equal(repeated.duplicate_rows_omitted, 1)
  assert.equal(repeated.observed_tokens_lower_bound, 100)
  assert.equal(repeated.unobserved_reserved_tokens, 400)
  assert.equal(repeated.active_calls, 1)
  const conflicting = summary([row, { ...row, committed_tokens: 501 }])
  assert.equal(conflicting.conflicting_workers, 1)
  assert.equal(conflicting.committed_tokens, null)
  assert.equal(conflicting.unobserved_reserved_tokens, null)
  const unavailable = summary([row, { worker_session_id: 'unavailable', error: 'RESTORE_FAILED' }])
  assert.equal(unavailable.workers, 2)
  assert.equal(unavailable.observed_tokens_lower_bound, null)
  assert.equal(unavailable.calls, null)
  const unidentified = summary([row, { calls: 1 }])
  assert.equal(unidentified.unattributed_rows, 1)
  assert.equal(unidentified.committed_tokens, null)
  assert.equal(summary([]).observed_tokens_lower_bound, 0)
})
