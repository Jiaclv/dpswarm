import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { Sidecar } from '../lib/sidecar.js'
import { FixedTeamController } from '../lib/fixed-team.js'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { HostModelRegistry } from '../lib/host-model-registry.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const coded = code => Object.assign(new Error(code), { code })
function fixture({ sidecarFactory: providedSidecarFactory } = {}) {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-rework-')), cwd = join(workspace, 'project'); mkdirSync(cwd)
  const cfg = { workspace, sidecarUrl: 'http://127.0.0.1:8791', autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    workerTimeoutSeconds: 600, workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28,
    reworkBudgetMode: 'unlimited' }
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [{ type: 'user/message', seq: 0, data: { id: 'user-task-1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create an HTML file; no tests.' }] } }],
    requestHeader() { return { config: this.route } }, route: { provider: 'fixture', model: 'lead', reasoningEffort: 'max' } } }
  const journal = new MemoryAuditJournal(), items = {}, children = [], calls = [], plans = [], allocations = new Map(), sessions = new Map()
  const h = { cfg, parent, journal, items, children, calls, plans, allocations, sessions, denyRoute: false, nextKind: 'completed' }
  const sidecarFactory = cfg => ({ cfg, async ensure() {}, async call(method, path, body) {
    calls.push({ method, path, body })
    if (path === '/api/plugin-audit') {
      if (method === 'POST') {
        assert.equal(body.expected_revision, (await journal.read(cfg.sessionId)).revision)
        return (await journal.transaction(cfg.sessionId, () => ({ events: body.events }))).journal
      }
      return journal.read(cfg.sessionId)
    }
    if (path === '/api/status') return { snapshot: { work_items: items,
      open_worker_slots_used: Object.values(items).filter(item => !['accepted', 'terminated'].includes(item.acceptance)).length,
      seal_phase: h.sealed ? { root: 'cutoff' } : {} } }
    if (path === '/api/delegate') { const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null }
      return { items: [{ item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive' }] } }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') { items[body.item_id].acceptance = 'submitted'; items[body.item_id].submission_package_id = `package-${body.item_id}` }
    if (path === '/api/execution/fail') { items[body.item_id].acceptance = 'terminated'; h.sealed = body.physical_cleanup_confirmed === false }
    if (path === '/api/review') { if (body.verdict === 'accept' && !items[body.item_id].submission_package_id) throw coded('PACKAGE_MISSING')
      items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated' }
    return { ok: true }
  } })
  const budget = {
    async beginTeamRun(_parent, { roles, expectedProfile }) { return { profile: expectedProfile, roles } },
    async finishTeamRun() {},
    async issueTeamWorker(_parent, _run, { task, label }) { return { prompt: task, role: label } },
    async diagnosticsForSession(workerId) { return { root_session_id: 'root', worker_session_id: workerId, mode: 'manual', tokenLimit: 600000, callLimit: 28, calls: 2, committed_tokens: 40000 } },
    async issueRework(_parent, args) {
      plans.push(structuredClone(args)); if (h.budgetError) throw coded(h.budgetError)
      const onIssue = h.onIssue; h.onIssue = null; await onIssue?.()
      if ([...allocations.values()].some(a => a.source === args.workerSessionId && !a.revoked)) throw coded('REWORK_SOURCE_SUPERSEDED')
      const id = `alloc-${allocations.size}`; allocations.set(id, { source: args.workerSessionId, bound: false, revoked: false })
      const profile = cfg.reworkBudgetMode === 'fixed' ? { mode: 'fixed', tokenLimit: cfg.reworkTokenLimit, callLimit: cfg.reworkCallLimit } : { mode: 'unlimited' }
      return { allocation_id: id, prompt: `ALLOCATION:${id}\n${args.task}`, role: 'implementer', profile, source_worker_session_id: args.workerSessionId }
    },
    async revokeRework(_parent, id) { if (h.revokeError) throw coded('REWORK_REVOKE_FAILED'); const a = allocations.get(id); if (a && !a.bound) a.revoked = true },
  }
  const subagents = { async start(provider, request) {
    const id = `child-${children.length}`, kind = h.nextKind; h.nextKind = 'completed'
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 }, events: [] }
    sessions.set(id, { id, session })
    const allocationId = request.prompt[0].text.match(/^ALLOCATION:(\S+)/)?.[1]
    if (allocationId) allocations.get(allocationId).bound = true
    let resolve
    const result = new Promise(r => { resolve = r })
    const finish = () => {
      const reason = kind === 'error' ? { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } }
        : kind === 'cancel' ? { kind: 'aborted', reason: { kind: 'parent' } } : { kind: 'completed' }
      session.events.push({ seq: 0, type: 'turn/end', data: { reason }, time: Date.now() })
      resolve({ output: [{ type: 'text', text: kind === 'completed' ? (h.outputFor?.[id] || 'Saved candidate. Exact task constraints retained.') : 'Partial candidate; unfinished.' }], stopReason: reason.kind === 'error' ? 'error' : reason.kind === 'aborted' ? 'aborted' : 'completed' })
    }
    const child = { id, request, provider, session, result, localAgent: { session },
      async dispose() { if (h.disposeError) throw coded('DISPOSE_FAILED'); if (kind === 'cancel') finish() } }
    children.push(child)
    if (kind === 'cancel') request.signal.addEventListener('abort', finish, { once: true })
    else finish()
    return child
  } }
  const modelRegistry = new HostModelRegistry(() => ({ async resolveCallConfig(route) { if (h.denyRoute) throw coded('NO_ADAPTER'); return route } }))
  h.controller = new FixedTeamController({ config: () => cfg, budget, subagents, modelRegistry, sidecarFactory: providedSidecarFactory || sidecarFactory, resolveSession: id => sessions.get(id) })
  h.requirement = new TeamRequirement({ config: () => cfg, journal })
  // This fixture exercises the frozen legacy lifecycle with its legacy mock service.
  // Strict native source/acceptance is covered by acceptance-native-bridge.test.mjs.
  h.requirement.sourceFor = () => undefined
  h.dispatcher = new TeamDispatcher({ controller: h.controller, requirement: h.requirement })
  h.signal = new AbortController(); h.exec = { agent: parent, signal: h.signal.signal }; h.budget = budget; h.sidecarFactory = providedSidecarFactory || sidecarFactory
  h.run = () => h.dispatcher.run({ task: 'Create an HTML file; no tests.', acceptance: 'Single requested file only.' }, h.exec)
  h.rework = item_id => h.dispatcher.rework({ item_id, feedback: 'Correct only the detached bicycle pedal; preserve all other content.' }, h.exec)
  return h
}

