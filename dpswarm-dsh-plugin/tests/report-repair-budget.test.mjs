import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { installBudget } from '../lib/budget.js'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { Context } = await import(hostModuleUrl(resolveHostRoot(), 'cordis/lib/index.js'))
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const message = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] })
const signal = () => new AbortController().signal
const request = { provider: 'fixture', model: 'frozen-worker', messages: [], maxTokens: 100 }

function fixture(config = {}) {
  const root = { id: 'lead', header: { id: 'lead' }, events: [] }
  const sessions = new Map([[root.id, root]])
  const cfg = { workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 4, ...config }
  const journal = new MemoryAuditJournal()
  const runtime = () => new WorkerBudgetRuntime({ config: () => cfg,
    resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  const child = (id, prompt, parent = root) => {
    const session = { id, header: { id, origin: 'subagent', parentSession: parent.id, delegationDepth: 1, seedLength: 0 },
      events: [{ type: 'user/message', seq: 0, data: message(prompt) }] }
    sessions.set(id, session)
    return session
  }
  const end = (session, kind = 'completed') => session.events.push({ type: 'turn/end', seq: session.events.length,
    time: 123, data: { reason: { kind } } })
  const original = async (r, label = 'tester') => {
    const decisions = cfg.workerBudgetMode === 'auto'
      ? { [label]: { tokenLimit: 500, callLimit: 2, reason: 'Original Lead decision.' } } : undefined
    const handle = await r.beginTeamRun({ session: root }, { roles: [label], ...(decisions ? { decisions } : {}) })
    const grant = await r.issueTeamWorker({ session: root }, handle, { task: 'Original fixed role task.', label })
    const session = child('original-' + label, grant.prompt), state = await r.ensure({ session }, signal())
    await r.finishTeamRun({ session: root }, handle)
    return { session, state, grant }
  }
  return { root, sessions, cfg, journal, runtime, child, end, original, lead: { session: root } }
}

function allocationOf(h, allocationId) {
  return h.journal.state(h.root.id).events.find(e => e.type === 'dpswarm/worker-budget-allocation'
    && e.data.allocation_id === allocationId)?.data
}

test('report repair gives tester the authenticated finite source remainder and preserves the original grant', async () => {
  const h = fixture(), r = h.runtime(), source = await h.original(r, 'tester')
  await r.settle(await r.admit(source.state, request), { inputTokens: 200, outputTokens: 100 }, 'stop')
  h.end(source.session)

  const grant = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Repair the report format only.' })
  assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 700, callLimit: 3 })
  assert.deepEqual(grant.source_remaining, { tokens: 700, calls: 3 })
  assert.equal(grant.role, 'tester')
  const allocation = allocationOf(h, grant.allocation_id)
  assert.equal(allocation.authority, 'fixed-team-rework')
  assert.equal(allocation.budget_origin, 'source_remaining_report_repair')
  assert.deepEqual(allocation.source_profile, { mode: 'manual', tokenLimit: 1000, callLimit: 4 })
  assert.deepEqual(allocation.source_consumption, { calls: 1, observed_tokens_lower_bound: 300, committed_tokens: 300,
    unknown_usage_calls: 0, active_calls: 0 })
  assert.deepEqual(allocation.source_remaining, { tokens: 700, calls: 3 })
  assert.equal(typeof allocation.source_accounting_sha256, 'string')
  assert.equal(allocation.source_accounting_sha256.length, 64)
  assert.equal(allocation.provider, undefined)
  assert.equal(allocation.model, undefined)

  const repaired = h.child('report-repair-tester', grant.prompt), state = await r.ensure({ session: repaired }, signal())
  assert.deepEqual(state.profile, grant.profile)
  assert.equal(state.policyBinding.source_worker_session_id, source.session.id)
  assert.deepEqual(r.describe(source.state).remaining_tokens, 700)
  assert.deepEqual(source.state.profile, { mode: 'manual', tokenLimit: 1000, callLimit: 4 })
})

