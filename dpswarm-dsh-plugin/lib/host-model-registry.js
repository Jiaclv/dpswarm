/** DPH owns route availability. This projection never invents model ratings. */
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const identity = route => JSON.stringify([route.provider, route.model, route.reasoningEffort ?? null])
const positiveInteger = value => Number.isSafeInteger(value) && value > 0 ? value : null

// Host model metadata (dsh-llm resolveModelInfo) also carries per-route request
// limits. Only positive integers are trusted; anything else stays unknown.
// The budget rail clamps a shaped output allowance to default_max_tokens so a
// large cumulative grant can never dispatch a request the route rejects
// (glm caps max_tokens at 131072; a 300k Auto rail shaped 131883 once).
export function readModelLimits(info) {
  return { default_max_tokens: positiveInteger(info?.defaultMaxTokens),
    context_window: positiveInteger(info?.context?.contextWindow) }
}

export class HostModelRegistry {
  constructor(getLLM, { timeoutMs = 12000 } = {}) { this.getLLM = getLLM; this.timeoutMs = timeoutMs; this.capabilities = new Map() }

  async resolve(routes, { signal, expected } = {}) {
    const llm = this.getLLM()
    if (typeof llm?.resolveCallConfig !== 'function') throw failure('HOST_MODEL_REGISTRY_UNAVAILABLE', 'DPH exact model resolution service is unavailable; update the host.')
    if (!Array.isArray(routes) || !routes.length || routes.length > 8) throw failure('HOST_MODEL_ROUTE_INVALID', 'Expected the fixed user-selected role routes.')
    const roles = new Set()
    for (const route of routes) {
      if (!['role','provider','model'].every(k => typeof route[k] === 'string' && route[k].trim()) || roles.has(route.role)
          || (route.reasoningEffort !== undefined && typeof route.reasoningEffort !== 'string')) throw failure('HOST_MODEL_ROUTE_INVALID', 'Invalid or duplicate fixed role.')
      roles.add(route.role)
    }
    const abort = new AbortController()
    const cancelled = () => abort.abort(signal.reason)
    signal?.addEventListener('abort', cancelled, { once: true })
    if (signal?.aborted) cancelled()
    let timer, rejectAbort
    const interrupted = new Promise((_, reject) => {
      rejectAbort = () => reject(failure(signal?.aborted ? 'SUBAGENT_ABORTED' : 'HOST_MODEL_REGISTRY_TIMEOUT', 'Model preflight cancelled or timed out.'))
      abort.signal.addEventListener('abort', rejectAbort, { once: true })
      if (abort.signal.aborted) rejectAbort()
    })
    timer = setTimeout(() => abort.abort(), this.timeoutMs)
    try {
      const models = await Promise.race([Promise.all(routes.map(async route => {
        const request = { provider: route.provider, model: route.model,
          ...(route.reasoningEffort === undefined ? {} : { reasoningEffort: route.reasoningEffort }) }
        let resolved
        try { resolved = await llm.resolveCallConfig(request, abort.signal) }
        catch (error) {
          const code = typeof error?.code === 'string' && /^[A-Z_]+$/.test(error.code) ? error.code : 'UNAVAILABLE'
          throw failure('HOST_MODEL_UNAVAILABLE', `DPH cannot resolve ${route.role}: ${route.provider}/${route.model} (${code}).`)
        }
        if (resolved?.provider !== route.provider || resolved?.model !== route.model
            || (resolved.reasoningEffort !== undefined && typeof resolved.reasoningEffort !== 'string')
            || (route.reasoningEffort !== undefined && resolved.reasoningEffort !== route.reasoningEffort)) throw failure('HOST_MODEL_ROUTE_DRIFT', 'DPH changed an explicitly selected route; no substitution was accepted.')
        return { role: route.role, provider: resolved.provider, model: resolved.model,
          ...(resolved.reasoningEffort === undefined ? {} : { reasoningEffort: resolved.reasoningEffort }) }
      })), interrupted])
      if (expected && (models.length !== expected.models.length || models.some((row, i) => row.role !== expected.models[i]?.role || identity(row) !== identity(expected.models[i])))) {
        throw failure('HOST_MODEL_ROUTE_DRIFT', 'A frozen role or default reasoning effort changed before dispatch.')
      }
      const unknownCapability = row => ({ role: row.role, provider: row.provider, model: row.model, input_modalities: null, image_input: 'unknown', default_max_tokens: null, context_window: null })
      let capabilities
      try { capabilities = await Promise.race([Promise.all(models.map(async row => {
        const result = unknownCapability(row)
        if (typeof llm.resolveModelInfo !== 'function') return result
        try {
          const info = await llm.resolveModelInfo(row.provider, row.model, abort.signal)
          if (info?.provider === row.provider && info?.id === row.model) {
            const limits = readModelLimits(info)
            result.default_max_tokens = limits.default_max_tokens
            result.context_window = limits.context_window
            if (Array.isArray(info.inputModalities) && info.inputModalities.every(v => typeof v === 'string')) {
              result.input_modalities = [...info.inputModalities]
              result.image_input = info.inputModalities.includes('image') ? 'supported' : 'unsupported'
            }
          }
        } catch { /* Missing metadata is unknown, never permission to substitute a route. */ }
        return result
      })), interrupted]) }
      catch (error) {
        // Route validation already succeeded. Optional metadata timing out is
        // unknown capability, not a new reason to reject that exact route.
        if (signal?.aborted || error.code !== 'HOST_MODEL_REGISTRY_TIMEOUT') throw error
        capabilities = models.map(unknownCapability)
      }
      if (signal?.aborted) throw failure('SUBAGENT_ABORTED', 'Model preflight cancelled.')
      for (const capability of capabilities) this.capabilities.set(JSON.stringify([capability.provider, capability.model]), capability)
      return { source: 'dph', availability_source: 'host-resolved', checked_at: new Date().toISOString(), models, capabilities }
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', cancelled)
      abort.signal.removeEventListener('abort', rejectAbort)
    }
  }