test('same-task rework uses exact original route, unrestricted new grant and latest chain; old deliveries stay unaccepted', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries.find(d => d.role === 'implementer')
  assert.equal((await h.requirement.beforeRun(h.parent)).phase, 'finished')
  h.cfg.workerTokenLimit = 1; h.cfg.workerCallLimit = 1; h.cfg.workerBudgetMode = 'auto'
  const reworked = await h.rework(old.item_id)
  assert.deepEqual(reworked.worker_budget_policy, { mode: 'unlimited' })
  assert.equal(h.plans[0].workerSessionId, old.execution_session_id)
  assert.deepEqual(Object.keys(h.plans[0]).sort(), ['task', 'workerSessionId'])
  assert.match(h.children[2].request.prompt[0].text, /Original task:[\s\S]*no tests/)
  assert.match(h.children[2].request.prompt[0].text, /Single requested file only/)
  assert.equal(h.children[2].request.agentOptions.model, 'lead'); assert.equal(h.children[2].request.agentOptions.reasoningEffort, 'max')
  assert.equal(h.items[old.item_id].acceptance, 'terminated')
  assert.equal(h.items[initial.deliveries[1].item_id].acceptance, 'terminated', 'superseded tester releases its reservation without being accepted')
  assert.equal((await h.controller.report({ item_id: initial.deliveries[1].item_id }, h.exec)).text, 'Saved candidate. Exact task constraints retained.', 'superseded report remains readable')
  await assert.rejects(h.rework(old.item_id), /REWORK_SOURCE_SUPERSEDED/)
  const last = reworked.deliveries[0]
  const third = await h.rework(last.item_id)
  const implPlans = h.plans.filter(p => p.task.includes('Necessary corrections'))
  assert.equal(implPlans[1].workerSessionId, last.execution_session_id)
  assert.equal(third.deliveries.length, 2)
  const events = (await h.journal.read('root')).events
  assert.equal(events.filter(e => e.type === 'dpswarm/fixed-team-binding').length, 1)
  assert.equal(events.filter(e => e.type === 'dpswarm/worker-rework' && e.data.phase === 'published').length, 2)
  assert.equal(events.filter(e => e.type === 'dpswarm/team-required-finished').length, 1, 'rework does not rewrite first team fulfillment')
})

