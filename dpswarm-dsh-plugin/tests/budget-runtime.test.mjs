import { CLOSEOUT_INSTRUCTION } from '../lib/worker-closeout.js'
import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime, budgetUsage, workerBudgetProfile, validateWorkerDecision } from '../lib/budget-runtime.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

function makeSession(id, parent = null, events = []) {
  return { id, header: { id, ...(parent ? { parentSession: parent, origin: 'subagent', delegationDepth: 1, seedLength: 0 } : {}) }, events }
}
function fixture(config = {}) {
  const root = makeSession('lead'), a = makeSession('a', root.id), b = makeSession('b', root.id), nested = makeSession('nested', a.id)
  const sessions = new Map([root, a, b, nested].map(session => [session.id, session]))
  const journal = new MemoryAuditJournal(), cfg = { workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 2, ...config }
  const runtime = () => new WorkerBudgetRuntime({ config: () => cfg, resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  return { root, a, b, nested, sessions, journal, cfg, runtime, agent: session => ({ session }) }
}
const request = { provider: 'worker-p', model: 'worker-m', messages: [], maxTokens: 200 }
const usage = { inputTokens: 100, cacheReadTokens: 50, outputTokens: 50, reasoningTokens: 20 }
const signal = () => new AbortController().signal

test('manual budgets are independent for siblings and descendants; Lead stays unlimited', async () => {
  const h = fixture(), runtime = h.runtime()
  const a = await runtime.ensure(h.agent(h.a), signal()), b = await runtime.ensure(h.agent(h.b), signal()), nested = await runtime.ensure(h.agent(h.nested), signal())
  assert.equal(await runtime.ensure(h.agent(h.root), signal()), null)
  for (let index = 0; index < 2; index++) {
    const ticket = await runtime.admit(a, request)
    await runtime.settle(ticket, usage, 'stop')
  }
  await assert.rejects(runtime.admit(a, request), { code: 'WORKER_CALL_LIMIT_REACHED' })
  assert.equal(runtime.describe(b).remaining_calls, 2)
  assert.equal(runtime.describe(nested).remaining_calls, 2)
  assert.ok(await runtime.admit(b, request))
  assert.ok(await runtime.admit(nested, request))
})

test('unlimited ignores stale manual values and remains available when audit observation fails', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited', workerTokenLimit: 0, workerCallLimit: 0 })
  const runtime = new WorkerBudgetRuntime({
    config: () => h.cfg, resolveSession: id => h.sessions.get(id), listSessions: () => [...h.sessions.values()],
    journal: { read: async () => { throw Object.assign(new Error('down'), { code: 'DOWN' }) }, transaction: async () => { throw new Error('down') } },
  })
  const state = await runtime.ensure(h.agent(h.a), signal())
  assert.equal(state.profile.mode, 'unlimited')
  assert.ok(await runtime.admit(state, { ...request, maxTokens: undefined }))
  assert.equal(state.auditFailure, 'UNLIMITED_OBSERVATION_NOT_PERSISTED')
})


test('Auto worker budgets are removed: plan is refused and a stored auto setting falls back to manual', async () => {
  const h = fixture({ workerBudgetMode: 'auto', workerTokenLimit: 600000, workerCallLimit: 28 }), runtime = h.runtime()
  await assert.rejects(runtime.plan(h.agent(h.root), { task: 'Draw one SVG.', tokenLimit: 600, callLimit: 1, reason: 'removed' }), { code: 'WORKER_BUDGET_AUTO_REQUIRED' })
  assert.deepEqual(workerBudgetProfile(h.cfg, h.root.id), { mode: 'manual', tokenLimit: 600000, callLimit: 28 })
  const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer'] })
  const grant = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement one SVG.', label: 'implementer' })
  h.a.events.push({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: grant.prompt }] } })
  const state = await runtime.ensure(h.agent(h.a), signal())
  assert.deepEqual(state.profile, { mode: 'manual', tokenLimit: 600000, callLimit: 28 })
  assert.equal(state.decision, null)
  await runtime.finishTeamRun(h.agent(h.root), handle)
})

