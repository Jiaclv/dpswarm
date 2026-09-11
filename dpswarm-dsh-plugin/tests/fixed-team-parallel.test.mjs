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
    workerTimeoutSeconds: 600, workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28,
    reworkBudgetMode: 'unlimited', teamModeOverrides: [{ sessionId: 'root', mode: 'parallel' }] }
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
    if (h.readyOnStart?.[id]) h.writeScope.updateArtifactState('root', h.readyOnStart[id], 'ready', 2)
    const allocationId = request.prompt[0].text.match(/^ALLOCATION:(\S+)/)?.[1]
    if (allocationId) allocations.get(allocationId).bound = true
    const reason = fail ? { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED' } } : { kind: 'completed' }
    session.events.push({ seq: 0, type: 'turn/end', data: { reason }, time: Date.now() })
    const marker = h.markerFor?.[id]
    const child = { id, request, provider, session, localAgent: { session },
      result: Promise.resolve({ output: [{ type: 'text', text: fail ? 'Partial candidate.' : marker ? `partial progress saved\n\n[DPSWARM_WAITING: ${marker}]` : (h.outputFor?.[id] ?? `done:${id}`) }],
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

test('the user team mode reaches the Lead: status surfaces it and a formless run under parallel says so (0.9.3)', async () => {
  const h = fixture()   // fixture overrides root to parallel
  const status = await h.controller.status(h.parent)
  assert.equal(status.team_mode.mode, 'parallel')
  assert.equal(status.team_mode.source, 'user popover override')
  assert.match(status.team_mode.instruction, /subtasks/)
  const result = await h.dispatcher.run({ task: 'Create the two parts.', acceptance: 'Both parts exist.' }, h.exec)
  assert.equal(result.team_mode.mode, 'parallel')
  assert.equal(result.team_mode.split_form, 'serial')
  assert.match(result.team_mode.note, /no matching split form/)
})

test('reviewing an already-terminal item the other way answers idempotently instead of hitting ILLEGAL_TRANSITION (0.9.4)', async () => {
  const h = fixture()
  const result = await h.run()
  const impl = result.deliveries.find(d => d.role === 'implementer')
  await h.controller.review({ item_id: impl.item_id, verdict: 'terminate', reason: 'superseded by a rework chain' }, h.exec)
  const again = await h.controller.review({ item_id: impl.item_id, verdict: 'accept', reason: 'late closeout attempt' }, h.exec)
  assert.equal(again.ok, true)
  assert.equal(again.outcome, 'already_terminated')
  assert.match(again.note, /already terminated/)
  assert.equal(h.items[impl.item_id].acceptance, 'terminated', 'no phantom transition landed')
})

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

test('whoever raised the defect re-checks it: a configured reviewer lineage re-reviews after rework and its verdict gates (0.9.6)', async () => {
  const h = fixture()
  h.cfg.reviewerMode = 'model'; h.cfg.reviewerProvider = 'fixture'; h.cfg.reviewerModel = 'reviewer'
  const result = await h.run()
  assert.equal(result.deliveries.length, 4, '2 implementers + tester + reviewer')
  const implA = result.deliveries.find(d => d.subtask === 'part-a')
  // Realistic flow: the initial reviewer's verdict drove the rework, so the Lead
  // settles it as evidence first; the lineage gate counts every open item.
  const firstReview = result.deliveries.find(d => d.role === 'reviewer')
  await h.dispatcher.review({ item_id: firstReview.item_id, verdict: 'accept' }, h.exec)
  const reworked = await h.dispatcher.rework({ item_id: implA.item_id, feedback: 'Fix the reviewed defects only.' }, h.exec)
  assert.equal(reworked.failed.length, 0)
  assert.deepEqual(reworked.deliveries.map(d => d.role), ['implementer', 'tester', 'reviewer'], 'rework round re-runs the whole verification chain')
  const reReview = reworked.deliveries.find(d => d.role === 'reviewer')
  assert.equal(reReview.verification_of, reworked.deliveries[0].item_id)
  assert.match(reReview.title, /re-review/)
  // The continuation is the latest reviewer lineage and its prompt carries its own earlier verdict context.
  const continuation = h.children.at(-1)
  assert.match(continuation.request.prompt[0].text, /linked re-review after implementer rework/)
  // Capacity: the initial run raised the cap for 4 roles; after the first
  // review, 3 originals stay open, so rework raises for 3 continuations to 6.
  assert.deepEqual(h.specCalls.map(c => c.max_team_workers), [4, 6])
  // The pending re-review verdict gates acceptance of the reworked delivery.
  const newImpl = reworked.deliveries[0]
  await assert.rejects(h.dispatcher.review({ item_id: newImpl.item_id, verdict: 'accept' }, h.exec), /REVIEWER_PENDING/)
  await h.dispatcher.review({ item_id: reReview.item_id, verdict: 'accept' }, h.exec)
  await h.dispatcher.review({ item_id: newImpl.item_id, verdict: 'accept' }, h.exec)
  assert.equal(h.items[newImpl.item_id].acceptance, 'accepted')
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

test('staged mode registers the artifact board and dispatches dependency waves', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  const result = await h.dispatcher.run({ task: 'Build the scene.', acceptance: 'Scene exists.', staged: {
    phases: [{ id: 'contracts', task: 'Write the layout contract.' }, { id: 'build', task: 'Build the parts per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define sizes and anchors under src/layout/**.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: 'Build the bicycle per the contract.', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
      { id: 'bird', title: '鹈鹕', task: 'Build the pelican per the contract.', write_globs: ['src/bird/**'], phase: 'build', deps: ['layout'] },
    ],
  } }, h.exec)
  assert.equal(result.failed.length, 0)
  assert.deepEqual(result.deliveries.map(d => [d.role, d.subtask ?? null]),
    [['implementer', 'layout'], ['implementer', 'bike'], ['implementer', 'bird'], ['tester', null]])
  assert.deepEqual(h.children.map(c => c.id), ['child-0', 'child-1', 'child-2', 'child-3'])
  assert.match(h.children[0].request.prompt[0].text, /布局契约/)
  assert.match(h.children[1].request.prompt[0].text, /dpswarm_artifact/)
  const artifacts = h.writeScope.artifactsFor('root')
  assert.deepEqual(artifacts.map(a => [a.id, a.state]).sort(), [['bike', 'claimed'], ['bird', 'claimed'], ['layout', 'claimed']])
  assert.deepEqual(h.writeScope.claimsFor('root').map(c => c.subtask).sort(), ['bike', 'bird', 'layout'])
})

test('dpswarm_artifact advances the caller-owned artifact and rejects unclaimed callers', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'only', task: 'Do both parts.' }],
    artifacts: [
      { id: 'a', title: 'A', task: 'Do A.', write_globs: ['a/**'], phase: 'only' },
      { id: 'b', title: 'B', task: 'Do B.', write_globs: ['b/**'], phase: 'only' },
    ],
  } }, h.exec)
  const wA = h.sessions.get('child-0').session
  const res = await h.controller.artifactState({ to: 'draft', note: 'started' }, { agent: { session: wA } })
  assert.equal(res.ok, true)
  const mirror = h.writeScope.artifactsFor('root').find(a => a.id === 'a')
  assert.equal(mirror.state, 'draft')
  assert.equal(mirror.version, 2)
  const testerSession = h.sessions.get('child-2').session
  await assert.rejects(h.controller.artifactState({ to: 'draft' }, { agent: { session: testerSession } }), { code: 'ARTIFACT_NOT_CLAIMED' })
  await assert.rejects(h.controller.artifactState({ to: '' }, { agent: { session: wA } }), { code: 'ARTIFACT_STATE_REQUIRED' })
})

