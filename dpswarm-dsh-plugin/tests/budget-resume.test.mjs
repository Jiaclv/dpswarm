import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime, workerBudgetProfile } from '../lib/budget-runtime.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { installBudget } from '../lib/budget.js'
import { hostModuleUrl, resolveHostRoot } from '../lib/host-modules.js'
const { Context } = await import(hostModuleUrl(resolveHostRoot(), 'cordis/lib/index.js'))

const signal = () => new AbortController().signal
const request = { provider: 'fixture', model: 'frozen-model', messages: [], maxTokens: 100 }
const eventName = suffix => `dpswarm/worker-budget-${suffix}`
function fixture(config = {}) {
  const root = { id: 'lead', header: { id: 'lead' }, events: [] }
  const sessions = new Map([[root.id, root]])
  const cfg = { workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 4, ...config }
  const journal = new MemoryAuditJournal()
  const runtime = () => new WorkerBudgetRuntime({ config: () => cfg, resolveSession: id => sessions.get(id),
    listSessions: () => [...sessions.values()], journal })
  const child = (id, prompt, parent = root) => {
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1, seedLength: 0 },
      events: [{ type: 'user/message', seq: 0, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: prompt }] } }] }
    sessions.set(id, session)
    return session
  }
  const events = suffix => journal.state(root.id).events.filter(e => e.type === eventName(suffix))
  const start = async (r, roles = ['implementer', 'tester', 'reviewer']) => r.beginTeamRun({ session: root }, { roles,
    expectedProfile: workerBudgetProfile(cfg, root.id) })
  const resume = (r, handle, roles = ['tester', 'reviewer']) => r.resumeTeamRun({ session: root }, {
    runId: handle.run_id, roles, expectedProfile: workerBudgetProfile(cfg, root.id) })
  return { root, lead: { session: root }, sessions, cfg, journal, runtime, child, events, start, resume }
}

test('resume uses original unissued-role budgets and real consumption; original usage is unchanged', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  const implGrant = await r.issueTeamWorker(h.lead, original, { label: 'implementer', task: 'Produce the original artifact.' })
  const impl = h.child('implementation', implGrant.prompt), implementation = await r.ensure({ session: impl }, signal())
  await r.settle(await r.admit(implementation, request), { inputTokens: 120, outputTokens: 80 }, 'stop')
  const initialUsage = r.totals(implementation)
  await r.finishTeamRun(h.lead, original)

  const recovery = h.runtime(), resumed = await h.resume(recovery, original)
  assert.equal(resumed.run_id, original.run_id)
  assert.equal(typeof resumed.resume_id, 'string')
  const tester = await recovery.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Verify the saved artifact.' })
  const testSession = h.child('tester', tester.prompt), testState = await recovery.ensure({ session: testSession }, signal())
  assert.deepEqual(testState.profile, { mode: 'manual', tokenLimit: 1000, callLimit: 4 })
  assert.equal(testState.policyBinding.run_id, original.run_id)
  assert.equal(testState.policyBinding.resume_id, resumed.resume_id)
  await recovery.settle(await recovery.admit(testState, request), { inputTokens: 40, outputTokens: 60 }, 'stop')
  assert.equal(recovery.describe(testState).remaining_tokens, 900)
  assert.equal(recovery.describe(testState).remaining_calls, 3)
  assert.deepEqual(r.totals(implementation), initialUsage)
  const restoredImpl = await recovery.ensure({ session: impl }, signal())
  assert.deepEqual(recovery.totals(restoredImpl), initialUsage)
  assert.equal(h.events('team-run').length, 1)
  assert.equal(h.events('team-run-resumed').length, 1)
  assert.equal(h.events('allocation').length, 2)
  assert.equal(h.events('allocation').at(-1).data.budget_origin, 'unissued_original_role')
  await recovery.finishTeamRun(h.lead, resumed)
  const cold = h.runtime(), restored = await cold.ensure({ session: testSession }, signal())
  assert.equal(cold.describe(restored).remaining_tokens, 900)
  assert.equal(h.events('team-run-ended').length, 1)
  assert.equal(h.events('team-run-resume-ended').length, 1)
})