test('failed implementer can rework; no-package accept reports meaningful error without false acceptance', async () => {
  const h = fixture(); h.nextKind = 'error'; const initial = await h.run(), old = initial.failed[0]
  assert.equal(old.code, 'WORKER_TOKEN_RESERVATION_DENIED')
  await assert.rejects(h.dispatcher.review({ item_id: old.item_id, verdict: 'accept' }, h.exec), /DELIVERY_PACKAGE_REQUIRED/)
  assert.equal(h.items[old.item_id].acceptance, 'terminated')
  assert.equal((await h.rework(old.item_id)).deliveries.length, 2)
})

test('rework rejects model/budget argument injection, tester identity, disabled switch and changed user task', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  for (const extra of [{ model: 'other' }, { worker_budget: { tokenLimit: 100, callLimit: 1, reason: 'wrong' } }]) {
    await assert.rejects(h.dispatcher.rework({ item_id: old.item_id, feedback: 'fix pedal', ...extra }, h.exec), /REWORK_FEEDBACK_REQUIRED/)
  }
  await assert.rejects(h.rework(initial.deliveries[1].item_id), /REWORK_RESTORE_REQUIRED/)
  h.cfg.enabledSessions = []; await assert.rejects(h.rework(old.item_id), /REWORK_TASK_NOT_SETTLED/); h.cfg.enabledSessions = ['root']
  h.parent.session.events.push({ seq: 1, type: 'user/message', data: { id: 'new-user-task', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'A new task' }] } })
  await assert.rejects(h.rework(old.item_id), /REWORK_TASK_NOT_SETTLED/)
  assert.equal(h.children.length, 2); assert.equal(h.plans.length, 0)
})

test('model changes, native cleanup uncertainty and unavailable routes block before old-item mutation', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  h.parent.session.route.reasoningEffort = 'high'
  await assert.rejects(h.rework(old.item_id), /REWORK_CONFIGURATION_CHANGED/)
  h.parent.session.route.reasoningEffort = 'max'; h.denyRoute = true
  await assert.rejects(h.rework(old.item_id), /HOST_MODEL_UNAVAILABLE/)
  assert.equal(h.items[old.item_id].acceptance, 'submitted'); assert.equal(h.plans.length, 0)
  h.denyRoute = false
  const events = h.journal.state('root').events, diagnostic = events.find(e => e.type === 'dpswarm/worker-diagnostic' && e.data.item_id === old.item_id)
  diagnostic.data.diagnostic.cleanup.physical_cleanup_confirmed = false
  await assert.rejects(h.rework(old.item_id), /REWORK_TERMINAL_EVIDENCE_REQUIRED/)
  assert.equal(h.items[old.item_id].acceptance, 'submitted')
})

test('budget preflight failure preserves submitted source; cancellation revokes unbound allocation', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  h.budgetError = 'REWORK_SOURCE_UNSETTLED'; await assert.rejects(h.rework(old.item_id), /REWORK_SOURCE_UNSETTLED/)
  assert.equal(h.items[old.item_id].acceptance, 'submitted'); h.budgetError = null
  h.onIssue = () => h.signal.abort()
  await assert.rejects(h.rework(old.item_id), /SUBAGENT_ABORTED/)
  assert.equal(h.items[old.item_id].acceptance, 'submitted')
  assert.equal([...h.allocations.values()][0].revoked, true)
  assert.equal(h.children.length, 2)
})

test('new task during awaited budget preflight cannot borrow old team fulfillment', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  h.onIssue = () => h.parent.session.events.push({ type: 'user/message', data: { id: 'new', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'different task' }] } })
  await assert.rejects(h.rework(old.item_id), /REWORK_TASK_NOT_SETTLED/)
  assert.equal([...h.allocations.values()][0].revoked, true)
  assert.equal(h.items[old.item_id].acceptance, 'submitted'); assert.equal(h.children.length, 2)
})

test('physical cleanup failure preserves workspace lease and failed candidate is never delivered', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  h.disposeError = true
  const result = await h.rework(old.item_id)
  assert.equal(result.deliveries.length, 0); assert.equal(result.failed.length, 1)
  assert.equal(result.cleanup.workspace_lease_held, true)
  assert.ok(existsSync(h.controller.sessions.get('root').lease.path))
  await assert.rejects(h.rework(result.failed[0].item_id), /REWORK_TERMINAL_EVIDENCE_REQUIRED/)
})