test('unknown source usage remains reserved when report repair derives its allowance', async () => {
  const h = fixture(), r = h.runtime(), source = await h.original(r, 'reviewer')
  const unknown = await r.admit(source.state, { ...request, maxTokens: 100 })
  await r.settle(unknown, null, 'error', 'SERVICE_UNAVAILABLE')
  await r.settle(await r.admit(source.state, request), { inputTokens: 25, outputTokens: 25 }, 'stop')
  h.end(source.session, 'error')

  const grant = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Supply the missing report JSON.' })
  const committed = unknown.call.reserved_tokens + 50
  assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 1000 - committed, callLimit: 2 })
  const allocation = allocationOf(h, grant.allocation_id)
  assert.equal(allocation.source_consumption.unknown_usage_calls, 1)
  assert.equal(allocation.source_consumption.committed_tokens, committed)
  assert.deepEqual(allocation.source_remaining, { tokens: 1000 - committed, calls: 2 })
})

test('unlimited source remains unlimited, while implementer and exhausted sources cannot use report repair', async () => {
  const unlimited = fixture({ workerBudgetMode: 'unlimited' }), unlimitedRuntime = unlimited.runtime()
  const reviewer = await unlimited.original(unlimitedRuntime, 'reviewer')
  unlimited.end(reviewer.session)
  const unlimitedGrant = await unlimitedRuntime.issueReportRepair(unlimited.lead,
    { workerSessionId: reviewer.session.id, task: 'Repair the report format.' })
  assert.deepEqual(unlimitedGrant.profile, { mode: 'unlimited' })
  assert.equal(unlimitedGrant.source_remaining, null)
  assert.equal(allocationOf(unlimited, unlimitedGrant.allocation_id).budget_origin, 'source_remaining_report_repair')

  const implementer = fixture(), implementerRuntime = implementer.runtime(), implementation = await implementer.original(implementerRuntime, 'implementer')
  implementer.end(implementation.session)
  await assert.rejects(implementerRuntime.issueReportRepair(implementer.lead,
    { workerSessionId: implementation.session.id, task: 'Repair the report format.' }), { code: 'REPORT_REPAIR_ROLE_UNSUPPORTED' })
  assert.equal(implementer.journal.state(implementer.root.id).events.filter(e => e.type === 'dpswarm/worker-budget-allocation'
    && e.data.budget_origin === 'source_remaining_report_repair').length, 0)

  const exhausted = fixture({ workerTokenLimit: 1000, workerCallLimit: 1 }), exhaustedRuntime = exhausted.runtime()
  const finished = await exhausted.original(exhaustedRuntime, 'tester')
  const ticket = await exhaustedRuntime.admit(finished.state, { ...request, maxTokens: 900 })
  await exhaustedRuntime.settle(ticket, { inputTokens: 800, outputTokens: 200 }, 'stop')
  exhausted.end(finished.session)
  await assert.rejects(exhaustedRuntime.issueReportRepair(exhausted.lead,
    { workerSessionId: finished.session.id, task: 'No budget remains.' }), { code: 'REPORT_REPAIR_NO_REMAINING_BUDGET' })
})

