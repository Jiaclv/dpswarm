import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { CLOSEOUT_INSTRUCTION } from '../lib/worker-closeout.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const message = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] })
const signal = () => new AbortController().signal
const request = { provider: 'fixture', model: 'frozen-worker', messages: [], maxTokens: 100 }
function fixture(config = {}) {
  const root = { id: 'lead', header: { id: 'lead' }, events: [] }
  const sessions = new Map([[root.id, root]])
  const cfg = { workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 4, reworkBudgetMode: 'unlimited', ...config }
  const journal = new MemoryAuditJournal()
  const runtime = () => new WorkerBudgetRuntime({ config: () => cfg, resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  const child = (id, prompt, parent = root) => {
    const session = { id, header: { id, origin: 'subagent', parentSession: parent.id, delegationDepth: 1, seedLength: 0 },
      events: [{ type: 'user/message', seq: 0, data: message(prompt) }] }
    sessions.set(id, session); return session
  }
  const end = (session, kind = 'completed') => session.events.push({ type: 'turn/end', seq: session.events.length, time: 123, data: { reason: { kind } } })
  const original = async (r, role = 'implementer') => {
    const decisions = cfg.workerBudgetMode === 'auto' ? { [role]: { tokenLimit: 500, callLimit: 2, reason: 'Original Lead decision.' } } : undefined
    const handle = await r.beginTeamRun({ session: root }, { roles: [role], ...(decisions ? { decisions } : {}) })
    const grant = await r.issueTeamWorker({ session: root }, handle, { task: 'Original fixed role task.', label: role })
    const session = child('original-' + role, grant.prompt), state = await r.ensure({ session }, signal())
    await r.finishTeamRun({ session: root }, handle)
    return { session, state, grant }
  }
  return { root, sessions, cfg, journal, runtime, child, end, original, lead: { session: root } }
}

test('exhausted manual worker can rework without caps while its original grant and ledger stay intact', async () => {
  const h = fixture({ workerTokenLimit: 200, workerCallLimit: 1, reworkBudgetMode: 'unlimited' }), r = h.runtime(), old = await h.original(r)
  await r.settle(await r.admit(old.state, request), { inputTokens: 150, outputTokens: 50 }, 'stop')
  h.end(old.session)
  const before = await r.diagnosticsForSession(old.session.id)
  h.cfg.workerTokenLimit = 600000; h.cfg.workerCallLimit = 28
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair the saved geometry.' })
  assert.deepEqual(grant.profile, { mode: 'unlimited' })
  assert.equal(grant.role, 'implementer')
  assert.equal(grant.source_worker_session_id, old.session.id)
  const next = h.child('repair-1', grant.prompt), state = await r.ensure({ session: next }, signal())
  assert.deepEqual(state.profile, grant.profile)
  assert.equal(state.policyBinding.authority, 'fixed-team-rework')
  assert.deepEqual(await r.diagnosticsForSession(old.session.id), before)
  assert.equal(r.describe(state).calls, 0)
  assert.equal(r.describe(state).remaining_tokens, null)
  assert.equal(r.describe(state).remaining_calls, null)
  assert.equal(old.state.profile.tokenLimit, 200)
})

test('initial Auto decision remains limited, but rework needs no decision and rejects budget overrides', async () => {
  const h = fixture({ workerBudgetMode: 'auto', reworkBudgetMode: 'unlimited' }), r = h.runtime(), old = await h.original(r); h.end(old.session)
  for (const override of [{ decision: { tokenLimit: 9000, callLimit: 7, reason: 'not applicable' } },
    { expectedProfile: { mode: 'auto' } }, { tokenLimit: 1 }, { callLimit: 1 }, { mode: 'manual' }]) {
    await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Fix feedback.', ...override }), { code: 'REWORK_BUDGET_OVERRIDES_NOT_ALLOWED' })
  }
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Fix feedback.' })
  assert.deepEqual(grant.profile, { mode: 'unlimited' })
  const state = await r.ensure({ session: h.child('auto-repair', grant.prompt) }, signal())
  assert.equal(state.decision, null)
  assert.equal(old.state.profile.tokenLimit, 500)
  assert.equal(old.state.profile.callLimit, 2)
  assert.equal(old.state.decision.reason, 'Original Lead decision.')
  const issued = (await h.journal.read(h.root.id)).events.find(e => e.data.allocation_id === grant.allocation_id).data
  assert.equal(issued.decided_by, 'user_authorized_unlimited_rework')
})

test('unlimited rework still has a one-use role allocation and never imports stale manual caps', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  h.cfg.workerBudgetMode = 'unlimited'; h.cfg.workerTokenLimit = 1; h.cfg.workerCallLimit = 1
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.', decision: { tokenLimit: 2, callLimit: 2, reason: 'not allowed' } }), { code: 'REWORK_BUDGET_OVERRIDES_NOT_ALLOWED' })
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' })
  assert.deepEqual(grant.profile, { mode: 'unlimited' })
  const next = h.child('unlimited-repair', grant.prompt), state = await r.ensure({ session: next }, signal())
  assert.equal(r.describe(state).remaining_tokens, null)
  assert.equal(r.describe(state).remaining_calls, null)
  await assert.rejects(r.ensure({ session: h.child('copy', grant.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
})

test('unknown usage and CM stay in the old ledger at their reservation; unlimited rework is separately accounted', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r)
  const cm = await r.admit(old.state, { ...request, purpose: 'compaction' })
  await r.settle(cm, null, 'error', 'SERVICE_UNAVAILABLE')
  await r.settle(await r.admit(old.state, request), { inputTokens: 25, outputTokens: 25 }, 'stop')
  h.end(old.session, 'error')
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Review the partial candidate.' })
  assert.deepEqual(grant.profile, { mode: 'unlimited' })
  const audit = await h.journal.read(h.root.id)
  const allocation = audit.events.find(e => e.data.allocation_id === grant.allocation_id).data
  assert.equal(allocation.source_consumption.calls, 2)
  assert.equal(allocation.source_consumption.unknown_usage_calls, 1)
  assert.equal(allocation.source_consumption.committed_tokens, cm.call.reserved_tokens + 50)
  assert.equal(allocation.budget_origin, 'unlimited_rework')
  const next = h.child('fresh-repair', grant.prompt); await r.ensure({ session: next }, signal())
  const cold = await h.runtime().diagnosticsForSession(old.session.id)
  assert.equal(cold.recent[0].observed_tokens, null)
  assert.equal(cold.committed_tokens, cm.call.reserved_tokens + 50)
})

test('in-flight or unterminated source fails closed even if a controller tries to issue rework', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r)
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' }), { code: 'REWORK_SOURCE_NOT_TERMINAL' })
  await r.admit(old.state, request); h.end(old.session, 'error')
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' }), { code: 'REWORK_SOURCE_UNSETTLED' })
  assert.equal((await h.journal.read(h.root.id)).events.filter(e => e.data.authority === 'fixed-team-rework').length, 0)
})

test('parallel issue has one winner; unbound revoke permits another attempt, bound claims cannot be revoked or replayed', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  const input = { workerSessionId: old.session.id, task: 'Repair once.' }
  const results = await Promise.allSettled([r.issueRework(h.lead, input), h.runtime().issueRework(h.lead, input)])
  assert.equal(results.filter(x => x.status === 'fulfilled').length, 1)
  assert.equal(results.find(x => x.status === 'rejected').reason.code, 'REWORK_ALREADY_CLAIMED')
  const abandoned = results.find(x => x.status === 'fulfilled').value
  await r.revokeRework(h.lead, abandoned.allocation_id)
  await assert.rejects(r.ensure({ session: h.child('revoked', abandoned.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  const grant = await r.issueRework(h.lead, input)
  const next = h.child('winner', grant.prompt); await r.ensure({ session: next }, signal())
  assert.equal((await r.revokeRework(h.lead, grant.allocation_id)).bound, true)
  await assert.rejects(r.issueRework(h.lead, input), { code: 'REWORK_ALREADY_CLAIMED' })
})

test('only the latest rework child may extend the lineage, including after cold restore and changed settings', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r, 'tester'); h.end(old.session)
  const first = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Correct first feedback.' })
  const next = h.child('second', first.prompt), nextState = await r.ensure({ session: next }, signal())
  await r.settle(await r.admit(nextState, request), { inputTokens: 30, outputTokens: 20 }, 'stop'); h.end(next)
  const cold = h.runtime(), restored = await cold.ensure({ session: next }, signal())
  assert.equal(cold.describe(restored).committed_tokens, 50)
  h.cfg.workerTokenLimit = 80000; h.cfg.workerCallLimit = 9
  const second = await cold.issueRework(h.lead, { workerSessionId: next.id, task: 'Correct second feedback.' })
  assert.equal(second.role, 'tester'); assert.equal(second.source_worker_session_id, next.id)
  assert.deepEqual(second.profile, { mode: 'unlimited' })
  const last = h.child('third', second.prompt); await cold.ensure({ session: last }, signal())
  assert.deepEqual((await h.runtime().ensure({ session: last }, signal())).profile, { mode: 'unlimited' })
  await assert.rejects(cold.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Fork old grant.' }), { code: 'REWORK_ALREADY_CLAIMED' })
})

test('cross-root, arbitrary native children and tampered prompts cannot claim a fixed-role rework grant', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  const other = { id: 'other-root', header: { id: 'other-root' }, events: [] }; h.sessions.set(other.id, other)
  await assert.rejects(r.issueRework({ session: other }, { workerSessionId: old.session.id, task: 'Steal.' }), { code: 'REWORK_SOURCE_UNAVAILABLE' })
  const plain = h.child('plain-native', 'Not a fixed role.'); await r.ensure({ session: plain }, signal()); h.end(plain)
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: plain.id, task: 'Not authorized.' }), { code: 'REWORK_SOURCE_NOT_FIXED_TEAM' })
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' })
  await assert.rejects(r.ensure({ session: h.child('tampered', grant.prompt + ' changed') }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  await assert.rejects(r.ensure({ session: h.child('other-child', grant.prompt, other) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  await assert.rejects(r.issueRework({ session: old.session }, { workerSessionId: old.session.id, task: 'Self authorize.' }), { code: 'WORKER_BUDGET_LEAD_REQUIRED' })
})

test('rework never reads current budget settings or changes the initial frozen worker profile', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  const originalProfile = structuredClone(old.state.profile)
  h.cfg.workerBudgetMode = 'invalid-new-setting'; h.cfg.workerTokenLimit = -1; h.cfg.workerCallLimit = -1
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' })
  const state = await r.ensure({ session: h.child('frozen', grant.prompt) }, signal())
  assert.deepEqual(state.profile, { mode: 'unlimited' })
  assert.deepEqual(old.state.profile, originalProfile)
  assert.equal(h.cfg.workerBudgetMode, 'invalid-new-setting')
})

test('unlimited rework grant does not install a budget lock on the original source', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Separate grant.' })
  // Control-plane session lifecycle is checked separately; budget accounting
  // itself must not silently revoke the user's original allowance.
  assert.ok(await r.admit(old.state, request))
})


test('concurrent child binding has one winner and revocation cannot race into a consumed grant', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair once.' })
  const children = [h.child('race-a', grant.prompt), h.child('race-b', grant.prompt)]
  const bound = await Promise.allSettled(children.map(session => h.runtime().ensure({ session }, signal())))
  assert.equal(bound.filter(v => v.status === 'fulfilled').length, 1)
  assert.equal(bound.find(v => v.status === 'rejected').reason.code, 'WORKER_BUDGET_DECISION_REQUIRED')
  assert.equal((await r.revokeRework(h.lead, grant.allocation_id)).bound, true)
  const events = (await h.journal.read(h.root.id)).events
  assert.equal(events.filter(e => e.type === 'dpswarm/worker-budget-allocation-bound' && e.data.allocation_id === grant.allocation_id).length, 1)
  assert.equal(events.filter(e => e.type === 'dpswarm/worker-budget-rework-revoked' && e.data.allocation_id === grant.allocation_id).length, 0)
  const revision = (await h.journal.read(h.root.id)).revision
  const outcome = await r.revokeRework(h.lead, grant.allocation_id)
  assert.equal(outcome.revoked, false); assert.equal(outcome.bound, true)
  assert.equal((await h.journal.read(h.root.id)).revision, revision)
})

test('unlimited rework still requires its authenticated frozen record to persist before running', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r); h.end(old.session)
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' })
  const original = h.journal.transaction.bind(h.journal)
  h.journal.transaction = (rootId, build) => original(rootId, async snapshot => {
    const proposed = await build(snapshot)
    if (proposed.events.some(e => e.type === 'dpswarm/worker-budget-frozen' && e.data.worker_session_id === 'cannot-freeze')) throw Object.assign(new Error('offline'), { code: 'FREEZE_OFFLINE' })
    return proposed
  })
  await assert.rejects(r.ensure({ session: h.child('cannot-freeze', grant.prompt) }, signal()), { code: 'FREEZE_OFFLINE' })
  assert.equal(r.states.has('cannot-freeze'), false)
  assert.equal((await r.revokeRework(h.lead, grant.allocation_id)).bound, true)
})