test('cold controller refuses inference and another root workspace lease cannot be stolen', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  const originalController = h.dispatcher.controller
  h.dispatcher.controller = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  await assert.rejects(h.rework(old.item_id), /REWORK_RESTORE_REQUIRED/)
  h.dispatcher.controller = originalController
  for (const delivery of initial.deliveries) await h.dispatcher.review({ item_id: delivery.item_id, verdict: 'terminate' }, h.exec)
  const state = originalController.sessions.get('root'); assert.equal(state.lease, null)
  h.cfg.enabledSessions.push('other')
  const other = { ...h.parent, id: 'other', session: { ...h.parent.session, id: 'other' } }, foreign = originalController.session(other)
  originalController.acquire(foreign, other)
  await assert.rejects(h.rework(old.item_id), /WORKSPACE_BUSY/)
  assert.equal(h.plans.length, 0); assert.ok(existsSync(foreign.lease.path))
  await originalController.release(foreign)
})

test('concurrent rework calls create only one linked child', async () => {
  const h = fixture(), initial = await h.run(), old = initial.deliveries[0]
  let release; h.onIssue = () => new Promise(resolve => { release = resolve })
  const running = h.rework(old.item_id)
  while (!release) await new Promise(resolve => setImmediate(resolve))
  await assert.rejects(h.rework(old.item_id), /RUN_PENDING/)
  release(); await running; assert.equal(h.children.length, 4)
})


test('tester and reviewer setting changes do not substitute or block the original implementer', async () => {
  const h = fixture(), initial = await h.run()
  h.cfg.testModel = 'new-tester'; h.cfg.reviewerMode = 'model'; h.cfg.reviewerModel = 'new-reviewer'
  const result = await h.rework(initial.deliveries[0].item_id)
  assert.equal(result.deliveries.length, 2); assert.equal(h.children.length, 4)
  assert.equal(h.children[2].request.agentOptions.model, 'lead')
})

test('failed original run can safely reacquire its own workspace after all old deliveries settle', async () => {
  const h = fixture(); h.nextKind = 'error'; const initial = await h.run(), old = initial.failed[0]
  for (const item of initial.deliveries) await h.dispatcher.review({ item_id: item.item_id, verdict: 'terminate' }, h.exec)
  assert.equal(h.controller.sessions.get('root').lease, null)
  const result = await h.rework(old.item_id)
  assert.equal(result.deliveries.length, 2)
  assert.ok(h.controller.sessions.get('root').lease)
})

test('published cancellation remains a failed linked attempt and latest item can be repaired again', async () => {
  const h = fixture(), initial = await h.run(); h.nextKind = 'cancel'
  const running = h.rework(initial.deliveries[0].item_id)
  while (h.children.length < 3) await new Promise(resolve => setImmediate(resolve))
  h.signal.abort(); const result = await running
  assert.equal(result.stopped, true); assert.equal(result.deliveries.length, 0)
  assert.equal(result.failed[0].code, 'WORKER_PARENT_CANCELLED')
  h.signal = new AbortController(); h.exec.signal = h.signal.signal
  assert.equal((await h.rework(result.failed[0].item_id)).deliveries.length, 2)
})

test('unconfirmed revoke retains the lease and reports cleanup error', async () => {
  const h = fixture(), initial = await h.run(); h.revokeError = true
  await assert.rejects(h.rework(initial.deliveries[0].item_id), /REWORK_REVOKE_FAILED/)
  assert.ok(h.controller.sessions.get('root').lease)
  assert.equal(h.controller.sessions.get('root').cleanup.budget_error.code, 'REWORK_REVOKE_FAILED')
})


function realBudget(h) {
  const runtime = new WorkerBudgetRuntime({ config: () => h.cfg, journal: h.journal,
    resolveSession: id => id === h.parent.id ? h.parent.session : h.sessions.get(id)?.session,
    listSessions: () => [h.parent.session, ...[...h.sessions.values()].map(agent => agent.session)] })
  h.controller.budget = runtime
  const start = h.controller.subagents.start
  h.controller.subagents.start = async (provider, request) => {
    const child = await start(provider, request)
    child.session.events.unshift({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: request.prompt } })
    child.session.events.forEach((event, seq) => { event.seq = seq })
    if (h.skipReworkBinding && request.prompt[0].text.startsWith('[DPSWARM_REWORK_WORKER_BUDGET_V1:')) return child
    const state = await runtime.ensure({ session: child.session }, request.signal)
    const ticket = await runtime.admit(state, { provider: request.agentOptions.provider, model: request.agentOptions.model, messages: [], maxTokens: 100 })
    await runtime.settle(ticket, { inputTokens: 20, outputTokens: 10 }, 'completed')
    return child
  }
  return runtime
}

