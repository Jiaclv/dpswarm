import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FixedTeamController } from '../lib/fixed-team.js'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { HostModelRegistry } from '../lib/host-model-registry.js'
import { WriteScopeRegistry } from '../lib/write-scope.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const coded = code => Object.assign(new Error(code), { code })

/** Parallel-capable fixture: N items per derive call, budget mock, real scope registry. */
function fixture() {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-parallel-')), cwd = join(workspace, 'project'); mkdirSync(cwd)
  const cfg = { workspace, sidecarUrl: 'http://127.0.0.1:8791', autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    workerTimeoutSeconds: 600, workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28 }
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [{ type: 'user/message', seq: 0, data: { id: 'user-task-1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create the two parts.' }] } }],
    requestHeader() { return { config: this.route } }, route: { provider: 'fixture', model: 'lead', reasoningEffort: 'max' } } }
  const journal = new MemoryAuditJournal(), items = {}, children = [], plans = [], allocations = new Map(), sessions = new Map(), specCalls = []
  const h = { cfg, parent, journal, items, children, plans, allocations, sessions, failAt: null, specCalls, spec: { max_team_workers: 3 } }
  const sidecarFactory = cfg => ({ cfg, async ensure() {}, async call(method, path, body) {
    if (path === '/api/plugin-audit') {
      if (method === 'POST') return (await journal.transaction(cfg.sessionId, () => ({ events: body.events }))).journal
      return journal.read(cfg.sessionId)
    }
    if (path === '/api/status') return { spec: { max_team_workers: h.spec.max_team_workers }, snapshot: { work_items: items,
      open_worker_slots_used: Object.values(items).filter(item => !['accepted', 'terminated'].includes(item.acceptance)).length,
      seal_phase: {} } }
    if (path === '/api/spec') { specCalls.push(structuredClone(body)); if (Number.isSafeInteger(body?.max_team_workers)) h.spec.max_team_workers = body.max_team_workers
      return { ok: true, revision: 1 } }
    if (path === '/api/delegate') {
      return { items: body.subtasks.map((st, i) => {
        const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null, kind: 'derive' }
        return { item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive', subtask_index: i }
      }) }
    }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') { items[body.item_id].acceptance = 'submitted'; items[body.item_id].submission_package_id = `package-${body.item_id}` }
    if (path === '/api/execution/fail') { items[body.item_id].acceptance = 'terminated' }
    if (path === '/api/review') { items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated' }
    return { ok: true }
  } })
  const budget = {
    async beginTeamRun(_parent, { roles, expectedProfile }) { return { profile: expectedProfile, roles } },
    async finishTeamRun() {},
    async issueTeamWorker(_parent, _run, args) { plans.push(structuredClone(args)); return { prompt: args.task, role: args.label, subtask: args.subtask ?? null } },
    async diagnosticsForSession(workerId) { return { root_session_id: 'root', worker_session_id: workerId, mode: 'manual', tokenLimit: 600000, callLimit: 28, calls: 2, committed_tokens: 40000 } },
    async issueRework(_parent, args) {
      if ([...allocations.values()].some(a => a.source === args.workerSessionId && !a.revoked)) throw coded('REWORK_SOURCE_SUPERSEDED')
      const id = `alloc-${allocations.size}`; allocations.set(id, { source: args.workerSessionId, bound: false, revoked: false })
      return { allocation_id: id, prompt: `ALLOCATION:${id}\n${args.task}`, role: 'implementer', profile: { mode: 'unlimited' }, source_worker_session_id: args.workerSessionId }
    },
    async revokeRework(_parent, id) { const a = allocations.get(id); if (a && !a.bound) a.revoked = true },
  }
  const subagents = { async start(provider, request) {
    const id = `child-${children.length}`, fail = h.failAt === id
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 }, events: [] }
    sessions.set(id, { id, session })
    const allocationId = request.prompt[0].text.match(/^ALLOCATION:(\S+)/)?.[1]
    if (allocationId) allocations.get(allocationId).bound = true
    const reason = fail ? { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } } : { kind: 'completed' }
    session.events.push({ seq: 0, type: 'turn/end', data: { reason }, time: Date.now() })
    const child = { id, request, provider, session, localAgent: { session },
      result: Promise.resolve({ output: [{ type: 'text', text: fail ? 'Partial candidate.' : `done:${id}` }],
        stopReason: fail ? 'error' : 'completed' }),
      async dispose() {} }
    children.push(child)
    return child
  } }
  const modelRegistry = new HostModelRegistry(() => ({ async resolveCallConfig(route) { return route } }))
  const writeScope = new WriteScopeRegistry({ journal })
  h.controller = new FixedTeamController({ config: () => cfg, budget, subagents, modelRegistry, sidecarFactory, writeScope, resolveSession: id => sessions.get(id) })
  h.requirement = new TeamRequirement({ config: () => cfg, journal })
  h.dispatcher = new TeamDispatcher({ controller: h.controller, requirement: h.requirement })
  h.signal = new AbortController(); h.exec = { agent: parent, signal: h.signal.signal }; h.budget = budget; h.writeScope = writeScope
  const subtasks = [
    { id: 'part-a', task: 'Build part A.', write_scope: ['src/a/**'] },
    { id: 'part-b', task: 'Build part B.', write_scope: ['src/b/**'] },
  ]
  h.run = () => h.dispatcher.run({ task: 'Create the two parts.', acceptance: 'Both parts exist.', subtasks }, h.exec)
  return h
}

