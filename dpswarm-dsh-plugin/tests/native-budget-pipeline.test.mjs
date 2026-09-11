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
const { createUserMessage, createSystemMessage } = llmTypes
const user = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
const plugin = (name, text) => createUserMessage({ source: { kind: 'plugin', plugin: name }, content: [{ type: 'text', text }] })
const makeSession = (id, extra = {}) => Session.create(id, undefined, { version: 3, id, createdAt: 1, isSeeded: false, ...extra })
const systemSource = '@deepseek-ai/dsh-system-prompt'

function fixture({ limit = 180000, assembly = { sections: [{ name: 'system', text: 'You are a worker.' }], contexts: [], variables: {}, tools: [] },
  update = 'in-history', retry = false, meter } = {}) {
  const ctx = new Context(), root = makeSession('native-budget-root')
  const session = makeSession('native-budget-child', { parentSession: root.id, origin: 'subagent', delegationDepth: 1 })
  const sessions = new Map([root, session].map(value => [value.id, value]))
  const journal = new MemoryAuditJournal(), calls = [], preparations = [], requestsBeforeCommit = [], guards = []
  const config = { workerBudgetMode: 'manual', workerTokenLimit: limit, workerCallLimit: 10 }
  let queued = [], providerCalls = 0
  const agent = Object.assign(Object.create(ReactLoopAgent.prototype), {
    id: session.id, session, options: { provider: 'fixture-provider', model: 'fixture-model', maxTokens: 256000 },
    phase: { kind: 'running', turn: 1, step: 1, abort: new AbortController() },
    requestHeaderLogged: false, requestSurfaceGeneration: session.surface.replaceGeneration,
    frozenMessages: new WeakSet(), assistantStreamRevision: 0, assistantAttemptCounter: 0,
  })
  ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()] })
  ctx.provide('agents', { get: id => id === session.id ? agent : undefined })
  ctx.provide('tools', { guard: guard => { guards.push(guard); return () => {} } })
  if (meter) ctx.provide('tokenMeter', meter)
  ctx.provide('systemPrompt', { assemble: async context => {
    const current = structuredClone(assembly)
    return ctx.waterfall('system-prompt/assemble', current, context, async () => current)
  } })
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
  return { ctx, root, session, sessions, agent, config, journal, service, calls, preparations, requestsBeforeCommit, assembly, preStep, run, request }
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
  assert.equal(input, 18598)
  assert.equal(request.maxTokens, 180000 - 18598)
  assert.equal(input + request.maxTokens, 180000)
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
  assert.equal(h.calls[0].maxTokens - h.calls[1].maxTokens, 110, 'only prior observed usage is charged')
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
  const h = fixture()
  await h.preStep([user('earlier batch '.repeat(300))])
  const pending = await h.request()
  const nextStep = await h.request({ step: 2 })
  const nextSignal = await h.request({ signal: new AbortController().signal })
  assert.ok(pending.maxTokens < nextStep.maxTokens)
  assert.equal(nextStep.maxTokens, nextSignal.maxTokens)
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
    assert.equal(request.maxTokens + estimateRequestTokens(request).input, 180000)
    const state = h.service.runtime.states.get(h.session.id)
    assert.ok(await h.service.runtime.admit(state, request))
  } finally { compat.headerPricesSystem.cache = cached; h.service.shutdown() }
})


test('native request adds accepted pending messages to its tokenizer measurement', async () => {
  const seen = [], meter = { measure: () => ({ totalTokens: 100 }),
    estimateMessage: message => { seen.push(message); return 5000 } }
  const h = fixture({ meter }), task = user('pending task')
  await h.run([task])
  assert.equal(h.calls[0].maxTokens, 180000 - 10100, '100 retained/tool tokens plus system and pending task')
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
  const messages = [...h.session.deriveMessages(), createSystemMessage(renderPrompt(h.assembly), systemSource)]
  assert.equal(180000 - bounded.maxTokens, estimateRequestTokens({ messages, tools: [] }).input)
  assert.equal(h.session.deriveMessages().some(message => message.id === task.id), false)
  h.service.shutdown()
})

test('cold restoration prices committed history without a stale pending batch', async () => {
  const h = fixture(), task = user('restored task '.repeat(200))
  await h.preStep([task])
  h.session.append('user/message', task, { surfaceOp: 'append' })
  const before = await h.request()
  h.service.shutdown()
  const restored = installBudget(h.ctx, () => h.config, { journal: h.journal })
  try {
    const after = await h.request()
    assert.equal(after.maxTokens, before.maxTokens)
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
  const prompt = createSystemMessage(renderPrompt(h.assembly), systemSource)
  assert.equal(180000 - main.maxTokens, estimateRequestTokens({ messages: [prompt, mainTask], tools: [] }).input)
  assert.equal(180000 - other.maxTokens, estimateRequestTokens({ messages: [prompt, siblingTask], tools: [] }).input)
  h.service.shutdown()
})

test('an empty first system remains conservatively bounded when native messages omit the empty node', async () => {
  const h = fixture({ assembly: { sections: [], contexts: [], variables: {}, tools: [] } })
  await h.run([user('task with no system text')])
  assert.ok(h.session.snapshotEvents().some(event => event.type === 'system/message' && event.data.message.content.length === 0))
  assert.equal(h.calls[0].messages.some(message => message.role === 'system'), false)
  assert.ok(h.calls[0].maxTokens + estimateRequestTokens(h.calls[0]).input <= 180000)
  h.service.shutdown()
})
