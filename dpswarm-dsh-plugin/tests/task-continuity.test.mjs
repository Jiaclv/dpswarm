import assert from 'node:assert/strict'
import test from 'node:test'
import { TeamRequirement } from '../lib/team-required.js'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { CONTINUED, resolveTaskContinuation } from '../lib/task-continuity.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const message = (id, text, kind = 'user') => ({ id, role: 'user', source: { kind }, content: [{ type: 'text', text }] })
const push = (h, id, text, kind) => h.agent.session.events.push({ type: 'user/message', data: message(id, text, kind) })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
async function fixture(phase = 'finished') {
  const cfg = { enabledSessions: ['root'] }, journal = new MemoryAuditJournal()
  const agent = { id: 'root', session: { id: 'root', header: { delegationDepth: 0 }, events: [] } }
  const h = { cfg, journal, agent }, requirement = new TeamRequirement({ config: () => cfg, journal, inspectAdmission: async () => null })
  h.requirement = requirement; push(h, 'human-1', 'Create one SVG file; keep the current requirements.')
  h.original = await requirement.beforeRun(agent)
  if (phase !== 'required') await requirement.markStarted(h.original, { run_id: 'run-1', execution_session_id: 'worker-1', role: 'implementer' })
  if (phase === 'finished' || phase === 'failed') await requirement.finishRun(h.original, { run_id: 'run-1', outcome: phase === 'failed' ? 'failed_takeover' : 'completed' })
  const state = { busy: false, fixedTask: { root_session_id: 'root', owner_session_id: 'root', run_id: 'run-1', task_binding: h.original.binding, acceptance_contract_id: 'contract-1' },
    acceptance: { id: 'contract-1', revision: 3, contract: { contract_id: 'contract-1', accepted: false, findings: { F1: 'unresolved' } } } }
  const controller = { session: () => state, async restoreAcceptance() {}, async run() { throw new Error('must not start workers') } }
  Object.assign(h, { state, controller, dispatcher: new TeamDispatcher({ controller, requirement }), exec: { agent, signal: new AbortController().signal } })
  return h
}
async function next(h, id = 'human-2', text = 'Please continue the same task.') {
  push(h, id, text); await h.requirement.beforeRun(h.agent)
  const c = await h.requirement.continuityContext(h.agent)
  return { current_binding_id: c.current_binding.binding_id, previous_binding_id: c.previous_binding.binding_id, reason: 'The user is continuing the original delivery, with no requirement change.' }
}
const gate = (h, name = 'write') => h.requirement.preExecute({ name, agent: h.agent }, async () => ({ kind: 'allow' }))

test('explicit continuation preserves contract, original source, grants and settled execution; wording alone does not continue', async () => {
  const h = await fixture(), args = await next(h), contract = structuredClone(h.state.acceptance)
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'required')
  const before = await h.journal.read('root'), result = await h.dispatcher.continueTask(args, h.exec), after = await h.journal.read('root')
  assert.equal(result.outcome, 'continued'); assert.equal(result.phase, 'finished')
  assert.deepEqual(result.binding, h.original.binding); assert.deepEqual(h.state.acceptance, contract)
  assert.deepEqual(after.events.slice(0, -1), before.events); assert.equal(after.events.at(-1).type, CONTINUED)
  assert.deepEqual(result.effects, { starts_workers: false, allocates_budget: false, changes_acceptance: false })
  assert.equal((await gate(h)).kind, 'allow'); assert.equal((await gate(h, 'dpswarm_run')).kind, 'deny')
  const current = await h.requirement.beforeRun(h.agent)
  assert.equal(h.requirement.sourceFor(h.agent, current).message_id, 'human-1')
  assert.equal((await h.requirement.status(h.agent)).continuity.decision_required, false)
})

test('active dispatcher run can record continuation before owner completes, without unlocking writes or a duplicate run', async () => {
  const h = await fixture('required'), started = deferred(), finish = deferred()
  h.controller.run = async (_args, _exec, hooks) => {
    h.state.busy = true; await hooks.onChildStarted({ run_id: 'run-1', execution_session_id: 'worker-live', role: 'implementer' })
    started.resolve(); await finish.promise; h.state.busy = false; return { deliveries: [], failed: [], stopped: false }
  }
  const run = h.dispatcher.run({}, h.exec); await started.promise
  h.controller.restoreAcceptance = async () => { throw new Error('busy state must not be restored or replaced') }
  const args = await next(h), result = await h.dispatcher.continueTask(args, h.exec)
  assert.equal(result.phase, 'started'); assert.equal(h.state.busy, true); assert.equal(h.dispatcher.running.has('root'), true)
  assert.equal((await gate(h)).kind, 'deny'); await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'RUN_PENDING' })
  finish.resolve(); await run
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'finished')
  const journal = await h.journal.read('root')
  assert.equal(journal.events.filter(e => e.type === 'dpswarm/team-required-started').length, 1)
  assert.equal(journal.events.findLast(e => e.type === 'dpswarm/team-required-finished').data.binding_id, h.original.binding.binding_id)
})

