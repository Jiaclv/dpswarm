import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { createUserMessage } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-llm/lib/index.js'))
import { TeamRequirement, installTeamRequirement, TEAM_REQUIRED_CODE } from '../lib/team-required.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const signal = () => new AbortController().signal
const user = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
const session = (id = 'root', events = []) => ({ id, header: { delegationDepth: 0 }, events })
const appendUser = (s, text) => s.events.push({ type: 'user/message', data: user(text) })
const agent = s => ({ id: s.id, session: s, steered: [], steer(message) { this.steered.push(message) } })
const fixture = () => {
  const cfg = { enabledSessions: ['root'] }, journal = new MemoryAuditJournal(), root = session()
  appendUser(root, 'Create one SVG file.')
  return { cfg, journal, root, req: new TeamRequirement({ config: () => cfg, journal }) }
}

test('enabled root binds the durable newest user task and rejects writes until native child binding', async () => {
  const h = fixture(), lead = agent(h.root), bound = await h.req.beforeRun(lead)
  assert.equal(bound.phase, 'required')
  assert.match(bound.binding.user_content_sha256, /^[0-9a-f]{64}$/)
  let called = 0
  const denied = await h.req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(called, 0); assert.equal(denied.kind, 'deny'); assert.match(denied.reason, /TEAM_REQUIRED/)
  await h.req.preExecute({ name: 'read', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(called, 1)
  const started = await h.req.markStarted(bound.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  assert.equal(started.phase, 'started'); assert.equal(started.starts[0].data.execution_kind, 'native_child_bound')
  const afterStart = await h.req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(afterStart.kind, 'deny')
  await h.req.finishRun(bound.binding, { outcome: 'failed_takeover', run_id: 'run-1' })
  await h.req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(called, 2)
  const repeat = await h.req.preExecute({ name: 'dpswarm_run', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(repeat.kind, 'deny'); assert.match(repeat.reason, /ALREADY_FULFILLED/)
})

test('a new real user message has a new requirement even after the previous team settled', async () => {
  const h = fixture(), lead = agent(h.root), first = await h.req.beforeRun(lead)
  await h.req.markStarted(first.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  await h.req.finishRun(first.binding, { outcome: 'completed', run_id: 'run-1' })
  appendUser(h.root, 'Now change only the colors.')
  const second = await h.req.beforeRun(lead)
  assert.equal(second.phase, 'required'); assert.notEqual(second.binding.binding_id, first.binding.binding_id)
})

test('status next text carries the user team mode guidance so the Lead sees the picker choice (0.9.3)', async () => {
  const h = fixture(), lead = agent(h.root)
  assert.match((await h.req.status(lead)).next, /left this task serial/)
  h.cfg.teamModeOverrides = [{ sessionId: 'root', mode: 'parallel' }]
  assert.match((await h.req.status(lead)).next, /set this task to parallel/)
  h.cfg.teamModeOverrides = [{ sessionId: 'root', mode: 'staged' }]
  assert.match((await h.req.status(lead)).next, /set this task to staged/)
})

test('raw session user events survive compaction-shaped history and child agents are outside the root gate', async () => {
  const h = fixture(), lead = agent(h.root)
  h.root.deriveMessages = () => [{ role: 'user', source: { kind: 'plugin' }, content: [] }]
  assert.equal((await h.req.beforeRun(lead)).phase, 'required')
  const childSession = { id: 'child', header: { origin: 'subagent', delegationDepth: 1 }, events: [] }
  const child = agent(childSession); let called = 0
  await h.req.preExecute({ name: 'write', agent: child, signal: signal() }, async () => { called++ })
  assert.equal(called, 1)
})

test('a fork does not inherit its parent task from seed history', async () => {
  const h = fixture(), inherited = h.root.events.map(event => structuredClone(event))
  const forkSession = { id: 'fork', header: { delegationDepth: 0, seedLength: inherited.length }, events: inherited }
  h.cfg.enabledSessions = ['fork']
  const fork = agent(forkSession), req = new TeamRequirement({ config: () => h.cfg, journal: h.journal })
  await assert.rejects(req.beforeRun(fork), { code: 'TEAM_REQUIRED_TASK_MISSING' })
  appendUser(forkSession, 'Create a different SVG.')
  const state = await req.beforeRun(fork)
  assert.equal(state.phase, 'required'); assert.notEqual(state.binding.root_session_id, h.root.id)
})
test('Code Mode outer transport is allowed but nested ordinary SDK calls are denied before a team starts', async () => {
  const h = fixture(), lead = agent(h.root); const outer = Symbol('run-code'); let called = 0
  await h.req.preExecute({ name: 'run_code', agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(called, 1)
  const nested = await h.req.preExecute({ name: 'write', parent: outer, agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(nested.kind, 'deny'); assert.equal(called, 1)
  await h.req.preExecute({ name: 'dpswarm_run', parent: outer, agent: lead, signal: signal() }, async () => { called++ })
  assert.equal(called, 2)
})

test('turn stopping steers once, then fails closed; abort never steers', async () => {
  const h = fixture(), lead = agent(h.root)
  await h.req.turnStopping({ agent: lead, turn: 1, signal: signal() })
  assert.equal(lead.steered.length, 1); assert.equal(lead.steered[0].source.kind, 'plugin')
  await assert.rejects(h.req.turnStopping({ agent: lead, turn: 1, signal: signal() }), { code: TEAM_REQUIRED_CODE })
  const aborted = new AbortController(); aborted.abort()
  await h.req.turnStopping({ agent: lead, turn: 2, signal: aborted.signal })
  assert.equal(lead.steered.length, 1)
  const settled = await h.req.beforeRun(lead)
  await h.req.markStarted(settled.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  await h.req.finishRun(settled.binding, { outcome: 'completed', run_id: 'run-1' })
  await h.req.turnStopping({ agent: lead, turn: 3, signal: signal() })
  assert.equal(lead.steered.length, 1, 'a settled requirement never steers again')
})

test('cold started run blocks duplicate dispatch until review/finish and zero-child finish cannot satisfy requirement', async () => {
  const h = fixture(), lead = agent(h.root), first = await h.req.beforeRun(lead)
  const blocked = await h.req.finishRun(first.binding, { outcome: 'completed' })
  assert.equal(blocked.blocked, true); assert.equal(blocked.phase, 'required')
  await h.req.markStarted(first.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  const resumed = new TeamRequirement({ config: () => h.cfg, journal: h.journal })
  const state = await resumed.beforeRun(lead)
  assert.equal(state.phase, 'started')
  const denied = await resumed.preExecute({ name: 'dpswarm_run', agent: lead, signal: signal() }, async () => { throw new Error('must not call') })
  assert.match(denied.reason, /REVIEW_REQUIRED/)
  await assert.rejects(resumed.finishRecovered(lead, { control_plane_confirmed: true, open_worker_slots_used: 0, all_workers_terminal: true, physical_cleanup_confirmed: true, run_id: 'wrong-run', execution_session_ids: ['child-1'] }), { code: 'TEAM_REQUIRED_RECOVERY_EVIDENCE_REQUIRED' })
  await assert.rejects(resumed.finishRecovered(lead, { control_plane_confirmed: true, open_worker_slots_used: 0, all_workers_terminal: true, physical_cleanup_confirmed: false, run_id: 'run-1', execution_session_ids: ['child-1'] }), { code: 'TEAM_REQUIRED_RECOVERY_EVIDENCE_REQUIRED' })
  await assert.rejects(resumed.finishRecovered(lead, { control_plane_confirmed: true, open_worker_slots_used: 0, all_workers_terminal: true, physical_cleanup_confirmed: true, run_id: 'run-1', execution_session_ids: ['other-child'] }), { code: 'TEAM_REQUIRED_RECOVERY_EVIDENCE_REQUIRED' })
  await resumed.finishRecovered(lead, { control_plane_confirmed: true, open_worker_slots_used: 0, all_workers_terminal: true, physical_cleanup_confirmed: true, run_id: 'run-1', execution_session_ids: ['child-1'] })
  assert.equal((await resumed.status(lead)).phase, 'finished')
})

test('a settled binding rejects another fixed-team dispatch through both gate surfaces', async () => {
  const h = fixture(), lead = agent(h.root), bound = await h.req.beforeRun(lead)
  await h.req.markStarted(bound.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  await h.req.finishRun(bound.binding, { outcome: 'completed', run_id: 'run-1' })
  const pre = await h.req.preExecute({ name: 'dpswarm_run', agent: lead, signal: signal() }, async () => { throw new Error('must not dispatch') })
  assert.equal(pre.kind, 'deny'); assert.match(pre.reason, /ALREADY_FULFILLED/)
  await assert.rejects(h.req.beforeDispatch(lead), { code: 'TEAM_REQUIRED_ALREADY_FULFILLED' })
})

test('manual, auto, and unlimited policies do not alter the team-required gate', async t => {
  for (const workerBudgetMode of ['manual', 'auto', 'unlimited']) await t.test(workerBudgetMode, async () => {
    const h = fixture(), lead = agent(h.root); h.cfg.workerBudgetMode = workerBudgetMode
    const state = await h.req.beforeRun(lead)
    assert.equal(state.phase, 'required')
    const denied = await h.req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { throw new Error('must not call') })
    assert.equal(denied.kind, 'deny'); assert.match(denied.reason, /TEAM_REQUIRED/)
  })
})

test('disabled sessions neither read nor write the audit journal', async () => {
  let reads = 0, writes = 0
  const journal = { async read() { reads++; throw new Error('must not read') }, async transaction() { writes++; throw new Error('must not write') } }
  const cfg = { enabledSessions: [] }, root = session(); appendUser(root, 'No team for this task.')
  const req = new TeamRequirement({ config: () => cfg, journal }), lead = agent(root); let next = 0
  assert.deepEqual(await req.beforeRun(lead), { required: false, phase: 'off', binding: null })
  await req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { next++ })
  assert.equal(next, 1); assert.equal(reads, 0); assert.equal(writes, 0)
})

test('later CM/plugin user messages do not replace the durable direct-user binding', async () => {
  const h = fixture(), lead = agent(h.root), first = await h.req.beforeRun(lead)
  h.root.events.push({ type: 'user/message', data: createUserMessage({
    source: { kind: 'plugin', plugin: 'dpswarm', form: 'notice', summary: 'CM retained a summary.' },
    content: [{ type: 'text', text: 'compressed context' }],
  }) })
  const again = await h.req.beforeRun(lead)
  assert.equal(again.binding.binding_id, first.binding.binding_id)
})

test('audit failures fail closed rather than permitting a root write', async t => {
  for (const [label, journal] of [
    ['read', { async read() { throw Object.assign(new Error('sidecar down'), { code: 'DOWN' }) }, async transaction() { throw new Error('unused') } }],
    ['write', { async read() { return { root_session_id: 'root', revision: 0, events: [], head_hash: 'x', missing: true } }, async transaction() { throw Object.assign(new Error('sidecar write down'), { code: 'DOWN' }) } }],
  ]) await t.test(label, async () => {
    const cfg = { enabledSessions: ['root'] }, root = session(); appendUser(root, 'Use a team.')
    const req = new TeamRequirement({ config: () => cfg, journal }), lead = agent(root); let next = 0
    await assert.rejects(req.preExecute({ name: 'write', agent: lead, signal: signal() }, async () => { next++ }))
    assert.equal(next, 0)
  })
})

test('a new direct user task remains required when an older started run later finishes', async () => {
  const h = fixture(), lead = agent(h.root), old = await h.req.beforeRun(lead)
  await h.req.markStarted(old.binding, { run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
  appendUser(h.root, 'A separate follow-up task.')
  const current = await h.req.beforeRun(lead)
  assert.notEqual(current.binding.binding_id, old.binding.binding_id); assert.equal(current.phase, 'required')
  await h.req.finishRun(old.binding, { outcome: 'failed_takeover', run_id: 'run-1' })
  const afterOldFinish = await h.req.beforeRun(lead)
  assert.equal(afterOldFinish.binding.binding_id, current.binding.binding_id)
  assert.equal(afterOldFinish.phase, 'required')
})
test('installer registers both public gates', () => {
  const h = fixture(), calls = [], provided = []
  const ctx = { on(name, handler, options) { calls.push([name, handler, options]) }, provide(name, value) { provided.push([name, value]) } }
  const installed = installTeamRequirement(ctx, { requirement: h.req })
  assert.equal(installed, h.req); assert.deepEqual(calls.map(([name]) => name), ['tools/pre-execute', 'agent/turn-stopping'])
  assert.equal(provided[0][0], 'dpswarmTeamRequirement')
  assert.deepEqual(calls.map(([, , options]) => options), [{ prepend: true, global: true }, { prepend: true, global: true }])
})
