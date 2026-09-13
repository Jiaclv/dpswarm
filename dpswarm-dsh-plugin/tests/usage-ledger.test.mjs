import assert from 'node:assert/strict'
import test from 'node:test'
import { usageLedger, leadUsageMessages } from '../lib/usage-ledger.js'

const event = (type, data) => ({ type, data: { root_session_id: 'root', ...data } })
const base = [
  event('dpswarm/worker-budget-allocation', { allocation_id: 'a1', authority: 'fixed-team-run', label: 'implementer', run_id: 'r1', at: 1000 }),
  event('dpswarm/worker-budget-allocation-bound', { allocation_id: 'a1', worker_session_id: 'w-impl', at: 1100 }),
  event('dpswarm/worker-budget-allocation', { allocation_id: 'a2', authority: 'fixed-team-run', label: 'tester', run_id: 'r1', at: 1200 }),
  event('dpswarm/worker-budget-allocation-bound', { allocation_id: 'a2', worker_session_id: 'w-test', at: 1300 }),
  event('dpswarm/worker-budget-admitted', { call_id: 'c1', worker_session_id: 'w-impl', purpose: 'worker', reserved_tokens: 500, started_at: 1400 }),
  event('dpswarm/worker-budget-settled', { call_id: 'c1', worker_session_id: 'w-impl', usage: { inputTokens: 100, outputTokens: 40, cacheReadTokens: 10, cacheWriteTokens: 5, reasoningTokens: 3 }, usage_complete: true, finished_at: 1500 }),
  event('dpswarm/worker-budget-admitted', { call_id: 'c2', worker_session_id: 'w-test', purpose: 'compaction', reserved_tokens: 300, started_at: 1600 }),
  event('dpswarm/worker-budget-settled', { call_id: 'c2', worker_session_id: 'w-test', usage: { inputTokens: 50, outputTokens: 20 }, usage_complete: true, finished_at: 1700 }),
  event('dpswarm/worker-budget-admitted', { call_id: 'c3', worker_session_id: 'w-impl', purpose: 'worker', reserved_tokens: 900, started_at: 1800 }),
]

test('usage ledger classifies activities, charges CM once and keeps unknowns as reservations', () => {
  const ledger = usageLedger(base, [{ usage: { inputTokens: 1000, outputTokens: 200, cacheReadTokens: 60 } }])
  assert.equal(ledger.by_activity.implementation.calls, 1)
  assert.equal(ledger.by_activity.implementation.input_tokens, 100)
  // The compaction call settles under the tester but is classified as cm.
  assert.equal(ledger.by_activity.cm.calls, 1)
  assert.equal(ledger.by_activity.cm.input_tokens, 50)
  assert.equal(ledger.by_activity.verification.calls, 0)
  // The unsettled c3 keeps its reservation; nothing is zero-filled.
  assert.equal(ledger.by_activity.implementation.unknown_usage_calls, 1)
  assert.equal(ledger.totals.reserved_tokens_unknown, 900)
  // Roles see their own calls; the tester's compaction is charged to cm activity only.
  assert.equal(ledger.by_role.implementer.calls, 1)
  assert.equal(ledger.by_role.tester.calls, 1)
  // Lead message usage lands in lead_control.
  assert.equal(ledger.by_activity.lead_control.calls, 1)
  assert.equal(ledger.by_activity.lead_control.input_tokens, 1000)
  assert.equal(ledger.window.started_at, 1000)
  assert.equal(ledger.window.ended_at, 1800)
})

test('rework and report-repair grants are attributed separately', () => {
  const events = [
    event('dpswarm/worker-budget-allocation', { allocation_id: 'a3', authority: 'fixed-team-rework', label: 'implementer', budget_origin: 'fixed_rework' }),
    event('dpswarm/worker-budget-allocation-bound', { allocation_id: 'a3', worker_session_id: 'w-fix' }),
    event('dpswarm/worker-budget-admitted', { call_id: 'c4', worker_session_id: 'w-fix', purpose: 'worker', reserved_tokens: 10 }),
    event('dpswarm/worker-budget-settled', { call_id: 'c4', worker_session_id: 'w-fix', usage: { inputTokens: 7, outputTokens: 3 }, usage_complete: true }),
    event('dpswarm/worker-budget-allocation', { allocation_id: 'a4', authority: 'fixed-team-rework', label: 'tester', budget_origin: 'source_remaining_report_repair' }),
    event('dpswarm/worker-budget-allocation-bound', { allocation_id: 'a4', worker_session_id: 'w-rep' }),
    event('dpswarm/worker-budget-admitted', { call_id: 'c5', worker_session_id: 'w-rep', purpose: 'worker', reserved_tokens: 10 }),
    event('dpswarm/worker-budget-settled', { call_id: 'c5', worker_session_id: 'w-rep', usage: { inputTokens: 5, outputTokens: 2 }, usage_complete: true }),
  ]
  const ledger = usageLedger(events, [])
  assert.equal(ledger.by_activity.product_rework.calls, 1)
  assert.equal(ledger.by_activity.report_repair.calls, 1)
  assert.equal(ledger.by_activity.implementation.calls, 0)
})

test('duplicate journal replay aggregates each settled call exactly once', () => {
  const ledger = usageLedger([...base, ...base], [])
  assert.equal(ledger.by_activity.implementation.calls, 1)
  assert.equal(ledger.totals.reserved_tokens_unknown, 900)
})

test('lead usage extraction filters to assistant messages with usage', () => {
  const session = { events: [
    { type: 'assistant/message', data: { usage: { inputTokens: 5, outputTokens: 2 } } },
    { type: 'assistant/message', data: { text: 'no usage here' } },
    { type: 'tool/result', data: {} },
  ] }
  const messages = leadUsageMessages(session)
  assert.equal(messages.length, 1)
  assert.equal(messages[0].usage.inputTokens, 5)
  assert.deepEqual(leadUsageMessages(null), [])
})
