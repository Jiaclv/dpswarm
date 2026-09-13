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
const base = resolve('.tmp/runtime-protocol-fix-20260911/infrastructure-fixtures'); mkdirSync(base, { recursive: true })

function fixture({ foreign = false, inspector, checkRuntime } = {}) {
  const directory = mkdtempSync(join(base, 'case-')), cwd = join(directory, 'project'), workspace = join(directory, 'state')
  mkdirSync(cwd); mkdirSync(join(workspace, 'workspace-leases'), { recursive: true })
  const rootId = 'root-' + randomUUID(), ctx = new Context(), journal = new MemoryAuditJournal()
  const session = Session.create(rootId, undefined, { version: 3, id: rootId, createdAt: 1, isSeeded: false, cwd, delegationDepth: 0 })
  const cfg = { workspace, enabledSessions: [rootId] }, calls = [], preparations = [], steering = [], failures = []
  const stream = { before: null }
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
      stream: options => (async function* () { calls.push(options); await stream.before?.(); yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Infrastructure is blocked; the original task remains pending.' } }; yield { type: 'finish', reason: { kind: 'stop' } } })() } },
  })
  const projections = new SessionProjectionRegistry(ctx)
  const dispatch = { waterfall: (name, payload, next) => ctx.waterfall(name, { ...payload, agent }, next),
    serial: (name, payload) => ctx.serial(name, { ...payload, agent }), emit: () => {} }
  Object.assign(agent, { loopCtx: ctx, dispatch, inbox: new ReactLoopInbox(projections, session, dispatch),
    runtimeContext: new RuntimeContextProjection(ctx, session), systemPrompt: new SystemPromptProjection(session) })
  const req = installTeamRequirement(ctx, { config: () => cfg, journal, checkRuntime, infrastructureStateDirectory: join(workspace, 'runtime-blocks'), ...(inspector ? { inspectAdmission: inspector } : {}) })
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
  return { directory, cwd, workspace, rootId, ctx, journal, session, cfg, agent, req, calls, preparations, steering, failures, leasePath, writeLease, claim, stream }
}

const err = (code, fingerprint) => Object.assign(new Error(code), { code, details: { fingerprint } })
const health = fingerprint => ({ compatible: true, fingerprint, runtime: {} })
const start = h => ({ run_id: 'same-run', execution_session_id: 'native-child', role: 'implementer' })
const resume = (h, options = {}) => new TeamRequirement({ config: () => h.cfg, journal: h.req.journal,
  checkRuntime: h.req.checkRuntime, infrastructureStateDirectory: join(h.workspace, 'runtime-blocks'), ...options })

async function publishAndFail(h, code = 'PLUGIN_AUDIT_INVALID_EVENT') {
  let dispatched = 0
  const dispatcher = new TeamDispatcher({ requirement: h.req, controller: { run: async (_args, _exec, hooks) => {
    dispatched++; await hooks.onChildStarted(start(h)); throw err(code)
  } } })
  await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code })
  return { dispatcher, count: () => dispatched }
}

test('native turn stopping after a published delivery infrastructure fault ends normally without steering or retry', async () => {
  const h = fixture({ checkRuntime: async () => health('old') })
  h.agent.inbox.append('next-turn', user('Use the original fixed team task'))
  h.stream.before = async () => { await publishAndFail(h) }
  await h.agent.turn()
  assert.equal(h.calls.length, 1); assert.equal(h.preparations.length, 1)
  assert.equal(h.steering.length, 0); assert.equal(h.failures.length, 0)
  const state = await h.req.status(h.agent)
  assert.equal(state.phase, 'blocked'); assert.equal(state.lifecycle_phase, 'started')
  assert.equal(state.native_child_bound_count, 1); assert.equal(state.finish, undefined)
  assert.ok(h.session.snapshotEvents().some(event => event.type === 'turn/end'))
  const original = (await h.req.beforeRun(h.agent)).binding
  for (let turn = 1; turn <= 4; turn++) await h.req.turnStopping({ agent: h.agent, turn, signal: h.agent.phase.abort.signal })
  assert.equal(h.steering.length, 0)
  h.agent.inbox.append('next-turn', createUserMessage({ source: { kind: 'plugin', plugin: 'fixture' }, content: [{ type: 'text', text: 'Resume original pending task' }] }))
  await h.agent.turn()
  assert.equal(h.calls.length, 1); assert.equal(h.preparations.length, 1)
  assert.deepEqual((await h.req.beforeRun(h.agent)).binding, original)
  assert.equal(h.session.snapshotEvents().filter(event => event.type === 'turn/end').at(-1).data.reason.kind, 'blocked')
  writeFileSync(join(h.directory, 'native-infrastructure-receipt.json'), JSON.stringify({ external_model_calls: 0,
    calls: h.calls.length, preparations: h.preparations.length, steering: h.steering, status: state,
    events: h.session.snapshotEvents() }, null, 2))
})