test('failed and infrastructure-blocked predecessors retain their exact phase; continuation is not recovery', async t => {
  for (const phase of ['failed', 'started']) await t.test(phase, async () => {
    const h = await fixture(phase)
    if (phase === 'started') await h.requirement.markInfrastructureBlocked(h.agent, Object.assign(new Error('runtime incompatible'), { code: 'HOST_RUNTIME_INCOMPATIBLE' }), { run_id: 'run-1' })
    const args = await next(h), result = await h.dispatcher.continueTask(args, h.exec)
    assert.equal(result.phase, phase === 'failed' ? 'finished' : 'blocked')
    if (phase === 'started') {
      assert.equal((await gate(h)).kind, 'deny')
      await h.requirement.markInfrastructureBlocked(h.agent, Object.assign(new Error('new runtime error'), { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' }), { run_id: 'run-1' })
      const current = await h.requirement.beforeRun(h.agent)
      assert.equal(current.phase, 'blocked'); assert.equal(current.admission.code, 'SIDECAR_RUNTIME_INCOMPATIBLE')
      assert.equal(current.binding.binding_id, h.original.binding.binding_id)
    } else assert.equal((await h.requirement.beforeRun(h.agent)).finish.outcome, 'failed_takeover')
  })
})

test('duplicate concurrent decisions commit once and cold replay plus another follow-up retains the original binding', async () => {
  const h = await fixture(), args = await next(h)
  const result = await Promise.all([h.dispatcher.continueTask(args, h.exec), h.dispatcher.continueTask(args, h.exec)])
  assert.deepEqual(result.map(r => r.outcome).sort(), ['already_continued', 'continued'])
  assert.equal((await h.journal.read('root')).events.filter(e => e.type === CONTINUED).length, 1)
  h.requirement = new TeamRequirement({ config: () => h.cfg, journal: h.journal, inspectAdmission: async () => null })
  h.dispatcher = new TeamDispatcher({ controller: h.controller, requirement: h.requirement })
  assert.equal((await h.requirement.beforeRun(h.agent)).binding.binding_id, h.original.binding.binding_id)
  const third = await next(h, 'human-3', 'Carry on with that same delivery.')
  assert.equal(third.previous_binding_id, args.current_binding_id)
  await h.dispatcher.continueTask(third, h.exec)
  assert.equal((await h.requirement.beforeRun(h.agent)).binding.binding_id, h.original.binding.binding_id)
  assert.equal((await h.journal.read('root')).events.filter(e => e.type === CONTINUED).length, 2)
})

test('unrelated or newly dispatched task is not silently inherited, and skipping an intervening user request is rejected', async () => {
  const h = await fixture(), second = await next(h, 'human-2', 'Create a different application.')
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'required')
  const third = await next(h, 'human-3', 'Continue.')
  await assert.rejects(h.dispatcher.continueTask({ ...third, previous_binding_id: second.previous_binding_id }, h.exec), { code: 'TASK_SOURCE_MISMATCH' })
  await assert.rejects(h.dispatcher.continueTask(third, h.exec), { code: 'TASK_CONTINUITY_SOURCE_MISMATCH' })
  const k = await fixture(), args = await next(k), current = await k.requirement.beforeRun(k.agent)
  await k.requirement.markStarted(current, { run_id: 'run-2', execution_session_id: 'other-worker', role: 'implementer' })
  await assert.rejects(k.dispatcher.continueTask(args, k.exec), { code: 'TASK_CONTINUITY_CONFLICT' })
  assert.equal((await k.journal.read('root')).events.some(e => e.type === CONTINUED), false)
})

test('plugin, embedded tool and unclaimed/canceled inbox messages never grant continuation authority', async () => {
  const h = await fixture(), old = h.original.binding.binding_id
  push(h, 'fake-plugin', 'continue task', 'plugin')
  h.agent.session.events.push({ type: 'tool/result', data: { message: message('fake-tool', 'continue task') } })
  h.agent.session.events.push({ type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, inserted: [message('queued', 'continue task')] } })
  assert.equal((await h.requirement.continuityContext(h.agent)).available, false)
  h.agent.session.events.push({ type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, removedCount: 1, inserted: [], outcome: 'canceled' } })
  await assert.rejects(h.dispatcher.continueTask({ current_binding_id: 'forged', previous_binding_id: old, reason: 'claimed by a tool' }, h.exec), { code: 'TASK_SOURCE_MISMATCH' })
  assert.equal((await h.requirement.beforeRun(h.agent)).binding.binding_id, old)
  const args = await next(h); h.agent.session.events.at(-1).data.content[0].text = 'Changed after context was read.'
  await assert.rejects(h.dispatcher.continueTask(args, h.exec), { code: 'TASK_SOURCE_MISMATCH' })
})