test('missing ledger rejects an already-progressed limited worker instead of resetting its grant', async () => {
  const h = fixture(), runtime = new WorkerBudgetRuntime({
    config: () => h.cfg, resolveSession: id => h.sessions.get(id), listSessions: () => [...h.sessions.values()],
    journal: { read: async () => ({ root_session_id: 'lead', revision: 0, events: [], head_hash: 'x', missing: true }), transaction: async () => { throw new Error('not used') } },
  })
  h.a.events.push({ type: 'assistant/message', data: { message: { role: 'assistant', content: [] } } })
  await assert.rejects(runtime.ensure(h.agent(h.a), signal()), { code: 'WORKER_BUDGET_LEDGER_MISSING' })
})

test('fixed-team policy is frozen and binds only its issued child prompt', async () => {
  const h = fixture({ workerBudgetMode: 'manual', workerTokenLimit: 700, workerCallLimit: 3 }), runtime = h.runtime()
  const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer'] })
  const grant = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement one SVG.', label: 'implementer' })
  h.a.events.push({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: grant.prompt }] } })
  const state = await runtime.ensure(h.agent(h.a), signal())
  assert.deepEqual(state.profile, { mode: 'manual', tokenLimit: 700, callLimit: 3 })
  await runtime.finishTeamRun(h.agent(h.root), handle)
})

test('usage and configured-profile validation preserve accounting boundaries', () => {
  assert.deepEqual(budgetUsage(usage), { usage, complete: true, total: 200 })
  assert.equal(budgetUsage({ inputTokens: 1 }).complete, false)
  assert.deepEqual(workerBudgetProfile({ workerBudgetMode: 'unlimited', workerTokenLimit: 0, workerCallLimit: 0 }, 'root'), { mode: 'unlimited' })
  assert.throws(() => validateWorkerDecision({ task: 'x', tokenLimit: 2.5, callLimit: 1, reason: 'r' }), { code: 'WORKER_BUDGET_DECISION_INVALID' })
})

test('settled usage above its reservation remains charged and blocks the next worker call', async () => {
  const h = fixture({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 3 }), runtime = h.runtime()
  const state = await runtime.ensure(h.agent(h.a), signal())
  const ticket = await runtime.admit(state, { ...request, maxTokens: 100 })
  await runtime.settle(ticket, { inputTokens: 350, outputTokens: 300 }, 'stop')
  assert.equal(runtime.totals(state).committed_tokens, 650)
  await assert.rejects(runtime.admit(state, { ...request, maxTokens: 1 }), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
})

test('a failed limited-worker settlement remains durably admitted and blocks after cold restore', async () => {
  const h = fixture({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 1 }), originalAppend = h.journal.append.bind(h.journal)
  h.journal.append = async (rootId, type, data) => {
    if (type === 'dpswarm/worker-budget-settled') throw Object.assign(new Error('settlement unavailable'), { code: 'SETTLEMENT_DOWN' })
    return originalAppend(rootId, type, data)
  }
  const first = h.runtime(), state = await first.ensure(h.agent(h.a), signal())
  const ticket = await first.admit(state, request)
  await assert.rejects(first.settle(ticket, usage, 'stop'), { code: 'SETTLEMENT_DOWN' })
  h.journal.append = originalAppend
  const resumed = h.runtime(), restored = await resumed.ensure(h.agent(h.a), signal())
  assert.equal(resumed.totals(restored).unknown_usage_calls, 1)
  await assert.rejects(resumed.admit(restored, request), { code: 'WORKER_CALL_LIMIT_REACHED' })
})

test('a root session override changes actual child enforcement, not merely settings schema', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited', workerTokenLimit: 1, workerCallLimit: 1,
    workerBudgetSessionOverrides: [{ sessionId: 'lead', mode: 'manual', tokenLimit: 300, callLimit: 1 }] })
  const runtime = h.runtime(), state = await runtime.ensure(h.agent(h.a), signal())
  assert.deepEqual(state.profile, { mode: 'manual', tokenLimit: 300, callLimit: 1 })
  await runtime.admit(state, { ...request, maxTokens: 100 })
  await assert.rejects(runtime.admit(state, { ...request, maxTokens: 1 }), { code: 'WORKER_CALL_LIMIT_REACHED' })
})


