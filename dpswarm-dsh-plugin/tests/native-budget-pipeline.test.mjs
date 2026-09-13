import assert from 'node:assert/strict'
import test from 'node:test'
import { registerHooks } from 'node:module'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const host = resolveHostRoot(), lib = process.env.DPSWARM_TEST_LIB
  ? pathToFileURL(resolve(process.env.DPSWARM_TEST_LIB) + '/').href : new URL('../lib/', import.meta.url).href
const loopUrl = hostModuleUrl(host, 'dsh-agent-loop/lib/index.js')
// Test-only exposure of native private classes; no host file or method changes.
const hooks = registerHooks({ load(url, context, nextLoad) {
  const loaded = nextLoad(url, context)
  return url === loopUrl ? { ...loaded, source: loaded.source + '\nexport { ReactLoopAgent, SystemPromptProjection, RuntimeContextProjection };\n' } : loaded
} })
const [{ ReactLoopAgent, SystemPromptProjection, RuntimeContextProjection }, { Session }, { Context }, llmTypes,
  { renderPrompt, renderContextSnapshot, renderContextSections }, { installBudget }, { estimateRequestTokens }, compat] = await Promise.all([
  import(loopUrl), import(hostModuleUrl(host, 'dsh-session/lib/index.js')), import(hostModuleUrl(host, 'cordis/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')), import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js')),
  import(new URL('budget.js', lib)), import(new URL('budget-runtime.js', lib)), import(new URL('host-session-compat.js', lib)),
])
hooks.deregister()
const { ToolRuntime, defineTool } = await import(hostModuleUrl(host, 'dsh-tools/lib/index.js'))
const { createUserMessage, createSystemMessage } = llmTypes
const user = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
const plugin = (name, text) => createUserMessage({ source: { kind: 'plugin', plugin: name }, content: [{ type: 'text', text }] })
const makeSession = (id, extra = {}) => Session.create(id, undefined, { version: 3, id, createdAt: 1, isSeeded: false, ...extra })
const systemSource = '@deepseek-ai/dsh-system-prompt'