test('native max-tokens is a terminal source while unknown terminal reasons still fail closed', async () => {
  const h = fixture(), r = h.runtime(), old = await h.original(r)
  await r.settle(await r.admit(old.state, request), { inputTokens: 20, outputTokens: 100 }, 'max-tokens')
  h.end(old.session, 'unknown-plugin-kind')
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Finish the truncated handoff.' }), { code: 'REWORK_SOURCE_NOT_TERMINAL' })
  h.end(old.session, 'max-tokens')
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Finish the truncated handoff.' })
  assert.deepEqual(grant.profile, { mode: 'unlimited' })
  const state = await r.ensure({ session: h.child('output-limit-rework', grant.prompt) }, signal())
  assert.equal(state.policyBinding.source_worker_session_id, old.session.id)
  const allocation = (await h.journal.read(h.root.id)).events.find(e => e.data.allocation_id === grant.allocation_id).data
  assert.equal(allocation.source_terminal.kind, 'max-tokens')
  assert.equal(allocation.source_consumption.committed_tokens, 120)
})

test('fixed rework mode issues an independently limited grant and enforces it like any limited worker', async () => {
  const h = fixture({ reworkBudgetMode: 'fixed', reworkTokenLimit: 100000, reworkCallLimit: 4 })
  const r = h.runtime(), old = await h.original(r)
  await r.settle(await r.admit(old.state, request), { inputTokens: 150, outputTokens: 50 }, 'stop')
  h.end(old.session)
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair the saved geometry.' })
  assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 100000, callLimit: 4 })
  const next = h.child('fixed-repair', grant.prompt), state = await r.ensure({ session: next }, signal())
  assert.deepEqual(state.profile, grant.profile)
  assert.equal(state.policyBinding.authority, 'fixed-team-rework')
  assert.equal(r.describe(state).remaining_tokens, 100000)
  assert.equal(r.describe(state).remaining_calls, 4)
  const issued = (await h.journal.read(h.root.id)).events.find(e => e.data.allocation_id === grant.allocation_id).data
  assert.equal(issued.budget_origin, 'fixed_rework')
  assert.equal(issued.decided_by, 'user_settings_at_rework')
  // The initial worker grant stays intact and separately accounted.
  assert.equal(old.state.profile.tokenLimit, 1000)
  await r.settle(await r.admit(state, request), { inputTokens: 40000, outputTokens: 10000 }, 'stop')
  // Limited path applies: final-only closeout and the hard admission floor.
  await r.prepareCloseout(state, { inputEstimate: 30000, finalInputEstimate: 30000 }, signal())
  assert.equal(state.closeout.mode, 'final_only')
  await r.settle(await r.admit(state, { ...request, system: CLOSEOUT_INSTRUCTION, tools: [], maxTokens: 100 }), { inputTokens: 20000, outputTokens: 5000 }, 'stop')
  // Rail closeout admits a tool-attempt step plus the report call, then refuses.
  await r.settle(await r.admit(state, { ...request, system: CLOSEOUT_INSTRUCTION, tools: [{ name: 'write' }], maxTokens: 100 }), { inputTokens: 100, outputTokens: 50 }, 'stop')
  await assert.rejects(r.admit(state, { ...request, system: CLOSEOUT_INSTRUCTION, tools: [], maxTokens: 100 }), { code: 'WORKER_CLOSEOUT_ALREADY_SENT' })
})

