import assert from 'node:assert/strict'
import test from 'node:test'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { CMRuntime } from '../lib/cm-runtime.js'

function sessions() {
  const root = { id: 'root', header: {}, events: [] }
  const child = { id: 'child', header: { origin: 'subagent', delegationDepth: 1, parentSession: 'root', seedLength: 0 }, events: [] }
  const map = new Map([[root.id, root], [child.id, child]])
  return { root, child, map }
}
const signal = () => new AbortController().signal

test('memory audit journal serializes per-root transactions and returns immutable snapshots', async () => {
  const journal = new MemoryAuditJournal()
  await Promise.all(Array.from({ length: 8 }, (_, index) => journal.append('root', 'dpswarm/test', { index })))
  const first = await journal.read('root')
  assert.equal(first.revision, 8); assert.equal(first.events.length, 8)
  first.events[0].data.index = 'mutated'
  assert.equal((await journal.read('root')).events[0].data.index, 0)
})

test('limited worker freezes then CAS-admits only one concurrent call; every record has root and owner', async () => {
  const { root, child, map } = sessions(), journal = new MemoryAuditJournal()
  const runtime = new WorkerBudgetRuntime({
    config: () => ({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 1 }),
    resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal,
  })
  const state = await runtime.ensure({ session: child }, signal())
  const options = { provider: 'p', model: 'm', messages: [], maxTokens: 100 }
  const results = await Promise.allSettled([runtime.admit(state, options), runtime.admit(state, options)])
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(results.filter(result => result.status === 'rejected')[0].reason.code, 'WORKER_CALL_LIMIT_REACHED')
  const records = (await journal.read('root')).events
  for (const record of records) {
    assert.equal(record.data.root_session_id, 'root')
    assert.equal(record.data.owner_session_id, 'child')
  }
})

test('new auto grant creates the fresh root ledger; copied prompt cannot bind a sibling', async () => {
  const { root, child, map } = sessions(), sibling = { id: 'sibling', header: { origin: 'subagent', delegationDepth: 1, parentSession: 'root', seedLength: 0 }, events: [] }
  map.set(sibling.id, sibling)
  const journal = new MemoryAuditJournal(), runtime = new WorkerBudgetRuntime({
    config: () => ({ workerBudgetMode: 'auto', workerBudgetSessionOverrides: [] }),
    resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal,
  })
  const plan = await runtime.plan({ session: root }, { task: 'Create one SVG.', tokenLimit: 300, callLimit: 1, reason: 'bounded task' })
  const message = { type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: plan.prompt }] } }
  child.events.push(message); sibling.events.push(message)
  await runtime.ensure({ session: child }, signal())
  await assert.rejects(runtime.ensure({ session: sibling }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
})

test('disabled CM does not read the sidecar ledger', async () => {
  const journal = { read: async () => { throw new Error('should not read') }, append: async () => { throw new Error('should not append') } }
  const runtime = new CMRuntime(() => ({ cmEnabledSessions: [], cmProvider: 'deepseek' }), () => null, journal)
  assert.equal(await runtime.profile({ id: 'root', header: {}, events: [] }), null)
})

test('cold restored unsettled admission stays charged and prevents a second dispatch', async () => {
  const { root, child, map } = sessions(), journal = new MemoryAuditJournal()
  const cfg = () => ({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 1 })
  const first = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  const state = await first.ensure({ session: child }, signal())
  await first.admit(state, { provider: 'p', model: 'm', messages: [], maxTokens: 100 })
  const resumed = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  const restored = await resumed.ensure({ session: child }, signal())
  assert.equal(restored.restored, true)
  assert.equal(resumed.totals(restored).unknown_usage_calls, 1)
  await assert.rejects(resumed.admit(restored, { provider: 'p', model: 'm', messages: [], maxTokens: 100 }), { code: 'WORKER_CALL_LIMIT_REACHED' })
})

test('two runtime instances share CAS quota and cannot overbook one worker', async () => {
  const { root, child, map } = sessions(), journal = new MemoryAuditJournal()
  const cfg = () => ({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 1 })
  const a = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  const first = await a.ensure({ session: child }, signal())
  const b = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  const second = await b.ensure({ session: child }, signal())
  const request = { provider: 'p', model: 'm', messages: [], maxTokens: 100 }
  const results = await Promise.allSettled([a.admit(first, request), b.admit(second, request)])
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(results.filter(result => result.status === 'rejected')[0].reason.code, 'WORKER_CALL_LIMIT_REACHED')
})

test('two CM runtimes atomically admit at most one compaction for a one-call turn', async () => {
  const journal = new MemoryAuditJournal(), cfg = { cmEnabledSessions: ['root'], cmProvider: 'deepseek' }
  const root = { id: 'root', header: {}, events: [{ type: 'turn/start', seq: 9, data: {} }] }
  const a = new CMRuntime(() => cfg, () => root, journal)
  const b = new CMRuntime(() => cfg, () => root, journal)
  const profile = { ...(await a.profile(root)), maxCallsPerAgentTurn: 1 }
  const results = await Promise.all([a.begin({ session: root }, profile, { trigger: 'test' }, signal()), b.begin({ session: root }, profile, { trigger: 'test' }, signal())])
  assert.equal(results.filter(Boolean).length, 1)
  const starts = (await journal.read('root')).events.filter(event => event.type === 'dpswarm/cm-start')
  assert.equal(starts.length, 1)
})

test('restored sorted-key profile matches the worker policy semantically', async () => {
  const { root, child, map } = sessions(), journal = new MemoryAuditJournal()
  const cfg = () => ({ workerBudgetMode: 'manual', workerTokenLimit: 500, workerCallLimit: 2 })
  const first = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  await first.ensure({ session: child }, signal())
  const frozen = journal.roots.get('root').events.find(event => event.type === 'dpswarm/worker-budget-frozen')
  frozen.data.profile = { callLimit: 2, mode: 'manual', tokenLimit: 500 }
  const resumed = new WorkerBudgetRuntime({ config: cfg, resolveSession: id => map.get(id), listSessions: () => [...map.values()], journal })
  assert.equal((await resumed.ensure({ session: child }, signal())).profile.mode, 'manual')
})