test('an awaited preflight cannot silently change the user budget before team admission', async t => {
  const changes = [
    ['manual to unlimited', cfg => { cfg.workerBudgetMode = 'unlimited' }],
    ['manual token limit', cfg => { cfg.workerTokenLimit = 1200000 }],
    ['manual call limit', cfg => { cfg.workerCallLimit = 56 }],
    ['new session override', cfg => { cfg.workerBudgetSessionOverrides = [{ sessionId: 'lead', mode: 'manual', tokenLimit: 80000, callLimit: 40 }] }],
  ]
  for (const [name, change] of changes) await t.test(name, async () => {
    const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 28 }), runtime = h.runtime()
    const expectedProfile = workerBudgetProfile(h.cfg, h.root.id)
    let completePreflight
    const preflight = new Promise(resolve => { completePreflight = resolve })
    const attempt = (async () => {
      await preflight
      const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer', 'tester'], expectedProfile })
      return runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement the requested artifact.', label: 'implementer' })
    })()
    change(h.cfg)
    completePreflight()
    await assert.rejects(attempt, { code: 'WORKER_BUDGET_SETTINGS_CHANGED' })
    assert.equal(h.journal.roots.size, 0, 'Rejected admission must not create a team-run or allocation ledger')
    assert.equal(h.journal.queues.size, 0)
    assert.equal(runtime.states.size, 0)
  })
})

test('matching preflight profiles preserve manual user values and unlimited semantics', async t => {
  const modes = [
    { mode: 'manual', expected: { mode: 'manual', tokenLimit: 600000, callLimit: 28 } },
    { mode: 'unlimited', expected: { mode: 'unlimited' } },
  ]
  for (const { mode, expected } of modes) await t.test(mode, async () => {
    const h = fixture({ workerBudgetMode: mode, workerTokenLimit: 600000, workerCallLimit: 28 }), runtime = h.runtime()
    const expectedProfile = workerBudgetProfile(h.cfg, h.root.id)
    await Promise.resolve()
    const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer'], expectedProfile })
    const grant = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement one SVG.', label: 'implementer' })
    h.a.events.push({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: grant.prompt }] } })
    const state = await runtime.ensure(h.agent(h.a), signal())
    assert.deepEqual(grant.profile, expected)
    assert.deepEqual(state.profile, expected)
    if (mode === 'unlimited') assert.equal(runtime.describe(state).remaining_tokens, null)
    await runtime.finishTeamRun(h.agent(h.root), handle)
  })
})

test('a complete session override stays authoritative when unrelated global defaults change during preflight', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited', workerBudgetSessionOverrides: [
    { sessionId: 'lead', mode: 'manual', tokenLimit: 600000, callLimit: 28 },
  ] }), runtime = h.runtime()
  const expectedProfile = workerBudgetProfile(h.cfg, h.root.id)
  await Promise.resolve()
  Object.assign(h.cfg, { workerBudgetMode: 'auto', workerTokenLimit: 1, workerCallLimit: 1 })
  const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer'], expectedProfile })
  const grant = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Use the session-specific allowance.', label: 'implementer' })
  assert.deepEqual(grant.profile, { mode: 'manual', tokenLimit: 600000, callLimit: 28 })
  await runtime.finishTeamRun(h.agent(h.root), handle)
})

test('once admitted, later roles keep the original independent manual budgets after settings change', async () => {
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 28 }), runtime = h.runtime()
  const expectedProfile = workerBudgetProfile(h.cfg, h.root.id)
  const handle = await runtime.beginTeamRun(h.agent(h.root), { roles: ['implementer', 'tester'], expectedProfile })
  const implementer = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement the SVG.', label: 'implementer' })
  // Settings edits after team admission affect future teams only.
  Object.assign(h.cfg, { workerBudgetMode: 'unlimited', workerTokenLimit: 1, workerCallLimit: 1 })
  await Promise.resolve()
  const tester = await runtime.issueTeamWorker(h.agent(h.root), handle, { task: 'Inspect the saved artifact.', label: 'tester' })
  for (const [session, grant] of [[h.a, implementer], [h.b, tester]]) {
    session.events.push({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: grant.prompt }] } })
    assert.deepEqual(grant.profile, expectedProfile)
  }
  const a = await runtime.ensure(h.agent(h.a), signal()), b = await runtime.ensure(h.agent(h.b), signal())
  assert.deepEqual(a.profile, expectedProfile)
  assert.deepEqual(b.profile, expectedProfile)
  const ticket = await runtime.admit(a, request)
  await runtime.settle(ticket, usage, 'stop')
  assert.equal(runtime.describe(a).remaining_calls, 27)
  assert.equal(runtime.describe(b).remaining_calls, 28)
  await runtime.finishTeamRun(h.agent(h.root), handle)
})


