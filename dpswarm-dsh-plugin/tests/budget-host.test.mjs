import assert from 'node:assert/strict'
import test from 'node:test'
import { estimateRequestTokens } from '../lib/budget-runtime.js'
import { CLOSEOUT_INSTRUCTION } from '../lib/worker-closeout.js'
import { installBudget } from '../lib/budget.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const [{ Context }, { Session, SESSION_FORMAT_VERSION }] = await Promise.all(['cordis', 'dsh-session'].map(p => import(hostModuleUrl(host, `${p}/lib/index.js`))))

const assignment = text => ({ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] })

function fixture(config = {}, provider) {
  const ctx = new Context(), root = Session.create('lead', undefined, { version: SESSION_FORMAT_VERSION, id: 'lead', createdAt: 1, isSeeded: false })
  const child = Session.create('child', undefined, { version: SESSION_FORMAT_VERSION, id: 'child', createdAt: 2, parentSession: root.id, origin: 'subagent', delegationDepth: 1, isSeeded: false })
  const sibling = Session.create('sibling', undefined, { version: SESSION_FORMAT_VERSION, id: 'sibling', createdAt: 3, parentSession: root.id, origin: 'subagent', delegationDepth: 1, isSeeded: false })
  const sessions = new Map([root, child, sibling].map(s => [s.id, s]))
  const agents = new Map([...sessions.values()].map(s => [s.id, { session: s, options: { provider: s.id === 'lead' ? 'lead-provider' : 'worker-provider', model: 'model', reasoningEffort: 'max' } }]))
  const calls = [], flushes = []
  const assembly = { sections: [], contexts: [], variables: {}, tools: [] }
  ctx.provide('systemPrompt', { assemble: async context => {
    const current = structuredClone(assembly)
    return ctx.waterfall('system-prompt/assemble', current, context, async () => current)
  } })
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
  const guards = []
  ctx.provide('tools', { guard: guard => { guards.push(guard); return () => guards.splice(guards.indexOf(guard), 1) } })
  const service = installBudget(ctx, () => cfg, { journal })
  const stream = async (id, extra = {}) => {
    const request = Object.freeze({ provider: 'worker-provider', model: 'model', messages: [], maxTokens: 100, sessionId: id, ...extra })
    for await (const _chunk of llm.stream(request)) { /* Consume exactly the host stream. */ }
    return request
  }
  return { ctx, root, child, sibling, agents, calls, flushes, cfg, journal, service, stream, llm, assembly, guards }
}

test('host request hook lowers only a limited worker output bound', async () => {
  const h = fixture({ workerTokenLimit: 1000, workerCallLimit: 2 })
  const original = Object.freeze({ provider: 'worker-provider', model: 'model', reasoningEffort: 'max', maxTokens: 5000 })
  const child = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.notEqual(child, original); assert.ok(child.maxTokens < 1000)
  assert.equal(original.maxTokens, 5000)
  assert.equal(await h.ctx.waterfall('agent/request', { agent: h.agents.get('lead') }, async () => original), original)
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

// Assembly, pre-step, append and request order matches the public native loop.
async function prepared(h, id, text = 'Continue the delegated task.') {
  const agent = h.agents.get(id), signal = new AbortController().signal
  const assembly = await h.ctx.get('systemPrompt').assemble({ agent, signal })
  const message = assignment(text)
  const decision = await h.ctx.waterfall('agent/pre-step', { agent, signal, messages: [message] },
    async () => ({ kind: 'enter', messages: [message] }))
  for (const m of decision.messages) agent.session.append('user/message', m, { surfaceOp: 'append' })
  const config = await h.ctx.waterfall('agent/request', { agent, signal }, async () => ({ provider: 'worker-provider', model: 'model', maxTokens: 1000 }))
  const system = assembly.sections.map(s => s.text).join('\n\n')
  return { agent, signal, assembly, config, system, messages: agent.session.deriveMessages() }
}

test('two-call worker can work once then report without a refused-tool round', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 2 })
  h.assembly.tools = [{ name: 'write', description: 'Save a file.', parameters: { type: 'object' } }]
  const first = await prepared(h, 'child')
  assert.equal(first.assembly.tools.length, 1)
  assert.equal((await h.service.diagnosticsForSession('child')).closeout, null)
  await h.stream('child', { ...first.config, system: first.system, tools: first.assembly.tools, messages: first.messages })
  h.child.append('user/message', assignment('A useful check completed; the final report has not yet been sent.'), { surfaceOp: 'append' })
  const last = await prepared(h, 'child')
  assert.equal(last.assembly.tools.length, 1, 'the final-only request keeps schemas visible; execution alone is denied at the tool gate')
  assert.match(last.system, /DPSWARM_WORKER_FINAL_ONLY/)
  assert.match(last.system, /If nothing was provably saved/)
  assert.equal((await h.service.diagnosticsForSession('child')).remaining_calls, 1)
  await h.stream('child', { ...last.config, system: last.system, tools: last.assembly.tools, messages: last.messages })
  assert.equal(h.calls.length, 2)
  assert.equal((await h.service.diagnosticsForSession('child')).remaining_calls, 0)
  h.service.shutdown()
})

