import { WorkerBudgetRuntime, budgetError, estimateRequestTokens, isWorkerSession } from './budget-runtime.js'
import { AuditJournal } from './audit.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'

const host = resolveHostRoot()
const [{ assembleContextFor }, { renderPrompt }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-agent/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js')),
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
  const dispose = []
  dispose.push(ctx.on('agent/pre-step', async ({ agent, signal, messages }, next) => {
    const state = await ensure(agent, signal, messages || [])
    runtime.guard(state)
    return next()
  }, { prepend: true, global: true }))
  dispose.push(ctx.on('agent/request', async ({ agent, signal }, next) => {
    const original = await next(), state = await ensure(agent, signal)
    if (!state || state.profile.mode === 'unlimited') return original
    const session = agent.session
    const promptService = get('systemPrompt')
    // Complete first-request accounting must include the system prompt and
    // constrained tool schema, not merely persisted user history.
    if (!promptService?.assemble) throw budgetError('WORKER_REQUEST_ENVELOPE_UNAVAILABLE')
    const assembly = await promptService.assemble(assembleContextFor(agent, signal))
    signal?.throwIfAborted()
    const system = renderPrompt(assembly), messages = session.deriveMessages?.() || []
    const measured = get('tokenMeter')?.measure?.(session)?.totalTokens
    const estimated = estimateRequestTokens({ messages, system, tools: assembly.tools }).input
    const native = await get('llm').resolveCallConfig(original, signal)
    const maxTokens = runtime.outputLimit(state, Math.max(measured || 0, estimated), native.maxTokens)
    return { ...original, maxTokens }
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
    runtime, journal, ensure,
    plan: (parent, decision) => runtime.plan(parent, decision),
    beginTeamRun: (parent, options) => runtime.beginTeamRun(parent, options),
    issueTeamWorker: (parent, handle, assignment) => runtime.issueTeamWorker(parent, handle, assignment),
    finishTeamRun: (parent, handle) => runtime.finishTeamRun(parent, handle),
    status: agent => runtime.status(agent),
    shutdown: () => { runtime.shutdown(); for (const fn of dispose) if (typeof fn === 'function') fn() },
  }
  return service
}
