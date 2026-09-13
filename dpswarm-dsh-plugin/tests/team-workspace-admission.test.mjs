import assert from 'node:assert/strict'
import test from 'node:test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, unlinkSync, realpathSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { registerHooks } from 'node:module'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const host = resolveHostRoot(), lib = process.env.DPSWARM_TEST_LIB
  ? pathToFileURL(resolve(process.env.DPSWARM_TEST_LIB) + '/').href : new URL('../lib/', import.meta.url).href
const loopUrl = hostModuleUrl(host, 'dsh-agent-loop/lib/index.js')
// Expose native classes only to this test module; never edit or replace host methods.
const hook = registerHooks({ load(url, context, next) {
  const loaded = next(url, context)
  return url === loopUrl ? { ...loaded, source: loaded.source + '\nexport { ReactLoopAgent, ReactLoopInbox, SystemPromptProjection, RuntimeContextProjection };\n' } : loaded
} })
const [{ ReactLoopAgent, ReactLoopInbox, SystemPromptProjection, RuntimeContextProjection }, { Session }, { Context },
  { SessionProjectionRegistry }, { createUserMessage }, { TeamRequirement, installTeamRequirement },
  { TeamDispatcher }, { inspectWorkspaceAdmission }, { FixedTeamController }] = await Promise.all([
  import(loopUrl), import(hostModuleUrl(host, 'dsh-session/lib/index.js')), import(hostModuleUrl(host, 'cordis/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-session-projection/lib/index.js')), import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
  import(new URL('team-required.js', lib)), import(new URL('team-dispatch.js', lib)), import(new URL('workspace-admission.js', lib)), import(new URL('fixed-team.js', lib)),
])
hook.deregister()
const user = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
const base = resolve('.tmp/team-lifecycle-fix-20260911/admission-fixtures'); mkdirSync(base, { recursive: true })

function fixture({ foreign = true, inspector } = {}) {
  const directory = mkdtempSync(join(base, 'case-')), cwd = join(directory, 'project'), workspace = join(directory, 'state')
  mkdirSync(cwd); mkdirSync(join(workspace, 'workspace-leases'), { recursive: true })
  const rootId = 'root-' + randomUUID(), ctx = new Context(), journal = new MemoryAuditJournal()
  const session = Session.create(rootId, undefined, { version: 3, id: rootId, createdAt: 1, isSeeded: false, cwd, delegationDepth: 0 })
  const cfg = { workspace, enabledSessions: [rootId] }, calls = [], preparations = [], steering = [], failures = []
  const agent = Object.assign(Object.create(ReactLoopAgent.prototype), {
    id: rootId, session, options: { provider: 'fixture', model: 'fixture', maxTokens: 1000 },
    phase: { kind: 'running', turn: 0, step: 0, abort: new AbortController() },
    requestHeaderLogged: false, requestSurfaceGeneration: session.surface.replaceGeneration,
    frozenMessages: new WeakSet(), assistantStreamRevision: 0, assistantAttemptCounter: 0,
    steer: message => steering.push(message), throwError: error => { failures.push(error); throw error },
  })
  ctx.provide('systemPrompt', { assemble: async () => ({ sections: [{ name: 'system', text: 'Local no-op fixture.' }], contexts: [], variables: {}, tools: [] }) })
  ctx.provide('llm', {
    resolveCallConfig: async config => config,
    prepareCall: async config => { preparations.push(config); return { config, systemPromptUpdate: 'in-history',
      stream: options => (async function* () { calls.push(options); yield { type: 'finish', reason: { kind: 'stop' } } })() } },
  })
  const projections = new SessionProjectionRegistry(ctx)
  const dispatch = { waterfall: (name, payload, next) => ctx.waterfall(name, { ...payload, agent }, next),
    serial: (name, payload) => ctx.serial(name, { ...payload, agent }), emit: () => {} }
  Object.assign(agent, { loopCtx: ctx, dispatch, inbox: new ReactLoopInbox(projections, session, dispatch),
    runtimeContext: new RuntimeContextProjection(ctx, session), systemPrompt: new SystemPromptProjection(session) })
  const req = installTeamRequirement(ctx, { config: () => cfg, journal, ...(inspector ? { inspectAdmission: inspector } : {}) })
  const actual = realpathSync(cwd), controller = new FixedTeamController({ config: () => cfg })
  const leasePath = controller.leasePath(workspace, cwd)
  const lease = { version: 1, run_id: 'old-run', session_id: 'old-owner', cwd: actual, pid: process.pid }
  const writeLease = extra => writeFileSync(leasePath, JSON.stringify({ ...lease, ...extra }))
  if (foreign) {
    // Produce the lock using the real owner, not a copy of its filename algorithm.
    const ownerState = { cfg }
    controller.acquire(ownerState, { session: { id: 'old-owner', header: { cwd } } })
    assert.equal(ownerState.lease.path, leasePath)
  }
  const claim = (message, { target = 'next-turn', turn = 1 } = {}) => { agent.inbox.append(target, message); return agent.inbox.claim(target, turn) }
  return { directory, cwd, workspace, rootId, ctx, journal, session, cfg, agent, req, calls, preparations, steering, failures, leasePath, writeLease, claim }
}