test('final-only keeps tool schemas, denies direct execution, and permits one tool-attempt plus one report', async () => {
  const h = fixture({ workerTokenLimit: 6000, workerCallLimit: 10 })
  h.assembly.tools = [{ name: 'run_code', parameters: { type: 'object' } }]
  // Token rail parks the worker at arrival (6,000 < input + final + 4,096 + 2,048).
  const last = await prepared(h, 'child')
  assert.equal(last.assembly.tools.length, 1, 'schemas stay listed; only execution is denied')
  assert.match(last.system, /DPSWARM_WORKER_FINAL_ONLY/)
  // A tool-attempt step is admitted but the tool never executes (cold-restore path covered).
  h.service.runtime.states.clear()
  let executed = 0
  for (const name of ['write', 'run_code']) {
    await assert.rejects(h.ctx.waterfall('tools/pre-execute', { agent: h.agents.get('child'), name }, async () => { executed++; return {} }), { code: 'WORKER_CLOSEOUT_FINAL_ONLY' })
    assert.match(h.guards[0]({ agent: h.agents.get('child'), name }), /budget rail reached/)
  }
  assert.equal(executed, 0)
  // One tool-attempt step plus one report call are admitted; a third post-closeout call is refused.
  await h.stream('child', { system: last.system, tools: last.assembly.tools, messages: last.messages })
  await h.stream('child', { system: last.system, tools: last.assembly.tools, messages: last.messages })
  await assert.rejects(h.stream('child', { system: last.system, tools: [] }), { code: 'WORKER_CLOSEOUT_ALREADY_SENT' })
  assert.equal(h.calls.length, 2)
  h.service.shutdown()
})

test('a tiny grant is refused before dispatch when even the closeout report envelope does not fit', async () => {
  const h = fixture({ workerTokenLimit: 500, workerCallLimit: 1 })
  h.assembly.tools = [{ name: 'write', description: 'large schema'.repeat(1000) }]
  // The closeout envelope keeps its tool schemas, so a grant too small for the
  // real envelope gets an honest pre-dispatch denial instead of a fake-worked report.
  await assert.rejects(prepared(h, 'child'), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  const d = await h.service.diagnosticsForSession('child')
  assert.equal(h.calls.length, 0)
  assert.equal(d.closeout.mode, 'final_only')
  assert.equal(d.last_denial.code, 'WORKER_TOKEN_RESERVATION_DENIED')
  assert.equal(d.last_denial.stage, 'request_output_limit')
  assert.ok(d.last_denial.input_estimate > 500)
  h.service.shutdown()
})

test('a first grant too small even for the final report is refused with durable exact input diagnostics', async () => {
  const h = fixture({ workerTokenLimit: 20, workerCallLimit: 1 })
  await assert.rejects(prepared(h, 'child'), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  const d = await h.service.diagnosticsForSession('child')
  assert.equal(h.calls.length, 0)
  assert.equal(d.last_denial.stage, 'request_output_limit')
  assert.equal(d.last_denial.remaining_tokens, 20)
  assert.ok(d.last_denial.input_estimate > 20)
  assert.equal(d.last_denial.output_limit, 1)
  assert.equal(d.last_denial.required_reservation, d.last_denial.input_estimate + 1)
  h.service.runtime.states.clear()
  assert.deepEqual((await h.service.diagnosticsForSession('child')).last_denial, d.last_denial)
  h.service.shutdown()
})

test('Lead and unlimited children preserve tools and receive no closeout instruction or tool denial', async () => {
  const h = fixture({ workerBudgetMode: 'unlimited', workerTokenLimit: 1, workerCallLimit: 1 })
  h.assembly.tools = [{ name: 'write' }]
  for (const id of ['lead', 'child']) {
    const step = await prepared(h, id)
    assert.equal(step.assembly.tools.length, 1)
    assert.doesNotMatch(step.system, /DPSWARM_WORKER_FINAL_ONLY/)
    assert.equal(h.guards[0]({ agent: h.agents.get(id), name: 'write' }), undefined)
  }
  assert.equal(await h.service.diagnosticsForSession('lead'), null)
  h.service.shutdown()
})


test('projected dynamic runtime context is priced once, including the first-step projection', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 5 })
  const { renderContextSnapshot } = await import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js'))
  h.assembly.contexts = [{ name: 'environment', text: 'E'.repeat(2000) }]
  const agent = h.agents.get('child'), signal = new AbortController().signal
  const assembly = await h.ctx.get('systemPrompt').assemble({ agent, signal })
  const context = renderContextSnapshot(assembly), task = assignment('Create the file.')
  const projected = { id: 'native-context', role: 'user', source: { kind: 'plugin', plugin: '@deepseek-ai/dsh-system-prompt' }, content: [{ type: 'text', text: context }] }
  await h.service.prepareStep(agent, { signal, messages: [task] })
  const before = h.service.runtime.states.get('child').stepBudget.input_estimate
  await h.service.prepareStep(agent, { signal, messages: [task, projected] })
  const after = h.service.runtime.states.get('child').stepBudget.input_estimate
  const { createSystemMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
  const system = createSystemMessage(assembly.sections.map(section => section.text).join('\n\n'), '@deepseek-ai/dsh-system-prompt')
  assert.equal(after, estimateRequestTokens({ tools: [], messages: [...[task, projected], system] }).input)
  // Metadata adds a few tokens; the entire 2k dynamic context is not duplicated.
  assert.ok(after - before < 100)
  h.service.shutdown()
})