test('known runtime incompatibility blocks first native turn with zero prepared or streamed model requests', async () => {
  const h = fixture({ checkRuntime: async () => { throw err('SIDECAR_RUNTIME_INCOMPATIBLE', 'old') } })
  h.agent.inbox.append('next-turn', user('Do this task'))
  await h.agent.turn()
  assert.equal(h.calls.length, 0); assert.equal(h.preparations.length, 0); assert.equal(h.steering.length, 0)
  assert.equal((await h.req.status(h.agent)).phase, 'blocked')
  const cold = resume(h)
  assert.equal((await cold.status(h.agent)).phase, 'blocked')
  h.req.checkRuntime = async () => { throw err('ECONNREFUSED') }
  assert.equal((await h.req.status(h.agent)).phase, 'blocked', 'unavailable probe is not recovery')
  h.req.checkRuntime = async () => health('new')
  assert.equal((await h.req.status(h.agent)).phase, 'required')
})

const hostFailure = () => Object.assign(new Error('The managed host capability lookup failed.'), {
  code: 'HOST_RUNTIME_INCOMPATIBLE', details: { component: 'storage', stage: 'lookup' },
})
const hostHealth = fingerprint => ({ ...health(fingerprint), host_runtime: { compatible: true } })

test('pre-child host failure lets the native turn explain the pending task and blocks repeat dispatch without ledger growth', async () => {
  const h = fixture({ checkRuntime: async () => health('sidecar-old') })
  let dispatched = 0
  const dispatcher = new TeamDispatcher({ requirement: h.req, controller: { run: async () => {
    dispatched++; throw hostFailure()
  } } })
  h.agent.inbox.append('next-turn', user('Use the team for the original task'))
  h.stream.before = async () => { await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'HOST_RUNTIME_INCOMPATIBLE' }) }
  await h.agent.turn()
  assert.equal(h.calls.length, 1); assert.equal(h.preparations.length, 1)
  assert.equal(h.steering.length, 0); assert.equal(h.failures.length, 0)
  const first = await h.req.status(h.agent), journal = h.journal.snapshot(h.rootId)
  assert.equal(first.phase, 'blocked'); assert.equal(first.lifecycle_phase, 'required')
  assert.equal(first.admission.code, 'HOST_RUNTIME_INCOMPATIBLE')
  assert.equal(first.native_child_bound_count, 0); assert.equal(first.finish, undefined)
  assert.equal(journal.events.filter(event => event.type === 'dpswarm/team-required-admission').length, 1)
  assert.ok(h.session.snapshotEvents().some(event => event.type === 'turn/end'))
  assert.ok(JSON.stringify(h.session.snapshotEvents()).includes('Infrastructure is blocked; the original task remains pending.'))
  h.req.checkRuntime = async () => health('sidecar-new')
  const cold = resume(h)
  for (let turn = 1; turn <= 4; turn++) {
    assert.equal((await h.req.status(h.agent)).phase, 'blocked')
    assert.equal((await cold.status(h.agent)).phase, 'blocked')
    assert.equal(await h.req.preExecute({ agent: h.agent, name: 'ask_user_question' }, () => 'question allowed'), 'question allowed')
    for (const name of ['write', 'pwsh', 'dpswarm_run', 'dpswarm_rework']) {
      assert.equal((await h.req.preExecute({ agent: h.agent, name, parent: {} }, () => { throw Error('must not call') })).kind, 'deny')
    }
    await h.req.turnStopping({ agent: h.agent, turn, signal: h.agent.phase.abort.signal })
    await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'HOST_RUNTIME_INCOMPATIBLE' })
    assert.equal(h.journal.snapshot(h.rootId).revision, journal.revision)
  }
  assert.equal(dispatched, 1); assert.equal(h.steering.length, 0)
  h.agent.inbox.append('next-turn', createUserMessage({ source: { kind: 'plugin', plugin: 'fixture' }, content: [{ type: 'text', text: 'Continue original task' }] }))
  await h.agent.turn()
  assert.equal(h.calls.length, 1); assert.equal(h.preparations.length, 1)
  assert.equal(h.session.snapshotEvents().filter(event => event.type === 'turn/end').at(-1).data.reason.kind, 'blocked')
  h.req.checkRuntime = async () => hostHealth('host-and-sidecar-compatible')
  const recovered = await h.req.status(h.agent)
  assert.equal(recovered.phase, 'required'); assert.deepEqual(recovered.binding, first.binding)
  assert.equal(recovered.native_child_bound_count, 0); assert.equal(recovered.finish, undefined)
  assert.equal((await h.req.beforeDispatch(h.agent)).phase, 'required')
  assert.equal((await h.req.preExecute({ agent: h.agent, name: 'write' }, () => 'forbidden')).kind, 'deny')
  assert.equal(h.journal.snapshot(h.rootId).events.filter(event => event.type === 'dpswarm/team-required-finished').length, 0)
  writeFileSync(join(h.directory, 'native-host-infrastructure-receipt.json'), JSON.stringify({ external_model_calls: 0,
    preparations: h.preparations.length, fixture_calls: h.calls.length, dispatched, first, recovered,
    events: h.session.snapshotEvents(), audit: h.journal.snapshot(h.rootId) }, null, 2))
})

