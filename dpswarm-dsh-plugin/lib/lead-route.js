const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })

/** Read the resolved request that produced the current Lead tool call.
 * Agent.options is a creation-time default; Web model selection overrides it
 * during request assembly. The native folded header is authoritative here.
 */
export function effectiveLeadRoute(parent) {
  if (typeof parent?.session?.requestHeader !== 'function') {
    throw failure('ROOT_MODEL_REQUIRED', 'The actual Lead request header is required; startup model defaults cannot be inherited')
  }
  const config = parent.session.requestHeader()?.config
  if (!config || !['provider', 'model'].every(key => typeof config[key] === 'string' && config[key].trim())) {
    throw failure('ROOT_MODEL_REQUIRED', 'The actual Lead request must supply provider and model before delegation')
  }
  if (config.reasoningEffort !== undefined && (typeof config.reasoningEffort !== 'string' || !config.reasoningEffort.trim())) {
    throw failure('INVALID_ROOT_EFFORT', 'The actual Lead reasoning effort must be a nonempty string when specified')
  }
  // An absent effort must remain absent: never merge Agent.options or user defaults.
  return Object.freeze({ provider: config.provider, model: config.model,
    ...(config.reasoningEffort === undefined ? {} : { reasoningEffort: config.reasoningEffort }) })
}

// A private in-process capability survives native AgentOptions spreading, but
// cannot be supplied by model-facing JSON or replayed into another child.
const childRouteKey = Symbol('dpswarm:child-request-route')
const childRoutes = new WeakMap()
const routeEvent = 'dpswarm/route-bound'
const routeProtocol = 'fixed-role-route-v1'

function directChild(agent, parentId) {
  const header = agent?.session?.header
  return header?.origin === 'subagent' && header.parentSession === parentId
    && header.delegationDepth === 1 && agent.id === agent.session.id
    && (header.id === undefined || header.id === agent.id)
}

/** Create the route capability before native start; bind only after CP publication. */
export function prepareChildRoute(parent, agentOptions, { journal, label }) {
  if (!journal?.transaction || typeof label !== 'string' || !label.startsWith('dpswarm:')) throw failure('CHILD_ROUTE_BINDING_REQUIRED', 'Trusted route journal and role label are required')
  const route = effectiveLeadRoute({ session: { requestHeader: () => ({ config: agentOptions }) } })
  const capability = Object.freeze({})
  let resolveBound, rejectBound
  const bound = new Promise((resolve, reject) => { resolveBound = resolve; rejectBound = reject })
  bound.catch(() => {})
  const state = { parentId: parent.session.id, route, label, childId: null, closed: false, bound }
  childRoutes.set(capability, state)
  return {
    agentOptions: { ...agentOptions, [childRouteKey]: capability },
    async bind(childId) {
      if (state.closed || typeof childId !== 'string' || !childId || state.childId !== null) {
        throw failure('CHILD_ROUTE_BINDING_INVALID', 'A role route must bind once to its published native child')
      }
      state.childId = childId
      const data = { protocol: routeProtocol, root_session_id: state.parentId,
        owner_session_id: childId, child_session_id: childId, parent_session_id: state.parentId, label, route }
      await journal.transaction(state.parentId, snapshot => {
        if (snapshot.events.some(e => e.type === routeEvent && (e.data.child_session_id === childId || e.data.owner_session_id === childId))) {
          throw failure('CHILD_ROUTE_BINDING_INVALID', 'This child already has a durable role route binding')
        }
        return { events: [{ type: routeEvent, data }] }
      })
      if (state.closed) throw failure('CHILD_ROUTE_NOT_BOUND', 'Role ended while its route binding was being saved')
      resolveBound(childId)
    },
    close() {
      state.closed = true
      rejectBound(failure('CHILD_ROUTE_NOT_BOUND', 'The role ended before its native route binding completed'))
    },
  }
}

async function waitForBinding(state, signal) {
  if (signal?.aborted) throw signal.reason || failure('CHILD_ROUTE_CANCELLED', 'Child request was cancelled')
  let cancelled
  const abort = new Promise((_, reject) => {
    cancelled = () => reject(signal.reason || failure('CHILD_ROUTE_CANCELLED', 'Child request was cancelled'))
    signal?.addEventListener('abort', cancelled, { once: true })
  })
  try { return signal ? await Promise.race([state.bound, abort]) : await state.bound }
  finally { signal?.removeEventListener('abort', cancelled) }
}