  capabilityGuideForAgent(agent) {
    // Prompt assembly precedes the first resolved request header on a cold
    // native session. Optional capability advice must not require a route yet.
    // Never promote creation defaults or UI selection to an executed route.
    const route = agent?.session?.requestHeader?.()?.config
    if (typeof route?.provider !== 'string' || !route.provider.trim()
      || typeof route?.model !== 'string' || !route.model.trim()) return ''
    return this.capabilityGuide(route)
  }

  capabilityGuide(route) {
    const capability = this.capabilities.get(JSON.stringify([route?.provider, route?.model]))
    if (!capability) return ''
    return 'Host-observed capability for the exact current route: ' + JSON.stringify(capability)
      + '\nIf image_input is unsupported, do not retry read_image or claim visual inspection. Choose suitable permitted evidence and disclose its limits. Unknown is not proof of support. default_max_tokens and context_window, when not null, bound one request; keep the user-selected model route.'
  }

  // Advisory metadata for a route outside a team preflight (Lead status views,
  // rail advice). Returns null on any unavailability; never rejects a route.
  async resolveRouteCapability(provider, model, { signal } = {}) {
    if (typeof provider !== 'string' || !provider.trim() || typeof model !== 'string' || !model.trim()) return null
    const key = JSON.stringify([provider, model])
    const cached = this.capabilities.get(key)
    if (cached) return cached
    const llm = this.getLLM()
    if (typeof llm?.resolveModelInfo !== 'function') return null
    let info = null
    try { info = await llm.resolveModelInfo(provider, model, signal) } catch { return null }
    if (info?.provider !== provider || info?.id !== model) return null
    const modalities = Array.isArray(info.inputModalities) && info.inputModalities.every(v => typeof v === 'string') ? [...info.inputModalities] : null
    const capability = { role: 'route', provider, model, input_modalities: modalities,
      image_input: modalities ? (modalities.includes('image') ? 'supported' : 'unsupported') : 'unknown', ...readModelLimits(info) }
    this.capabilities.set(key, capability)
    return capability
  }

  checkLead(current, routes) {
    const frozen = routes.find(row => row.role === 'lead')
    if (!frozen || identity(current) !== identity(frozen)) throw failure('HOST_MODEL_ROUTE_DRIFT', 'The current Lead request differs from the frozen team route.')
  }

  async publish(sidecar, receipt) {
    const pairs = [...new Map(receipt.models.map(({ provider, model }) => [JSON.stringify([provider,model]), { provider, model }])).values()]
    return sidecar.call('POST', '/api/models/host-catalog', { source: 'dph', models: pairs })
  }
}