test('typed host probe failure blocks before the first native model request until both runtimes are compatible', async () => {
  const h = fixture({ checkRuntime: async () => { throw hostFailure() } })
  h.agent.inbox.append('next-turn', user('Perform the fixed team task'))
  await h.agent.turn()
  assert.equal(h.calls.length, 0); assert.equal(h.preparations.length, 0); assert.equal(h.steering.length, 0)
  const blocked = await h.req.status(h.agent), revision = h.journal.snapshot(h.rootId).revision
  assert.equal(blocked.phase, 'blocked'); assert.equal(blocked.native_child_bound_count, 0)
  assert.equal((await h.req.status(h.agent)).phase, 'blocked')
  assert.equal(h.journal.snapshot(h.rootId).revision, revision)
  for (const probe of [health('new-sidecar'), { ...health('still-broken'), host_runtime: { compatible: false } },
    { ...hostHealth('sidecar-incompatible'), compatible: false }]) {
    h.req.checkRuntime = async () => probe
    assert.equal((await h.req.status(h.agent)).phase, 'blocked')
    assert.equal(h.journal.snapshot(h.rootId).revision, revision)
  }
  h.req.checkRuntime = async () => hostHealth('repaired')
  const recovered = await h.req.status(h.agent)
  assert.equal(recovered.phase, 'required'); assert.deepEqual(recovered.binding, blocked.binding)
  assert.equal(recovered.finish, undefined)
})

test('authenticated host failure checkpoint survives a cold restart and sidecar-only health', async () => {
  const h = fixture({ checkRuntime: async () => health('sidecar') }); h.claim(user('Task pending host repair'))
  const original = await h.req.beforeRun(h.agent), transaction = h.journal.transaction.bind(h.journal)
  h.req.checkRuntime = async () => { throw hostFailure() }
  h.journal.transaction = async () => { throw err('PLUGIN_AUDIT_IO_ERROR') }
  assert.equal((await h.req.status(h.agent)).admission.code, 'HOST_RUNTIME_INCOMPATIBLE')
  h.journal.transaction = transaction
  const cold = resume(h, { checkRuntime: async () => health('new-sidecar') })
  const retained = await cold.status(h.agent)
  assert.equal(retained.phase, 'blocked'); assert.equal(retained.admission.code, 'HOST_RUNTIME_INCOMPATIBLE')
  assert.equal(retained.native_child_bound_count, 0)
  assert.equal((await cold.preExecute({ agent: h.agent, name: 'write' }, () => 'forbidden')).kind, 'deny')
  cold.checkRuntime = async () => hostHealth('host-repaired')
  const recovered = await cold.status(h.agent)
  assert.equal(recovered.phase, 'required'); assert.equal(recovered.finish, undefined)
  assert.equal(recovered.binding.binding_id, original.binding.binding_id)
  const { InfrastructureBlockedStore } = await import(new URL('infrastructure-blocked.js', lib))
  assert.equal(new InfrastructureBlockedStore(join(h.workspace, 'runtime-blocks')).read(original.binding), null)
})