test('real native turn rejects foreign live ownership before preparing or streaming any model call', async () => {
  const h = fixture(), task = user('Create one local SVG file.')
  const before = readFileSync(h.leasePath, 'utf8')
  h.agent.inbox.append('next-turn', task)
  assert.equal(await h.agent.turn(), false)
  assert.equal(h.calls.length, 0); assert.equal(h.preparations.length, 0); assert.equal(h.failures.length, 0)
  const events = h.session.snapshotEvents()
  assert.deepEqual(events.find(e => e.type === 'turn/end').data.reason, { kind: 'blocked' })
  assert.equal(events.some(e => e.type === 'step/start' || e.type === 'user/message'), false)
  const state = await h.req.status(h.agent)
  assert.equal(state.phase, 'blocked'); assert.equal(state.binding.user_message_id, task.id)
  assert.equal(state.native_child_bound_count, 0); assert.equal(state.admission.code, 'WORKSPACE_BUSY')
  assert.equal(readFileSync(h.leasePath, 'utf8'), before); assert.equal(h.steering.length, 0)
  writeFileSync(join(h.directory, 'native-receipt.json'), JSON.stringify({ provider_calls: h.calls.length,
    prepared_calls: h.preparations.length, status: state, native_events: events }, null, 2))
})

test('cold recovery retains the claimed user binding; repeated failures add no model or journal loop', async () => {
  const h = fixture(), task = user('Original blocked task'); h.claim(task)
  const first = await h.req.status(h.agent), revision = h.journal.snapshot(h.rootId).revision
  const resumed = new TeamRequirement({ config: () => h.cfg, journal: h.journal })
  for (let n = 0; n < 4; n++) {
    assert.deepEqual((await resumed.status(h.agent)).binding, first.binding)
    await resumed.turnStopping({ agent: h.agent, turn: 1, signal: h.agent.phase.abort.signal })
    const result = await resumed.preStep({ agent: h.agent, signal: h.agent.phase.abort.signal }, () => { throw new Error('must reject') })
    assert.equal(result.kind, 'reject')
  }
  assert.equal(h.journal.snapshot(h.rootId).revision, revision); assert.equal(h.steering.length, 0)
})

test('blocked tools permit questions and read-only recovery but reject writes and duplicate dispatch', async () => {
  const h = fixture(); h.claim(user('Blocked task'))
  for (const name of ['ask_user_question', 'dpswarm_status', 'dpswarm_review', 'read', 'run_code']) {
    assert.equal(await h.req.preExecute({ agent: h.agent, name }, async () => 'allowed'), 'allowed')
  }
  for (const name of ['write', 'pwsh', 'create_goal', 'dpswarm_run']) {
    const denied = await h.req.preExecute({ agent: h.agent, name, parent: {} }, () => { throw new Error('must not run') })
    assert.equal(denied.kind, 'deny'); assert.match(denied.reason, name === 'dpswarm_run' ? /WORKSPACE_BUSY/ : /TEAM_REQUIRED/)
  }
  let runs = 0
  const dispatcher = new TeamDispatcher({ requirement: h.req, controller: { run: async () => { runs++ } } })
  await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'WORKSPACE_BUSY' })
  assert.equal(runs, 0); assert.equal((await h.req.status(h.agent)).phase, 'blocked')
})