function fixture({ limit = 180000, callLimit = 10, assembly = { sections: [{ name: 'system', text: 'You are a worker.' }], contexts: [], variables: {}, tools: [] },
  update = 'in-history', retry = false, meter, provider, nativeTools = false, nativeTool } = {}) {
  const ctx = new Context(), root = makeSession('native-budget-root')
  const session = makeSession('native-budget-child', { parentSession: root.id, origin: 'subagent', delegationDepth: 1 })
  const sessions = new Map([root, session].map(value => [value.id, value]))
  const journal = new MemoryAuditJournal(), calls = [], preparations = [], requestsBeforeCommit = [], guards = []
  const config = { workerBudgetMode: 'manual', workerTokenLimit: limit, workerCallLimit: callLimit }
  let queued = [], providerCalls = 0
  const toolExecutions = []
  const agent = Object.assign(Object.create(ReactLoopAgent.prototype), {
    id: session.id, session, options: { provider: 'fixture-provider', model: 'fixture-model', maxTokens: 256000 },
    phase: { kind: 'running', turn: 1, step: 1, abort: new AbortController() },
    requestHeaderLogged: false, requestSurfaceGeneration: session.surface.replaceGeneration,
    frozenMessages: new WeakSet(), assistantStreamRevision: 0, assistantAttemptCounter: 0,
  })
  ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()] })
  ctx.provide('agents', { get: id => id === session.id ? agent : undefined, requireInitiator: () => agent })
  if (!nativeTools) ctx.provide('tools', { guard: guard => { guards.push(guard); return () => {} } })
  if (meter) ctx.provide('tokenMeter', meter)
  ctx.provide('systemPrompt', { tools: () => () => {}, assemble: async context => {
    const current = structuredClone(assembly)
    return ctx.waterfall('system-prompt/assemble', current, context, async () => current)
  } })
  if (nativeTools) {
    ctx.provide('agentLoop', { config: { maxParallelToolCalls: 1 } })
    const tools = new ToolRuntime(ctx)
    tools.register(defineTool({ name: 'tool_0', description: 'Forbidden fixture operation.', parameters: {},
      output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => nativeTool ? [{ type: 'text', text: JSON.stringify(value) }] : [] },
      ...nativeTool, execute: (...args) => { toolExecutions.push('executed'); return nativeTool?.execute(...args) ?? {} } }))
  }
  const llm = {
    resolveCallConfig: async value => ({ ...value, maxTokens: value.maxTokens ?? 256000 }),
    prepareCall: async value => {
      const frozen = Object.freeze({ ...value })
      preparations.push(frozen)
      return { config: frozen, systemPromptUpdate: update, stream: options => llm.stream(options) }
    },
    stream: options => ctx.waterfall('llm/stream', options, () => (async function* () {
      calls.push(options); providerCalls++
      assert.equal(Object.isFrozen(options), true)
      assert.equal(options.maxTokens, preparations.at(-1).maxTokens)
      assert.equal(options.maxTokens, session.requestHeader().config.maxTokens)
      if (provider) { yield* provider(options, calls.length); return }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 10 } }
      yield { type: 'finish', reason: retry && providerCalls === 1
        ? { kind: 'error', failure: { code: 'FIXTURE_RETRY', message: 'retry in memory' } } : { kind: 'stop' } }
    })()),
  }
  ctx.provide('llm', llm)
  Object.assign(agent, { loopCtx: ctx,
    dispatch: { waterfall: (name, payload, next) => ctx.waterfall(name, { ...payload, agent }, next), emit: () => {} },
    inbox: { claim: () => { const batch = queued; queued = []; return batch } },
    runtimeContext: new RuntimeContextProjection(ctx, session), systemPrompt: new SystemPromptProjection(session),
  })
  ctx.on('agent/request', async (_payload, next) => { requestsBeforeCommit.push(session.deriveMessages()); return next() })
  if (retry) ctx.on('agent/request-error', async () => ({ kind: 'retry' }))
  const service = installBudget(ctx, () => config, { journal })
  const preStep = async (messages, { turn = 1, step = 1, newSignal = false } = {}) => {
    agent.phase.turn = turn; agent.phase.step = step
    if (newSignal) agent.phase.abort = new AbortController()
    agent.runtimeContext = new RuntimeContextProjection(ctx, session)
    queued = messages
    return agent.preStep('next-step', { turn, step })
  }
  const run = async (messages, position) => {
    const decision = await preStep(messages, position)
    assert.equal(decision.kind, 'enter')
    await agent.step(decision)
    return decision
  }
  const request = (extra = {}) => ctx.waterfall('agent/request', { agent, signal: agent.phase.abort.signal,
    turn: agent.phase.turn, step: agent.phase.step, ...extra }, async () => agent.options)
  return { ctx, root, session, sessions, agent, config, journal, service, calls, preparations, requestsBeforeCommit, assembly, preStep, run, request, toolExecutions }
}

function productionShape() {
  const assembly = { sections: [{ name: 'system', text: 'S'.repeat(9500) }],
    contexts: [{ name: 'environment', text: 'E'.repeat(300) }], variables: {},
    tools: Array.from({ length: 35 }, (_, index) => ({ name: 'tool_' + index, description: 'T'.repeat(500), parameters: { type: 'object' } })) }
  const system = renderPrompt(assembly)
  const envelope = { system, messages: [], tools: assembly.tools }
  const characters = JSON.stringify(envelope).length
  assembly.tools[0].description += 'T'.repeat(14501 * 3 - characters)
  assert.equal(estimateRequestTokens(envelope).input, 14501)
  const skill = plugin('@deepseek-ai/dsh-skill', 'Skill catalog: ' + 'K'.repeat(1300))
  const context = createUserMessage({ content: [{ type: 'text', text: renderContextSnapshot(assembly) }],
    source: { kind: 'plugin', plugin: systemSource, form: 'snapshot', sections: renderContextSections(assembly) } })
  const projected = [createSystemMessage(system, systemSource), user(''), context, skill]
  const actualChars = JSON.stringify({ system: '', messages: projected, tools: assembly.tools }).length
  const task = user('P'.repeat(18598 * 3 - actualChars))
  assert.equal(estimateRequestTokens({ messages: [projected[0], task, context, skill], tools: assembly.tools }).input, 18598)
  return { assembly, task, skill }
}