function recordedRole(agent) {
  const header = agent?.session?.header
  if (!directChild(agent, header?.parentSession) || typeof header.parentSession !== 'string') return false
  // This immutable native descriptor identifies a DP role; session/title is mutable.
  const descriptor = agent.session.events?.find(e => e.type === 'subagent/descriptor')?.data
  return typeof descriptor?.label === 'string' && /^dpswarm:DPswarm (implementer|tester|reviewer)$/.test(descriptor.label)
}

/** Resolve the exact fixed-role route without preparing or sending a model request.
 * The same publication barrier and live/cold identity checks govern both CM
 * pressure measurements and native request dispatch. Ordinary agents return null.
 */
export async function resolveChildRoute(agent, { journal, signal } = {}) {
  const capability = agent?.options?.[childRouteKey]
  let route
  if (capability !== undefined) {
    const state = childRoutes.get(capability)
    const descriptor = agent?.session?.events?.find(e => e.type === 'subagent/descriptor')?.data
    if (!state || state.closed || !directChild(agent, state.parentId) || descriptor?.label !== state.label) {
      throw failure('CHILD_ROUTE_BINDING_INVALID', 'This native child does not own the frozen role route')
    }
    const childId = await waitForBinding(state, signal)
    if (state.closed || childId !== agent.id) throw failure('CHILD_ROUTE_BINDING_INVALID', 'Published child identity differs from the requesting child')
    route = state.route
  } else if (recordedRole(agent)) {
    // The pre-fix native header could have lost effort. Only an independent
    // durable binding can authorize a cold fixed role; old logs remain readable.
    if (!journal?.read) throw failure('CHILD_ROUTE_BINDING_REQUIRED', 'Cold fixed role has no trusted route journal')
    const rootId = agent.session.header.parentSession, snapshot = await journal.read(rootId)
    const records = snapshot.events.filter(e => e.type === routeEvent
      && (e.data.child_session_id === agent.id || e.data.owner_session_id === agent.id))
    if (!records.length) throw failure('CHILD_ROUTE_BINDING_REQUIRED', 'Legacy or incomplete role has no durable frozen route')
    if (records.length !== 1) throw failure('CHILD_ROUTE_BINDING_INVALID', 'Ambiguous role route binding')
    const data = records[0].data
    const label = agent.session.events.find(e => e.type === 'subagent/descriptor').data.label
    if (snapshot.root_session_id !== rootId || data.protocol !== routeProtocol || data.root_session_id !== rootId
      || data.parent_session_id !== rootId || data.child_session_id !== agent.id || data.owner_session_id !== agent.id
      || data.label !== label || !directChild(agent, rootId)) throw failure('CHILD_ROUTE_BINDING_INVALID', 'Cold role identity differs from its binding')
    route = effectiveLeadRoute({ session: { requestHeader: () => ({ config: data.route }) } })
    if (Object.keys(data.route).some(key => !['provider', 'model', 'reasoningEffort'].includes(key))) throw failure('CHILD_ROUTE_BINDING_INVALID', 'Frozen route contains unsupported fields')
    if (agent.session.requestHeader?.()) {
      const recorded = effectiveLeadRoute(agent)
      if (['provider', 'model', 'reasoningEffort'].some(key => route[key] !== recorded[key])) {
        throw failure('CHILD_ROUTE_HEADER_MISMATCH', 'Native role request differs from its durable frozen route')
      }
    }
  } else return null
  return route
}

/** Public request waterfall, before native request/header and provider dispatch.
 * Child AgentOptions retain the requested effort, but the host does not project
 * it into call config. Override only a bound DP role, after native selection.
 */
export function installChildRoutes(ctx, { journal } = {}) {
  return ctx.on('agent/request', async ({ agent, signal }, next) => {
    const route = await resolveChildRoute(agent, { journal, signal })
    if (route === null) return next()
    const native = await next()
    const { reasoningEffort: _discarded, ...withoutEffort } = native
    return { ...withoutEffort, ...route }
  }, { prepend: true, global: true })
}