test('real budget runtime: completed bound rework is not revoked, old usage stays and review releases lease', async () => {
  const h = fixture()
  const runtime = realBudget(h)
  const initial = await h.run(), first = initial.deliveries[0]
  const original = await runtime.diagnosticsForSession(first.execution_session_id)
  const repaired = await h.rework(first.item_id)
  assert.equal(repaired.deliveries.length, 2); assert.equal(repaired.cleanup.budget_error, null)
  const next = await runtime.diagnosticsForSession(repaired.deliveries[0].execution_session_id)
  assert.equal(next.mode, 'unlimited'); assert.equal(next.remaining_tokens, null); assert.equal(next.calls, 1)
  assert.deepEqual(await runtime.diagnosticsForSession(first.execution_session_id), original)
  const allocation = (await h.journal.read('root')).events.find(e => e.type === 'dpswarm/worker-budget-allocation' && e.data.authority === 'fixed-team-rework').data
  assert.deepEqual(await runtime.revokeRework(h.parent, allocation.allocation_id), { revoked: false, bound: true, allocation_id: allocation.allocation_id, source_worker_session_id: first.execution_session_id, bound_worker_session_id: repaired.deliveries[0].execution_session_id })
  await assert.rejects(runtime.issueRework(h.parent, { workerSessionId: first.execution_session_id, task: 'duplicate' }), /REWORK_ALREADY_CLAIMED/)
  const last = await h.rework(repaired.deliveries[0].item_id)
  for (const item of [...initial.deliveries.slice(1), ...repaired.deliveries.slice(1), ...last.deliveries]) await h.dispatcher.review({ item_id: item.item_id, verdict: 'accept' }, h.exec)
  assert.equal(h.controller.sessions.get('root').lease, null)
  assert.equal((await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/worker-budget-rework-revoked').length, 0)
})


for (const bound of [false, true]) test(`real budget: published cancellation ${bound ? 'after' : 'before'} allocation binding preserves correct continuation`, async () => {
  const h = fixture(), runtime = realBudget(h), initial = await h.run(), source = initial.deliveries[0]
  h.skipReworkBinding = !bound; h.nextKind = 'cancel'
  const running = h.rework(source.item_id)
  while (!(await h.journal.read('root')).events.some(e => e.type === 'dpswarm/worker-rework' && e.data.phase === 'published')) await new Promise(resolve => setImmediate(resolve))
  h.signal.abort(); const result = await running, cancelled = result.failed[0]
  assert.equal(result.deliveries.length, 0)
  assert.equal(cancelled.code, 'WORKER_PARENT_CANCELLED')
  assert.equal(result.rework_recovery.source_retry_allowed, !bound)
  assert.equal(result.rework_recovery.published_child_eligible, bound)
  h.signal = new AbortController(); h.exec.signal = h.signal.signal; h.skipReworkBinding = false
  if (bound) {
    await assert.rejects(h.rework(source.item_id), /REWORK_SOURCE_SUPERSEDED/)
    assert.equal((await h.rework(cancelled.item_id)).deliveries.length, 2)
  } else {
    await assert.rejects(h.rework(cancelled.item_id), /REWORK_ALLOCATION_REVOKED/)
    assert.equal((await h.rework(source.item_id)).deliveries.length, 2)
    const revoked = (await h.journal.read('root')).events.find(e => e.type === 'dpswarm/worker-rework' && e.data.phase === 'revoked')
    assert.equal(revoked.data.source_retry_allowed, true)
    assert.equal(revoked.data.published_child_eligible, false)
  }
  assert.equal((await runtime.diagnosticsForSession(source.execution_session_id)).calls, 1)
})

test('unbound published cancellation with unconfirmed physical cleanup does not reopen original source', async () => {
  const h = fixture(); realBudget(h); const initial = await h.run(), source = initial.deliveries[0]
  h.skipReworkBinding = true; h.nextKind = 'cancel'; h.disposeError = true
  const running = h.rework(source.item_id)
  while (!(await h.journal.read('root')).events.some(e => e.type === 'dpswarm/worker-rework' && e.data.phase === 'published')) await new Promise(resolve => setImmediate(resolve))
  h.signal.abort(); const result = await running
  assert.equal(result.rework_recovery.allocation_revoked, true)
  assert.equal(result.rework_recovery.source_retry_allowed, false)
  assert.equal(result.cleanup.workspace_lease_held, true)
  h.signal = new AbortController(); h.exec.signal = h.signal.signal
  await assert.rejects(h.rework(source.item_id), /REWORK_SOURCE_SUPERSEDED/)
})

test('task changes during final internal model preflight prevent child start and preserve new task requirement', async () => {
  const h = fixture(), initial = await h.run(), source = initial.deliveries[0]
  const resolve = h.controller.modelRegistry.resolve.bind(h.controller.modelRegistry); let preflights = 0
  h.controller.modelRegistry.resolve = async (...args) => {
    const result = await resolve(...args)
    if (++preflights === 2) h.parent.session.events.push({ type: 'user/message', data: { id: 'new-at-last-preflight', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'A distinct user task' }] } })
    return result
  }
  const result = await h.rework(source.item_id)
  assert.equal(h.exec.signal.aborted, false)
  assert.equal(h.children.length, 2)
  assert.equal(result.failed[0].code, 'REWORK_TASK_NOT_SETTLED')
  assert.equal([...h.allocations.values()][0].revoked, true)
  assert.equal(h.controller.sessions.get('root').implementers.get(source.item_id).superseded, false)
  assert.equal((await h.requirement.beforeRun(h.parent)).phase, 'required')
})