test('parallel implementers fan out with claims and per-item identity, tester joins after', async () => {
  const h = fixture()
  const result = await h.run()
  assert.deepEqual(h.children.map(c => c.id), ['child-0', 'child-1', 'child-2'])
  assert.deepEqual(result.deliveries.map(d => [d.role, d.subtask ?? null]),
    [['implementer', 'part-a'], ['implementer', 'part-b'], ['tester', null]])
  assert.equal(result.failed.length, 0)
  // Every child carries its own subtask prompt, and only the two implementer prompts carry a scope clause.
  assert.match(h.children[0].request.prompt[0].text, /你负责的子任务（part-a）/)
  assert.match(h.children[0].request.prompt[0].text, /写范围（工具层强制）/)
  assert.match(h.children[1].request.prompt[0].text, /你负责的子任务（part-b）/)
  assert.doesNotMatch(h.children[2].request.prompt[0].text, /写范围（工具层强制）/)
  // Tester sees both earlier deliveries as untrusted evidence.
  assert.match(h.children[2].request.prompt[0].text, /Earlier deliveries and failures/)
  // Two claims, disjoint scopes, audit-ledgered for diagnostics and cold reads.
  const claims = h.writeScope.claimsFor('root')
  assert.deepEqual(claims.map(c => [c.worker_session_id, c.subtask]), [['child-0', 'part-a'], ['child-1', 'part-b']])
  const events = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/write-scope')
  assert.equal(events.length, 2)
  assert.deepEqual(events.map(e => e.data.subtask).sort(), ['part-a', 'part-b'])
  // Budget issuance used role+subtask keys.
  assert.deepEqual(h.plans.map(p => [p.label, p.subtask ?? null]), [['implementer', 'part-a'], ['implementer', 'part-b'], ['tester', null]])
})

test('a failing subtask reports in place and the tester still joins', async () => {
  const h = fixture(); h.failAt = 'child-1'
  const result = await h.run()
  assert.equal(result.failed.length, 1)
  assert.equal(result.failed[0].subtask, 'part-b')
  assert.equal(result.failed[0].code, 'WORKER_TOKEN_RESERVATION_DENIED')
  assert.deepEqual(result.deliveries.map(d => d.role), ['implementer', 'tester'])
  assert.equal(h.children.length, 3, 'tester starts after the partial parallel phase')
  assert.match(h.children[2].request.prompt[0].text, /WORKER_TOKEN_RESERVATION_DENIED/)
})