test('fixed rework rejects oversize reservations and its lineage chains and restores correctly', async () => {
  const h = fixture({ reworkBudgetMode: 'fixed', reworkTokenLimit: 1000, reworkCallLimit: 5 })
  const r = h.runtime(), old = await h.original(r); h.end(old.session)
  const first = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'First repair.' })
  const next = h.child('fixed-first', first.prompt), state = await r.ensure({ session: next }, signal())
  await r.settle(await r.admit(state, request), { inputTokens: 700, outputTokens: 200 }, 'stop')
  await assert.rejects(r.admit(state, { ...request, maxTokens: 200 }), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  h.end(next)
  // Cold restore of a fixed rework worker keeps the limited profile.
  const cold = h.runtime(), restored = await cold.ensure({ session: next }, signal())
  assert.deepEqual(restored.profile, { mode: 'fixed', tokenLimit: 1000, callLimit: 5 })
  assert.equal(cold.describe(restored).remaining_tokens, 100)
  // Chained rework of the fixed rework worker is itself fixed and validates lineage.
  const second = await cold.issueRework(h.lead, { workerSessionId: next.id, task: 'Second repair.' })
  assert.deepEqual(second.profile, { mode: 'fixed', tokenLimit: 1000, callLimit: 5 })
  const last = h.child('fixed-second', second.prompt), lastState = await cold.ensure({ session: last }, signal())
  assert.equal(lastState.policyBinding.authority, 'fixed-team-rework')
  assert.equal(lastState.policyBinding.source_worker_session_id, next.id)
})

test('fixed rework mode requires valid positive limits and ignores broken initial-worker settings', async () => {
  const h = fixture({ reworkBudgetMode: 'fixed', reworkTokenLimit: 0, reworkCallLimit: 28 })
  const r = h.runtime(), old = await h.original(r); h.end(old.session)
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' }), { code: 'REWORK_BUDGET_LIMIT_INVALID' })
  h.cfg.reworkBudgetMode = 'nonsense'; h.cfg.reworkTokenLimit = 1000
  await assert.rejects(r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' }), { code: 'REWORK_BUDGET_MODE_INVALID' })
  // The rework settings are self-contained: broken initial-worker values never leak in.
  h.cfg.reworkBudgetMode = 'fixed'; h.cfg.workerBudgetMode = 'invalid-new-setting'; h.cfg.workerTokenLimit = -1; h.cfg.workerCallLimit = -1
  const grant = await r.issueRework(h.lead, { workerSessionId: old.session.id, task: 'Repair.' })
  assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 1000, callLimit: 28 })
})
