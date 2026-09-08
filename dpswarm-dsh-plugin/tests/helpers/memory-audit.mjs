import { createHash } from 'node:crypto'

const clone = value => JSON.parse(JSON.stringify(value))
const hash = events => createHash('sha256').update(JSON.stringify(events)).digest('hex')

/** In-memory implementation of the sidecar plugin-audit contract for unit tests. */
export class MemoryAuditJournal {
  constructor() {
    this.roots = new Map()
    this.queues = new Map()
    this.transactionIds = new Map()
  }
  state(rootId) {
    if (!this.roots.has(rootId)) this.roots.set(rootId, { revision: 0, events: [], head_hash: hash([]) })
    return this.roots.get(rootId)
  }
  snapshot(rootId) {
    const state = this.state(rootId)
    return { root_session_id: rootId, revision: state.revision, events: clone(state.events), head_hash: state.head_hash }
  }
  async read(rootId) { return this.snapshot(rootId) }
  queued(rootId, operation) {
    const prior = this.queues.get(rootId) || Promise.resolve()
    const current = prior.catch(() => undefined).then(operation)
    this.queues.set(rootId, current)
    return current.finally(() => { if (this.queues.get(rootId) === current) this.queues.delete(rootId) })
  }
  async transaction(rootId, build) {
    return this.queued(rootId, async () => {
      const snapshot = this.snapshot(rootId)
      const built = await build(snapshot)
      if (!built || !Array.isArray(built.events)) throw new TypeError('PLUGIN_AUDIT_TRANSACTION_INVALID')
      if (!built.events.length) return { ...built, journal: snapshot }
      const state = this.state(rootId), revision = state.revision + 1, transaction_id = 'memory-' + revision
      const events = built.events.map((event, offset) => ({
        seq: state.events.length + offset, type: event.type, data: clone(event.data),
        revision, transaction_id,
      }))
      state.events.push(...events)
      state.revision = revision
      state.head_hash = hash(state.events)
      return { ...built, journal: this.snapshot(rootId) }
    })
  }
  append(rootId, type, data) {
    return this.transaction(rootId, () => ({ events: [{ type, data }] }))
  }
}