test('resume preserves the original unlimited policy; historical auto ledgers stay replayable', async () => {
  {
    const h = fixture({ workerBudgetMode: 'unlimited' }), r = h.runtime(), original = await h.start(r)
    await r.finishTeamRun(h.lead, original)
    const resumed = await h.resume(r, original)
    const grant = await r.issueTeamWorker(h.lead, resumed, { label: 'reviewer', task: 'Review the saved candidate.' })
    const session = h.child('reviewer', grant.prompt), state = await r.ensure({ session }, signal())
    assert.deepEqual(state.profile, { mode: 'unlimited' })
    await r.finishTeamRun(h.lead, resumed)
  }
  // A stored auto setting no longer produces auto profiles; a fresh team run
  // freezes the manual fallback instead (replay of historical auto ledgers is
  // covered by their own frozen records).
  const h = fixture({ workerBudgetMode: 'auto', workerTokenLimit: 600000, workerCallLimit: 28 })
  assert.deepEqual(workerBudgetProfile(h.cfg, h.root.id), { mode: 'manual', tokenLimit: 600000, callLimit: 28 })
})

test('recovery rejects missing, active, foreign-root, unexpected roles, and budget overrides without a claim', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r, ['implementer', 'tester'])
  await assert.rejects(r.resumeTeamRun(h.lead, { runId: 'missing', roles: ['tester'] }), { code: 'WORKER_BUDGET_RESUME_SOURCE_INVALID' })
  await assert.rejects(h.resume(r, original, ['tester']), { code: 'WORKER_BUDGET_RESUME_SOURCE_NOT_ENDED' })
  await r.finishTeamRun(h.lead, original)
  await assert.rejects(h.resume(r, original, ['reviewer']), { code: 'WORKER_BUDGET_RESUME_ROLE_NOT_PLANNED' })
  for (const roles of [[], ['implementer'], ['tester', 'tester'], ['other']]) {
    await assert.rejects(h.resume(r, original, roles), { code: 'WORKER_BUDGET_RESUME_REQUEST_INVALID' })
  }
  await assert.rejects(r.resumeTeamRun(h.lead, { runId: original.run_id, roles: ['tester'], tokenLimit: 999999 }), { code: 'WORKER_BUDGET_RESUME_REQUEST_INVALID' })
  const foreign = { id: 'foreign', header: { id: 'foreign' }, events: [] }
  h.sessions.set(foreign.id, foreign)
  await assert.rejects(r.resumeTeamRun({ session: foreign }, { runId: original.run_id, roles: ['tester'] }), { code: 'WORKER_BUDGET_RESUME_SOURCE_INVALID' })
  const child = h.child('nested-lead', 'Cannot resume as child.')
  await assert.rejects(r.resumeTeamRun({ session: child }, { runId: original.run_id, roles: ['tester'] }), { code: 'WORKER_BUDGET_LEAD_REQUIRED' })
  assert.equal(h.events('team-run-resumed').length, 0)
})