test('closeout follows current full-input cost rather than cumulative percent or a sibling grant', async () => {
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 }), r = h.runtime()
  const a = await r.ensure(h.agent(h.a), signal()), b = await r.ensure(h.agent(h.b), signal())
  for (let i = 0; i < 7; i++) await r.settle(await r.admit(a, request), { inputTokens: 60000, outputTokens: 10000 }, 'stop')
  assert.equal(r.describe(a).remaining_tokens, 110000)
  await r.prepareCloseout(a, { inputEstimate: 70000, finalInputEstimate: 69000 }, signal())
  assert.equal(a.closeout.trigger, 'budget_rail')
  assert.equal(a.closeout.calls_at_closeout, 7)
  assert.equal(r.describe(a).remaining_calls, 29)
  await r.prepareCloseout(b, { inputEstimate: 70000, finalInputEstimate: 69000 }, signal())
  assert.equal(b.closeout, undefined)
  const clone = await r.diagnosticsForSession('a'); clone.closeout.mode = 'changed'
  assert.equal(a.closeout.mode, 'final_only')
  assert.equal(await r.diagnosticsForSession('not-a-native-session'), null)
})

test('rail model: normal work preserves final input, output and history growth within its grant', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 4 }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  await r.prepareCloseout(a, { inputEstimate: 10000, finalInputEstimate: 9000 }, signal())
  assert.equal(a.closeout, undefined)
  const output = r.outputLimit(a, 10000, 90000)
  assert.equal(10000 + 2 * output + a.stepBudget.final_report_reserve, 100000, 'reserve the tool-free report plus one copy of this output in its input history')
  assert.equal(r.outputLimit(a, 10000, 2000), 2000, 'an already smaller requested output limit remains unchanged')
  assert.equal(a.profile.tokenLimit, 100000)
})

