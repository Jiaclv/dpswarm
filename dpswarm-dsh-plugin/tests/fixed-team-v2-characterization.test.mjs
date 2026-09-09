/**
 * fixed-team-v2 P0 characterization: pin today's sequential single-implementer
 * semantics and the delegateOnce multi-subtask substrate before the parallel
 * phase lands. Tests marked "flips in P2" are expected to change then.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FixedTeamController } from '../lib/fixed-team.js'
import { delegateOnce } from '../lib/delegation.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

/** Minimal controller fixture: in-memory sidecar + deferred children. */
function fixture() {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-v2-')), cwd = join(workspace, 'project'); mkdirSync(cwd)
  const cfg = { workspace, sidecarUrl: 'http://127.0.0.1:8791', autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    workerTimeoutSeconds: 600, workerBudgetMode: 'unlimited' }
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [{ type: 'user/message', seq: 0, data: { id: 'user-task-1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create files.' }] } }],
    requestHeader() { return { config: { provider: 'fixture', model: 'lead' } } } } }
  const journal = new MemoryAuditJournal(), items = {}, children = []
  const sidecarFactory = cfg => ({ cfg, async ensure() {}, async call(method, path, body) {
    if (path === '/api/plugin-audit') {
      if (method === 'POST') return (await journal.transaction(cfg.sessionId, () => ({ events: body.events }))).journal
      return journal.read(cfg.sessionId)
    }
    if (path === '/api/status') return { snapshot: { work_items: items, seal_phase: {},
      open_worker_slots_used: Object.values(items).filter(item => !['accepted', 'terminated'].includes(item.acceptance)).length } }
    if (path === '/api/delegate') { const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null }
      return { items: [{ item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive' }] } }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') { items[body.item_id].acceptance = 'submitted'; items[body.item_id].submission_package_id = `package-${body.item_id}` }
    if (path === '/api/review') { items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated' }
    return { ok: true }
  } })
  const subagents = { async start(provider, request) {
    const id = `child-${children.length}`
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 },
      events: [{ seq: 0, type: 'turn/end', data: { reason: { kind: 'completed' } }, time: 123 }] }
    const child = { id, request, provider, session, localAgent: { session },
      result: Promise.resolve({ output: [{ type: 'text', text: `done:${id}` }], stopReason: 'completed' }),
      async dispose() {} }
    children.push(child)
    return child
  } }
  const controller = new FixedTeamController({ config: () => cfg, budget: null, subagents, sidecarFactory, resolveSession: () => null })
  const exec = { agent: parent, signal: new AbortController().signal }
  return { controller, exec, parent, items, children, journal, sidecarFactory }
}

test('P0 pin: without subtasks the team is one implementer then one tester, strictly in order (must stay green in v2)', async () => {
  const h = fixture()
  const result = await h.controller.run({ task: 'Create files.', acceptance: 'Files exist.' }, h.exec)
  assert.equal(result.deliveries.length, 2)
  assert.deepEqual(result.deliveries.map(d => d.role), ['implementer', 'tester'])
  assert.deepEqual(h.children.map(c => c.id), ['child-0', 'child-1'])
  assert.equal(Object.keys(h.items).length, 2)
})

test('P0 pin flipped in P2: subtasks are validated strictly before any dispatch', async () => {
  const h = fixture()
  const base = { task: 'Create files.', acceptance: 'Files exist.' }
  await assert.rejects(h.controller.run({ ...base, subtasks: [] }, h.exec), /PARALLEL_SUBTASKS_INVALID/)
  await assert.rejects(h.controller.run({ ...base, subtasks: [
    { id: 'a', task: 'A', write_scope: ['a/**'] }, { id: 'b', task: 'B', write_scope: ['b/**'] },
    { id: 'c', task: 'C', write_scope: ['c/**'] }, { id: 'd', task: 'D', write_scope: ['d/**'] }] }, h.exec), /PARALLEL_SUBTASKS_INVALID/)
  await assert.rejects(h.controller.run({ ...base, subtasks: [
    { id: 'a', task: 'A', write_scope: ['a/**'] }, { id: 'b', task: 'B', write_scope: ['a/b/**'] }] }, h.exec), /WORKER_SCOPE_OVERLAP/)
  await assert.rejects(h.controller.run({ ...base, subtasks: [{ id: 'a', task: 'A' }] }, h.exec), /PARALLEL_SUBTASKS_INVALID/)
  assert.equal(h.children.length, 0, 'invalid splits never dispatch a child')
})

