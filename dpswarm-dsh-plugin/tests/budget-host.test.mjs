import assert from 'node:assert/strict'
import test from 'node:test'
import { installBudget } from '../lib/budget.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const [{ Context }, { Session }] = await Promise.all(['cordis', 'dsh-session'].map(p => import(hostModuleUrl(host, `${p}/lib/index.js`))))

const assignment = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] })

function fixture(config = {}, provider) {
  const ctx = new Context(), root = Session.create('lead', undefined, { version: 0, id: 'lead', createdAt: 1 })
  const child = Session.create('child', undefined, { version: 0, id: 'child', createdAt: 2, parentSession: root.id, origin: 'subagent', delegationDepth: 1 })
  const sibling = Session.create('sibling', undefined, { version: 0, id: 'sibling', createdAt: 3, parentSession: root.id, origin: 'subagent', delegationDepth: 1 })
  const sessions = new Map([root, child, sibling].map(s => [s.id, s]))
  const agents = new Map([...sessions.values()].map(s => [s.id, { session: s, options: { provider: s.id === 'lead' ? 'lead-provider' : 'worker-provider', model: 'model', reasoningEffort: 'max' } }]))
  const calls = [], flushes = []
  const assembly = { sections: [], contexts: [], variables: {}, tools: [] }
  ctx.provide('systemPrompt', { assemble: async () => structuredClone(assembly) })
  ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()], flush: async s => { flushes.push(s.id) } })
  ctx.provide('agents', { get: id => agents.get(id) })
  const llm = { resolveCallConfig: async c => ({ ...c, maxTokens: c.maxTokens ?? config.fixtureDefaultMaxTokens ?? 32768 }),
    stream: options => ctx.waterfall('llm/stream', options, () => (async function* () {
    calls.push(options)
    if (provider) { yield* provider(options); return }
    yield { type: 'usage', usage: { inputTokens: 10, cacheReadTokens: 5, outputTokens: 5, reasoningTokens: 2 } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })()) }
  ctx.provide('llm', llm)
  const cfg = { workerBudgetMode: 'manual', workerTokenLimit: 10000, workerCallLimit: 1, ...config }
  const journal = new MemoryAuditJournal()
  const service = installBudget(ctx, () => cfg, { journal })
  const stream = async (id, extra = {}) => {
    const request = Object.freeze({ provider: 'worker-provider', model: 'model', messages: [], maxTokens: 100, sessionId: id, ...extra })
    for await (const _chunk of llm.stream(request)) { /* Consume exactly the host stream. */ }
    return request
  }
  return { ctx, root, child, sibling, agents, calls, flushes, cfg, journal, service, stream, llm, assembly }
}

test('host request hook lowers only a limited worker output bound', async () => {
  const h = fixture({ workerTokenLimit: 500, workerCallLimit: 2 })
  const original = Object.freeze({ provider: 'worker-provider', model: 'model', reasoningEffort: 'max', maxTokens: 5000 })
  const child = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.notEqual(child, original); assert.ok(child.maxTokens < 500)
  assert.equal(original.maxTokens, 5000)
  assert.equal(await h.ctx.waterfall('agent/request', { agent: h.agents.get('lead') }, async () => original), original)
  h.service.shutdown()
})

test('host Auto pre-step accepts one durable Lead allocation before worker dispatch', async () => {
  const h = fixture({ workerBudgetMode: 'auto' })
  const plan = await h.service.plan(h.agents.get('lead'), { task: 'Build one SVG.', tokenLimit: 9000, callLimit: 2, reason: 'bounded SVG child' })
  const message = assignment(plan.prompt)
  await h.ctx.waterfall('agent/pre-step', { agent: h.agents.get('child'), messages: [message] }, async () => ({ kind: 'enter' }))
  h.child.append('user/message', message, { surfaceOp: 'append' })
  await h.stream('child')
  assert.equal(h.calls.length, 1)
  const status = await h.service.status(h.agents.get('child'))
  assert.equal(status.decision.allocation_id, plan.allocation_id)
  assert.equal(status.remaining_calls, 1)
  h.service.shutdown()
})

test('unlimited child request preserves the exact native config and never assembles a budget envelope', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited' })
  h.ctx.get('systemPrompt').assemble = () => { throw new Error('unlimited should not assemble') }
  const original = Object.freeze({ provider: 'worker-provider', model: 'model' })
  assert.equal(await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original), original)
  await h.stream('child', { maxTokens: undefined })
  assert.equal(h.calls.length, 1)
  h.service.shutdown()
})

test('large first request includes system and tool envelope before dispatch', async () => {
  const h = fixture({ workerTokenLimit: 80000, workerCallLimit: 2, fixtureDefaultMaxTokens: 256000 })
  h.assembly.sections = [{ name: 'system', text: 'S'.repeat(9500) }]
  h.assembly.tools = [{ name: 'large_tool', description: 'T'.repeat(30000), parameters: { type: 'object' } }]
  const native = Object.freeze({ provider: 'worker-provider', model: 'model' })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => native)
  assert.ok(bounded.maxTokens > 1 && bounded.maxTokens < 256000)
  await h.stream('child', { system: 'S'.repeat(9500), tools: h.assembly.tools, maxTokens: bounded.maxTokens })
  const status = await h.service.status(h.agents.get('child'))
  assert.ok(status.recent[0].input_estimate > 13000)
  h.service.shutdown()
})