test('rework of a parallel item re-claims the same subtask scope for its continuation', async () => {
  const h = fixture()
  const result = await h.run()
  const itemA = result.deliveries.find(d => d.subtask === 'part-a')
  const reworked = await h.dispatcher.rework({ item_id: itemA.item_id, feedback: 'Fix the off-by-one in part A only.' }, h.exec)
  assert.equal(reworked.deliveries.length, 2, 'implementer rework plus the original tester re-verification')
  const reworkChild = h.children[3], verifyChild = h.children[4]
  assert.match(reworkChild.request.prompt[0].text, /写范围（工具层强制）/)
  assert.match(reworkChild.request.prompt[0].text, /src\/a\/\*\*/)
  assert.match(reworkChild.request.prompt[0].text, /Prior attempt context/)
  assert.match(verifyChild.request.prompt[0].text, /linked re-verification after implementer rework/)
  assert.match(verifyChild.request.prompt[0].text, /Corrections the implementer was asked to make/)
  // The claim moved to the rework session; part-b's claim is untouched.
  assert.equal(h.writeScope.forSession('child-0'), null)
  assert.equal(h.writeScope.forSession(reworkChild.id).subtask, 'part-a')
  assert.equal(h.writeScope.forSession('child-1').subtask, 'part-b')
  assert.equal(h.items[itemA.item_id].acceptance, 'terminated')
})

test('auto mode hands each parallel implementer its own index-aligned decision', async () => {
  const h = fixture()
  h.cfg.workerBudgetMode = 'auto'
  const decisions = {
    implementer: [
      { tokenLimit: 80000, callLimit: 40, reason: 'part A is the larger share' },
      { tokenLimit: 50000, callLimit: 20, reason: 'part B is smaller' },
    ],
    tester: { tokenLimit: 30000, callLimit: 10, reason: 'read-only verification' },
  }
  const runtime = new WorkerBudgetRuntime({ config: () => h.cfg, journal: h.journal,
    resolveSession: id => id === h.parent.id ? h.parent.session : h.sessions.get(id)?.session,
    listSessions: () => [h.parent.session, ...[...h.sessions.values()].map(agent => agent.session)] })
  h.controller.budget = runtime
  const originalStart = h.controller.subagents.start
  h.controller.subagents.start = async (provider, request) => {
    const child = await originalStart(provider, request)
    child.session.events.unshift({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: request.prompt } })
    child.session.events.forEach((event, seq) => { event.seq = seq })
    const state = await runtime.ensure({ session: child.session }, request.signal)
    const ticket = await runtime.admit(state, { provider: 'fixture', model: 'm', messages: [], maxTokens: 100 })
    await runtime.settle(ticket, { inputTokens: 20, outputTokens: 10 }, 'completed')
    return child
  }
  await h.dispatcher.run({ task: 'Create the two parts.', acceptance: 'Both parts exist.',
    subtasks: [
      { id: 'part-a', task: 'Build part A.', write_scope: ['src/a/**'] },
      { id: 'part-b', task: 'Build part B.', write_scope: ['src/b/**'] },
    ], worker_budgets: decisions }, h.exec)
  const first = await runtime.diagnosticsForSession('child-0')
  const second = await runtime.diagnosticsForSession('child-1')
  assert.equal(first.tokenLimit, 80000)
  assert.equal(second.tokenLimit, 50000)
  assert.equal(first.policy_binding.subtask, 'part-a')
  assert.equal(second.policy_binding.subtask, 'part-b')
  // A same-subtask repeat issuance stays one-use.
  const events = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/worker-budget-allocation' && e.data.authority === 'fixed-team-run')
  assert.equal(events.length, 3)
})