test('host recovery preserves published-child ownership and never permits a duplicate team dispatch', async () => {
  const h = fixture({ checkRuntime: async () => health('sidecar') }); h.claim(user('Original published task'))
  const operation = await publishAndFail(h, 'HOST_RUNTIME_INCOMPATIBLE')
  const blocked = await h.req.status(h.agent)
  assert.equal(blocked.phase, 'blocked'); assert.equal(blocked.lifecycle_phase, 'started')
  assert.equal(blocked.native_child_bound_count, 1)
  await assert.rejects(operation.dispatcher.run({}, { agent: h.agent }), { code: 'HOST_RUNTIME_INCOMPATIBLE' })
  h.req.checkRuntime = async () => hostHealth('repaired-host')
  const recovered = await h.req.status(h.agent)
  assert.equal(recovered.phase, 'started'); assert.equal(recovered.native_child_bound_count, 1)
  assert.deepEqual(recovered.binding, blocked.binding); assert.equal(recovered.finish, undefined)
  await assert.rejects(operation.dispatcher.run({}, { agent: h.agent }), { code: 'TEAM_REQUIRED_REVIEW_REQUIRED' })
  assert.equal(operation.count(), 1)
  const audit = h.journal.snapshot(h.rootId)
  assert.equal(audit.events.filter(event => event.type === 'dpswarm/team-required-started').length, 1)
  assert.equal(audit.events.filter(event => event.type === 'dpswarm/team-required-finished').length, 0)
})

test('unstarted service connection refusal does not prevent ordinary ensure from starting a fresh run', async () => {
  const h = fixture({ checkRuntime: async () => { throw err('ECONNREFUSED') } })
  h.claim(user('Fresh task'))
  assert.equal((await h.req.beforeDispatch(h.agent)).phase, 'required')
})

test('blocked tools keep status, review and questions available, deny writes and duplicate dispatch', async () => {
  const h = fixture({ checkRuntime: async () => health('old') }); h.claim(user('Original task'))
  const operation = await publishAndFail(h)
  const revision = h.journal.snapshot(h.rootId).revision
  for (const name of ['dpswarm_status', 'dpswarm_review', 'ask_user_question', 'read', 'run_code']) {
    assert.equal(await h.req.preExecute({ agent: h.agent, name }, async () => 'allowed'), 'allowed')
  }
  for (const name of ['write', 'pwsh', 'dpswarm_run', 'dpswarm_rework']) {
    assert.equal((await h.req.preExecute({ agent: h.agent, name, parent: {} }, () => { throw Error('must not call') })).kind, 'deny')
  }
  await assert.rejects(operation.dispatcher.run({}, { agent: h.agent }), { code: 'PLUGIN_AUDIT_INVALID_EVENT' })
  assert.equal(operation.count(), 1)
  assert.equal(h.journal.snapshot(h.rootId).revision, revision, 'same blocker never adds a journal retry loop')
})

test('business, child cleanup and reviewer errors are never classified as infrastructure blocked', async t => {
  for (const code of ['TEST_FAILED', 'REVIEWER_PENDING', 'CHILD_CLEANUP_FAILED', 'WORKER_TOKEN_RESERVATION_DENIED', 'EUNKNOWN']) await t.test(code, async () => {
    const h = fixture({ checkRuntime: async () => health('old') }); h.claim(user('Task'))
    await publishAndFail(h, code)
    assert.equal((await h.req.status(h.agent)).phase, 'started')
  })
})

