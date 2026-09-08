import { createHash, randomUUID } from 'node:crypto'
import { Sidecar } from './sidecar.js'
import { runtimePaths } from './paths.js'

/**
 * Plugin-owned durable audit journal.  It deliberately never writes a DSH
 * Session event: DSH has no public persistent-plugin-event registration seam.
 * The sidecar serializes/cas-protects each root journal and fsyncs accepted
 * transactions before returning.
 */
export class AuditJournal {
  constructor({ config, sidecarFactory } = {}) {
    this.config = config || (() => ({}))
    this.sidecarFactory = sidecarFactory || ((rootId) => {
      const cfg = this.config()
      return new Sidecar({ ...cfg, ...runtimePaths(cfg), sessionId: rootId,
        sessionIsolation: true, auditJournalRequired: true })
    })
    this.sidecars = new Map()
    this.queues = new Map()
  }

  sidecar(rootId) {
    let sidecar = this.sidecars.get(rootId)
    if (!sidecar) {
      sidecar = this.sidecarFactory(rootId)
      this.sidecars.set(rootId, sidecar)
    }
    return sidecar
  }

  async read(rootId) {
    const sidecar = this.sidecar(rootId)
    await sidecar.ensure()
    let result
    try { result = await sidecar.call('GET', '/api/plugin-audit') }
    catch (error) {
      // The sidecar distinguishes a fresh root (not found) from a corrupt or
      // unavailable ledger. A caller may create only the former at revision 0.
      const text = String(error?.code || '') + ' ' + String(error?.message || '')
      if (/PLUGIN_AUDIT_(NOT_)?FOUND/i.test(text)) {
        return { root_session_id: rootId, revision: 0, events: [],
          head_hash: createHash('sha256').update('').digest('hex'), missing: true }
      }
      throw error
    }
    if (!result || result.root_session_id !== rootId || !Number.isSafeInteger(result.revision)
      || !Array.isArray(result.events) || typeof result.head_hash !== 'string') {
      throw Object.assign(new Error('PLUGIN_AUDIT_INVALID_READ'), { code: 'PLUGIN_AUDIT_INVALID_READ' })
    }
    return result
  }

  // The process-local queue avoids needless CAS collisions.  The sidecar CAS
  // remains the authority for separate DSH processes or a restart race.
  queued(rootId, operation) {
    const prior = this.queues.get(rootId) || Promise.resolve()
    const current = prior.catch(() => undefined).then(operation)
    this.queues.set(rootId, current)
    return current.finally(() => { if (this.queues.get(rootId) === current) this.queues.delete(rootId) })
  }

  async transaction(rootId, build) {
    return this.queued(rootId, async () => {
      let transactionId = randomUUID(), last
      for (let attempt = 0; attempt < 4; attempt++) {
        const snapshot = await this.read(rootId)
        const built = await build(snapshot)
        if (!built || !Array.isArray(built.events)) throw new TypeError('PLUGIN_AUDIT_TRANSACTION_INVALID')
        if (!built.events.length) return { ...built, journal: snapshot }
        try {
          const journal = await this.sidecar(rootId).call('POST', '/api/plugin-audit', {
            root_session_id: rootId, expected_revision: snapshot.revision,
            transaction_id: transactionId, events: built.events,
          })
          if (!journal || journal.root_session_id !== rootId || !Number.isSafeInteger(journal.revision)
            || !Array.isArray(journal.events) || typeof journal.head_hash !== 'string') {
            throw Object.assign(new Error('PLUGIN_AUDIT_INVALID_WRITE'), { code: 'PLUGIN_AUDIT_INVALID_WRITE' })
          }
          return { ...built, journal }
        } catch (error) {
          last = error
          // A committed request replayed after a transport error must use the
          // same id, while a confirmed CAS refusal needs a fresh transaction.
          if (String(error?.code || '').includes('REVISION') || String(error?.message || '').includes('REVISION')) {
            transactionId = randomUUID()
            continue
          }
          throw error
        }
      }
      throw last || Object.assign(new Error('PLUGIN_AUDIT_CAS_EXHAUSTED'), { code: 'PLUGIN_AUDIT_CAS_EXHAUSTED' })
    })
  }

  append(rootId, type, data) {
    return this.transaction(rootId, () => ({ events: [{ type, data }] }))
  }
}

export const auditEvent = (type, data) => ({ type, data: JSON.parse(JSON.stringify(data)) })
