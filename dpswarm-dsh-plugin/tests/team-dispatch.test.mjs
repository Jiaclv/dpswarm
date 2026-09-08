import assert from 'node:assert/strict'
import test from 'node:test'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { createUserMessage } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-llm/lib/index.js'))

const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const error = code => Object.assign(new Error(code), { code })
const child = (id = 'child-1', role = 'implementer') => ({ run_id: 'run-1', execution_session_id: id, role })
const success = () => ({ mode: 'fixed-team-v1', deliveries: [{ role: 'implementer' }], failed: [], stopped: false })
function fixture(run) {
  const cfg = { enabledSessions: ['root'] }, journal = new MemoryAuditJournal()
  const session = { id: 'root', header: { delegationDepth: 0 }, events: [{ type: 'user/message', data: createUserMessage({
    source: { kind: 'user' }, content: [{ type: 'text', text: 'Create one SVG.' }],
  }) }] }
  const agent = { id: 'root', session }, exec = { agent, signal: new AbortController().signal }
  const requirement = new TeamRequirement({ config: () => cfg, journal })
  const controller = { run }, dispatcher = new TeamDispatcher({ controller, requirement })
  return { cfg, journal, agent, exec, requirement, controller, dispatcher }
}
const writeGate = h => h.requirement.preExecute({ name: 'write', agent: h.agent }, async () => ({ kind: 'allow' }))

// These assertions use the real durable gate so a losing call cannot silently
// authorize Lead writes on behalf of a different operation.
test('concurrent loser cannot finish the owner or release its root lock', async () => {
  const started = deferred(), settle = deferred(), result = success()
  let calls = 0
  const h = fixture(async (args, exec, { onChildStarted }) => {
    calls++; await onChildStarted(child()); started.resolve(); await settle.promise; return result
  })
  const first = h.dispatcher.run({}, h.exec)
  await started.promise
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'RUN_PENDING' })
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'RUN_PENDING' })
  assert.equal(calls, 1); assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'started')
  assert.equal((await writeGate(h)).kind, 'deny')
  settle.resolve(); assert.equal(await first, result)
  assert.equal((await writeGate(h)).kind, 'allow')
})

test('lock covers the asynchronous durable pre-dispatch read', async () => {
  const entered = deferred(), release = deferred(), h = fixture(async () => success())
  const original = h.requirement.beforeDispatch.bind(h.requirement)
  h.requirement.beforeDispatch = async agent => { entered.resolve(); await release.promise; return original(agent) }
  const first = h.dispatcher.run({}, h.exec); await entered.promise
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'RUN_PENDING' })
  release.resolve(); await first
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'required')
})

test('zero-child preflight and budget errors retain identity and allow a later retry', async t => {
  for (const code of ['FIXED_ROUTE_REQUIRED', 'WORKER_BUDGET_SETTINGS_CHANGED', 'WORKER_BUDGET_REQUIRED']) {
    await t.test(code, async () => {
      const expected = error(code), h = fixture(async () => { throw expected })
      await assert.rejects(h.dispatcher.run({}, h.exec), caught => caught === expected)
      assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'required')
      assert.equal((await writeGate(h)).kind, 'deny')
      const result = success()
      h.controller.run = async (args, exec, { onChildStarted }) => { await onChildStarted(child()); return result }
      assert.equal(await h.dispatcher.run({}, h.exec), result)
    })
  }
})

test('disabled collaboration preserves the controller DPSWARM_DISABLED error', async () => {
  const expected = error('DPSWARM_DISABLED'), h = fixture(async () => { throw expected })
  h.cfg.enabledSessions = []
  await assert.rejects(h.dispatcher.run({}, h.exec), caught => caught === expected)
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'off')
  assert.equal(h.journal.roots.size, 0)
})

test('a settled user task rejects another run before calling the controller', async () => {
  let calls = 0
  const h = fixture(async (args, exec, { onChildStarted }) => { calls++; await onChildStarted(child()); return success() })
  await h.dispatcher.run({}, h.exec)
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'TEAM_REQUIRED_ALREADY_FULFILLED' })
  assert.equal(calls, 1)
})

