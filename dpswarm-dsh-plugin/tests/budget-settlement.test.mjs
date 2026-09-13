import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime, estimateRequestTokens } from '../lib/budget-runtime.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

async function fixture() {
  const root = { id: 'lead', header: { id: 'lead' }, events: [] }
  const child = { id: 'child', header: { id: 'child', parentSession: 'lead', origin: 'subagent', delegationDepth: 1 }, events: [] }
  const sessions = new Map([root, child].map(session => [session.id, session])), journal = new MemoryAuditJournal()
  const create = () => new WorkerBudgetRuntime({ config: () => ({ workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28 }),
    resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  const runtime = create(), state = await runtime.ensure({ session: child })
  return { runtime, state, create, child, journal }
}
const request = extra => ({ provider: 'local', model: 'fixture', messages: [], maxTokens: 100, ...extra })

test('settling an active ticket after another admission updates the live projection by call id', async () => {
  const h = await fixture()
  const worker = await h.runtime.admit(h.state, request({ maxTokens: 1000 }))
  const cm = await h.runtime.admit(h.state, request({ purpose: 'compaction', maxTokens: 500 }))
  assert.notEqual(h.state.calls.get(worker.call.call_id), worker.call, 'durable fold replaced the active ticket object')
  await h.runtime.settle(worker, { inputTokens: 100, cacheReadTokens: 50, outputTokens: 20 }, 'stop')
  await h.runtime.settle(cm, { inputTokens: 40, outputTokens: 10 }, 'stop')
  const live = h.runtime.describe(h.state), cold = h.create(), restored = await cold.ensure({ session: h.child })
  assert.equal(live.observed_tokens_lower_bound, 220)
  assert.equal(live.committed_tokens, 220)
  assert.equal(live.unknown_usage_calls, 0)
  assert.equal(live.calls, 2)
  for (const key of ['observed_tokens_lower_bound', 'committed_tokens', 'unknown_usage_calls', 'calls']) assert.equal(live[key], cold.describe(restored)[key])
})

test('98f6 transport failure keeps exactly its unknown reservation beside four known usages', async () => {
  const h = await fixture()
  for (const total of [38273, 58471]) await h.runtime.settle(await h.runtime.admit(h.state, request()), { inputTokens: total - 100, outputTokens: 100 }, 'stop')
  const failedRequest = request({ maxTokens: 256000, system: '' })
  const empty = estimateRequestTokens(failedRequest).input
  failedRequest.system = 'x'.repeat((69013 - empty) * 3)
  assert.equal(estimateRequestTokens(failedRequest).input, 69013)
  const failed = await h.runtime.admit(h.state, failedRequest)
  await h.runtime.settle(failed, null, 'error', 'TRANSPORT')
  assert.equal(failed.call.reserved_tokens, 325013)
  for (const total of [77203, 77733]) await h.runtime.settle(await h.runtime.admit(h.state, request()), { inputTokens: total - 100, outputTokens: 100 }, 'stop')
  const status = h.runtime.describe(h.state)
  assert.equal(status.calls, 5)
  assert.equal(status.observed_tokens_lower_bound, 251680)
  assert.equal(status.committed_tokens, 576693)
  assert.equal(status.unknown_usage_calls, 1)
  assert.equal(status.remaining_tokens, 23307)
  const restored = h.create(), state = await restored.ensure({ session: h.child })
  assert.equal(restored.describe(state).committed_tokens, 576693, 'cold restore cannot convert missing usage into zero')
})


test('trusted request pricing only raises the serialized admission floor and cannot come from request content', async () => {
  const h = await fixture(), options = request({ system: 'Context '.repeat(900), inputEstimate: 1, input_estimate: 1 })
  const serialized = estimateRequestTokens(options).input
  const plain = await h.runtime.admit(h.state, options)
  assert.equal(plain.call.input_estimate, serialized)
  const trusted = await h.runtime.admit(h.state, request(), { inputEstimate: 50000 })
  assert.equal(trusted.call.reserved_tokens, 50100)
  const low = await h.runtime.admit(h.state, options, { inputEstimate: 1 })
  assert.equal(low.call.input_estimate, serialized)
  await assert.rejects(h.runtime.admit(h.state, request(), { inputEstimate: NaN }), { code: 'WORKER_REQUEST_PRICING_UNAVAILABLE' })
})
