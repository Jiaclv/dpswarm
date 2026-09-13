import { createHash } from 'node:crypto'

export const HOST_SERVICES_SCHEMA = 'dpswarm-host-services-v1'
const fingerprint = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')

export function hostRuntimeIncompatible(component, stage, cause) {
  const details = { schema: HOST_SERVICES_SCHEMA, component, stage }
  details.fingerprint = fingerprint(details)
  return Object.assign(new Error(`HOST_RUNTIME_INCOMPATIBLE: host ${component} does not support ${stage}`, cause ? { cause } : undefined),
    { code: 'HOST_RUNTIME_INCOMPATIBLE', details })
}

// Cordis plugin contexts reject ctx[name] unless that service was injected.
// Its public get(name, false) API deliberately supports optional host services.
export function getOptionalHostService(ctx, name) {
  if (typeof ctx?.get !== 'function') throw new TypeError('Context.get is unavailable')
  return ctx.get(name, false) ?? null
}

export function hostServiceShape(component, stage, inspect) {
  try { return inspect() } catch (cause) {
    throw hostRuntimeIncompatible(component, stage, cause)
  }
}

export function optionalHostStorage(ctx) {
  const hub = hostServiceShape('storage', 'service-lookup', () => getOptionalHostService(ctx, 'storage'))
  if (hub === null) return null
  return hostServiceShape('storage', 'backend-shape', () => {
    if (typeof hub.backend?.get !== 'function' || typeof hub.backend?.names !== 'function') {
      throw new TypeError('Storage backend registry is unavailable')
    }
    const names = hub.backend.names()
    if (!Array.isArray(names) || names.some(name => typeof name !== 'string')) {
      throw new TypeError('Storage backend names must be an array of strings')
    }
    return { hub, names }
  })
}

export function hostStorageKv(storage) {
  if (!storage) return null
  const { hub, names } = storage
  const order = names.includes('json') ? ['json', ...names.filter(name => name !== 'json')] : names
  for (const name of order) {
    const kv = hostServiceShape('storage', 'backend-lookup', () => hub.backend.get(name)?.kv)
    if (kv == null) continue
    return hostServiceShape('storage', 'kv-shape', () => {
      if (typeof kv.open !== 'function') throw new TypeError('KV open is unavailable')
      return kv
    })
  }
  if (names.length) throw hostRuntimeIncompatible('storage', 'kv-shape')
  return null
}

export function optionalHostSessions(ctx) {
  const sessions = hostServiceShape('sessions', 'service-lookup', () => getOptionalHostService(ctx, 'sessions'))
  if (sessions === null) return null
  return hostServiceShape('sessions', 'registry-shape', () => {
    if (typeof sessions.get !== 'function') throw new TypeError('Session lookup is unavailable')
    return sessions
  })
}

export function resolveHostSession(ctx, id) {
  const sessions = optionalHostSessions(ctx)
  if (sessions === null) return null
  // The capability probe checks the method shape, not this operation.
  return sessions.get(id) ?? null
}

// No KV open/read/write and no model calls: this only validates the host seam.
export function probeHostRuntime(ctx) {
  const storage = optionalHostStorage(ctx)
  const kv = hostStorageKv(storage)
  const sessions = optionalHostSessions(ctx)
  const observed = { schema: HOST_SERVICES_SCHEMA,
    storage: { available: Boolean(kv), composed: Boolean(storage) },
    sessions: { available: Boolean(sessions) } }
  return { compatible: true, ...observed, fingerprint: fingerprint(observed) }
}

export function combineRuntimeCompatibility(hostRuntime, sidecarRuntime) {
  return { ...sidecarRuntime, sidecar_fingerprint: sidecarRuntime.fingerprint, host_runtime: hostRuntime,
    fingerprint: fingerprint({ schema: HOST_SERVICES_SCHEMA, host: hostRuntime.fingerprint, sidecar: sidecarRuntime.fingerprint }) }
}