test('P0 pin: delegateOnce already runs multiple subtasks concurrently with per-item identity', async () => {
  const h = fixture()
  const journal = new MemoryAuditJournal()
  const started = [], resolvers = []
  const subagents = { async start(provider, request) {
    const n = started.length
    const id = `parallel-${n}`
    started.push(request.prompt[0].text)
    const session = { id, header: { id, parentSession: h.parent.id, origin: 'subagent', delegationDepth: 1 },
      events: [{ seq: 0, type: 'turn/end', data: { reason: { kind: 'completed' } }, time: 123 }] }
    let resolve
    const result = new Promise(r => { resolve = r }); resolvers.push(resolve)
    return { id, request, provider, session, localAgent: { session }, result, async dispose() {} }
  } }
  const sidecar = { cfg: { subagentProvider: 'spawn' }, async ensure() {}, async call(method, path, body) {
    if (path === '/api/execution/root') return { ok: true }
    if (path === '/api/delegate') {
      return { items: body.subtasks.map((st, i) => ({ item_id: `wi-${i}`, node_id: `node-${i}`, session_id: `rs-${i}`,
        context_epoch: 0, attempt: 1, kind: body.kind, subtask_index: i })) }
    }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') return { ok: true }
    return { ok: true }
  } }
  const run = delegateOnce({ kind: 'derive', subtasks: [
    { provider: 'fixture', model: 'm', title: 'Part A', prompt: 'prompt-A' },
    { provider: 'fixture', model: 'm', title: 'Part B', prompt: 'prompt-B' }] },
    h.exec, sidecar, subagents, { routeJournal: journal, resolveSession: () => null, budget: null })
  await nextTurn(); await nextTurn()
  assert.deepEqual(started, ['prompt-A', 'prompt-B'], 'both workers start before either completes')
  resolvers.forEach((resolve, i) => resolve({ output: [{ type: 'text', text: `done-${i}` }], stopReason: 'completed' }))
  const result = await run
  assert.equal(result.deliveries.length, 2)
  assert.equal(result.failed.length, 0)
  assert.deepEqual(result.deliveries.map(d => d.output), ['done-0', 'done-1'])
  assert.deepEqual(result.deliveries.map(d => d.item_id), ['wi-0', 'wi-1'])
})

test('v3 staged validation: mutual exclusion, cycles, later-phase deps and overlapping globs refuse before dispatch', async () => {
  const h = fixture()
  const base = { task: 'Build.', acceptance: 'Done.' }
  const good = { phases: [{ id: 'p1', task: 'Phase one.' }], artifacts: [{ id: 'a', title: 'A', task: 'Do A.', write_globs: ['a/**'], phase: 'p1' }] }
  await assert.rejects(h.controller.run({ ...base, subtasks: [{ id: 'x', task: 'X', write_scope: ['x/**'] }], staged: good }, h.exec), /STAGED_INVALID/)
  await assert.rejects(h.controller.run({ ...base, staged: { phases: [{ id: 'p1', task: 'P' }], artifacts: [
      { id: 'a', title: 'A', task: 'A', write_globs: ['a/**'], phase: 'p1', deps: ['b'] },
      { id: 'b', title: 'B', task: 'B', write_globs: ['b/**'], phase: 'p1', deps: ['a'] }] } }, h.exec), /ARTIFACT_CYCLE/)
  await assert.rejects(h.controller.run({ ...base, staged: { phases: [{ id: 'p1', task: 'P' }, { id: 'p2', task: 'Q' }], artifacts: [
      { id: 'a', title: 'A', task: 'A', write_globs: ['a/**'], phase: 'p1', deps: ['b'] },
      { id: 'b', title: 'B', task: 'B', write_globs: ['b/**'], phase: 'p2' }] } }, h.exec), /later phase/)
  await assert.rejects(h.controller.run({ ...base, staged: { phases: [{ id: 'p1', task: 'P' }], artifacts: [
      { id: 'a', title: 'A', task: 'A', write_globs: ['a/**'], phase: 'p1' },
      { id: 'b', title: 'B', task: 'B', write_globs: ['a/b/**'], phase: 'p1' }] } }, h.exec), /WORKER_SCOPE_OVERLAP/)
  assert.equal(h.children.length, 0, 'invalid staged specs never dispatch a child')
})