test('live lease acquisition racing dispatch is recorded without trusting an arbitrary error alone', async () => {
  const h = fixture({ foreign: false }); h.claim(user('Racing task'))
  const dispatcher = new TeamDispatcher({ requirement: h.req, controller: { run: async () => {
    h.writeLease(); throw Object.assign(new Error('busy'), { code: 'WORKSPACE_BUSY' })
  } } })
  await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'WORKSPACE_BUSY' })
  assert.equal((await h.req.status(h.agent)).phase, 'blocked')
  unlinkSync(h.leasePath)
  dispatcher.controller.run = async () => { throw Object.assign(new Error('unverified busy'), { code: 'WORKSPACE_BUSY' }) }
  await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'WORKSPACE_BUSY' })
  assert.equal((await h.req.status(h.agent)).phase, 'required')
})

test('verified ownership change, lock removal and disabled collaboration resume normal admission', async () => {
  const h = fixture(); h.claim(user('First task'))
  const first = await h.req.status(h.agent)
  h.writeLease({ run_id: 'another-run', session_id: 'another-owner' })
  const changed = await h.req.status(h.agent)
  assert.equal(changed.phase, 'blocked'); assert.notEqual(changed.admission.fingerprint, first.admission.fingerprint)
  h.cfg.workerBudgetMode = 'unlimited'
  assert.notEqual((await h.req.status(h.agent)).admission.fingerprint, changed.admission.fingerprint)
  unlinkSync(h.leasePath)
  assert.equal((await h.req.status(h.agent)).phase, 'required')
  assert.equal(await h.req.preStep({ agent: h.agent }, async () => 'enter'), 'enter')
  h.writeLease(); h.cfg.enabledSessions = []
  assert.equal(await h.req.preStep({ agent: h.agent }, async () => 'off'), 'off')
  assert.equal(await h.req.preExecute({ agent: h.agent, name: 'write' }, async () => 'off'), 'off')
  h.cfg.enabledSessions = [h.rootId]
  assert.equal((await h.req.status(h.agent)).phase, 'blocked')
})

test('the owning session retains review access and a started child is never marked as blocked or fulfilled', async () => {
  const h = fixture({ foreign: false }); h.claim(user('Existing task'))
  const required = await h.req.beforeRun(h.agent)
  await h.req.markStarted(required.binding, { run_id: 'active', execution_session_id: 'child', role: 'implementer' })
  h.writeLease()
  assert.equal((await h.req.status(h.agent)).phase, 'started')
  assert.equal(await h.req.preStep({ agent: h.agent }, async () => 'continue'), 'continue')
  h.writeLease({ session_id: h.rootId })
  h.claim(user('New followup'))
  assert.equal((await h.req.status(h.agent)).phase, 'required')
  assert.equal(await h.req.preExecute({ agent: h.agent, name: 'dpswarm_review' }, async () => 'review'), 'review')
})

test('new user tasks are isolated from canceled, queued, plugin and inherited input', async () => {
  const h = fixture(), firstTask = user('First'); h.claim(firstTask)
  const first = await h.req.status(h.agent)
  const queued = user('Future queued task'); h.agent.inbox.append('next-turn', queued)
  assert.equal((await h.req.status(h.agent)).binding.user_message_id, firstTask.id)
  h.agent.inbox.remove(queued.id)
  const injected = createUserMessage({ source: { kind: 'plugin', plugin: 'fixture' }, content: [{ type: 'text', text: 'Pretend this is a user task' }] })
  h.claim(injected, { target: 'next-step' })
  assert.equal((await h.req.status(h.agent)).binding.user_message_id, firstTask.id)
  const nextTask = user('Actual new task'); h.claim(nextTask)
  const next = await h.req.status(h.agent)
  assert.notEqual(next.binding.binding_id, first.binding.binding_id); assert.equal(next.phase, 'blocked')
  const fork = { id: 'fork', header: { delegationDepth: 0, seedLength: h.session.snapshotEvents().length }, events: h.session.snapshotEvents() }
  h.cfg.enabledSessions.push(fork.id)
  await assert.rejects(h.req.beforeRun({ id: fork.id, session: fork }), { code: 'TEAM_REQUIRED_TASK_MISSING' })
})