test('audit append failure uses authenticated root/task checkpoint; cold restart stays blocked and changed runtime restores only review', async () => {
  const h = fixture({ checkRuntime: async () => health('old') }); const task = user('Original task'); h.claim(task)
  const originalTransaction = h.journal.transaction.bind(h.journal)
  let auditBroken = false
  h.journal.transaction = (rootId, build) => originalTransaction(rootId, async snapshot => {
    const built = await build(snapshot)
    if (auditBroken && built.events.length) throw err('PLUGIN_AUDIT_IO_ERROR')
    return built
  })
  const binding = await h.req.beforeRun(h.agent)
  await h.req.markStarted(binding, start(h)); auditBroken = true
  await h.req.markInfrastructureBlocked(h.agent, err('PLUGIN_AUDIT_INVALID_EVENT'), { run_id: 'same-run' })
  const first = await h.req.status(h.agent)
  assert.equal(first.phase, 'blocked'); assert.equal(first.native_child_bound_count, 1)
  const cold = resume(h)
  const checkpoint = (await import(new URL('infrastructure-blocked.js', lib))).InfrastructureBlockedStore
  const store = new checkpoint(join(h.workspace, 'runtime-blocks')), before = readFileSync(store.path(h.rootId), 'utf8')
  for (let n = 0; n < 3; n++) {
    assert.equal((await cold.status(h.agent)).phase, 'blocked')
    await cold.turnStopping({ agent: h.agent, turn: n, signal: h.agent.phase.abort.signal })
  }
  assert.equal(readFileSync(store.path(h.rootId), 'utf8'), before)
  assert.equal(h.steering.length, 0)
  cold.checkRuntime = async () => health('new')
  assert.equal((await cold.status(h.agent)).phase, 'blocked', 'runtime change alone cannot bypass a still broken audit writer')
  auditBroken = false
  const restored = await cold.status(h.agent)
  assert.equal(restored.phase, 'started'); assert.equal(restored.native_child_bound_count, 1)
  await assert.rejects(cold.beforeDispatch(h.agent), { code: 'TEAM_REQUIRED_REVIEW_REQUIRED' })
  assert.equal((await cold.preExecute({ agent: h.agent, name: 'write' }, () => 'forbidden')).kind, 'deny')
  assert.equal(restored.finish, undefined)
})

test('unsigned checkpoint and changed root/task identities cannot create blocked authority', async () => {
  const h = fixture({ checkRuntime: async () => health('old') }); h.claim(user('Original'))
  const binding = (await h.req.beforeRun(h.agent)).binding
  const { InfrastructureBlockedStore } = await import(new URL('infrastructure-blocked.js', lib))
  const store = new InfrastructureBlockedStore(join(h.workspace, 'runtime-blocks'))
  mkdirSync(store.directory, { recursive: true })
  writeFileSync(store.path(h.rootId), JSON.stringify({ record: { version: 1, state: { phase: 'finished', binding } }, mac: '00'.repeat(32) }))
  assert.throws(() => store.read(binding))
  unlinkSync(store.path(h.rootId))
  const state = { ...(await h.req.beforeRun(h.agent)), phase: 'blocked', admission: { kind: 'infrastructure', blocked: true, code: 'PLUGIN_AUDIT_IO_ERROR' } }
  store.write(state)
  assert.equal(store.read({ ...binding, binding_id: 'another-task' }), null)
  assert.equal(store.read({ ...binding, root_session_id: 'another-root' }), null)
})

test('real Python audit persists started infrastructure blocker across hub restart and only compatible change allows original-run review', { timeout: 15000 }, async t => {
  const { AuditJournal } = await import(new URL('audit.js', lib))
  const h = fixture({ checkRuntime: async () => health('old') })
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
  h.claim(user('Keep the original task and published child pending through infrastructure repair'))
  await publishAndFail(h)
  const before = await h.req.journal.read(h.rootId)
  const first = await h.req.status(h.agent)
  assert.equal(first.phase, 'blocked'); assert.equal(first.lifecycle_phase, 'started')
  assert.equal(before.events.filter(e => e.type === 'dpswarm/team-required-finished').length, 0)
  child.stdin.write('{"command":"restart"}\n'); assert.equal((await next()).restarted, true)
  const cold = resume(h, { journal: new AuditJournal({ config: () => h.cfg }) })
  const recovered = await cold.status(h.agent)
  assert.deepEqual(recovered.binding, first.binding); assert.equal(recovered.phase, 'blocked')
  assert.equal((await cold.journal.read(h.rootId)).head_hash, before.head_hash)
  h.agent.inbox.append('next-turn', createUserMessage({ source: { kind: 'plugin', plugin: 'fixture' }, content: [{ type: 'text', text: 'Continue pending task' }] }))
  await h.agent.turn()
  assert.equal(h.calls.length, 0); assert.equal(h.preparations.length, 0); assert.equal(h.steering.length, 0)
  cold.checkRuntime = async () => health('new')
  assert.equal((await cold.status(h.agent)).phase, 'started')
  await assert.rejects(cold.beforeDispatch(h.agent), { code: 'TEAM_REQUIRED_REVIEW_REQUIRED' })
  const after = await cold.journal.read(h.rootId)
  assert.equal(after.events.at(-1).type, 'dpswarm/team-required-admission')
  assert.equal(after.events.at(-1).data.blocked, false)
  assert.equal(after.events.filter(e => e.type === 'dpswarm/team-required-started').length, 1)
  assert.equal(after.events.filter(e => e.type === 'dpswarm/team-required-finished').length, 0)
  writeFileSync(join(h.directory, 'python-infrastructure-receipt.json'), JSON.stringify({ external_model_calls: 0,
    prepared_calls: h.preparations.length, provider_calls: h.calls.length, first, recovered, before, after }, null, 2))
})