test('a started task rejects a second dispatch after an uncertain controller error', async () => {
  const expected = error('CHILD_CLEANUP_FAILED'); let calls = 0
  const h = fixture(async (args, exec, { onChildStarted }) => { calls++; await onChildStarted(child()); throw expected })
  await assert.rejects(h.dispatcher.run({}, h.exec), caught => caught === expected)
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'TEAM_REQUIRED_REVIEW_REQUIRED' })
  assert.equal(calls, 1); assert.equal((await writeGate(h)).kind, 'deny')
})

test('unknown or failed physical/control-plane cleanup cannot authorize takeover', async t => {
  for (const [name, failed] of [
    ['physical worker remains', [{ control_settlement: { ok: true }, details: { physicalCleanupConfirmed: false } }]],
    ['control-plane failed', [{ control_settlement: { ok: false }, details: { physicalCleanupConfirmed: true } }]],
    ['no control-plane evidence', [{ code: 'MODEL_PREFLIGHT_FAILED' }]],
    ['malformed result', undefined],
  ]) await t.test(name, async () => {
    const result = { ...success(), failed }, h = fixture(async (args, exec, { onChildStarted }) => { await onChildStarted(child()); return result })
    assert.equal(await h.dispatcher.run({}, h.exec), result)
    assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'started')
    assert.equal((await writeGate(h)).kind, 'deny')
  })
})

test('only published, normally returned and settled results finish the gate', async t => {
  for (const [name, result, outcome] of [
    ['normal completion', success(), 'completed'],
    ['confirmed failed child', { ...success(), failed: [{ control_settlement: { ok: true }, details: { physicalCleanupConfirmed: true } }] }, 'failed_takeover'],
    ['stopped after cleanup', { ...success(), stopped: true }, 'failed_takeover'],
  ]) await t.test(name, async () => {
    const h = fixture(async (args, exec, { onChildStarted }) => { await onChildStarted(child()); return result })
    assert.equal(await h.dispatcher.run({}, h.exec), result)
    const state = await h.requirement.beforeRun(h.agent)
    assert.equal(state.phase, 'finished'); assert.equal(state.finish.outcome, outcome); assert.equal(state.finish.run_id, 'run-1')
    assert.equal((await writeGate(h)).kind, 'allow')
  })
})

test('a zero-child returned result never unlocks the required task', async () => {
  const result = success(), h = fixture(async () => result)
  assert.equal(await h.dispatcher.run({}, h.exec), result)
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'required')
  assert.equal((await writeGate(h)).kind, 'deny')
})

async function recoveryFixture() {
  const h = fixture(async () => success())
  const binding = await h.requirement.beforeRun(h.agent)
  await h.requirement.markStarted(binding, child())
  await h.requirement.markStarted(binding, child('child-2', 'tester'))
  h.state = { busy: false, cfg: { subagentProvider: 'spawn' }, lease: { recovered: true, run_id: 'run-1', pid: 12345, session_id: 'root' } }
  h.snapshot = {
    open_worker_slots_used: 0,
    nodes: {
      'node-1': { item: 'item-1', execution_session_id: 'child-1', execution_parent_session_id: 'root', execution_provider: 'spawn', lifecycle: 'drained' },
      'node-2': { item: 'item-2', execution_session_id: 'child-2', execution_parent_session_id: 'root', execution_provider: 'spawn', lifecycle: 'drained' },
    },
    work_items: { 'item-1': { acceptance: 'accepted' }, 'item-2': { acceptance: 'terminated' } },
  }
  h.reviewResult = { ok: true, outcome: 'already_terminated' }
  h.controller.session = () => h.state
  h.controller.review = async () => { h.state.lease = null; return h.reviewResult }
  h.controller.status = async () => ({ snapshot: h.snapshot })
  return h
}

test('a recovered dead-owner spawn run settles only after exact terminal child evidence', async () => {
  const h = await recoveryFixture()
  assert.equal(await h.dispatcher.review({ item_id: 'item-2', verdict: 'terminate' }, h.exec), h.reviewResult)
  const state = await h.requirement.beforeRun(h.agent)
  assert.equal(state.phase, 'finished'); assert.equal(state.finish.error_code, 'RECOVERED_TERMINAL_REVIEW')
  assert.equal((await writeGate(h)).kind, 'allow')
})