test('staged suspend/wake: a worker waiting on a not-ready artifact is continued after it turns ready', async () => {
  const h = fixture()
  h.markerFor = { 'child-1': 'bike' }      // bird's first pass ends waiting on bike
  h.readyOnStart = { 'child-1': 'bike' }   // bike's artifact flips ready while bird runs
  h.cfg.teamModeOverrides[0].mode = 'staged'
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'build', task: 'Build both parts.' }],
    artifacts: [
      { id: 'bike', title: '自行车', task: 'Build the bicycle.', write_globs: ['src/bike/**'], phase: 'build' },
      { id: 'bird', title: '鹈鹕', task: 'Build the pelican.', write_globs: ['src/bird/**'], phase: 'build', deps: ['bike'] },
    ],
  } }, h.exec)
  assert.equal(result.failed.length, 0)
  // children: bike, bird(first pass waits), bird wake continuation, tester
  assert.equal(h.children.length, 4)
  assert.match(h.children[2].request.prompt[0].text, /唤醒继续（bird）/)
  assert.match(h.children[2].request.prompt[0].text, /此前的进度/)
  assert.deepEqual(result.deliveries.map(d => [d.role, d.subtask ?? null]),
    [['implementer', 'bike'], ['implementer', 'bird'], ['tester', null]])
  // The waiting first pass never landed in deliveries as a completed item.
  assert.equal(result.deliveries.filter(d => /DPSWARM_WAITING/.test(d.output || '')).length, 0)
})

