import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdirSync, mkdtempSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { Sidecar } from '../lib/sidecar.js'
import { delegateOnce } from '../lib/delegation.js'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const parent = { id: 'admission-root', session: { id: 'admission-root', header: {},
  requestHeader: () => ({ config: { provider: 'mock', model: 'b-kimi' } }) } }
const exec = () => ({ agent: parent, signal: new AbortController().signal })
const task = () => ({ provider: 'mock', model: 'b-kimi', title: 'offline fixture', prompt: 'never invoke a model' })
const request = (count = 1) => ({ kind: 'derive', subtasks: Array.from({ length: count }, task) })

function rejected() {
  return { ok: false, error: 'POINTS_EXCEEDED', message: '9 > 8', partial: [], pending: [],
    created: [{ item_id: 'new-item', subtask_index: 0 }],
    started: [{ item_id: 'new-item', node_id: 'new-node', subtask_index: 0, kind: 'derive',
      level: 'B', lifecycle: 'active', attempt: 1, context_epoch: 0, session_id: 'reservation', execution_binding: null }],
    cleanup: [{ item_id: 'new-item', node_ids: ['new-node'], status: 'retained' }], cleanup_complete: false }
}
function snapshot() {
  return { work_items: { 'new-item': { kind: 'derive', acceptance: null, attempt: 1 } },
    nodes: { 'new-node': { item: 'new-item', epoch: 0, lifecycle: 'active', terminated: false,
      execution_session_id: null, execution_provider: null } } }
}
function fake(admission = rejected(), current = snapshot(), options = {}) {
  const error = Object.assign(new Error('POINTS_EXCEEDED: 9 > 8'), { code: 'POINTS_EXCEEDED', details: admission })
  const calls = [], h = { error, starts: 0, calls }
  const sidecar = { cfg: { subagentProvider: 'spawn' }, async ensure() {}, async call(method, path, body) {
    calls.push({ method, path, body })
    if (path === '/api/delegate') throw error
    if (path === '/api/status') {
      if (options.statusError) throw new Error('transport unavailable')
      return { snapshot: current }
    }
    if (path === '/api/execution/fail') {
      if (options.settlementError) throw Object.assign(new Error('settlement failed'), { code: options.settlementError })
      return options.settlementResponse || { ok: true, outcome: 'terminated' }
    }
    return { ok: true }
  } }
  h.execute = () => delegateOnce(request(), exec(), sidecar, { async start() { h.starts++; throw new Error('native start forbidden') } })
  h.settlements = () => calls.filter(row => row.path === '/api/execution/fail')
  return h
}

test('admission settlement covers a reserved node omitted from partial without invoking native start', async () => {
  const h = fake()
  await assert.rejects(h.execute(), error => {
    assert.equal(error, h.error)
    assert.equal(error.details.admission_settlement.complete, true)
    assert.equal(error.controlSettlement.ok, true)
    assert.deepEqual(error.details.partial, [])
    assert.equal(error.details.cleanup_complete, false) // Original CP receipt stays unchanged.
    return true
  })
  assert.equal(h.starts, 0)
  assert.equal(h.settlements().length, 1)
  assert.equal(h.settlements()[0].body.admission_cleanup, true)
  assert.equal(h.settlements()[0].body.published, false)
  assert.equal(h.settlements()[0].body.physical_cleanup_confirmed, true)
  assert.equal(h.settlements()[0].body.reservation_session_id, 'reservation')
})

test('provisioning reservation with null session uses the exact fence', async () => {
  const admission = rejected(), current = snapshot()
  Object.assign(admission.started[0], { lifecycle: 'provisioning', session_id: null })
  current.nodes['new-node'].lifecycle = 'provisioning'
  const h = fake(admission, current)
  await assert.rejects(h.execute(), error => error.details.admission_settlement.complete)
  assert.equal(h.settlements()[0].body.reservation_session_id, null)
  assert.equal(h.starts, 0)
})

for (const reason of ['bound-receipt', 'missing-binding', 'bound-current', 'existing-root', 'foreign-created',
  'extra-current-node', 'multiple-receipt-nodes', 'stale-attempt', 'missing-created', 'malformed-current', 'inconsistent-complete', 'legacy-response']) {
  test('unsafe admission cleanup is retained: ' + reason, async () => {
    const admission = rejected(), current = snapshot()
    if (reason === 'bound-receipt') admission.started[0].execution_binding = { execution_session_id: 'published' }
    if (reason === 'missing-binding') delete admission.started[0].execution_binding
    if (reason === 'bound-current') current.nodes['new-node'].execution_session_id = 'published'
    if (reason === 'existing-root') current.work_items['new-item'].kind = 'root'
    if (reason === 'foreign-created') admission.created = []
    if (reason === 'extra-current-node') current.nodes.other = { ...current.nodes['new-node'] }
    if (reason === 'multiple-receipt-nodes') { admission.started.push({ ...admission.started[0], node_id: 'sibling' }); admission.cleanup[0].node_ids.push('sibling'); current.nodes.sibling = { ...current.nodes['new-node'] } }
    if (reason === 'stale-attempt') current.work_items['new-item'].attempt++
    if (reason === 'missing-created') delete admission.created
    if (reason === 'malformed-current') current.nodes = { 'new-node': null }
    if (reason === 'inconsistent-complete') admission.cleanup_complete = true
    if (reason === 'legacy-response') { delete admission.started; delete admission.cleanup }
    const h = fake(admission, current)
    await assert.rejects(h.execute(), error => error.code === 'POINTS_EXCEEDED' && error.details.admission_settlement.complete === false)
    assert.equal(h.settlements().length, 0)
    assert.equal(h.starts, 0)
  })
}