test('ordinary/live reviews and incomplete recovered evidence keep the gate closed', async t => {
  const mutations = [
    ['live lease', h => { h.state.lease.recovered = false }],
    ['non-spawn provider', h => { h.state.cfg.subagentProvider = 'sdk' }],
    ['missing former PID', h => { delete h.state.lease.pid }],
    ['foreign lease', h => { h.state.lease.session_id = 'foreign' }],
    ['wrong run', h => { h.state.lease.run_id = 'another-run' }],
    ['lease not released', h => { h.controller.review = async () => h.reviewResult }],
    ['still busy', h => { h.state.busy = true }],
    ['open slot', h => { h.snapshot.open_worker_slots_used = 1 }],
    ['missing slot evidence', h => { delete h.snapshot.open_worker_slots_used }],
    ['missing child node', h => { delete h.snapshot.nodes['node-2'] }],
    ['duplicate native binding', h => { h.snapshot.nodes.duplicate = { ...h.snapshot.nodes['node-2'] } }],
    ['foreign child parent', h => { h.snapshot.nodes['node-2'].execution_parent_session_id = 'foreign' }],
    ['non-spawn child', h => { h.snapshot.nodes['node-2'].execution_provider = 'sdk' }],
    ['undrained child', h => { h.snapshot.nodes['node-2'].lifecycle = 'active' }],
    ['no terminal item', h => { h.snapshot.work_items['item-2'].acceptance = 'submitted' }],
    ['missing corresponding item', h => { delete h.snapshot.work_items['item-2'] }],
  ]
  for (const [name, mutate] of mutations) await t.test(name, async () => {
    const h = await recoveryFixture(); mutate(h)
    assert.equal(await h.dispatcher.review({ item_id: 'item-2', verdict: 'terminate' }, h.exec), h.reviewResult)
    assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'started')
    assert.equal((await writeGate(h)).kind, 'deny')
  })
})

test('review controller errors remain unchanged and never finish the requirement', async () => {
  const h = await recoveryFixture(), expected = error('RUN_ACTIVE')
  h.controller.review = async () => { throw expected }
  await assert.rejects(h.dispatcher.review({}, h.exec), caught => caught === expected)
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'started')
})

test('a finished gate does not require another recovery status query', async () => {
  const h = await recoveryFixture(), binding = await h.requirement.beforeRun(h.agent)
  await h.requirement.finishRun(binding, { outcome: 'completed', run_id: 'run-1' })
  h.controller.status = async () => { throw new Error('must not recheck a finished task') }
  assert.equal(await h.dispatcher.review({}, h.exec), h.reviewResult)
})


test('a later role model preflight failure permits takeover after this run published a child', async () => {
  const result = { ...success(), failed: [{ role: 'tester', code: 'MODEL_NOT_FOUND', error: 'Provider model was removed', admission_stage: 'model_preflight' }] }
  const h = fixture(async (args, exec, { onChildStarted }) => { await onChildStarted(child()); return result })
  assert.equal(await h.dispatcher.run({}, h.exec), result)
  const state = await h.requirement.beforeRun(h.agent)
  assert.equal(state.phase, 'finished'); assert.equal(state.finish.outcome, 'failed_takeover')
  assert.equal((await writeGate(h)).kind, 'allow')
})

test('model preflight failures before any child publication never fulfill the task requirement', async () => {
  const result = { ...success(), deliveries: [], failed: [
    { role: 'implementer', code: 'MODEL_NOT_FOUND', error: 'Provider model was removed', admission_stage: 'model_preflight' },
    { role: 'tester', code: 'MODEL_NOT_FOUND', error: 'Provider model was removed', admission_stage: 'model_preflight' },
  ] }
  const h = fixture(async () => result)
  assert.equal(await h.dispatcher.run({}, h.exec), result)
  const state = await h.requirement.beforeRun(h.agent)
  assert.equal(state.phase, 'required'); assert.equal(state.starts.length, 0); assert.equal(state.finish, null)
  assert.equal((await writeGate(h)).kind, 'deny')
})