test('fixed rework mode announces the configured allowance in the linked prompt and matches the issued grant', async () => {
  const h = fixture()
  h.cfg.reworkBudgetMode = 'fixed'; h.cfg.reworkTokenLimit = 300000; h.cfg.reworkCallLimit = 10
  const initial = await h.run(), old = initial.deliveries.find(d => d.role === 'implementer')
  const reworked = await h.rework(old.item_id)
  assert.deepEqual(reworked.worker_budget_policy, { mode: 'fixed', tokenLimit: 300000, callLimit: 10 })
  assert.match(h.children[2].request.prompt[0].text, /fixed allowance of 300000 cumulative tokens and 10 model calls/)
  assert.doesNotMatch(h.children[2].request.prompt[0].text, /without a token or call cap/)
  assert.equal(h.items[old.item_id].acceptance, 'terminated')
})

test('a grant profile differing from the announced rework allowance is rejected before child start', async () => {
  const h = fixture()
  h.cfg.reworkBudgetMode = 'fixed'; h.cfg.reworkTokenLimit = 300000; h.cfg.reworkCallLimit = 10
  const initial = await h.run(), old = initial.deliveries.find(d => d.role === 'implementer')
  h.cfg.reworkTokenLimit = 99999 // settings drift between prompt build and issuance
  const originalIssue = h.budget.issueRework.bind(h.budget)
  h.budget.issueRework = async (parent, args) => ({ ...await originalIssue(parent, args), profile: { mode: 'unlimited' } })
  await assert.rejects(h.rework(old.item_id), /WORKER_REWORK_ALLOCATION_INVALID/)
  assert.equal(h.items[old.item_id].acceptance, 'submitted', 'source item is not terminated when the grant mismatches')
})


function independentReviewer(h) {
  Object.assign(h.cfg, { reviewerMode: 'model', reviewerProvider: 'fixture', reviewerModel: 'reviewer' })
}

for (const missing of [true, false]) test('old accepted reviewer cannot approve the latest candidate when its replacement is ' + (missing ? 'not admitted' : 'failed'), async () => {
  const h = fixture(); independentReviewer(h)
  const first = await h.run()
  await h.dispatcher.review({ item_id: first.deliveries[2].item_id, verdict: 'accept' }, h.exec)
  const start = h.controller.subagents.start
  h.controller.subagents.start = async (provider, request) => {
    if (request.label === 'dpswarm:DPswarm reviewer re-review') {
      if (missing) throw coded('TEST_REVIEWER_NOT_ADMITTED')
      h.nextKind = 'error'
    }
    return start(provider, request)
  }
  const next = await h.rework(first.deliveries[0].item_id)
  const candidate = next.deliveries[0]
  assert.equal(next.failed[0].role, 'reviewer')
  await assert.rejects(h.dispatcher.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/, 'cold review must recover the same generation gate')
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept', takeover: true, reason: '  ' }, h.exec), /REVIEW_TAKEOVER_REASON_REQUIRED/)
  const accepted = await cold.review({ item_id: candidate.item_id, verdict: 'accept', takeover: true,
    reason: 'Replacement reviewer unavailable; Lead checked the candidate and original requirements directly.' }, h.exec)
  assert.equal(accepted.acceptance_basis, 'lead-takeover')
  assert.equal(h.items[candidate.item_id].acceptance, 'accepted')
  const takeover = (await h.journal.read('root')).events.filter(event => event.type === 'dpswarm/verification-takeover')
  assert.equal(takeover.length, 1)
  assert.deepEqual(takeover[0].data.candidate_item_ids, [candidate.item_id])
  await cold.review({ item_id: candidate.item_id, verdict: 'accept', takeover: true, reason: takeover[0].data.reason }, h.exec)
  assert.equal((await h.journal.read('root')).events.filter(event => event.type === 'dpswarm/verification-takeover').length, 1)
})