test('native lifecycle prices all pending task/context/skill messages before freezing its 180k request', async () => {
  const shape = productionShape(), h = fixture({ assembly: shape.assembly })
  // A later pre-step hook replaces the message array with a skill contribution.
  h.ctx.on('agent/pre-step', async (_payload, next) => { const decision = await next(); return { ...decision, messages: [...decision.messages, shape.skill] } }, { prepend: true })
  await h.run([shape.task])
  assert.equal(h.requestsBeforeCommit[0].length, 0, 'request hook runs before inbox/system commit')
  assert.equal(h.calls.length, 1)
  const request = h.calls[0], input = estimateRequestTokens(request).input
  assert.ok(input > 18598, 'the native request now includes its stable budget policy and context notice')
  assert.equal(h.service.runtime.states.get(h.session.id).requestBudget.input_estimate, input)
  const reserve = h.service.runtime.states.get(h.session.id).requestBudget
  assert.ok(input + request.maxTokens + reserve.final_report_reserve + reserve.output_context_reserve <= 180000)
  assert.equal(request.system, undefined)
  assert.equal(request.messages.filter(message => message.role === 'user').length, 3)
  assert.equal((await h.service.diagnosticsForSession(h.session.id)).last_denial, null)
  h.service.shutdown()
})

test('native retry commits a pending batch once and does not price it a second time', async () => {
  const h = fixture({ retry: true })
  const task = user('Current task '.repeat(600))
  await h.run([task])
  assert.equal(h.calls.length, 2)
  for (const call of h.calls) assert.equal(call.messages.filter(message => message.id === task.id).length, 1)
  assert.equal(estimateRequestTokens(h.calls[0]).input, estimateRequestTokens(h.calls[1]).input)
  assert.equal(h.service.runtime.states.get(h.session.id).requestBudget.remaining_tokens, 180000 - 110, 'only prior observed usage is charged; no duplicate pending task')
  h.service.shutdown()
})

for (const update of ['in-history', 'replace']) for (const kind of ['same', 'changed', 'cleared']) {
  test('native ' + update + ' projection with ' + kind + ' system remains inside the grant', async () => {
    const h = fixture({ update })
    await h.run([user('first task')])
    h.assembly.sections[0].text = kind === 'same' ? h.assembly.sections[0].text : kind === 'changed' ? 'New instructions '.repeat(300) : ''
    await h.run([user('second task')], { step: 2 })
    assert.equal(h.calls.length, 2)
    const second = h.calls[1], input = estimateRequestTokens(second).input
    assert.ok(input + second.maxTokens <= 180000 - 110)
    const systemMessages = second.messages.filter(message => message.role === 'system')
    if (kind === 'same') assert.equal(systemMessages.length, 1)
    if (kind === 'changed' && update === 'in-history') assert.equal(systemMessages.length, 2)
    assert.equal((await h.service.diagnosticsForSession(h.session.id)).last_denial, null)
    h.service.shutdown()
  })
}

