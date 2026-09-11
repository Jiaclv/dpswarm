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
  const plan = await h.service.plan(h.agents.get('lead'), { task: 'Build one SVG.', tokenLimit: 50000, callLimit: 2, reason: 'bounded SVG child; above the 4,096 closeout report floor' })
  const message = assignment(plan.prompt)
  await h.ctx.get('systemPrompt').assemble({ agent: h.agents.get('child') })
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

test('two-call worker can save a candidate first and sends tool-free delivery on its final call', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 2 })
  h.assembly.tools = [{ name: 'write', description: 'Save a file.', parameters: { type: 'object' } }]
  const first = await prepared(h, 'child')
  assert.equal(first.assembly.tools.length, 1)
  assert.equal((await h.service.diagnosticsForSession('child')).closeout, null)
  await h.stream('child', { ...first.config, system: first.system, tools: first.assembly.tools, messages: first.messages })
  h.child.append('user/message', assignment('write succeeded: candidate.html was saved; final report has not yet been sent.'), { surfaceOp: 'append' })
  const last = await prepared(h, 'child')
  assert.equal(last.assembly.tools.length, 1, 'tool schemas stay visible in the final-only assembly')
  assert.match(last.system, /DPSWARM_WORKER_FINAL_ONLY/)
  assert.match(last.system, /If nothing was provably saved/)
  assert.equal((await h.service.diagnosticsForSession('child')).remaining_calls, 1)
  await h.stream('child', { ...last.config, system: last.system, tools: last.assembly.tools, messages: last.messages })
  assert.equal(h.calls.length, 2)
  assert.equal((await h.service.diagnosticsForSession('child')).remaining_calls, 0)
  h.service.shutdown()
})

test('final-only keeps tool schemas visible, denies their execution, and bounds extra calls', async () => {
  const h = fixture({ workerTokenLimit: 6000, workerCallLimit: 10 })
  h.assembly.tools = [{ name: 'run_code', parameters: { type: 'object' } }]
  // Token rail parks the worker at arrival (6,000 < input + final + 4,096 + 2,048).
  const last = await prepared(h, 'child')
  assert.equal(last.assembly.tools.length, 1, 'schemas stay visible; execution is denied at the tool gate instead')
  assert.match(last.system, /DPSWARM_WORKER_FINAL_ONLY/)
  // A tool-attempt step is admitted but the tool never executes (cold-restore path covered).
  h.service.runtime.states.clear()
  let executed = 0
  for (const name of ['write', 'run_code']) {
    await assert.rejects(h.ctx.waterfall('tools/pre-execute', { agent: h.agents.get('child'), name }, async () => { executed++; return {} }), { code: 'WORKER_CLOSEOUT_FINAL_ONLY' })
    assert.match(h.guards[0]({ agent: h.agents.get('child'), name }), /budget rail reached/)
  }
  assert.equal(executed, 0)
  // The report call follows; a third post-closeout call is refused.
  await h.stream('child', { system: last.system, tools: last.assembly.tools, messages: last.messages })
  await h.stream('child', { system: last.system, tools: [], messages: last.messages })
  await assert.rejects(h.stream('child', { system: last.system, tools: [] }), { code: 'WORKER_CLOSEOUT_ALREADY_SENT' })
  assert.equal(h.calls.length, 2)
  h.service.shutdown()
})

test('a tiny grant is refused before dispatch with the real (never-stripped) envelope priced in', async () => {
  const h = fixture({ workerTokenLimit: 500, workerCallLimit: 1 })
  h.assembly.tools = [{ name: 'write', description: 'large schema'.repeat(1000) }]
  // Tool schemas are never stripped now, so a grant too small for the real
  // envelope gets an honest pre-dispatch denial instead of a fake-worked report.
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
  assert.equal(after, estimateRequestTokens({ system: '', tools: [], messages: [task, projected] }).input)
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


test('final report input measurement prices the retained tool schemas (they are never stripped)', async () => {
  const h = fixture({ workerTokenLimit: 11000, workerCallLimit: 3 })
  h.assembly.sections = [{ name: 'system', text: 'S'.repeat(9500) }]
  h.assembly.tools = [{ name: 'old_tool', description: 'T'.repeat(6000) }]
  let measuredHeader
  h.ctx.provide('tokenMeter', { measure: (_session, header) => {
    measuredHeader = header
    return { totalTokens: header ? 300 : 5000 }
  } })
  const step = await prepared(h, 'child')
  assert.equal((measuredHeader.tools || []).length, 1, 'the retained schema is priced into the envelope instead of charging a stale or a stripped one')
  assert.match(measuredHeader.system, /DPSWARM_WORKER_FINAL_ONLY/)
  assert.ok(step.config.maxTokens > 0)
  await h.stream('child', { ...step.config, system: step.system, tools: step.assembly.tools, messages: step.messages })
  assert.equal(h.calls.length, 1)
  h.service.shutdown()
})