test('source changes during journal admission, foreign root identity and extra budget arguments are refused', async () => {
  const h = await fixture(), args = await next(h)
  await assert.rejects(h.dispatcher.continueTask({ ...args, worker_budgets: {} }, h.exec), { code: 'TASK_CONTINUITY_REQUEST_INVALID' })
  const transaction = h.journal.transaction.bind(h.journal); let changed = false
  h.journal.transaction = (root, build) => transaction(root, snapshot => {
    if (!changed && snapshot.events.some(e => e.type === 'dpswarm/team-required-bound' && e.data.binding_id === args.current_binding_id)) { changed = true; push(h, 'human-3', 'Actually start a new task.') }
    return build(snapshot)
  })
  await assert.rejects(h.dispatcher.continueTask(args, h.exec), { code: 'TASK_SOURCE_MISMATCH' })
  assert.equal((await h.journal.read('root')).events.some(e => e.type === CONTINUED), false)
  const k = await fixture(), a = await next(k); k.state.fixedTask.root_session_id = 'foreign'
  await assert.rejects(k.dispatcher.continueTask(a, k.exec), { code: 'TASK_CONTINUITY_SOURCE_MISMATCH' })
  await assert.rejects(k.dispatcher.continueTask(a, { ...k.exec, agent: { ...k.agent, id: 'child' } }), /PARENT_IDENTITY_REQUIRED/)
})

test('corrupt replay cannot combine continuation with a separate lifecycle or redirect to another task', async () => {
  const h = await fixture(), args = await next(h); await h.dispatcher.continueTask(args, h.exec)
  const snapshot = await h.journal.read('root'), current = (await h.requirement.beforeRun(h.agent)).message_binding
  const foreign = structuredClone(snapshot); foreign.events.find(e => e.type === CONTINUED).data.effective_binding_id = 'other'
  assert.throws(() => resolveTaskContinuation(foreign, current), { code: 'TASK_CONTINUITY_INVALID' })
  await h.journal.append('root', 'dpswarm/team-required-started', { root_session_id: 'root', binding_id: args.current_binding_id, run_id: 'second', execution_session_id: 'unexpected' })
  await assert.rejects(h.requirement.beforeRun(h.agent), { code: 'TASK_CONTINUITY_INVALID' })
})

test('verifyRework receives original task authorization and serializes dispatch; read evidence keeps its normal host gate', async () => {
  const h = await fixture(), args = await next(h); await h.dispatcher.continueTask(args, h.exec)
  const entered = deferred(), release = deferred()
  h.controller.verifyRework = async (_args, _exec, authorization) => {
    assert.equal(authorization.taskBinding.binding.binding_id, h.original.binding.binding_id)
    assert.equal((await authorization.validateTask()).binding.binding_id, h.original.binding.binding_id)
    entered.resolve(); await release.promise; return { ok: true }
  }
  const running = h.dispatcher.verifyRework({ item_id: 'deferred', reason: 'Read current evidence.' }, h.exec); await entered.promise
  await assert.rejects(h.dispatcher.verifyRework({}, h.exec), { code: 'RUN_PENDING' })
  release.resolve(); assert.deepEqual(await running, { ok: true })
  await next(h, 'human-3', 'A different task.')
  await assert.rejects(h.dispatcher.verifyRework({}, h.exec), { code: 'REWORK_TASK_NOT_SETTLED' })
  let forwarded = false
  const result = await h.requirement.preExecute({ name: 'dpswarm_read_evidence', agent: h.agent }, async () => { forwarded = true; return { kind: 'deny', reason: 'host filesystem permissions' } })
  assert.equal(forwarded, true); assert.equal(result.kind, 'deny')
})