test('a wait whose artifact never turns ready fails honestly with ARTIFACT_WAIT_TIMEOUT', async () => {
  const h = fixture()
  h.markerFor = { 'child-1': 'bike' }   // bird waits on bike; nothing marks bike ready
  h.cfg.teamModeOverrides[0].mode = 'staged'
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'build', task: 'Build both parts.' }],
    artifacts: [
      { id: 'bike', title: '自行车', task: 'Build the bicycle.', write_globs: ['src/bike/**'], phase: 'build' },
      { id: 'bird', title: '鹈鹕', task: 'Build the pelican.', write_globs: ['src/bird/**'], phase: 'build', deps: ['bike'] },
    ],
  } }, h.exec)
  assert.equal(h.children.length, 3, 'bike, bird-wait, tester — no wake continuation')
  const timeout = result.failed.find(f => f.code === 'ARTIFACT_WAIT_TIMEOUT')
  assert.ok(timeout)
  assert.equal(timeout.subtask, 'bird')
})

test('staged phase handoff: a wave opening a new phase carries the prior deliveries digest', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'contracts', task: 'Write the contract.' }, { id: 'build', task: 'Build per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define the layout.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: 'Build it per the contract.', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
    ],
  } }, h.exec)
  assert.equal(result.failed.length, 0)
  assert.equal(h.children.length, 3)
  assert.match(h.children[1].request.prompt[0].text, /前序相位交付摘要/)
  assert.match(h.children[1].request.prompt[0].text, /【layout】/)
  assert.match(h.children[1].request.prompt[0].text, /done:child-0/, 'the digest carries the prior delivery excerpt')
  assert.doesNotMatch(h.children[0].request.prompt[0].text, /前序相位交付摘要/, 'the first phase gets no digest')
})

// ---- 0.10.0：三层交接包 / verbatim 直贴契约 / 验收可见性（Python 五轮实验回移）----

test('staged phase handoff is a three-layer package: L2 digest, L1 verbatim facts, L0 read-gate reference', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  h.outputFor = { 'child-0': '契约 v1.0.0\n```json\n{"type":"object"}\n```' }
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'contracts', task: 'Write the contract.' }, { id: 'build', task: 'Build per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define the layout.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: 'Build it per the contract.', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
    ],
  } }, h.exec)
  assert.equal(result.failed.length, 0)
  const prompt = h.children[1].request.prompt[0].text
  assert.match(prompt, /## 上游交付交接（三层：L2 摘要导航 \/ L1 逐字依据 \/ L0 原文回查）（handoff_profile=semantic）/)
  assert.ok(prompt.indexOf('### L2 摘要层') < prompt.indexOf('### L1 原子事实层'), 'semantic keeps the digest first')
  assert.ok(prompt.indexOf('### L1 原子事实层') < prompt.indexOf('### L0 原文层'), 'L0 reference closes the package')
  assert.match(prompt, /### L2 摘要层（前序相位交付摘要）（摘要仅供导航，逐字内容以 L1\/原文为准）/)
  assert.match(prompt, /【layout】契约 v1\.0\.0/, 'L2 keeps the bounded per-delivery digest')
  assert.ok(prompt.includes('```json\n{"type":"object"}\n```'), 'L1 carries the fenced block verbatim')
  assert.match(prompt, /### L0 原文层[\s\S]*【layout】「布局契约」文件路径：src\/layout\/\*\*；逐字原文：直接读取上述路径取回（ready 产物读门放行/)
  assert.doesNotMatch(prompt, /不可信摘要/, 'the untrusted-summary wording is retired')
  assert.doesNotMatch(prompt, /禁止凭记忆复述/, 'semantic profile carries no direct-paste contract')
  assert.doesNotMatch(h.children[0].request.prompt[0].text, /上游交付交接/, 'the first phase gets no handoff')
  const events = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/handoff-profile')
  assert.equal(events.length, 1, 'one classification audit per handed-off artifact')
  assert.equal(events[0].data.artifact_id, 'bike')
  assert.deepEqual({ profile: events[0].data.profile, decider: events[0].data.decider }, { profile: 'semantic', decider: 'rule' })
  assert.deepEqual(events[0].data.upstreams, ['layout'])
})

test('staged phase handoff: a verbatim-dependency artifact gets L1 first plus the direct-paste contract', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  h.outputFor = { 'child-0': '契约\n```json\n{"type":"object"}\n```' }
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'contracts', task: 'Write the contract.' }, { id: 'build', task: 'Build per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define the layout schema.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: '逐字引用 layout 的 schema 契约实现校验器。', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
    ],
  } }, h.exec)
  assert.equal(result.failed.length, 0)
  const prompt = h.children[1].request.prompt[0].text
  assert.match(prompt, /handoff_profile=verbatim/)
  assert.ok(prompt.indexOf('### L1 原子事实层') < prompt.indexOf('### L2 摘要层'), 'verbatim promotes L1 above the digest')
  assert.match(prompt, /\*\*逐字内容禁止凭记忆复述\*\*：交付中需要逐字引用上游内容时，必须先读取上游产物原文/)
  assert.match(prompt, /再逐字直贴；L1 层仅供定位预览。/)
  const events = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/handoff-profile')
  assert.deepEqual(events.map(e => [e.data.artifact_id, e.data.profile, e.data.decider]), [['bike', 'verbatim', 'rule']])
})