test('cancelled pre-step and immutable schema fail before any model dispatch', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 1 })
  const agent = h.agents.get('child'), abort = new AbortController()
  await h.ctx.get('systemPrompt').assemble({ agent, signal: abort.signal })
  abort.abort()
  await assert.rejects(h.service.prepareStep(agent, { signal: abort.signal }), { name: 'AbortError' })
  assert.equal(h.calls.length, 0)
  const signal = new AbortController().signal
  const assembly = await h.ctx.get('systemPrompt').assemble({ agent, signal })
  Object.freeze(assembly.tools)
  Object.freeze(assembly.sections)
  await assert.rejects(h.service.prepareStep(agent, { signal }), { code: 'WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE' })
  assert.equal(h.calls.length, 0)
  assert.equal((await h.service.diagnosticsForSession('child')).last_denial.code, 'WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
  h.service.shutdown()
})


test('final report input measurement prices the current envelope with its visible schemas', async () => {
  const h = fixture({ workerTokenLimit: 11000, workerCallLimit: 3 })
  h.assembly.sections = [{ name: 'system', text: 'S'.repeat(9500) }]
  h.assembly.tools = [{ name: 'old_tool', description: 'T'.repeat(6000) }]
  let measuredHeader
  h.ctx.provide('tokenMeter', { measure: (_session, header) => {
    measuredHeader = header
    return { totalTokens: header ? 300 : 5000 }
  } })
  const step = await prepared(h, 'child')
  assert.equal((measuredHeader.tools || []).length, 1, 'the closeout request still carries the schema, so it is charged')
  assert.match(measuredHeader.system, /DPSWARM_WORKER_FINAL_ONLY/)
  assert.ok(step.config.maxTokens > 0)
  await h.stream('child', { ...step.config, system: step.system, tools: step.assembly.tools, messages: step.messages })
  assert.equal(h.calls.length, 1)
  h.service.shutdown()
})

test('request output limit is clamped to a host-observed route cap when the rail shapes above it', async () => {
  // Live session 88122af1: glm rejects max_tokens above 131072 while a 300k
  // rail shaped a larger allowance; the provider refused the first request.
  const h = fixture({ workerTokenLimit: 300000, workerCallLimit: 2 })
  h.llm.resolveCallConfig = async c => ({ ...c }) // no host default output bound applied
  let lookups = 0
  h.llm.resolveModelInfo = async (provider, model) => { lookups++
    return { provider, id: model, defaultMaxTokens: 131072, context: { contextWindow: 131072 } } }
  const original = Object.freeze({ provider: 'worker-provider', model: 'model' })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.equal(bounded.maxTokens, 131072, 'the route cap wins over the rail-shaped allowance')
  assert.ok(bounded.maxTokens < 300000)
  const second = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.equal(second.maxTokens, 131072)
  assert.equal(lookups, 1, 'the route cap is resolved once per route, then cached')
  assert.equal((await h.service.diagnosticsForSession('child')).request_budget.route_output_cap, 131072)
  h.service.shutdown()
})

