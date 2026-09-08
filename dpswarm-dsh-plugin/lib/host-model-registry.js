/** DPH owns route availability. This projection never invents model ratings. */
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const identity = route => JSON.stringify([route.provider, route.model, route.reasoningEffort ?? null])

export class HostModelRegistry {
  constructor(getLLM, { timeoutMs = 12000 } = {}) { this.getLLM = getLLM; this.timeoutMs = timeoutMs }

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
      return { source: 'dph', availability_source: 'host-resolved', checked_at: new Date().toISOString(), models }
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', cancelled)
      abort.signal.removeEventListener('abort', rejectAbort)
    }
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