test('report repair rejects route or budget overrides and permits an unbound revoke retry', async () => {
  const h = fixture(), r = h.runtime(), source = await h.original(r, 'tester')
  h.end(source.session)
  await assert.rejects(r.issueReportRepair(h.lead,
    { workerSessionId: source.session.id, task: 'Repair.', provider: 'other-provider' }), { code: 'REPORT_REPAIR_BUDGET_OVERRIDES_NOT_ALLOWED' })
  const first = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Repair once.' })
  await assert.rejects(r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Duplicate.' }), { code: 'REWORK_ALREADY_CLAIMED' })
  assert.deepEqual(await r.revokeRework(h.lead, first.allocation_id), {
    revoked: true, allocation_id: first.allocation_id, source_worker_session_id: source.session.id })
  await assert.rejects(r.ensure({ session: h.child('revoked-report-repair', first.prompt) }, signal()),
    { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  const retry = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Retry once.' })
  assert.notEqual(retry.allocation_id, first.allocation_id)
})

for (const field of ['source_consumption', 'source_accounting_sha256', 'source_profile', 'source_remaining']) {
  test('report repair binding rejects tampered ' + field + ' before child execution', async () => {
    const h = fixture(), r = h.runtime(), source = await h.original(r, 'tester')
    await r.settle(await r.admit(source.state, request), { inputTokens: 200, outputTokens: 100 }, 'stop')
    h.end(source.session)
    const grant = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Repair.' })
    const allocation = allocationOf(h, grant.allocation_id)
    if (field === 'source_consumption') allocation.source_consumption.committed_tokens++
    if (field === 'source_accounting_sha256') allocation.source_accounting_sha256 = '0'.repeat(64)
    if (field === 'source_profile') allocation.source_profile.tokenLimit++
    if (field === 'source_remaining') allocation.source_remaining.tokens++
    await assert.rejects(r.ensure({ session: h.child('tampered-' + field, grant.prompt) }, signal()),
      { code: 'REWORK_SOURCE_INVALID' })
  })
}

test('report repair lineage survives cold restore and can continue from a newer tester remainder', async () => {
  const h = fixture(), r = h.runtime(), source = await h.original(r, 'tester')
  await r.settle(await r.admit(source.state, request), { inputTokens: 100, outputTokens: 50 }, 'stop')
  h.end(source.session)
  const first = await r.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Repair the first report.' })
  const firstSession = h.child('first-report-repair', first.prompt), firstState = await r.ensure({ session: firstSession }, signal())
  await r.settle(await r.admit(firstState, request), { inputTokens: 200, outputTokens: 100 }, 'stop')
  h.end(firstSession)

  const cold = h.runtime(), restored = await cold.ensure({ session: firstSession }, signal())
  assert.deepEqual(restored.profile, { mode: 'fixed', tokenLimit: 850, callLimit: 3 })
  const second = await cold.issueReportRepair(h.lead, { workerSessionId: firstSession.id, task: 'Repair the second report.' })
  assert.deepEqual(second.profile, { mode: 'fixed', tokenLimit: 550, callLimit: 2 })
  const secondSession = h.child('second-report-repair', second.prompt)
  const secondState = await cold.ensure({ session: secondSession }, signal())
  assert.deepEqual(secondState.profile, second.profile)
  const allocation = allocationOf(h, second.allocation_id)
  assert.deepEqual(allocation.source_profile, { mode: 'fixed', tokenLimit: 850, callLimit: 3 })
  assert.deepEqual(allocation.source_consumption, { calls: 1, observed_tokens_lower_bound: 300, committed_tokens: 300,
    unknown_usage_calls: 0, active_calls: 0 })
})


test('installed budget service exposes report repair with the exact 220k source remainder', async () => {
  const h = fixture({ workerTokenLimit: 220000, workerCallLimit: 10 }), ctx = new Context()
  ctx.provide('sessions', { get: id => h.sessions.get(id), list: () => [...h.sessions.values()] })
  const service = installBudget(ctx, () => h.cfg, { journal: h.journal })
  try {
    const source = await h.original(service.runtime, 'reviewer')
    const usages = [
      { inputTokens: 7810, cacheReadTokens: 11648, outputTokens: 457 },
      { inputTokens: 9790, cacheReadTokens: 19840, outputTokens: 2657 },
      { inputTokens: 275, cacheReadTokens: 32256, outputTokens: 3494 },
      { inputTokens: 3529, cacheReadTokens: 35968, outputTokens: 2317 },
      { inputTokens: 30197, cacheReadTokens: 11904, outputTokens: 3211 },
    ]
    for (const usage of usages) await service.runtime.settle(await service.runtime.admit(source.state, request), usage, 'tool-calls')
    h.end(source.session, 'error')
    const grant = await service.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Finish the current candidate report.' })
    assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 44647, callLimit: 5 })
    assert.deepEqual(grant.source_remaining, { tokens: 44647, calls: 5 })
    assert.equal(grant.role, 'reviewer'); assert.equal(grant.source_worker_session_id, source.session.id)
    assert.deepEqual(source.state.profile, { mode: 'manual', tokenLimit: 220000, callLimit: 10 })
    assert.equal(service.runtime.describe(source.state).committed_tokens, 175353)
    await assert.rejects(service.issueReportRepair(h.lead, { workerSessionId: source.session.id, task: 'Duplicate repair' }), { code: 'REWORK_ALREADY_CLAIMED' })
    const allocation = allocationOf(h, grant.allocation_id)
    assert.equal(allocation.budget_origin, 'source_remaining_report_repair')
    assert.deepEqual(allocation.source_consumption, { calls: 5, observed_tokens_lower_bound: 175353, committed_tokens: 175353,
      unknown_usage_calls: 0, active_calls: 0 })
  } finally { service.shutdown() }
})