test('a context-window-only route (glmcp shape: no defaultMaxTokens) is clamped by its window', async () => {
  // Live session e3589816: glmcp exposes context.contextWindow=131072 but no
  // defaultMaxTokens; the tester's shaped maxTokens 269854 died at the provider.
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 })
  h.llm.resolveCallConfig = async c => ({ ...c }) // no host default output bound applied
  h.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model, context: { contextWindow: 131072 } })
  const original = Object.freeze({ provider: 'worker-provider', model: 'model' })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.equal(bounded.maxTokens, 131072, 'the window alone clamps the shaped allowance')
  assert.equal((await h.service.diagnosticsForSession('child')).request_budget.route_output_cap, 131072)
  h.service.shutdown()
})

test('a route exposing both bounds is clamped by the smaller one', async () => {
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 })
  h.llm.resolveCallConfig = async c => ({ ...c })
  h.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model, defaultMaxTokens: 98304, context: { contextWindow: 131072 } })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => Object.freeze({ provider: 'worker-provider', model: 'model' }))
  assert.equal(bounded.maxTokens, 98304)
  h.service.shutdown()
})

test('a registry-blind route falls back to the measured provider cap, and the registry still wins when present', async () => {
  // Live shape: glmcp reports no defaultMaxTokens and (in some setups) no
  // contextWindow either; the measured gateway bound is 131072.
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 })
  h.llm.resolveCallConfig = async c => ({ ...c })
  h.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model })
  const original = Object.freeze({ provider: 'glmcp', model: 'glm-5.3-flash' })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.equal(bounded.maxTokens, 131072, 'the measured fallback clamps the registry-blind route')
  // An unknown provider with no registry data stays unclamped.
  const unknown = Object.freeze({ provider: 'other-provider', model: 'm' })
  const free = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => unknown)
  assert.ok(free.maxTokens > 131072, 'unknown routes are never clamped by guesses')
  h.service.shutdown()
})

test('an optimistic registry (262144) loses to the measured gateway cap (131072), a conservative one still wins', async () => {
  // Live 03e8db1b: the glmcp registry reports 262144 while the gateway rejects
  // anything above 131072; only the measured value prevents the dead request.
  {
    const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 })
    h.llm.resolveCallConfig = async c => ({ ...c, maxTokens: 262144 })
    h.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model, defaultMaxTokens: 262144, context: { contextWindow: 262144 } })
    const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => Object.freeze({ provider: 'glmcp', model: 'glm-5.3-flash' }))
    assert.equal(bounded.maxTokens, 131072, 'the measured gateway cap wins over the optimistic registry')
    h.service.shutdown()
  }
  {
    const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 })
    h.llm.resolveCallConfig = async c => ({ ...c })
    h.llm.resolveModelInfo = async (provider, model) => ({ provider, id: model, defaultMaxTokens: 65536 })
    const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => Object.freeze({ provider: 'glmcp', model: 'glm-5.3-flash' }))
    assert.equal(bounded.maxTokens, 65536, 'a conservative registry value still lowers the bound')
    h.service.shutdown()
  }
})

test('an unavailable model lookup leaves the rail-shaped bound unclamped', async () => {
  const h = fixture({ workerTokenLimit: 300000, workerCallLimit: 2 })
  h.llm.resolveCallConfig = async c => ({ ...c })
  h.llm.resolveModelInfo = async () => { throw new Error('metadata unavailable') }
  const original = Object.freeze({ provider: 'worker-provider', model: 'model' })
  const bounded = await h.ctx.waterfall('agent/request', { agent: h.agents.get('child') }, async () => original)
  assert.ok(bounded.maxTokens > 131072, 'unknown metadata must not invent a clamp')
  assert.equal((await h.service.diagnosticsForSession('child')).request_budget.route_output_cap, null)
  h.service.shutdown()
})