test('a route output cap wins over the rail-shaped allowance and the requested bound', async () => {
  // Live session 88122af1: a 300k Auto rail shaped a glm worker's maxTokens to
  // 131883 (rail minus input estimate; the forecast had already entered
  // final_only so no report reserve applied) for a route rejecting anything
  // above 131072; the first implementer request died at the provider, zero usage.
  const h = fixture({ workerTokenLimit: 300000, workerCallLimit: 4 }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  await r.prepareCloseout(a, { inputEstimate: 168117, finalInputEstimate: 168117 }, signal())
  assert.equal(a.closeout?.mode, 'final_only')
  assert.equal(r.outputLimit(a, 168117, null), 131883, 'preclamp shape reproduces the live request header')
  assert.equal(a.requestBudget.route_output_cap, null)
  assert.equal(r.outputLimit(a, 168117, null, 131072), 131072, 'the host-observed route cap clamps the shaped allowance')
  assert.equal(r.outputLimit(a, 168117, 140000, 131072), 131072, 'an oversized requested bound is also clamped')
  assert.equal(r.outputLimit(a, 168117, 2000, 131072), 2000, 'an already smaller requested bound is preserved')
  assert.equal(r.outputLimit(a, 168117, null, 500000), 131883, 'a cap above the allowance changes nothing')
  assert.equal(a.requestBudget.route_output_cap, 500000)
  assert.equal(r.outputLimit(a, 168117, null, '131072'), 131883, 'a non-integer cap is ignored, never a partial clamp')
})

test('CM cannot consume the last planned request, and denied CM is not a worker failure or admitted call', async () => {
  // 20,500 sits above the 4,096 closeout report floor (no final-only here) but
  // still tight enough that a 14,000-output CM request must be deferred.
  const h = fixture({ workerTokenLimit: 20500, workerCallLimit: 4 }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  await r.prepareCloseout(a, { inputEstimate: 5000, finalInputEstimate: 5000 }, signal())
  assert.equal(a.closeout, undefined)
  await assert.rejects(r.admit(a, { ...request, purpose: 'compaction', maxTokens: 14000 }), { code: 'WORKER_CLOSEOUT_CM_DEFERRED' })
  assert.equal(r.totals(a).calls, 0)
  assert.equal(a.phase, 'ready')
  assert.equal(a.failure, null)
  assert.equal((await r.diagnosticsForSession('a')).last_denial.code, 'WORKER_CLOSEOUT_CM_DEFERRED')
  assert.ok(await r.admit(a, request))
})

test('cold final-only state defers CM, refuses a second delivery, and keeps exact unknown reservations', async () => {
  const h = fixture({ workerTokenLimit: 10000, workerCallLimit: 1 }), first = h.runtime(), a = await first.ensure(h.agent(h.a), signal())
  await first.prepareCloseout(a, { inputEstimate: 200, finalInputEstimate: 400 }, signal())
  const second = h.runtime(), restored = await second.ensure(h.agent(h.a), signal())
  assert.equal(restored.closeout.mode, 'final_only')
  await assert.rejects(second.admit(restored, { ...request, purpose: 'compaction' }), { code: 'WORKER_CLOSEOUT_CM_DEFERRED' })
  const ticket = await second.admit(restored, { ...request, system: CLOSEOUT_INSTRUCTION, tools: [] })
  await second.settle(ticket, null, 'cancelled')
  const d = await h.runtime().diagnosticsForSession('a')
  assert.equal(d.unknown_usage_calls, 1)
  assert.equal(d.recent[0].observed_tokens, null)
  assert.equal(d.committed_tokens, ticket.call.reserved_tokens)
  assert.equal(d.remaining_calls, 0)
})

test('cancellation before closeout or queued admission does not create a call or reset the allowance', async () => {
  const h = fixture({ workerTokenLimit: 10000, workerCallLimit: 1 }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  const controller = new AbortController(); controller.abort()
  await assert.rejects(r.prepareCloseout(a, { inputEstimate: 100, finalInputEstimate: 400 }, controller.signal), { name: 'AbortError' })
  assert.equal(a.closeout, undefined)
  await assert.rejects(r.admit(a, { ...request, signal: controller.signal }), { name: 'AbortError' })
  assert.equal(r.describe(a).remaining_calls, 1)
  assert.equal(r.describe(a).remaining_tokens, 10000)
})


test('a missing output bound is durably unknown, not recorded as a zero-output reservation', async () => {
  const h = fixture(), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  await assert.rejects(r.admit(a, { ...request, maxTokens: undefined }), { code: 'WORKER_OUTPUT_LIMIT_REQUIRED' })
  const d = await h.runtime().diagnosticsForSession('a')
  assert.equal(d.calls, 0)
  assert.equal(d.last_denial.output_limit, null)
  assert.equal(d.last_denial.required_reservation, null)
  assert.ok(d.last_denial.input_estimate > 0)
})

test('cancellation while awaiting the journal cannot dispatch through unlimited observation fallback', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited' }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  const original = h.journal.transaction.bind(h.journal), abort = new AbortController()
  h.journal.transaction = async (rootId, build) => {
    await Promise.resolve(); abort.abort()
    return original(rootId, build)
  }
  await assert.rejects(r.admit(a, { ...request, signal: abort.signal }), { name: 'AbortError' })
  assert.equal(r.totals(a).calls, 0)
  assert.equal(a.auditFailure, undefined)
})


test('closeout forecasts an additional full recovery request inside the existing grant', async () => {
  const h = fixture({ workerTokenLimit: 90000, workerCallLimit: 10 }), r = h.runtime(), a = await r.ensure(h.agent(h.a), signal())
  await r.prepareCloseout(a, { inputEstimate: 20000, finalInputEstimate: 22000 }, signal())
  assert.equal(a.closeout, undefined, 'a single-report forecast still fits')
  await r.prepareCloseout(a, { inputEstimate: 20000, finalInputEstimate: 22000, recoveryInputEstimate: 23000 }, signal())
  assert.equal(a.closeout.mode, 'final_only')
  assert.equal(a.closeout.recovery_token_reserve, 23000 + Math.ceil(23000 / 3) + 2048)
  assert.equal(a.closeout.recovery_call_reserve, 1)
  assert.equal(a.closeout.remaining_tokens, 90000)
  assert.equal(a.closeout.remaining_calls, 10)
  assert.deepEqual(a.profile, { mode: 'manual', tokenLimit: 90000, callLimit: 10 })
  const cold = await h.runtime().ensure(h.agent(h.a), signal())
  assert.equal(cold.closeout.recovery_token_reserve, a.closeout.recovery_token_reserve)
  assert.equal(cold.closeout.calls_at_closeout, 0)
})