test('a pending batch larger than its grant is rejected before preparing or streaming a call', async () => {
  const h = fixture({ limit: 1000 })
  await assert.rejects(h.run([user('large task '.repeat(1000))]), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  assert.equal(h.calls.length, 0)
  assert.equal(h.preparations.length, 0)
  assert.equal(h.session.deriveMessages().length, 0)
  const diagnostic = await h.service.diagnosticsForSession(h.session.id)
  assert.equal(diagnostic.last_denial.stage, 'request_output_limit')
  assert.ok(diagnostic.last_denial.input_estimate > 1000)
  h.service.shutdown()
})

test('new step identity and cancellation signal never reuse an earlier pending batch', async () => {
  const seen = [], h = fixture({ meter: { measure: () => ({ totalTokens: 0 }),
    estimateMessage: message => { seen.push(message.id); return estimateRequestTokens({ messages: [message] }).input } } })
  const task = user('earlier batch '.repeat(300))
  await h.preStep([task])
  const pending = await h.request()
  seen.length = 0
  const nextStep = await h.request({ step: 2 })
  assert.equal(seen.includes(task.id), false, 'a new step cannot carry the preceding uncommitted batch')
  seen.length = 0
  const nextSignal = await h.request({ signal: new AbortController().signal })
  assert.equal(seen.includes(task.id), false, 'a new cancellation scope cannot carry that batch either')
  assert.ok(pending.maxTokens < nextStep.maxTokens && pending.maxTokens < nextSignal.maxTokens)
  h.service.shutdown()
})

test('a rejected subsequent pre-step clears the preceding accepted pending batch', async () => {
  const h = fixture()
  await h.preStep([user('earlier batch '.repeat(300))])
  const pending = await h.request()
  const remove = h.ctx.on('agent/pre-step', async () => ({ kind: 'reject' }))
  assert.equal((await h.preStep([])).kind, 'reject')
  assert.ok((await h.request()).maxTokens > pending.maxTokens)
  remove(); h.service.shutdown()
})

test('legacy header-system request pricing also includes its accepted pending messages', async () => {
  const h = fixture(), task = user('Legacy pending task '.repeat(200))
  const cached = compat.headerPricesSystem.cache
  try {
    compat.headerPricesSystem.cache = true
    const decision = await h.preStep([task])
    const bounded = await h.request()
    const request = { ...bounded, system: renderPrompt(decision.assembly), messages: decision.messages, tools: decision.assembly.tools }
    assert.equal(h.service.runtime.states.get(h.session.id).requestBudget.input_estimate, estimateRequestTokens(request).input)
    assert.ok(request.maxTokens + estimateRequestTokens(request).input < 180000, 'normal work preserves the report reserve')
    const state = h.service.runtime.states.get(h.session.id)
    assert.ok(await h.service.runtime.admit(state, request))
  } finally { compat.headerPricesSystem.cache = cached; h.service.shutdown() }
})


test('native request adds accepted pending messages to its tokenizer measurement', async () => {
  const seen = [], meter = { measure: () => ({ totalTokens: 100 }),
    estimateMessage: message => { seen.push(message); return 5000 } }
  const h = fixture({ meter }), task = user('pending task')
  await h.run([task])
  assert.equal(h.service.runtime.states.get(h.session.id).requestBudget.input_estimate, 15100, '100 retained/tool tokens plus system, pending task and budget runtime context')
  assert.ok(seen.some(message => message.id === task.id))
  h.service.shutdown()
})

test('committed messages removed by a surface replacement are not reintroduced on retry', async () => {
  const h = fixture(), task = user('task before compaction '.repeat(500))
  await h.preStep([task])
  const original = h.session.append('user/message', task, { surfaceOp: 'append' })
  h.session.append('user/message', plugin('fixture-cm', 'short summary'), {
    surfaceOp: { op: 'replace', startSeq: original.seq, endSeq: original.seq }, sourceEventSeqs: [original.seq],
  })
  const bounded = await h.request()
  const decision = await h.preStep([], { step: 2 })
  await h.request()
  const messages = [...h.session.deriveMessages(), ...decision.messages, createSystemMessage(renderPrompt(decision.assembly), systemSource)]
  assert.equal(h.service.runtime.states.get(h.session.id).requestBudget.input_estimate, estimateRequestTokens({ messages, tools: [] }).input)
  assert.equal(h.session.deriveMessages().some(message => message.id === task.id), false)
  h.service.shutdown()
})

test('cold restoration prices committed history without a stale pending batch', async () => {
  const seen = [], h = fixture({ meter: { measure: () => ({ totalTokens: 0 }),
    estimateMessage: message => { seen.push(message.id); return estimateRequestTokens({ messages: [message] }).input } } }), task = user('restored task '.repeat(200))
  await h.preStep([task])
  h.session.append('user/message', task, { surfaceOp: 'append' })
  const before = await h.request()
  h.service.shutdown()
  const restored = installBudget(h.ctx, () => h.config, { journal: h.journal })
  try {
    seen.length = 0
    const after = await h.request()
    assert.equal(seen.includes(task.id), false, 'the durable task is measured through retained history, never duplicated as pending')
    assert.equal(h.session.deriveMessages().filter(message => message.id === task.id).length, 1)
    assert.ok(after.maxTokens > 0 && before.maxTokens > 0)
    assert.equal(restored.runtime.states.get(h.session.id).restored, true)
  } finally { restored.shutdown() }
})

test('later prepend replacement hooks run once per step and budget listeners fully detach', async () => {
  const h = fixture(), notices = [], snapshots = [], countListeners = () => h.ctx.events.dispatch('waterfall',
    ['agent/pre-step', { agent: h.agent, messages: [], turn: 1, step: 1, signal: h.agent.phase.abort.signal }, async () => ({ kind: 'reject' })]).length
  const remove = h.ctx.on('agent/pre-step', async (_payload, next) => {
    const decision = await next(), notice = plugin('fixture-late-notice', 'N'.repeat(2000))
    notices.push(notice); return { ...decision, messages: [...decision.messages, notice] }
  }, { prepend: true })
  for (let step = 1; step <= 3; step++) {
    snapshots.push(countListeners())
    await h.run([user('step ' + step)], { step })
  }
  assert.deepEqual(snapshots, [2, 2, 2])
  assert.equal(notices.length, 3)
  for (let i = 0; i < h.calls.length; i++) for (const notice of notices.slice(0, i + 1)) {
    assert.equal(h.calls[i].messages.filter(message => message.id === notice.id).length, 1)
  }
  h.service.shutdown()
  assert.equal(countListeners(), 1)
  assert.equal(countListeners(), 1, 'shutdown also removes the dispatch rebinder')
  remove()
  assert.equal(countListeners(), 0)
})

test('a later prepend hook cannot add an unaffordable batch after budget sizing', async () => {
  const h = fixture({ limit: 1000 })
  h.ctx.on('agent/pre-step', async (_payload, next) => {
    const decision = await next(); return { ...decision, messages: [...decision.messages, plugin('fixture-late', 'L'.repeat(10000))] }
  }, { prepend: true })
  await assert.rejects(h.run([user('small original task')]), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  assert.equal(h.preparations.length, 0)
  assert.equal(h.calls.length, 0)
  h.service.shutdown()
})

test('concurrent agents keep independent accepted batches through the shared dispatcher', async () => {
  const h = fixture(), session = makeSession('native-budget-sibling', { parentSession: h.root.id, origin: 'subagent', delegationDepth: 1 })
  h.sessions.set(session.id, session)
  const siblingTask = user('sibling task '.repeat(900))
  const sibling = Object.assign(Object.create(ReactLoopAgent.prototype), h.agent, {
    id: session.id, session, phase: { kind: 'running', turn: 1, step: 1, abort: new AbortController() },
    inbox: { claim: () => [siblingTask] }, runtimeContext: new RuntimeContextProjection(h.ctx, session), systemPrompt: new SystemPromptProjection(session),
  })
  sibling.dispatch = { waterfall: (name, payload, next) => h.ctx.waterfall(name, { ...payload, agent: sibling }, next), emit: () => {} }
  const mainTask = user('main task')
  await Promise.all([h.preStep([mainTask]), sibling.preStep('next-step', { turn: 1, step: 1 })])
  const [main, other] = await Promise.all([h.request(), h.ctx.waterfall('agent/request', {
    agent: sibling, turn: 1, step: 1, signal: sibling.phase.abort.signal,
  }, async () => sibling.options)])
  const mainInput = h.service.runtime.states.get(h.session.id).requestBudget.input_estimate
  const otherInput = h.service.runtime.states.get(session.id).requestBudget.input_estimate
  const taskDifference = (JSON.stringify(siblingTask).length - JSON.stringify(mainTask).length) / 3
  assert.ok(Math.abs(otherInput - mainInput - taskDifference) <= 1, 'same envelope; only the independently pending task differs')
  h.service.shutdown()
})

test('an otherwise empty worker system receives the stable conditional budget policy', async () => {
  const h = fixture({ assembly: { sections: [], contexts: [], variables: {}, tools: [] } })
  await h.run([user('task with no system text')])
  assert.ok(h.calls[0].messages.some(message => message.role === 'system' && JSON.stringify(message).includes('DPSWARM_WORKER_BUDGET_POLICY')))
  assert.ok(h.calls[0].maxTokens + estimateRequestTokens(h.calls[0]).input <= 180000)
  h.service.shutdown()
})


for (const update of ['in-history', 'replace']) test('native ' + update + ' closeout keeps tool schemas visible and sends one report with its real envelope', async () => {
  const shape = productionShape(), limit = 60000
  const h = fixture({ limit, callLimit: 1, assembly: shape.assembly, update, nativeTools: true,
    provider: async function* (options) {
      // Schemas stay listed at closeout: the model can answer with plain text
      // directly, which is the common case — no refusal round is spent.
      assert.equal(options.tools?.length, 35)
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Final report: no file was saved; the task remains incomplete.' } }
      yield { type: 'usage', usage: { inputTokens: estimateRequestTokens(options).input, outputTokens: 100 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } })
  try {
    await h.run([shape.task])
    const first = h.calls[0], state = h.service.runtime.states.get(h.session.id)
    assert.equal(state.closeout.mode, 'final_only')
    assert.equal(first.tools?.length, 35)
    assert.ok(state.closeout.final_input_estimate >= estimateRequestTokens(first).input)
    // No extra recovery reserve: the slack and report floor cushion one refused
    // attempt; a tighter grant must not be parked at arrival.
    assert.equal(state.closeout.recovery_call_reserve, 0)
    assert.deepEqual(state.profile, { mode: 'manual', tokenLimit: limit, callLimit: 1 })
    assert.equal(h.toolExecutions.length, 0)
    assert.equal(h.session.snapshotEvents().some(event => event.type === 'tool/result'), false, 'no paid refusal round needed')
    assert.ok(h.session.deriveMessages().some(message => message.content.some(block => block.type === 'text' && block.text.startsWith('Final report:'))))
    const after = await h.service.diagnosticsForSession(h.session.id)
    assert.equal(after.calls, 1); assert.equal(after.unknown_usage_calls, 0)
    assert.ok(after.committed_tokens < limit); assert.equal(after.last_denial, null)
    await assert.rejects(h.run([], { step: 2 }), { code: 'WORKER_CALL_LIMIT_REACHED' })
    assert.equal(h.calls.length, 1)
  } finally { h.service.shutdown() }
})

test('native closeout turns a tool attempt into a refusal result, then accepts the plain-text report', async () => {
  const shape = productionShape()
  // 55,000 is below the park threshold (56,368) for this shape: the worker is
  // parked by the token rail at arrival with all 10 calls still available.
  const h = fixture({ limit: 55000, callLimit: 10, assembly: shape.assembly, nativeTools: true,
    provider: async function* (options, ordinal) {
      assert.equal(options.tools?.length, 35, 'schemas stay listed through the whole closeout')
      if (ordinal === 1) {
        // The parked worker tries one more real tool call instead of reporting.
        yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'closeout-attempt', name: 'tool_0', arguments: '{}' } }
      } else {
        assert.equal(ordinal, 2, 'one attempt plus one report, no hidden extra call')
        const refusal = options.messages.flatMap(message => message.content || [])
          .find(block => block.type === 'tool-result' && block.toolCallId === 'closeout-attempt')
        assert.ok(refusal, 'the refused attempt is visible in the report input')
        assert.equal(refusal.isError, true)
        assert.match(JSON.stringify(refusal), /WORKER_CLOSEOUT_FINAL_ONLY/)
        yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Final report: saved nothing; verification incomplete.' } }
      }
      yield { type: 'usage', usage: { inputTokens: estimateRequestTokens(options).input, outputTokens: 50 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } })
  try {
    await h.run([shape.task])
    assert.equal(h.toolExecutions.length, 0, 'the tool gate denied execution')
    await h.run([], { step: 2 })
    assert.equal(h.calls.length, 2)
    const d = await h.service.diagnosticsForSession(h.session.id)
    assert.equal(d.closeout.mode, 'final_only')
    assert.equal(d.calls, 2)
    assert.equal(d.last_denial, null)
    assert.ok(d.committed_tokens < d.tokenLimit)
    assert.ok(h.session.deriveMessages().some(message => message.content.some(block => block.type === 'text' && block.text.startsWith('Final report:'))))
    // The pair exhausted the visible-tool closeout: a third call is refused —
    // ALREADY_SENT at admission when tokens still fit, or an honest
    // WORKER_TOKEN_RESERVATION_DENIED at sizing once the pair consumed the rest.
    await assert.rejects(h.run([], { step: 3 }))
    assert.equal(h.calls.length, 2)
  } finally { h.service.shutdown() }
})

for (const update of ['in-history', 'replace']) test('native ' + update + ' transport retry reforecasts unknown reservation into a plain-text report', async () => {
  const h = fixture({ limit: 250000, retry: true, update,
    assembly: { sections: [{ name: 'system', text: 'A bounded worker.' }], contexts: [], variables: {},
      tools: [{ name: 'tool_0', description: 'T'.repeat(60000), parameters: { type: 'object' } }] },
    provider: async function* (options, ordinal) {
      if (ordinal === 1) {
        assert.equal(options.tools.length, 1)
        yield { type: 'finish', reason: { kind: 'error', failure: { code: 'TRANSPORT', message: 'fixture transport failure without usage' } } }
        return
      }
      assert.equal(options.tools.length, 1, 'retry reforecasts after unknown attempt; schemas stay visible, execution alone is refused')
      assert.ok(options.messages.some(message => message.role === 'system' && JSON.stringify(message).includes('DPSWARM_WORKER_BUDGET_POLICY')))
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Final report: transport failed; no verification was completed.' } }
      yield { type: 'usage', usage: { inputTokens: estimateRequestTokens(options).input, outputTokens: 100 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } })
  try {
    await h.run([user('Current bounded task. ' + 'U'.repeat(150000))])
    assert.equal(h.calls.length, 2)
    const d = await h.service.diagnosticsForSession(h.session.id)
    assert.equal(d.closeout.mode, 'final_only')
    assert.equal(d.unknown_usage_calls, 1)
    const unknown = d.recent.find(call => !call.usage_complete)
    assert.equal(unknown.failure, 'TRANSPORT')
    assert.equal(d.committed_tokens, unknown.reserved_tokens + d.observed_tokens_lower_bound)
    assert.equal(d.last_denial, null)
    assert.ok(d.committed_tokens < d.tokenLimit)
    assert.ok(h.session.snapshotEvents().some(event => event.type === 'assistant/attempt'))
    assert.ok(h.session.deriveMessages().some(message => message.content.some(block => block.type === 'text' && block.text.startsWith('Final report:'))))
  } finally { h.service.shutdown() }
})


test('authenticated report repair reads UTF16 evidence then finishes in its original 211353-token remainder', async () => {
  const { readEvidence } = await import(new URL('evidence-reader.js', lib))
  const bytes = Buffer.concat([Buffer.from([255, 254]), Buffer.from(JSON.stringify({ result: { value: 37, verified: false } }), 'utf16le')])
  const reads = [], evidenceFs = { fs: {
    resolve: async path => { assert.equal(path, 'saved-result.json'); return { displayPath: path } },
    stat: async () => ({ type: 'file', size: bytes.length }),
    readBytes: async () => { reads.push('read'); return bytes },
  } }
  const tool = { name: 'dpswarm_read_evidence', description: 'Read saved JSON evidence.', parameters: { file_path: { type: 'string' }, pointer: { type: 'string' } },
    execute: (args, exec) => readEvidence(evidenceFs, args, exec) }
  const h = fixture({ limit: 1200000, callLimit: 24, nativeTools: true, nativeTool: tool,
    assembly: { sections: [{ name: 'system', text: 'Read the saved evidence and report only what it supports.' }], contexts: [], variables: {}, tools: [{ name: tool.name, description: tool.description, parameters: { type: 'object', properties: tool.parameters } }] },
    provider: async function* (options, ordinal) {
      if (ordinal === 1) {
        assert.equal(options.tools[0].name, tool.name)
        yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'fixture-evidence-read', name: tool.name, arguments: JSON.stringify({ file_path: 'saved-result.json', pointer: '/result' }) } }
      } else {
        assert.equal(ordinal, 2, 'no hidden request or extra worker allocation')
        assert.equal(options.tools?.length, 1, 'the closeout report request keeps the evidence tool schema visible')
        const result = options.messages.flatMap(message => message.content).find(block => block.type === 'tool-result' && block.toolCallId === 'fixture-evidence-read')
        assert.ok(result, 'native tool result is in the final report input')
        assert.match(JSON.stringify(result), /observed-data-not-a-verdict/)
        assert.match(JSON.stringify(result), /37/)
        assert.equal(reads.length, 1)
        yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Read-back report: saved result value is 37; verification remains incomplete.' } }
      }
      yield { type: 'usage', usage: { inputTokens: estimateRequestTokens(options).input, outputTokens: 100 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } })
  try {
    const lead = { session: h.root }, r = h.service.runtime
    const handle = await h.service.beginTeamRun(lead, { roles: ['reviewer'] })
    const original = await h.service.issueTeamWorker(lead, handle, { label: 'reviewer', task: 'Original independent reviewer.' })
    const source = makeSession('original-report-reviewer', { parentSession: h.root.id, origin: 'subagent', delegationDepth: 1 })
    source.append('user/message', user(original.prompt), { surfaceOp: 'append' }); h.sessions.set(source.id, source)
    const sourceState = await h.service.ensure({ session: source })
    for (let i = 0; i < 15; i++) await r.settle(await r.admit(sourceState, { provider: 'fixture-provider', model: 'fixture-model', messages: [], maxTokens: 100 }), { inputTokens: i < 14 ? 65000 : 78647, outputTokens: 0 }, 'stop')
    await h.service.finishTeamRun(lead, handle)
    source.append('turn/end', { turn: 1, reason: { kind: 'completed' } })
    const grant = await h.service.issueReportRepair(lead, { workerSessionId: source.id, task: 'Read saved-result.json and complete the honest report. Retained task context: ' + 'C'.repeat(190000) })
    assert.deepEqual(grant.profile, { mode: 'fixed', tokenLimit: 211353, callLimit: 9 })
    await h.run([user(grant.prompt)])
    assert.equal(h.toolExecutions.length, 1)
    await h.run([], { step: 2 })
    const result = await h.service.diagnosticsForSession(h.session.id)
    assert.equal(result.policy_binding.source_worker_session_id, source.id)
    assert.equal(result.closeout.mode, 'final_only'); assert.equal(result.calls, 2)
    assert.equal(result.unknown_usage_calls, 0); assert.equal(result.last_denial, null)
    assert.ok(result.committed_tokens <= 211353)
    assert.ok(r.describe(sourceState).committed_tokens + result.committed_tokens <= 1200000)
    assert.equal(r.describe(sourceState).committed_tokens, 988647)
    assert.ok(h.session.deriveMessages().some(message => message.content.some(block => block.type === 'text' && block.text.startsWith('Read-back report:'))))
  } finally { h.service.shutdown() }
})


for (const update of ['in-history', 'replace']) test('native ' + update + ' high-meter transport reservation survives retry and cold replay without losing its input bound', async () => {
  const meter = { measure: session => ({ totalTokens: session.id === 'native-budget-child' ? 50000 : 0 }),
    estimateMessage: message => estimateRequestTokens({ messages: [message] }).input }
  const h = fixture({ limit: 250000, retry: true, update, meter,
    assembly: { sections: [{ name: 'system', text: 'Use the frozen input estimate.' }], contexts: [], variables: {}, tools: [{ name: 'tool_0', description: 'A work tool.', parameters: {} }] },
    provider: async function* (options, ordinal) {
      assert.ok(estimateRequestTokens(options).input < 50000)
      if (ordinal === 1) {
        yield { type: 'finish', reason: { kind: 'error', failure: { code: 'TRANSPORT', message: 'No usage was returned.' } } }
        return
      }
      assert.equal(ordinal, 2); assert.equal(options.tools?.length, 1, 'schemas stay visible after the transport retry reforecast')
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Final report: transport failed; evidence is incomplete.' } }
      yield { type: 'usage', usage: { inputTokens: 50000, outputTokens: 100 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    } })
  try {
    await h.run([user('Summarize saved evidence.')])
    const d = await h.service.diagnosticsForSession(h.session.id), unknown = d.recent.find(call => !call.usage_complete)
    assert.equal(unknown.input_estimate, 50000)
    assert.equal(unknown.reserved_tokens, 50000 + h.calls[0].maxTokens, 'unknown reservation keeps the trusted meter input, not chars/3')
    assert.equal(d.committed_tokens, unknown.reserved_tokens + 50100)
    assert.equal(d.unknown_usage_calls, 1); assert.equal(d.calls, 2); assert.equal(d.last_denial, null)
    assert.ok(d.committed_tokens <= 250000)
    const Runtime = h.service.runtime.constructor, cold = new Runtime({ config: () => h.config,
      resolveSession: id => h.sessions.get(id), listSessions: () => [...h.sessions.values()], journal: h.journal })
    try {
      const restored = await cold.ensure({ session: h.session }), after = cold.describe(restored)
      assert.equal(after.committed_tokens, d.committed_tokens); assert.equal(after.unknown_usage_calls, 1)
      assert.equal(after.recent.find(call => !call.usage_complete).input_estimate, 50000)
    } finally { cold.shutdown() }
  } finally { h.service.shutdown() }
})

test('detached CM meters its own frozen messages without borrowing the active worker estimate', async () => {
  const meter = { measure: session => ({ totalTokens: session.id === 'native-budget-child' ? 50000 : 0 }),
    estimateMessage: message => estimateRequestTokens({ messages: [message] }).input }
  let cm
  const h = fixture({ limit: 250000, meter, provider: async function* () {
    cm = Object.freeze({ sessionId: h.session.id, provider: 'fixture-provider', model: 'fixture-model', purpose: 'compaction',
      signal: h.agent.phase.abort.signal, messages: Object.freeze([user('Small detached CM input.')]), tools: [], maxTokens: 100 })
    const stream = h.ctx.waterfall('llm/stream', cm, () => (async function* () {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'TRANSPORT', message: 'Unknown detached CM attempt.' } } }
    })())
    for await (const _ of stream) {}
    yield { type: 'usage', usage: { inputTokens: 50000, outputTokens: 10 } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  } })
  try {
    await h.run([user('Current worker task.')])
    const d = await h.service.diagnosticsForSession(h.session.id), call = d.recent.find(value => value.purpose === 'compaction')
    assert.ok(call.input_estimate >= estimateRequestTokens(cm).input)
    assert.ok(call.input_estimate < 1000, 'a different frozen surface cannot reuse the 50000 worker estimate')
    assert.equal(call.reserved_tokens, call.input_estimate + 100)
    assert.equal(d.committed_tokens, 50010 + call.reserved_tokens)
    assert.equal(d.unknown_usage_calls, 1)
  } finally { h.service.shutdown() }
})
