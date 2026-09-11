import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { measureSystemRequest, pendingSystemText, retainedSystemText } from '../lib/host-session-compat.js'
import { installBudget } from '../lib/budget.js'
import { estimateRequestTokens } from '../lib/budget-runtime.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
const host = resolveHostRoot()
const [{ Context }, { Session, canonicalHeader }, { TokenMeter }, { createSystemMessage }] = await Promise.all(
  ['cordis', 'dsh-session', 'dsh-token-meter', 'dsh-llm'].map(name => import(hostModuleUrl(host, name + '/lib/index.js'))))
const header = { config: { provider: 'fixture', model: 'fixture' }, tools: [{ name: 'read', parameters: { type: 'object' } }] }
const addSystem = (session, text) => session.append('system/message', { message: { role: 'system', content: text ? [{ type: 'text', text }] : [] } }, { surfaceOp: 'append' })
const addUser = session => session.append('user/message', { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create the animation.' }] }, { surfaceOp: 'append' })
function fixture(t, initial) {
  const ctx = new Context()
  t.after(() => ctx.fiber.dispose())
  ctx.provide('sessionProjections', { register() {} })
  const meter = new TokenMeter(ctx), session = Session.create('system-pricing')
  if (initial !== undefined) addSystem(session, initial)
  addUser(session)
  return { ctx, meter, session }
}
for (const scenario of ['none', 'same', 'changed', 'cleared', 'empty-tail']) {
  test('native system pricing: ' + scenario, t => {
    const old = 'Previous system '.repeat(50), next = scenario === 'cleared' ? '' : scenario === 'same' || scenario === 'empty-tail' ? old : 'New system '.repeat(70)
    const { meter, session } = fixture(t, scenario === 'none' ? undefined : old)
    if (scenario === 'empty-tail') addSystem(session, '')
    const events = session.snapshotEvents(), measured = meter.measure(session, header)
    const projected = measureSystemRequest(meter, session, header, next, canonicalHeader)
    const same = scenario === 'same' || scenario === 'empty-tail'
    const extra = !same && next ? meter.estimateMessage({ role: 'system', content: [{ type: 'text', text: next }] }) : 0
    assert.equal(projected.totalTokens, measured.totalTokens + extra)
    assert.equal(pendingSystemText(session.deriveMessages(), next), same ? '' : next)
    assert.equal(session.snapshotEvents(), events, 'measurement leaves durable history unchanged')
    if (scenario === 'changed' || scenario === 'none') {
      addSystem(session, next)
      assert.equal(projected.totalTokens, meter.measure(session, header).totalTokens, 'matches native in-history append')
    }
    const normalized = Session.create('normalized')
    addSystem(normalized, next); addUser(normalized)
    assert.ok(projected.totalTokens >= meter.measure(normalized, header).totalTokens, 'bounds native replacement/normalization')
  })
}
for (const scenario of ['none', 'same', 'changed']) {
  test('native usage anchor after system ' + scenario, t => {
    const old = 'Existing system', next = scenario === 'same' ? old : 'Changed system'
    const { meter, session } = fixture(t, scenario === 'none' ? undefined : old)
    session.append('step/start', { turn: 1, step: 1 })
    session.append('request/header', { header, reason: 'initial' })
    session.append('assistant/message', { turn: 1, step: 1, message: { role: 'assistant', content: [{ type: 'text', text: 'Done' }] },
      stream: [{ type: 'chunk', chunk: { type: 'text-delta', index: 0, text: 'Done' } }], usage: { inputTokens: 90000, outputTokens: 5 } }, { surfaceOp: 'append' })
    session.append('step/end', { turn: 1, step: 1 })
    const before = meter.measure(session, header)
    assert.equal(before.baseline.kind, 'usage')
    const projected = measureSystemRequest(meter, session, header, next, canonicalHeader)
    if (scenario === 'same') assert.equal(projected.totalTokens, before.totalTokens)
    else {
      assert.equal(projected.baseline.kind, 'estimated')
      const empty = Session.create('empty-tools'), tools = meter.measure(empty, header)
      assert.equal(tools.surfaceTokens, 0)
      assert.equal(tools.baseline.tokens, tools.totalTokens)
      assert.equal(projected.totalTokens, before.surfaceTokens + tools.totalTokens + meter.estimateMessage({ role: 'system', content: [{ type: 'text', text: next }] }))
    }
  })
}
for (const scenario of ['none', 'same', 'changed']) for (const nativeMeter of [false, true]) {
  test('budget ' + (nativeMeter ? 'native meter' : 'fallback') + ' system ' + scenario, async t => {
    const ctx = new Context()
    t.after(() => ctx.fiber.dispose())
    const root = Session.create('budget-root'), child = Session.create('budget-child', undefined, { version: 3, id: 'budget-child', createdAt: 1, isSeeded: false, origin: 'subagent', parentSession: root.id, delegationDepth: 1 })
    const old = 'Budget system '.repeat(100), next = scenario === 'same' ? old : 'Changed system '.repeat(120)
    if (scenario !== 'none') addSystem(child, old)
    const sessions = new Map([[root.id, root], [child.id, child]]), agent = { id: child.id, session: child, options: { provider: 'fixture', model: 'fixture' } }
    ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()] })
    ctx.provide('agents', { get: id => id === child.id ? agent : null })
    ctx.provide('tools', { guard: () => () => {} })
    ctx.provide('llm', { imageRequestPricing: () => null, resolveCallConfig: async config => ({ ...config, maxTokens: 256000 }) })
    const assembly = { sections: [{ name: 'system', text: next }], contexts: [], tools: [], variables: { provider: 'fixture', model: 'fixture' } }
    ctx.provide('systemPrompt', { assemble: async context => ctx.waterfall('system-prompt/assemble', structuredClone(assembly), context, async () => structuredClone(assembly)) })
    let meter
    if (nativeMeter) { ctx.provide('sessionProjections', { register() {} }); meter = new TokenMeter(ctx) }
    const service = installBudget(ctx, () => ({ workerBudgetMode: 'manual', workerTokenLimit: 100000, workerCallLimit: 5 }), { journal: new MemoryAuditJournal() })
    t.after(() => service.shutdown())
    const signal = new AbortController().signal
    await ctx.get('systemPrompt').assemble({ agent, signal })
    await service.prepareStep(agent, { signal, messages: [] })
    const messages = child.deriveMessages(), separate = scenario === 'same' ? '' : next
    const serialized = estimateRequestTokens({ messages, system: separate, tools: [] }).input
    const metered = meter ? measureSystemRequest(meter, child, { config: agent.options }, next, canonicalHeader).totalTokens : 0
    const expected = Math.max(serialized, metered)
    assert.equal(service.runtime.states.get(child.id).stepBudget.input_estimate, expected)
    const result = await ctx.waterfall('agent/request', { agent, signal }, async () => ({ ...agent.options, maxTokens: 256000 }))
    const state = service.runtime.states.get(child.id)
    // Materialize the native in-history system message as the request oracle:
    // unlike a bare system string it also carries role/content/source/id.
    if (scenario !== 'same') child.append('system/message', {
      message: createSystemMessage(next, '@deepseek-ai/dsh-system-prompt'),
    }, { surfaceOp: 'append' })
    const projected = estimateRequestTokens({ messages: child.deriveMessages(), tools: [] }).input
    assert.equal(result.maxTokens, service.runtime.outputLimit(state, Math.max(projected, metered), 256000))
    if (scenario === 'same') assert.equal(retainedSystemText(child), next)
  })
}