test('authenticated fallback rejects rollback, altered equal revision and missing published starts before recovery', async t => {
  for (const [label, change] of [
    ['earlier valid binding-only ledger', (state, older) => Object.assign(state, older)],
    ['same revision changed hash', state => { state.head_hash = 'changed' }],
    ['newer ledger missing child', state => { state.revision++; state.head_hash = 'new'; state.events = state.events.filter(e => e.type !== 'dpswarm/team-required-started') }],
  ]) await t.test(label, async () => {
    const h = fixture({ checkRuntime: async () => health('old') }); h.claim(user('Original pending task'))
    const binding = await h.req.beforeRun(h.agent), older = structuredClone(h.journal.state(h.rootId))
    await h.req.markStarted(binding, start(h))
    const actual = structuredClone(h.journal.state(h.rootId)), transaction = h.journal.transaction.bind(h.journal)
    h.journal.transaction = async () => { throw err('PLUGIN_AUDIT_IO_ERROR') }
    await h.req.markInfrastructureBlocked(h.agent, err('PLUGIN_AUDIT_INVALID_EVENT'), { run_id: 'same-run' })
    h.journal.transaction = transaction
    change(h.journal.state(h.rootId), older)
    const cold = resume(h, { checkRuntime: async () => health('new') })
    const state = await cold.status(h.agent)
    assert.equal(state.phase, 'blocked'); assert.equal(state.native_child_bound_count, 1)
    assert.equal(state.admission.code, 'PLUGIN_AUDIT_CONTINUITY_LOST')
    await assert.rejects(cold.beforeDispatch(h.agent), { code: 'PLUGIN_AUDIT_CONTINUITY_LOST' })
    assert.equal((await cold.preExecute({ agent: h.agent, name: 'write' }, () => 'forbidden')).kind, 'deny')
    Object.assign(h.journal.state(h.rootId), actual)
    assert.equal((await cold.status(h.agent)).phase, 'started', 'restoring the actual retained ledger permits review only')
    await assert.rejects(cold.beforeDispatch(h.agent), { code: 'TEAM_REQUIRED_REVIEW_REQUIRED' })
  })
})

test('a failed dispatcher catch cannot shrink the authenticated published-child checkpoint after rollback', async () => {
  const h = fixture({ checkRuntime: async () => health('old') }); h.claim(user('Original pending task'))
  const binding = await h.req.beforeRun(h.agent), older = structuredClone(h.journal.state(h.rootId))
  await h.req.markStarted(binding, start(h))
  const transaction = h.journal.transaction.bind(h.journal)
  h.journal.transaction = async () => { throw err('PLUGIN_AUDIT_IO_ERROR') }
  await h.req.markInfrastructureBlocked(h.agent, err('PLUGIN_AUDIT_INVALID_EVENT'), { run_id: 'same-run' })
  h.journal.transaction = (rootId, build) => transaction(rootId, async snapshot => {
    const built = await build(snapshot)
    if (built.events.length) throw err('PLUGIN_AUDIT_IO_ERROR')
    return built
  })
  Object.assign(h.journal.state(h.rootId), older)
  const cold = resume(h, { checkRuntime: async () => health('new') })
  const dispatcher = new TeamDispatcher({ requirement: cold, controller: { run: async () => { throw Error('duplicate dispatch') } } })
  await assert.rejects(dispatcher.run({}, { agent: h.agent }), { code: 'PLUGIN_AUDIT_CONTINUITY_LOST' })
  const { InfrastructureBlockedStore } = await import(new URL('infrastructure-blocked.js', lib))
  const retained = new InfrastructureBlockedStore(join(h.workspace, 'runtime-blocks')).read(binding.binding)
  assert.equal(retained.starts.length, 1); assert.equal(retained.activeRunId, 'same-run')
  assert.equal(retained.journal_anchor.revision, 2)
})