test('a 3-way split raises the team-worker cap before dispatch and restores it after settle', async () => {
  const h = fixture()
  const result = await h.dispatcher.run({ task: 'Create three parts.', acceptance: 'All exist.', subtasks: [
    { id: 'part-a', task: 'Build part A.', write_scope: ['src/a/**'] },
    { id: 'part-b', task: 'Build part B.', write_scope: ['src/b/**'] },
    { id: 'part-c', task: 'Build part C.', write_scope: ['src/c/**'] },
  ] }, h.exec)
  assert.equal(result.failed.length, 0)
  // 3 implementers + tester join = 4 non-terminal worker items > default cap 3.
  assert.deepEqual(h.specCalls.map(c => c.max_team_workers), [4])
  assert.equal(h.spec.max_team_workers, 4)
  for (const delivery of result.deliveries) await h.dispatcher.review({ item_id: delivery.item_id, verdict: 'accept' }, h.exec)
  assert.deepEqual(h.specCalls.map(c => c.max_team_workers), [4, 3], 'cap restored after the last review settles every item')
  assert.equal(h.spec.max_team_workers, 3)
})

test('a 2-way split fits the default cap and never touches the spec', async () => {
  const h = fixture()
  const result = await h.run()
  assert.equal(result.failed.length, 0)
  assert.deepEqual(h.specCalls, [], '2 implementers + tester = 3, exactly the default cap')
})

test('rework raises the cap for the implementer plus tester continuation while originals are open', async () => {
  const h = fixture()
  const result = await h.run()
  const itemA = result.deliveries.find(d => d.subtask === 'part-a')
  // Original items remain submitted (unreviewed): 3 open + 2 new = cap 5.
  const reworked = await h.dispatcher.rework({ item_id: itemA.item_id, feedback: 'Fix part A only.' }, h.exec)
  assert.equal(reworked.failed.length, 0)
  assert.deepEqual(h.specCalls.map(c => c.max_team_workers), [5])
  assert.equal(reworked.deliveries.length, 2)
})

test('role separation: with a configured reviewer, accepting the implementer before its verdict is refused', async () => {
  const h = fixture()
  h.cfg.reviewerMode = 'model'; h.cfg.reviewerProvider = 'fixture'; h.cfg.reviewerModel = 'reviewer'
  const result = await h.run()
  assert.equal(result.deliveries.length, 4, '2 implementers + tester + reviewer')
  const reviewer = result.deliveries.find(d => d.role === 'reviewer')
  const implA = result.deliveries.find(d => d.subtask === 'part-a')
  await assert.rejects(h.dispatcher.review({ item_id: implA.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  assert.equal(h.items[implA.item_id].acceptance, 'submitted', 'production delivery is not accepted while review is pending')
  await h.dispatcher.review({ item_id: reviewer.item_id, verdict: 'accept' }, h.exec)
  await h.dispatcher.review({ item_id: implA.item_id, verdict: 'accept' }, h.exec)
  assert.equal(h.items[implA.item_id].acceptance, 'accepted')
})

test('reviewer takeover: terminating the reviewer item with a reason unlocks Lead acceptance', async () => {
  const h = fixture()
  h.cfg.reviewerMode = 'model'; h.cfg.reviewerProvider = 'fixture'; h.cfg.reviewerModel = 'reviewer'
  const result = await h.run()
  const reviewer = result.deliveries.find(d => d.role === 'reviewer')
  const implA = result.deliveries.find(d => d.subtask === 'part-a')
  await assert.rejects(h.dispatcher.review({ item_id: implA.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  await h.dispatcher.review({ item_id: reviewer.item_id, verdict: 'terminate', reason: 'Reviewer died mid-run; Lead verified the files directly.' }, h.exec)
  await h.dispatcher.review({ item_id: implA.item_id, verdict: 'accept' }, h.exec)
  assert.equal(h.items[implA.item_id].acceptance, 'accepted')
})

test('without a configured reviewer the gate stays off (Lead verifies as before)', async () => {
  const h = fixture()
  const result = await h.run()
  const implA = result.deliveries.find(d => d.subtask === 'part-a')
  await h.dispatcher.review({ item_id: implA.item_id, verdict: 'accept' }, h.exec)
  assert.equal(h.items[implA.item_id].acceptance, 'accepted')
})