test('reviewer prompt carries upstream ready deliveries for artifacts with deps; missing material is explicit', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  h.cfg.reviewerMode = 'model'; h.cfg.reviewerProvider = 'fixture'; h.cfg.reviewerModel = 'reviewer'
  h.outputFor = { 'child-0': '布局契约交付：锚点 v1.0.0' }
  h.readyOnStart = { 'child-1': 'layout' }   // layout flips ready while bike runs
  const staged = {
    phases: [{ id: 'contracts', task: 'Write the contract.' }, { id: 'build', task: 'Build per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define the layout.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: 'Build it per the contract.', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
    ],
  }
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged }, h.exec)
  assert.equal(result.deliveries.length, 4, 'layout, bike, tester, reviewer')
  const reviewerPrompt = h.children[3].request.prompt[0].text
  assert.match(reviewerPrompt, /## 验收可见性（上游依赖交付；跨产物依赖约束必须对照上游交付逐字核验；缺材料不可验收通过。）/)
  assert.match(reviewerPrompt, /### 产物「自行车」（bike）的上游依赖核验材料/)
  assert.match(reviewerPrompt, /【layout】「布局契约」（状态 ready；路径：src\/layout\/\*\*；交付全文：dpswarm_report\("item-0"\) 分页读取）逐字交付：\n布局契约交付：锚点 v1\.0\.0/)
  // A second run without the ready flip marks the upstream material as missing.
  const h2 = fixture()
  h2.cfg.teamModeOverrides[0].mode = 'staged'
  h2.cfg.reviewerMode = 'model'; h2.cfg.reviewerProvider = 'fixture'; h2.cfg.reviewerModel = 'reviewer'
  const result2 = await h2.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged }, h2.exec)
  assert.equal(result2.failed.length, 0)
  const reviewerPrompt2 = h2.children[3].request.prompt[0].text
  assert.match(reviewerPrompt2, /【layout】「布局契约」（状态 claimed；路径：src\/layout\/\*\*；交付全文：dpswarm_report\("item-0"\) 分页读取）交付内容不可见——缺材料不可验收通过/)
  assert.ok(!reviewerPrompt2.includes('done:child-0\n'), 'a non-ready upstream delivery body never leaks into the verdict material')
})

test('acceptance visibility rides the run result and dpswarm_review for dep-carrying artifacts', async () => {
  const h = fixture()
  h.cfg.teamModeOverrides[0].mode = 'staged'
  h.outputFor = { 'child-0': '布局契约交付：锚点 v1.0.0' }
  h.readyOnStart = { 'child-1': 'layout' }
  const result = await h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: {
    phases: [{ id: 'contracts', task: 'Write the contract.' }, { id: 'build', task: 'Build per the contract.' }],
    artifacts: [
      { id: 'layout', title: '布局契约', task: 'Define the layout.', write_globs: ['src/layout/**'], phase: 'contracts' },
      { id: 'bike', title: '自行车', task: 'Build it per the contract.', write_globs: ['src/bike/**'], phase: 'build', deps: ['layout'] },
    ],
  } }, h.exec)
  assert.match(result.acceptance_visibility, /## 验收可见性/)
  assert.match(result.acceptance_visibility, /逐字交付：\n布局契约交付：锚点 v1\.0\.0/)
  assert.match(result.next, /缺材料不可验收通过/, 'the Lead guidance states the missing-material rule')
  const bike = result.deliveries.find(d => d.subtask === 'bike')
  const reviewed = await h.dispatcher.review({ item_id: bike.item_id, verdict: 'accept' }, h.exec)
  assert.match(reviewed.upstream_evidence, /### 产物「自行车」（bike）的上游依赖核验材料/)
  assert.match(reviewed.upstream_evidence, /逐字交付：\n布局契约交付：锚点 v1\.0\.0/, 'review result co-locates the upstream evidence from the audit ledger')
  const layout = result.deliveries.find(d => d.subtask === 'layout')
  const layoutReview = await h.dispatcher.review({ item_id: layout.item_id, verdict: 'accept' }, h.exec)
  assert.equal(layoutReview.upstream_evidence, undefined, 'no deps → no upstream evidence attached')
})