test('supersession never invents cleanup proof and partial retirement resumes idempotently', async () => {
  const h = fixture(); independentReviewer(h)
  const first = await h.run(), old = first.deliveries[0]
  const event = h.journal.state('root').events.find(event => event.type === 'dpswarm/worker-diagnostic' && event.data.role === 'reviewer')
  event.data.diagnostic.cleanup.physical_cleanup_confirmed = false
  await assert.rejects(h.rework(old.item_id), /REWORK_VERIFIER_UNSETTLED/)
  assert.deepEqual(first.deliveries.map(item => h.items[item.item_id].acceptance), ['submitted', 'submitted', 'submitted'])
  event.data.diagnostic.cleanup.physical_cleanup_confirmed = true
  const state = h.controller.sessions.get('root'), call = state.sidecar.call.bind(state.sidecar)
  let denied = false
  state.sidecar.call = async (method, path, body) => {
    if (!denied && path === '/api/review' && body.item_id === first.deliveries[2].item_id) { denied = true; throw coded('CONTROL_UNAVAILABLE') }
    return call(method, path, body)
  }
  await assert.rejects(h.rework(old.item_id), /CONTROL_UNAVAILABLE/)
  assert.equal(h.items[first.deliveries[1].item_id].acceptance, 'terminated')
  assert.equal(h.items[old.item_id].acceptance, 'submitted')
  const retried = await h.rework(old.item_id)
  assert.deepEqual(retried.deliveries.map(item => item.role), ['implementer', 'tester', 'reviewer'])
  assert.equal(h.calls.filter(call => call.path === '/api/review' && call.body.item_id === first.deliveries[1].item_id).length, 1)
  assert.equal((await h.controller.report({ item_id: first.deliveries[1].item_id }, h.exec)).completion, 'completed')
})

test('old generation takeover is not inherited by a reworked candidate and cold reviewer termination records current takeover', async () => {
  const h = fixture(); independentReviewer(h)
  const first = await h.run()
  await h.dispatcher.review({ item_id: first.deliveries[2].item_id, verdict: 'terminate', reason: 'Lead verified the first candidate directly.' }, h.exec)
  const next = await h.rework(first.deliveries[0].item_id)
  const candidate = next.deliveries[0], reviewer = next.deliveries[2]
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  await cold.review({ item_id: reviewer.item_id, verdict: 'terminate' }, h.exec)
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/, 'termination without a documented takeover must not pass')
  await cold.review({ item_id: reviewer.item_id, verdict: 'terminate', reason: 'Lead verified the replacement candidate directly.' }, h.exec)
  assert.equal((await cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec)).acceptance_basis, 'lead-takeover')
})