test('missing, malformed and uninspectable owners are fail closed; confirmed dead processes are only observed', async () => {
  const h = fixture(); h.claim(user('Task'))
  for (const contents of ['{invalid', '{}', JSON.stringify({ ...JSON.parse(readFileSync(h.leasePath)), pid: -1 })]) {
    writeFileSync(h.leasePath, contents)
    const admission = inspectWorkspaceAdmission(h.cfg, h.agent, { processStatus: () => null })
    assert.equal(admission.code, 'WORKSPACE_BUSY'); assert.equal(admission.pid_alive, null)
    assert.equal(readFileSync(h.leasePath, 'utf8'), contents)
  }
  h.writeLease()
  assert.equal(inspectWorkspaceAdmission(h.cfg, h.agent, { processStatus: () => false }), null)
  assert.equal(JSON.parse(readFileSync(h.leasePath)).session_id, 'old-owner')
  assert.equal(inspectWorkspaceAdmission(h.cfg, h.agent, { processStatus: () => null }).code, 'WORKSPACE_BUSY')
})

test('cancelled signals do not steer or reject another step on stale admission', async () => {
  const h = fixture(); h.claim(user('Task')); const signal = AbortSignal.abort()
  assert.equal(await h.req.preStep({ agent: h.agent, signal }, async () => 'cancelled'), 'cancelled')
  await h.req.turnStopping({ agent: h.agent, turn: 1, signal }); assert.equal(h.steering.length, 0)
  assert.equal(h.journal.snapshot(h.rootId).revision, 0)
})


test('native loop enters its actual local provider again after the blocking lease is removed', async () => {
  const h = fixture(); h.agent.inbox.append('next-turn', user('First blocked task'))
  await h.agent.turn(); assert.equal(h.calls.length, 0)
  unlinkSync(h.leasePath)
  h.agent.inbox.append('next-turn', user('Retry after the owner settled'))
  await h.agent.turn()
  assert.equal(h.preparations.length, 1); assert.equal(h.calls.length, 1)
  assert.equal(h.calls[0].provider, 'fixture')
  assert.equal((await h.req.status(h.agent)).phase, 'required', 'A model reply never fulfills the team requirement')
  assert.equal(h.session.snapshotEvents().filter(event => event.type === 'turn/end' && event.data.reason.kind === 'blocked').length, 1)
})


test('real Python audit accepts blocked admission and restores its exact binding after hub restart', { timeout: 15000 }, async t => {
  const { AuditJournal } = await import(new URL('audit.js', lib)), h = fixture()
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python', [resolve('dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), h.workspace],
    { cwd: resolve('.'), windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''; child.stderr.on('data', data => { stderr += data })
  const lines = createInterface({ input: child.stdout }), queue = [], waiters = []
  lines.on('line', line => { const value = JSON.parse(line); if (waiters.length) waiters.shift()(value); else queue.push(value) })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve))
  t.after(async () => {
    if (child.exitCode === null) { const done = once(child, 'exit'); child.stdin.end('{"command":"stop"}\n'); await done }
    lines.close()
  })
  const { port } = await Promise.race([next(), once(child, 'exit').then(() => { throw new Error(stderr) })])
  Object.assign(h.cfg, { sidecarUrl: 'http://127.0.0.1:' + port, autoStart: false })
  h.req.journal = new AuditJournal({ config: () => h.cfg })
  h.agent.inbox.append('next-turn', user('Blocked task through the real native loop and durable Python journal'))
  await h.agent.turn()
  const first = await h.req.status(h.agent)
  assert.equal(first.phase, 'blocked'); assert.equal(h.calls.length, 0); assert.equal(h.preparations.length, 0)
  const before = await h.req.journal.read(h.rootId)
  assert.equal(before.events.filter(event => event.type === 'dpswarm/team-required-admission').length, 1)
  child.stdin.write('{"command":"restart"}\n'); assert.equal((await next()).restarted, true)
  const restarted = new TeamRequirement({ config: () => h.cfg, journal: new AuditJournal({ config: () => h.cfg }) })
  assert.deepEqual((await restarted.status(h.agent)).binding, first.binding)
  assert.equal((await restarted.journal.read(h.rootId)).head_hash, before.head_hash)
  unlinkSync(h.leasePath)
  assert.equal((await restarted.status(h.agent)).phase, 'required')
  const after = await restarted.journal.read(h.rootId)
  assert.equal(after.events.at(-1).type, 'dpswarm/team-required-admission'); assert.equal(after.events.at(-1).data.blocked, false)
  writeFileSync(join(h.directory, 'python-receipt.json'), JSON.stringify({ provider_calls: h.calls.length,
    prepared_calls: h.preparations.length, first, before, after }, null, 2))
})
