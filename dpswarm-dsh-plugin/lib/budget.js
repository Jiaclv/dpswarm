import { WorkerBudgetRuntime, budgetError, estimateRequestTokens, isWorkerSession } from './budget-runtime.js'
import { CLOSEOUT_INSTRUCTION } from './worker-closeout.js'
import { AuditJournal } from './audit.js'
import { resolveChildRoute } from './lead-route.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'

const host = resolveHostRoot()
const [{ assembleContextFor }, { renderPrompt, renderContextSnapshot }, { canonicalHeader }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-agent/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-session/lib/index.js')),
])

export function installBudget(ctx, configGetter, { journal = new AuditJournal({ config: configGetter }) } = {}) {
  const get = name => ctx.get?.(name, false) || ctx[name]
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
  const dispose = [], assemblies = new WeakMap()
  const limited = state => state && state.profile.mode !== 'unlimited'
  const applyCloseout = (state, assembly) => {
    if (!state?.closeout || !assembly) return
    // DSH renders system/tools after pre-step from this assembly. Its complete
    // persona wrapper may copy the outer object, so retain/mutate these arrays.
    // A wire guard below refuses dispatch if a host stops preserving them.
    if (!Array.isArray(assembly.tools) || !Array.isArray(assembly.sections)
      || Object.isFrozen(assembly.tools) || Object.isFrozen(assembly.sections)) throw budgetError('WORKER_CLOSEOUT_ASSEMBLY_IMMUTABLE')
    assembly.tools.splice(0)
    if (!assembly.sections.some(s => s.name === 'dpswarm-worker-closeout')) assembly.sections.push({ name: 'dpswarm-worker-closeout', text: CLOSEOUT_INSTRUCTION })
  }
  dispose.push(ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next(), agent = context?.agent
    if (agent?.session && isWorkerSession(agent.session)) {
      assemblies.set(agent.session, { assembly, signal: context.signal })
      applyCloseout(runtime.states.get(agent.session.id), assembly)
    }
    return assembly
  }, { prepend: true, global: true }))
  const meteredInput = async (agent, { system, tools, pending, signal }) => {
    const meter = get('tokenMeter')
    if (typeof meter?.measure !== 'function') return null
    // The meter prices the retained surface and header with the real tokenizer;
    // chars/3 skews 2-3x on CJK and ~1.3x on ASCII (911 tester closeout). A
    // measurement failure must never break a worker over an advisory estimate.
    try {
      const frozen = await resolveChildRoute(agent, { journal, signal }).catch(() => null)
      const config = frozen || agent.session.requestHeader?.()?.config
        || { provider: agent.options?.provider, model: agent.options?.model }
      if (!['provider', 'model'].every(key => typeof config?.[key] === 'string' && config[key].trim())) return null
      const measured = meter.measure(agent.session, canonicalHeader({ config, system, tools }))?.totalTokens
      if (!Number.isSafeInteger(measured) || measured < 0) return null
      const pendingTokens = (pending || []).reduce((total, message) =>
        total + (meter.estimateMessage?.(message) ?? estimateRequestTokens({ messages: [message] }).input), 0)
      return measured + pendingTokens
    } catch { return null }
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
      const visible = agent.session.deriveMessages?.() || [], ids = new Set(visible.map(m => m.id).filter(Boolean))
      const incoming = messages.filter(m => !m.id || !ids.has(m.id))
      // Runtime-context projection happens just before pre-step. Price its
      // pending snapshot as well as claimed inbox messages (not in surface yet).
      const context = renderContextSnapshot(assembly)
      const retained = (agent.session.events || []).filter(e => e.type === 'user/message'
        && e.data?.source?.kind === 'plugin' && e.data.source.plugin === '@deepseek-ai/dsh-system-prompt'
        && agent.session.surface?.nodes?.includes(e.seq)).at(-1)?.data
      const alreadyRetained = retained?.content?.length === 1 && retained.content[0]?.text === context
      const alreadyPending = incoming.some(m => m.source?.kind === 'plugin' && m.source.plugin === '@deepseek-ai/dsh-system-prompt'
        && m.content?.length === 1 && m.content[0]?.text === context)
      const pendingContext = context && !alreadyRetained && !alreadyPending ? [{ role: 'user', content: [{ type: 'text', text: context }] }] : []
      const history = [...visible, ...incoming, ...pendingContext]
      const system = renderPrompt(assembly), tools = assembly.tools || []
      const pending = [...incoming, ...pendingContext]
      // Same basis as agent/request below: prefer the native meter, keep the
      // chars/3 serialization as a floor and as the no-meter fallback.
      const metered = await meteredInput(agent, { system, tools, pending, signal })
      const input = Math.max(metered ?? 0, estimateRequestTokens({ messages: history, system, tools }).input)
      const finalSystem = state.closeout ? system : `${system}\n\n${CLOSEOUT_INSTRUCTION}`
      const meteredFinal = await meteredInput(agent, { system: finalSystem, tools: [], pending, signal })
      const finalInput = Math.max(meteredFinal ?? 0, estimateRequestTokens({ messages: history, system: finalSystem, tools: [] }).input)
      await runtime.prepareCloseout(state, { inputEstimate: input, finalInputEstimate: finalInput }, signal)
      applyCloseout(state, assembly)
      return state
    } catch (error) {
      await runtime.recordDenied(state, error, 'pre_step')
      throw error
    }
  }
  dispose.push(ctx.on('agent/pre-step', async ({ agent, signal, messages }, next) => {
    await prepareStep(agent, { signal, messages: messages || [] })
    const decision = await next()
    // Other pre-step hooks can add messages or compact. Refresh the estimate
    // before the request, preserving the same array object passed to the loop.
    if (decision.kind === 'enter' && Array.isArray(decision.messages)) await prepareStep(agent, { signal, messages: decision.messages })
    return decision
  }, { prepend: true, global: true }))
  const toolGuard = exec => runtime.states.get(exec.agent?.session?.id)?.closeout?.mode === 'final_only'
    ? 'WORKER_CLOSEOUT_FINAL_ONLY: return the saved candidate and unfinished work without further tools.' : undefined
  if (get('tools')?.guard) dispose.push(get('tools').guard(toolGuard))
  dispose.push(ctx.on('tools/pre-execute', async (exec, next) => {
    // Cold restored children can reach tool execution without a new pre-step.
    // Restore their durable state before the synchronous monotonic guard runs.
    if (exec.agent?.session && isWorkerSession(exec.agent.session)) await ensure(exec.agent, exec.signal)
    const reason = toolGuard(exec)
    if (reason) throw budgetError('WORKER_CLOSEOUT_FINAL_ONLY', reason)
    return next()
  }, { prepend: true, global: true }))
  dispose.push(ctx.on('agent/request', async ({ agent, signal }, next) => {
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
      applyCloseout(state, assembly)
      const system = renderPrompt(assembly), messages = session.deriveMessages?.() || []
      const native = await get('llm').resolveCallConfig(original, signal)
      // A previous header can contain tools removed for final-only. Measure the
      // current envelope instead of charging that stale schema again.
      const measured = get('tokenMeter')?.measure?.(session, canonicalHeader({ config: native, system, tools: assembly.tools || [] }))?.totalTokens
      estimated = Math.max(measured || 0, estimateRequestTokens({ messages, system, tools: assembly.tools }).input)
      requested = native.maxTokens
      const maxTokens = runtime.outputLimit(state, estimated, requested)
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
    const ticket = await runtime.admit(state, options)
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
    plan: (parent, decision) => runtime.plan(parent, decision),
    beginTeamRun: (parent, options) => runtime.beginTeamRun(parent, options),
    issueTeamWorker: (parent, handle, assignment) => runtime.issueTeamWorker(parent, handle, assignment),
    finishTeamRun: (parent, handle) => runtime.finishTeamRun(parent, handle),
    issueRework: (parent, options) => runtime.issueRework(parent, options),
    revokeRework: (parent, allocationId) => runtime.revokeRework(parent, allocationId),
    status: agent => runtime.status(agent),
    shutdown: () => { runtime.shutdown(); for (const fn of dispose) if (typeof fn === 'function') fn() },
  }
  return service
}