test('legacy candidate with configured reviewer needs explicit takeover when no generation binding can be recovered', async () => {
  const h = fixture(); independentReviewer(h)
  const first = await h.run()
  const journalState = h.journal.state('root')
  journalState.events = journalState.events.filter(event => !event.type.startsWith('dpswarm/verification-'))
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  await h.dispatcher.review({ item_id: first.deliveries[2].item_id, verdict: 'accept' }, h.exec)
  await assert.rejects(cold.review({ item_id: first.deliveries[0].item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  assert.equal((await cold.review({ item_id: first.deliveries[0].item_id, verdict: 'accept', takeover: true,
    reason: 'Historical reviewer binding unavailable; Lead verified this candidate directly.' }, h.exec)).acceptance_basis, 'lead-takeover')
})

test('real Python sidecar keeps 4 slots and 8 points through two rework generations, durable review and lease release', { timeout: 60000 }, async t => {
  const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  const directory = mkdtempSync(join(tmpdir(), 'dpswarm-real-rework-'))
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python', [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), directory],
    { cwd: repo, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''; child.stderr.on('data', data => { stderr += data })
  const lines = createInterface({ input: child.stdout }), queue = [], waiters = []
  lines.on('line', line => { const value = JSON.parse(line); if (waiters.length) waiters.shift()(value); else queue.push(value) })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve))
  t.after(async () => { if (child.exitCode === null) { const done = once(child, 'exit'); child.stdin.end('{"command":"stop"}\n'); await done } lines.close() })
  const { port } = await Promise.race([next(), once(child, 'exit').then(() => { throw new Error(stderr) })])
  const samples = [], specChanges = []
  const h = fixture({ sidecarFactory: cfg => {
    const sidecar = new Sidecar({ ...cfg, workspace: directory, sidecarUrl: 'http://127.0.0.1:' + port })
    const call = sidecar.call.bind(sidecar)
    sidecar.call = async (method, path, body) => {
      const result = await call(method, path, body)
      if (path === '/api/spec' && method === 'POST') specChanges.push(body)
      if (path === '/api/status' && result.snapshot) samples.push({ points: result.snapshot.active_points,
        slots: result.snapshot.open_worker_slots_used, spec: result.spec })
      return result
    }
    return sidecar
  } })
  independentReviewer(h)
  realBudget(h)
  const first = await h.run()
  assert.deepEqual(first.failed, [])
  const state = h.controller.sessions.get('root')
  const status = await state.sidecar.call('GET', '/api/status')
  assert.equal(status.spec.max_open_work_items, 4)
  assert.equal(status.spec.max_active_node_points, 8)
  assert.equal(status.snapshot.open_worker_slots_used, 3)
  assert.equal(status.snapshot.active_points, 7)
  let previous = first
  for (let round = 0; round < 2; round++) {
    const reworked = await h.rework(previous.deliveries[0].item_id)
    assert.deepEqual(reworked.failed, [])
    assert.deepEqual(reworked.deliveries.map(item => item.role), ['implementer', 'tester', 'reviewer'])
    const current = await state.sidecar.call('GET', '/api/status')
    assert.deepEqual(previous.deliveries.map(item => current.snapshot.work_items[item.item_id].acceptance), ['terminated', 'terminated', 'terminated'])
    assert.equal(current.snapshot.open_worker_slots_used, 3)
    assert.equal(current.snapshot.active_points, 7)
    assert.equal((await h.controller.report({ item_id: previous.deliveries[2].item_id }, h.exec)).completion, 'completed')
    await assert.rejects(h.dispatcher.review({ item_id: reworked.deliveries[0].item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
    previous = reworked
  }
  assert.deepEqual(specChanges, [], 'no team-worker or root resource cap needs raising')
  assert.ok(samples.every(sample => sample.points <= 8 && sample.slots <= 4))
  child.stdin.write('{"command":"restart"}\n'); assert.equal((await next()).restarted, true)
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  const candidate = previous.deliveries[0], reviewer = previous.deliveries[2]
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  await cold.review({ item_id: reviewer.item_id, verdict: 'accept', reason: 'Current independent fixture review read.' }, h.exec)
  assert.equal((await cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec)).acceptance_basis, 'independent-reviewer-evidence')
  await h.dispatcher.review({ item_id: previous.deliveries[1].item_id, verdict: 'accept' }, h.exec)
  assert.equal(state.lease, null)
  const end = await state.sidecar.call('GET', '/api/status')
  assert.equal(end.snapshot.open_worker_slots_used, 0)
  assert.equal(end.spec.max_open_work_items, 4)
  assert.equal(end.spec.max_active_node_points, 8)
  assert.equal((await cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec)).outcome, 'already_accepted')
})


for (const verdict of ['needs-rework', 'blocked']) test('current explicit ' + verdict + ' verdict cannot be replaced by an old pass or evidence acceptance', async () => {
  const h = fixture(); independentReviewer(h)
  h.outputFor = { 'child-2': 'VERDICT: pass\nThe original candidate was checked.', 'child-5': 'Current candidate remains unverified.\nVERDICT: ' + verdict }
  const first = await h.run()
  await h.dispatcher.review({ item_id: first.deliveries[2].item_id, verdict: 'accept' }, h.exec)
  const next = await h.rework(first.deliveries[0].item_id), candidate = next.deliveries[0]
  await h.dispatcher.review({ item_id: next.deliveries[2].item_id, verdict: 'accept' }, h.exec)
  await assert.rejects(h.dispatcher.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_REJECTED/)
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  await assert.rejects(cold.review({ item_id: candidate.item_id, verdict: 'accept' }, h.exec), /REVIEWER_REJECTED/)
  assert.equal((await cold.review({ item_id: candidate.item_id, verdict: 'accept', takeover: true,
    reason: 'Lead reproduced and resolved the reported verification limitation against the current files.' }, h.exec)).acceptance_basis, 'lead-takeover')
})