for (const options of [{ statusError: true }, { settlementError: 'TRANSPORT_FAILED' },
  { settlementError: 'FENCE_VIOLATION' }, { settlementResponse: { ok: false } }]) {
  test('cleanup failures remain explicit: ' + JSON.stringify(options), async () => {
    const h = fake(rejected(), snapshot(), options)
    await assert.rejects(h.execute(), error => error.code === 'POINTS_EXCEEDED'
      && error.details.admission_settlement.complete === false && error.controlSettlement.ok === false)
    assert.equal(h.starts, 0)
  })
}

async function realSidecar(t) {
  const base = process.env.DPSWARM_ADMISSION_TEST_OUTPUT || join(repo, '.tmp/team-lifecycle-fix-20260911/js-admission-sidecars')
  mkdirSync(base, { recursive: true })
  const directory = mkdtempSync(join(base, 'run-'))
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python',
    [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), directory],
    { cwd: repo, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''; child.stderr.on('data', value => { stderr += value })
  const lines = createInterface({ input: child.stdout }), queue = [], waiters = []
  lines.on('line', line => { const value = JSON.parse(line); if (waiters.length) waiters.shift()(value); else queue.push(value) })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve))
  t.after(async () => {
    if (child.exitCode === null) { const done = once(child, 'exit'); child.stdin.end('{"command":"stop"}\n'); await done }
    lines.close()
  })
  const { port } = await Promise.race([next(), once(child, 'exit').then(() => { throw new Error(stderr) })])
  const sidecar = new Sidecar({ sidecarUrl: 'http://127.0.0.1:' + port, workspace: directory,
    sessionId: parent.id, sessionIsolation: true, autoStart: false, subagentProvider: 'spawn' })
  const restart = async () => { child.stdin.write('{"command":"restart"}\n'); assert.equal((await next()).restarted, true) }
  await sidecar.call('POST', '/api/execution/root', { parent_session_id: parent.id, delegation_depth: 0, provider: 'mock', model: 'b-kimi' })
  await sidecar.call('POST', '/api/spec', { max_team_workers: 6 })
  return { sidecar, restart }
}

for (const mode of ['partial-compensated', 'zero-start-denied', 'bound-during-failure', 'lost-admission-response']) {
  test('real CP admission ' + mode + ': native starts remain zero and resources follow confirmed ownership', { timeout: 30000 }, async t => {
    const { sidecar, restart } = await realSidecar(t)
    const call = sidecar.call.bind(sidecar)
    const existing = await call('POST', '/api/delegate', request(mode === 'zero-start-denied' ? 3 : 1))
    let starts = 0, failedReceipt, boundItem
    const settlements = []
    sidecar.call = async (method, path, body) => {
      if (path === '/api/execution/fail') settlements.push(body)
      try { return await call(method, path, body) }
      catch (error) {
        if (path !== '/api/delegate') throw error
        failedReceipt = structuredClone(error.details)
        if (mode === 'bound-during-failure') {
          const node = error.details.started[0]
          boundItem = node.item_id
          await call('POST', '/api/execution/bind', { item_id: node.item_id, node_id: node.node_id,
            attempt: node.attempt, context_epoch: node.context_epoch, reservation_session_id: node.session_id,
            execution_session_id: 'externally-published', parent_session_id: parent.id, execution_provider: 'spawn' })
        }
        if (mode === 'lost-admission-response') throw new TypeError('transport lost after admission')
        throw error
      }
    }
    const run = () => delegateOnce(request(mode === 'zero-start-denied' ? 1 : 3), exec(), sidecar,
      { async start() { starts++; throw new Error('native start forbidden') } })
    let reported
    await assert.rejects(run(), error => { reported = error; return true })
    assert.equal(starts, 0)
    assert.equal(failedReceipt.error, 'POINTS_EXCEEDED')
    assert.equal(failedReceipt.started.length, mode === 'zero-start-denied' ? 0 : 2)
    const expectedComplete = ['partial-compensated', 'zero-start-denied'].includes(mode)
    assert.equal(reported.details.admission_settlement.complete, expectedComplete)
    const expectedSlots = mode === 'partial-compensated' ? 1 : mode === 'bound-during-failure' ? 2 : 3
    const before = await call('GET', '/api/status')
    assert.equal(before.snapshot.open_worker_slots_used, expectedSlots)
    assert.equal(before.snapshot.active_points, expectedSlots * 2 + 1)
    assert.equal(before.spec.max_open_work_items, 4)
    assert.equal(before.spec.max_active_node_points, 8)
    assert.equal(settlements.length, mode === 'partial-compensated' ? 2 : mode === 'bound-during-failure' ? 1 : 0)
    for (const item of existing.items) assert.equal(before.snapshot.work_items[item.item_id].acceptance, null)
    if (boundItem) assert.equal(before.snapshot.work_items[boundItem].acceptance, null)
    for (const body of settlements) {
      assert.equal(body.published, false)
      assert.equal(body.physical_cleanup_confirmed, true)
      assert.equal(before.snapshot.work_items[body.item_id].acceptance, 'terminated')
    }
    await restart()
    const after = await call('GET', '/api/status')
    assert.deepEqual(after.snapshot, before.snapshot)
  })
}