test('even an unbound revoked original role cannot be reissued or revived by a different role recovery', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  const revoked = await r.issueTeamWorker(h.lead, original, { label: 'tester', task: 'Original tester.' })
  await r.finishTeamRun(h.lead, original)
  await assert.rejects(h.resume(r, original, ['tester']), { code: 'WORKER_BUDGET_ROLE_ALREADY_ISSUED' })
  const resumed = await h.resume(r, original, ['reviewer'])
  await assert.rejects(r.ensure({ session: h.child('revoked-tester', revoked.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  await assert.rejects(r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Try again.' }), { code: 'WORKER_BUDGET_RESUME_ROLE_NOT_PLANNED' })
  await r.finishTeamRun(h.lead, resumed)
})

test('concurrent runtimes claim only once, and concurrent issue calls allocate a role only once', async () => {
  const h = fixture(), r = h.runtime(), other = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  const claims = await Promise.allSettled([h.resume(r, original), h.resume(other, original)])
  assert.equal(claims.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(claims.find(result => result.status === 'rejected').reason.code, 'WORKER_BUDGET_RESUME_ALREADY_CLAIMED')
  const index = claims.findIndex(result => result.status === 'fulfilled'), winner = index === 0 ? r : other
  const resumed = claims[index].value
  const issues = await Promise.allSettled([1, 2].map(() => winner.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Same role once.' })))
  assert.equal(issues.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(issues.find(result => result.status === 'rejected').reason.code, 'WORKER_BUDGET_ROLE_ALREADY_ISSUED')
  assert.equal(h.events('allocation').length, 1)
  await winner.finishTeamRun(h.lead, resumed)
  await assert.rejects(h.resume(h.runtime(), original), { code: 'WORKER_BUDGET_RESUME_ALREADY_CLAIMED' })
})

test('current and expected policies must equal the frozen policy, including after a queued claim', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  h.cfg.workerTokenLimit = 2000
  await assert.rejects(h.resume(r, original), { code: 'WORKER_BUDGET_SETTINGS_CHANGED' })
  h.cfg.workerTokenLimit = 1000
  await assert.rejects(r.resumeTeamRun(h.lead, { runId: original.run_id, roles: ['tester'], expectedProfile: { mode: 'unlimited' } }), { code: 'WORKER_BUDGET_SETTINGS_CHANGED' })
  const queued = h.resume(r, original)
  h.cfg.workerBudgetMode = 'unlimited'
  await assert.rejects(queued, { code: 'WORKER_BUDGET_SETTINGS_CHANGED' })
  assert.equal(h.events('team-run-resumed').length, 0)
  h.cfg.workerBudgetMode = 'manual'
  const resumed = await h.resume(r, original)
  h.cfg.workerCallLimit = 8
  await assert.rejects(r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Do not silently increase policy.' }), { code: 'WORKER_BUDGET_SETTINGS_CHANGED' })
  assert.equal(h.events('allocation').length, 0)
  await r.finishTeamRun(h.lead, resumed)
})

test('finish revokes only new unbound grants, preserves bound workers, and never opens old handles', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  const resumed = await h.resume(r, original)
  const testGrant = await r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Check saved artifact.' })
  const reviewGrant = await r.issueTeamWorker(h.lead, resumed, { label: 'reviewer', task: 'Inspect evidence.' })
  const testSession = h.child('bound-tester', testGrant.prompt), testState = await r.ensure({ session: testSession }, signal())
  await r.finishTeamRun(h.lead, resumed)
  await r.finishTeamRun(h.lead, resumed)
  assert.ok(await r.admit(testState, request))
  await assert.rejects(r.ensure({ session: h.child('late-reviewer', reviewGrant.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  await assert.rejects(r.issueTeamWorker(h.lead, resumed, { label: 'reviewer', task: 'Reuse closed handle.' }), { code: 'WORKER_BUDGET_TEAM_HANDLE_INVALID' })
  await assert.rejects(r.issueTeamWorker(h.lead, original, { label: 'tester', task: 'Reuse original handle.' }), { code: 'WORKER_BUDGET_TEAM_HANDLE_INVALID' })
  assert.equal(h.events('team-run-resume-ended').length, 1)
})

test('opaque handles, descendant/subtask escapes, and forged resume identities cannot allocate or bind', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  const resumed = await h.resume(r, original)
  await assert.rejects(r.issueTeamWorker(h.lead, { ...resumed }, { label: 'tester', task: 'Forged handle.' }), { code: 'WORKER_BUDGET_TEAM_HANDLE_INVALID' })
  for (const extra of [{ subtask: 'anything' }, { subtaskIndex: 0 }, { attempt: 1 }]) {
    await assert.rejects(r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'No new role variants.', ...extra }), { code: 'WORKER_BUDGET_RESUME_ROLE_NOT_PLANNED' })
  }
  const grant = await r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'One authorized tester.' })
  h.events('allocation').at(-1).data.resume_id = 'forged'
  await assert.rejects(r.ensure({ session: h.child('forged-binding', grant.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  await r.finishTeamRun(h.lead, resumed)
})


test('real Cordis budget service exposes resume and resumed verifiers retain report-repair lineage', async () => {
  const h = fixture(), ctx = new Context()
  ctx.provide('sessions', { get: id => h.sessions.get(id), list: () => [...h.sessions.values()] })
  const service = installBudget(ctx, () => h.cfg, { journal: h.journal })
  try {
    const original = await service.beginTeamRun(h.lead, { roles: ['implementer', 'tester'],
      expectedProfile: { mode: 'manual', tokenLimit: 1000, callLimit: 4 } })
    await service.finishTeamRun(h.lead, original)
    const resumed = await service.resumeTeamRun(h.lead, { runId: original.run_id, roles: ['tester'],
      expectedProfile: original.profile })
    assert.deepEqual(await service.teamRunRecoveryStatus(h.lead, { runId: original.run_id }),
      { claimed: true, resume_id: resumed.resume_id, ended: false, issued_roles: [] })
    const grant = await service.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Verify the sealed existing candidate.' })
    const session = h.child('resumed-tester', grant.prompt), state = await service.ensure({ session }, signal())
    await service.runtime.settle(await service.runtime.admit(state, request), { inputTokens: 120, outputTokens: 180 }, 'stop')
    session.events.push({ type: 'turn/end', seq: session.events.length, time: 123, data: { reason: { kind: 'completed' } } })
    await service.finishTeamRun(h.lead, resumed)
    const repair = await service.issueReportRepair(h.lead, { workerSessionId: session.id, task: 'Repair this report format only.' })
    assert.deepEqual(repair.source_remaining, { tokens: 700, calls: 3 })
    assert.deepEqual(repair.profile, { mode: 'fixed', tokenLimit: 700, callLimit: 3 })
    const repaired = h.child('repair', repair.prompt), repairState = await service.ensure({ session: repaired }, signal())
    assert.deepEqual(repairState.profile, repair.profile)
    assert.equal(repairState.policyBinding.source_worker_session_id, session.id)
  } finally { service.shutdown() }
})

test('a lost recovery handle cannot be recreated even before any role allocation', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  await h.resume(r, original)
  await assert.rejects(h.resume(h.runtime(), original), { code: 'WORKER_BUDGET_RESUME_ALREADY_CLAIMED' })
  assert.equal(h.events('allocation').length, 0)
  assert.equal(h.events('team-run-resumed').length, 1)
})


test('recovery status observes a committed claim without writing and distinguishes issued from started', async () => {
  const h = fixture(), r = h.runtime(), original = await h.start(r)
  await r.finishTeamRun(h.lead, original)
  const check = async expected => {
    const before = await h.journal.read(h.root.id)
    assert.deepEqual(await r.teamRunRecoveryStatus(h.lead, { runId: original.run_id }), expected)
    assert.deepEqual(await h.journal.read(h.root.id), before)
  }
  await check({ claimed: false, resume_id: null, ended: false, issued_roles: [] })
  const resumed = await h.resume(r, original)
  await check({ claimed: true, resume_id: resumed.resume_id, ended: false, issued_roles: [] })
  await r.issueTeamWorker(h.lead, resumed, { label: 'tester', task: 'Issued but never started.' })
  await check({ claimed: true, resume_id: resumed.resume_id, ended: false, issued_roles: ['tester'] })
  await r.finishTeamRun(h.lead, resumed)
  await check({ claimed: true, resume_id: resumed.resume_id, ended: true, issued_roles: ['tester'] })
  const cold = h.runtime()
  assert.deepEqual(await cold.teamRunRecoveryStatus(h.lead, { runId: original.run_id }),
    { claimed: true, resume_id: resumed.resume_id, ended: true, issued_roles: ['tester'] })
  await assert.rejects(r.teamRunRecoveryStatus(h.lead, { runId: 'missing' }), { code: 'WORKER_BUDGET_RESUME_SOURCE_INVALID' })
  const foreign = { id: 'foreign-status', header: { id: 'foreign-status' }, events: [] }
  h.sessions.set(foreign.id, foreign)
  await assert.rejects(r.teamRunRecoveryStatus({ session: foreign }, { runId: original.run_id }), { code: 'WORKER_BUDGET_RESUME_SOURCE_INVALID' })
})
