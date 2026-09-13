import { WorkerBudgetRuntime, budgetError, estimateRequestTokens, isWorkerSession } from './budget-runtime.js'
import { CLOSEOUT_INSTRUCTION, BUDGET_POLICY_INSTRUCTION, requestSystemText } from './worker-closeout.js'
import { getOptionalHostService } from './host-services.js'
import { AuditJournal } from './audit.js'
import { resolveChildRoute } from './lead-route.js'
import { readModelLimits } from './host-model-registry.js'
import { sessionEvents, measureSystemRequest, headerPricesSystem } from './host-session-compat.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'

const host = resolveHostRoot()
const [{ assembleContextFor }, { renderPrompt, renderContextSnapshot, renderContextSections }, { canonicalHeader }, { createSystemMessage, createUserMessage }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-agent/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-session/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
])

const CLOSEOUT_TOOL_REFUSAL = 'WORKER_CLOSEOUT_FINAL_ONLY: budget rail reached and tool calls are disabled. Write the final report now as plain text: what is complete, saved file paths, what remains.'

export function installBudget(ctx, configGetter, { journal = new AuditJournal({ config: configGetter }) } = {}) {
  const get = name => getOptionalHostService(ctx, name)
  const runtime = new WorkerBudgetRuntime({
    config: configGetter,
    resolveSession: id => get('sessions')?.get(id),
    listSessions: () => get('sessions')?.list() || [],
    journal,
  })
  const agents = new Map()
  const ensure = (agent, signal, incoming = []) => {
    agents.set(agent.session.id, agent)
    return runtime.ensure(agent, signal, incoming)
  }
  const dispose = [], assemblies = new WeakMap(), pendingSteps = new WeakMap()
  // A DP role route is durable and immutable; cache the resolution per session
  // object so cold-restored children do not re-read the whole audit ledger on
  // every pre-step estimate. Non-DP children deterministically resolve to null.
  const routeCache = new WeakMap()
  // Measured per-route single-request output caps for providers whose gateway
  // rejects values below what any registry field reports. The measured value
  // participates in the minimum together with registry fields; routes with no
  // data at all stay unclamped rather than guessing.
  // glmcp: its gateway validates max_tokens at the parameter level; measured
  // 2026-09-13 from the gateway's own rejection (live sessions 88122af1/e3589816):
  // 400 {"code":"1210","message":"max_tokens参数非法：限制数值范围[1,131072]"}
  // while the host registry reports 262144 (live 03e8db1b).
  const FALLBACK_OUTPUT_CAPS = { glmcp: 131072 }
  const fallbackOutputCap = (provider, model) => {
    const value = FALLBACK_OUTPUT_CAPS[`${provider}/${model}`] ?? FALLBACK_OUTPUT_CAPS[provider]
    return Number.isSafeInteger(value) && value > 0 ? value : null
  }
  // The rail shapes output allowances from the cumulative grant; a route may
  // reject a max_tokens above its own cap even when budget remains (glm 131072
  // vs a 600k manual rail). All available bounds participate as a minimum: the
  // measured fallback, the host's per-request output default, and the context
  // window (output beyond the window can never fit). A gateway's parameter
  // ceiling can sit below every registry field (live 03e8db1b: glmcp registry
  // 262144 vs gateway 131072), so clamping too tightly is safe while clamping
  // too loosely is a guaranteed provider rejection. Only a successful
  // authoritative lookup is cached; unknown routes stay unclamped.
  const routeCaps = new Map()
  const routeOutputCap = async (native, signal) => {
    const provider = native?.provider, model = native?.model
    if (typeof provider !== 'string' || !provider.trim() || typeof model !== 'string' || !model.trim()) return null
    const key = JSON.stringify([provider, model])
    if (routeCaps.has(key)) return routeCaps.get(key)
    let cap = null, resolved = false
    try {
      const llm = get('llm')
      if (typeof llm?.resolveModelInfo === 'function') {
        const info = await llm.resolveModelInfo(provider, model, signal)
        if (info?.provider === provider && info?.id === model) {
          const limits = readModelLimits(info)
          const bounds = [fallbackOutputCap(provider, model), limits.default_max_tokens, limits.context_window].filter(value => value !== null)
          cap = bounds.length ? Math.min(...bounds) : null
          resolved = true
        }
      }
    } catch (error) {
      if (signal?.aborted) throw error
    }
    if (resolved) {
      if (routeCaps.size >= 64) routeCaps.clear()
      routeCaps.set(key, cap)
    }
    return cap
  }
  const limited = state => state && state.profile.mode !== 'unlimited'
  const applyCloseout = (state, assembly) => {
    if (!state?.closeout || !assembly) return
    // Tool schemas stay listed so the model keeps its native call→refusal→report
    // pattern; the tool gate denies execution with the report instruction. A
    // tools-stripped closeout made DeepSeek-class models emit DSML tool-call
    // markup as plain text (0.8.3; again all three roles in the 0.14.1 live run).
    if (!Array.isArray(assembly.sections) || Object.isFrozen(assembly.sections)) throw budgetError('WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
    if (!assembly.sections.some(s => s.name === 'dpswarm-worker-closeout')) assembly.sections.push({ name: 'dpswarm-worker-closeout', text: CLOSEOUT_INSTRUCTION })
  }
  dispose.push(ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next(), agent = context?.agent
    if (agent?.session && isWorkerSession(agent.session)) {
      assemblies.set(agent.session, { assembly, signal: context.signal })
      const state = runtime.states.get(agent.session.id)
      if (state ? limited(state) : (configGetter().workerBudgetMode ?? 'unlimited') !== 'unlimited') {
        if (!Array.isArray(assembly.sections) || Object.isFrozen(assembly.sections)) throw budgetError('WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
        if (!assembly.sections.some(s => s.name === 'dpswarm-worker-budget-policy')) assembly.sections.push({ name: 'dpswarm-worker-budget-policy', text: BUDGET_POLICY_INSTRUCTION })
        if (!Array.isArray(assembly.contexts) || Object.isFrozen(assembly.contexts)) throw budgetError('WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
        const advice = state ? runtime.describe(state).next_step : { mode: 'awaiting_first_estimate',
          guidance: 'Your frozen grant is checked before dispatch. Each call resends full input; preserve room to read evidence and report.' }
        const item = { name: 'dpswarm-worker-budget', text: JSON.stringify(advice) }
        const at = assembly.contexts.findIndex(value => value.name === item.name)
        if (at < 0) assembly.contexts.push(item)
        else assembly.contexts[at] = item
      }
      applyCloseout(state, assembly)
    }
    return assembly
  }, { prepend: true, global: true }))
  // Accepted messages are committed after agent/request on the native loop.
  // Keep the decision (not a copied array) so later in-place edits are visible.
  // Retried attempts have already committed this batch, including messages a
  // compactor subsequently removed from the surface; do not add those again.
  const pendingMessages = (session, { signal, turn, step }) => {
    const saved = pendingSteps.get(session)
    if (!saved || saved.signal !== signal || (turn !== undefined && saved.turn !== turn)
      || (step !== undefined && saved.step !== step)) return []
    const committed = sessionEvents(session).filter(event => event.type === 'user/message').map(event => event.data)
    const visible = session.deriveMessages?.() || []
    const ids = new Set([...committed, ...visible].map(message => message.id).filter(Boolean))
    const identities = new Set([...committed, ...visible])
    return (saved.decision.messages || []).filter(message => message.id ? !ids.has(message.id) : !identities.has(message))
  }
  const pendingTokens = (meter, messages) => messages.reduce((total, message) =>
    total + (meter?.estimateMessage?.(message) ?? estimateRequestTokens({ messages: [message] }).input), 0)
  const serializedInput = (messages, system, tools) => {
    if (headerPricesSystem(canonicalHeader)) return estimateRequestTokens({ messages, system, tools }).input
    // New hosts serialize identified system messages, not a separate system
    // string. Price the actual wrapper too. Before prepareCall its projection
    // mode is unknown: cover both normalization and in-history append, leaving
    // all retained history in place. This changes no live surface or request.
    const systemIndices = messages.flatMap((message, index) => message.role === 'system' ? [index] : [])
    const text = message => (message.content || []).filter(block => block.type === 'text').map(block => block.text).join('\n')
    const projected = () => createSystemMessage(system, '@deepseek-ai/dsh-system-prompt')
    if (!systemIndices.length) return estimateRequestTokens({ messages: [...messages, projected()], tools }).input
    const lastText = systemIndices.map(index => text(messages[index])).findLast(value => value !== '') || ''
    const appended = system && lastText !== system ? [...messages, projected()] : messages
    const normalized = [...messages]
    for (const [position, index] of systemIndices.entries()) {
      const wanted = position === 0 ? system : ''
      if (text(messages[index]) !== wanted) normalized[index] = createSystemMessage(wanted, '@deepseek-ai/dsh-system-prompt')
    }
    return Math.max(estimateRequestTokens({ messages: appended, tools }).input,
      estimateRequestTokens({ messages: normalized, tools }).input)
  }
  const meteredInput = async (agent, { system, tools, pending, signal, config: preparedConfig }) => {
    const meter = get('tokenMeter')
    if (typeof meter?.measure !== 'function') return null
    // The meter prices the retained surface and header with the real tokenizer;
    // chars/3 skews 2-3x on CJK and ~1.3x on ASCII (911 tester closeout). A
    // measurement failure must never break a worker over an advisory estimate.
    try {
      if (!routeCache.has(agent.session)) {
        routeCache.set(agent.session, await resolveChildRoute(agent, { journal, signal }).catch(() => null))
      }
      const frozen = routeCache.get(agent.session)
      const config = preparedConfig || frozen || agent.session.requestHeader?.()?.config
        || { provider: agent.options?.provider, model: agent.options?.model }
      if (!['provider', 'model'].every(key => typeof config?.[key] === 'string' && config[key].trim())) return null
      const envelope = { config, ...(system ? { system } : {}), ...(Array.isArray(tools) && tools.length ? { tools } : {}) }
      const measured = measureSystemRequest(meter, agent.session, envelope, system, canonicalHeader)?.totalTokens
      if (!Number.isSafeInteger(measured) || measured < 0) return null
      return measured + pendingTokens(meter, pending || [])
    } catch { return null }
  }
  // Both forecasts and request admission price the same tool envelope and the
  // host's possible system projections. The final frozen stream is still
  // checked independently by runtime.admit; an estimate never grants a call.
  const estimateInput = async (agent, { messages, system, tools, pending, signal, config }) => {
    const metered = await meteredInput(agent, { system, tools, pending, signal, config })
    return Math.max(metered ?? 0, serializedInput(messages, system, tools))
  }
  const frozenInputEstimate = (agent, options) => {
    const serialized = estimateRequestTokens(options).input, meter = get('tokenMeter')
    if (typeof meter?.measure !== 'function') return serialized
    try {
      const messages = options.messages || [], current = agent.session.deriveMessages?.()
      const header = { config: options, ...(options.system ? { system: options.system } : {}),
        ...(options.tools?.length ? { tools: options.tools } : {}) }
      let measured
      if (Array.isArray(current) && JSON.stringify(current) === JSON.stringify(messages)) {
        // The native loop committed exactly this frozen surface. Preserve its
        // usage anchor, repriced with this request's route and tool schema.
        measured = measureSystemRequest(meter, agent.session, header, requestSystemText(options), canonicalHeader)?.totalTokens
      } else {
        // CM and detached calls can have their own messages. Never borrow the
        // latest worker's surface/estimate: price this exact envelope instead.
        const empty = agent.session.constructor?.create?.('dpswarm-frozen-request-price')
        if (!empty) throw budgetError('WORKER_REQUEST_PRICING_UNAVAILABLE')
        measured = meter.measure(empty, header)?.totalTokens
        if (Number.isSafeInteger(measured) && measured >= 0) {
          measured += pendingTokens(meter, messages)
          if (options.system && !headerPricesSystem(canonicalHeader)) measured += pendingTokens(meter, [createSystemMessage(options.system, '@deepseek-ai/dsh-system-prompt')])
        }
      }
      if (!Number.isSafeInteger(measured) || measured < 0) throw budgetError('WORKER_REQUEST_PRICING_UNAVAILABLE')
      return Math.max(serialized, measured)
    } catch (error) {
      throw budgetError('WORKER_REQUEST_PRICING_UNAVAILABLE', 'The frozen request could not be measured; no request was dispatched.')
    }
  }
  const prepareStep = async (agent, { messages = [], signal } = {}) => {
    const state = await ensure(agent, signal, messages)
    if (!limited(state)) return state
    try {
      signal?.throwIfAborted()
      runtime.guard(state)
      const captured = assemblies.get(agent.session)
      if (!captured || (captured.signal && captured.signal !== signal)) throw budgetError('WORKER_REQUEST_ENVELOPE_UNAVAILABLE')
      const assembly = captured.assembly
      if (!Array.isArray(assembly.sections) || Object.isFrozen(assembly.sections)) throw budgetError('WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
      if (!assembly.sections.some(s => s.name === 'dpswarm-worker-budget-policy')) assembly.sections.push({ name: 'dpswarm-worker-budget-policy', text: BUDGET_POLICY_INSTRUCTION })
      const visible = agent.session.deriveMessages?.() || [], ids = new Set(visible.map(m => m.id).filter(Boolean))
      const incoming = messages.filter(m => !m.id || !ids.has(m.id))
      // Runtime-context projection happens just before pre-step. Price its
      // pending snapshot as well as claimed inbox messages (not in surface yet).
      const context = renderContextSnapshot(assembly)
      const retained = sessionEvents(agent.session).filter(e => e.type === 'user/message'
        && e.data?.source?.kind === 'plugin' && e.data.source.plugin === '@deepseek-ai/dsh-system-prompt'
        && agent.session.surface?.nodes?.includes(e.seq)).at(-1)?.data
      const alreadyRetained = retained?.content?.length === 1 && retained.content[0]?.text === context
      const alreadyPending = incoming.some(m => m.source?.kind === 'plugin' && m.source.plugin === '@deepseek-ai/dsh-system-prompt'
        && m.content?.length === 1 && m.content[0]?.text === context)
      const pendingContext = context && !alreadyRetained && !alreadyPending ? [createUserMessage({
        content: [{ type: 'text', text: context }], source: { kind: 'plugin', plugin: '@deepseek-ai/dsh-system-prompt',
          form: 'snapshot', sections: renderContextSections(assembly) },
      })] : []
      const history = [...visible, ...incoming, ...pendingContext]
      const system = renderPrompt(assembly) || '', tools = assembly.tools || []
      const pending = [...incoming, ...pendingContext]
      const input = await estimateInput(agent, { messages: history, system, tools, pending, signal })
      const finalSystem = state.closeout ? system : `${system}\n\n${CLOSEOUT_INSTRUCTION}`
      // The closeout request keeps its tool schemas: price the envelope that is
      // actually sent. No extra recovery reserve: the existing slack and report
      // floor cushion one refused tool attempt, a tighter grant must not park
      // at arrival (0.8.2 lesson), and a spent attempt that genuinely leaves no
      // room fails admission honestly instead of producing a fake report.
      const finalInput = await estimateInput(agent, { messages: history, system: finalSystem, tools, pending, signal })
      await runtime.prepareCloseout(state, { inputEstimate: input, finalInputEstimate: finalInput }, signal)
      applyCloseout(state, assembly)
      return state
    } catch (error) {
      await runtime.recordDenied(state, error, 'pre_step')
      throw error
    }
  }
  const preStepHook = async ({ agent, signal, messages, turn, step }, next) => {
    pendingSteps.delete(agent.session)
    await prepareStep(agent, { signal, messages: messages || [] })
    const decision = await next()
    // Other pre-step hooks can add messages or compact. Refresh the estimate
    // before the request, preserving the same array object passed to the loop.
    if (decision.kind === 'enter' && Array.isArray(decision.messages)) {
      await prepareStep(agent, { signal, messages: decision.messages })
      pendingSteps.set(agent.session, { signal, turn, step, decision })
    }
    return decision
  }
  let disposePreStep = ctx.on('agent/pre-step', preStepHook, { prepend: true, global: true })
  // Cordis publishes this event before taking its listener snapshot. Rebind
  // only our wrapper so it also surrounds later prepend hooks (e.g. native
  // model-switch notices). Dispose first: there is exactly one live wrapper.
  dispose.push(ctx.on('internal/dispatch', (mode, name) => {
    if (mode !== 'waterfall' || name !== 'agent/pre-step') return
    disposePreStep()
    disposePreStep = ctx.on('agent/pre-step', preStepHook, { prepend: true, global: true })
  }, { global: true }))
  dispose.push(() => disposePreStep())
  const toolGuard = exec => runtime.states.get(exec.agent?.session?.id)?.closeout?.mode === 'final_only'
    ? CLOSEOUT_TOOL_REFUSAL : undefined
  if (get('tools')?.guard) dispose.push(get('tools').guard(toolGuard))
  dispose.push(ctx.on('tools/pre-execute', async (exec, next) => {
    // Cold restored children can reach tool execution without a new pre-step.
    // Restore their durable state before the synchronous monotonic guard runs.
    if (exec.agent?.session && isWorkerSession(exec.agent.session)) await ensure(exec.agent, exec.signal)
    const reason = toolGuard(exec)
    if (reason) throw budgetError('WORKER_CLOSEOUT_FINAL_ONLY', reason)
    return next()
  }, { prepend: true, global: true }))
  dispose.push(ctx.on('agent/request', async ({ agent, signal, turn, step }, next) => {
    const original = await next(), state = await ensure(agent, signal)
    if (!limited(state)) return original
    let estimated = null, requested = null
    try {
      const session = agent.session, promptService = get('systemPrompt')
      if (!promptService?.assemble) throw budgetError('WORKER_REQUEST_ENVELOPE_UNAVAILABLE')
      const captured = assemblies.get(session)
      const assembly = captured && (!captured.signal || captured.signal === signal)
        ? captured.assembly : await promptService.assemble(assembleContextFor(agent, signal))
      signal?.throwIfAborted()
      // Native retries do not rerun pre-step. Reforecast after the preceding
      // attempt settled (including an unknown transport reservation).
      assemblies.set(session, { assembly, signal })
      await prepareStep(agent, { signal, messages: pendingMessages(session, { signal, turn, step }) })
      applyCloseout(state, assembly)
      const system = renderPrompt(assembly) || ''
      const pending = pendingMessages(session, { signal, turn, step })
      const messages = [...(session.deriveMessages?.() || []), ...pending]
      const native = await get('llm').resolveCallConfig(original, signal)
      // A previous header can contain tools removed for final-only. Measure the
      // current envelope instead of charging that stale schema again. System
      // projection uses the same conservative pending-input bound as pre-step.
      estimated = await estimateInput(agent, { messages, system, tools: assembly.tools || [], pending, signal, config: native })
      requested = native.maxTokens
      const maxTokens = runtime.outputLimit(state, estimated, requested, await routeOutputCap(native, signal))
      return { ...original, maxTokens }
    } catch (error) {
      await runtime.recordDenied(state, error, 'request_output_limit', estimated, requested)
      throw error
    }
  }, { prepend: true, global: true }))
  dispose.push(ctx.on('llm/stream', (options, next) => (async function* () {
    const session = options.sessionId ? get('sessions')?.get(options.sessionId) : null
    if (!session || !isWorkerSession(session) || options.purpose === 'session-title') { yield* next(); return }
    const agent = agents.get(session.id) || get('agents')?.get(session.id) || { session }
    const state = await ensure(agent, options.signal)
    options.signal?.throwIfAborted()
    const inputEstimate = limited(state) ? frozenInputEstimate(agent, options) : null
    const ticket = await runtime.admit(state, options, inputEstimate === null ? null : { inputEstimate })
    let usage = null, finish = null, error = null
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage
        if (chunk.type === 'finish') finish = chunk.reason
        yield chunk
      }
    } catch (e) { error = e; throw e }
    finally {
      // For limited workers, the settlement barrier is intentional: any
      // interrupted process restores an admitted call as charged/unknown.
      await runtime.settle(ticket, usage, options.signal?.aborted ? 'cancelled' : error ? 'failed' : finish?.kind || 'incomplete', error?.code || finish?.failure?.code || null)
    }
  })(), { prepend: true, global: true }))
  const service = {
    runtime, journal, ensure, prepareStep,
    diagnosticsForSession: sessionId => runtime.diagnosticsForSession(sessionId),
    beginTeamRun: (parent, options) => runtime.beginTeamRun(parent, options),
    resumeTeamRun: (parent, options) => runtime.resumeTeamRun(parent, options),
    teamRunRecoveryStatus: (parent, options) => runtime.teamRunRecoveryStatus(parent, options),
    issueTeamWorker: (parent, handle, assignment) => runtime.issueTeamWorker(parent, handle, assignment),
    finishTeamRun: (parent, handle) => runtime.finishTeamRun(parent, handle),
    issueRework: (parent, options) => runtime.issueRework(parent, options),
    issueReportRepair: (parent, options) => runtime.issueReportRepair(parent, options),
    revokeRework: (parent, allocationId) => runtime.revokeRework(parent, allocationId),
    status: agent => runtime.status(agent),
    shutdown: () => { runtime.shutdown(); for (const fn of dispose) if (typeof fn === 'function') fn() },
  }
  return service
}
