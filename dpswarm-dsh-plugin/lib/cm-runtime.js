import { createHash, randomUUID } from 'node:crypto'

export const CM_MODEL = 'deepseek-v4-flash'
export const CM_POLICY = Object.freeze({ version: 'dpswarm-cm-v1', model: CM_MODEL,
  thresholdTokens: 12000, keepRecent: 4, targetTokens: 2000, maxTokens: 4096,
  maxCallsPerAgentTurn: 12, timeoutSeconds: 120, reasoningEffort: 'off' })
export const cmError = code => Object.assign(new Error(code), { code })
export const cmHash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const enabled = (cfg, id) => Array.isArray(cfg.cmEnabledSessions) && cfg.cmEnabledSessions.includes(id)
const isChild = session => session.header?.origin === 'subagent' || (session.header?.delegationDepth || 0) > 0
const rootId = session => isChild(session) ? session.header?.parentSession || session.id : session.id
const currentTurn = session => {
  for (let i = (session.events?.length || 0) - 1; i >= 0; i--) if (session.events[i].type === 'turn/start') return session.events[i].seq
  return -1
}
const eventName = suffix => `dpswarm/cm-${suffix}`
const recordsFor = (journal, root) => journal.events.filter(event => event?.data?.root_session_id === root)

export function cmProfile(cfg) {
  if (typeof cfg.cmProvider !== 'string' || !cfg.cmProvider.trim()) throw cmError('CM_PROVIDER_REQUIRED')
  const model = cfg.cmModel ?? CM_MODEL, effort = cfg.cmEffort ?? CM_POLICY.reasoningEffort
  if (typeof model !== 'string' || !model.trim()) throw cmError('CM_MODEL_REQUIRED')
  if (typeof effort !== 'string') throw cmError('CM_EFFORT_INVALID')
  const profile = { ...CM_POLICY, provider: cfg.cmProvider.trim(), model: model.trim(), reasoningEffort: effort.trim() }
  if (!profile.reasoningEffort) delete profile.reasoningEffort
  return Object.freeze({ ...profile, id: cmHash(profile) })
}
export function completeUsage(usage) {
  return !!usage && ['inputTokens', 'outputTokens'].every(k => Number.isSafeInteger(usage[k]) && usage[k] >= 0)
    && Object.values(usage).every(n => Number.isSafeInteger(n) && n >= 0)
}
export function observedUsage(usage) {
  if (!usage || typeof usage !== 'object') return null
  return Object.fromEntries(['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens']
    .filter(k => Number.isSafeInteger(usage[k]) && usage[k] >= 0).map(k => [k, usage[k]]))
}

/** CM auditing is sidecar-owned. DSH Session events remain exclusively native. */
export class CMRuntime {
  constructor(config, resolveSession = () => null, journal) {
    if (!journal) throw new TypeError('CM_JOURNAL_REQUIRED')
    this.config = config
    this.resolveSession = resolveSession
    this.journal = journal
    this.frozen = new Map()
    this.active = new Map()
    this.beginning = new Set()
    this.seen = new Map()
    this.engines = new Set()
    this.closed = false
  }
  attach(engine) { this.engines.add(engine); return () => this.engines.delete(engine) }
  async journalFor(id) { return this.journal.read(id) }
  async restoreRun(session) {
    if (!session || isChild(session) || this.frozen.has(session.id)) return this.frozen.get(session?.id) || null
    const records = recordsFor(await this.journalFor(session.id), session.id)
    const event = [...records].reverse().find(e => e.type === eventName('team'))
    if (event?.data.active) {
      const frozen = { profile: event.data.profile, disabled: !!event.data.disabled, session }
      this.frozen.set(session.id, frozen)
      return frozen
    }
    return null
  }
  async append(id, suffix, data) {
    return this.journal.append(id, eventName(suffix), { version: 2, root_session_id: id, owner_session_id: id, ...data })
  }
  async beginRun(parent) {
    const id = parent.session.id
    await this.restoreRun(parent.session)
    if (this.frozen.has(id)) throw cmError('CM_RUN_ALREADY_FROZEN')
    const cfg = this.config(), on = enabled(cfg, id)
    const profile = on ? cmProfile(cfg) : null
    await this.append(id, 'team', { active: true, profile, disabled: false, frozen_at: Date.now() })
    this.frozen.set(id, { profile, disabled: false, session: parent.session })
    return { enabled: on, profile }
  }
  async finishRun(id, session) {
    const owner = this.frozen.get(id)?.session || session
    if (owner) await this.append(id, 'team', { active: false, profile: null, disabled: false, finished_at: Date.now() })
    this.frozen.delete(id)
  }
  async profile(session) {
    if (this.closed) return null
    const cfg = this.config(), id = rootId(session)
    // Ordinary conversations with CM switched off must not start or contact a
    // sidecar. beginRun is the only path that restores a team snapshot while off.
    if (session.header?.delegationDepth > 1 || (isChild(session) && !session.header?.parentSession) || !enabled(cfg, id)) return null
    await this.restoreRun(id === session.id ? session : this.resolveSession(id))
    const frozen = this.frozen.get(id)
    return frozen ? (frozen.disabled ? null : frozen.profile) : cmProfile(cfg)
  }
  async settingsChanged() {
    const cfg = this.config(), writes = []
    for (const [id, frozen] of this.frozen) if (!enabled(cfg, id) && !frozen.disabled) {
      // Cancellation is a local safety action; audit latency/failure cannot let
      // a newly-disabled CM request keep running.
      frozen.disabled = true
      writes.push(this.append(id, 'team', { active: true, profile: frozen.profile, disabled: true, disabled_at: Date.now() }))
    }
    for (const ticket of this.active.values()) if (!enabled(cfg, ticket.rootId)) ticket.abort.abort(cmError('CM_DISABLED'))
    await Promise.all(writes)
  }
async begin(agent, profile, details, signal) {
    const session = agent.session, turnSeq = currentTurn(session)
    if (this.active.has(session.id) || this.beginning.has(session.id)) return null
    this.beginning.add(session.id)
    let ticket
    try {
      const abort = new AbortController(), forward = () => abort.abort(signal.reason)
      signal?.addEventListener('abort', forward, { once: true })
      if (signal?.aborted) forward()
      ticket = { id: randomUUID(), rootId: rootId(session), session, profile, abort, start: performance.now(),
        turnSeq, usage: null, streamCalled: false, streamPending: false,
        detach: () => signal?.removeEventListener('abort', forward) }
      const admitted = await this.journal.transaction(ticket.rootId, journal => {
        const attempts = recordsFor(journal, ticket.rootId).filter(event => event.type === eventName('start')
          && event.data.agent_session_id === session.id && event.data.turn_seq === turnSeq).length
        if (attempts >= profile.maxCallsPerAgentTurn) return { events: [], allowed: false }
        return { events: [{ type: eventName('start'), data: { version: 2, root_session_id: ticket.rootId,
          owner_session_id: session.id, call_id: ticket.id, agent_session_id: session.id, turn_seq: turnSeq,
          profile, ...details } }], allowed: true }
      })
      if (!admitted.allowed) { ticket.detach(); return null }
      this.seen.set(session.id, new WeakRef(session))
      this.active.set(session.id, ticket)
      return ticket
    } catch (error) {
      ticket?.detach()
      throw error
    } finally { this.beginning.delete(session.id) }
  }
  guard(ticket) {
    ticket.abort.signal.throwIfAborted()
    if (this.closed || !enabled(this.config(), ticket.rootId) || this.frozen.get(ticket.rootId)?.disabled) throw cmError('CM_DISABLED')
  }
  async finish(ticket, result, error, afterTokens) {
    const data = { call_id: ticket.id, agent_session_id: ticket.session.id, owner_session_id: ticket.session.id,
      outcome: result ? 'adopted' : 'failed', error_code: error ? (error.code || 'CM_COMPACTION_FAILED') : null,
      provider_failure_code: ticket.providerFailureCode || null, duration_ms: Math.round(performance.now() - ticket.start),
      usage: observedUsage(ticket.usage), usage_complete: completeUsage(ticket.usage),
      llm_stream_calls: ticket.streamCalled ? 1 : 0, stream_pending: ticket.streamPending,
      surface_tokens_after_estimate: afterTokens, compaction_id: result?.compactionId || null, summary_seq: result?.summarySeq ?? null }
    try { await this.append(ticket.rootId, 'end', data) }
    finally { if (!ticket.streamPending) this.release(ticket) }
  }
  release(ticket) {
    ticket.detach()
    if (this.active.get(ticket.session.id) === ticket) this.active.delete(ticket.session.id)
  }
  async lateUsage(ticket) {
    try { await this.append(ticket.rootId, 'usage', { call_id: ticket.id, agent_session_id: ticket.session.id,
      owner_session_id: ticket.session.id, usage: observedUsage(ticket.usage), usage_complete: completeUsage(ticket.usage),
      stream_pending: false, late: true }) }
    finally { this.release(ticket) }
  }
  async status(agent) {
    const session = agent.session, id = rootId(session)
    let profile = null, error = null, journal = null
    try { profile = await this.profile(session); journal = await this.journalFor(id) } catch (e) { error = e.code || 'CM_CONFIGURATION_INVALID' }
    const sessions = new Map([[session.id, session]])
    for (const [sid, reference] of this.seen) {
      const child = reference.deref()
      if (!child) this.seen.delete(sid)
      else if (rootId(child) === id) sessions.set(sid, child)
    }
    const allowed = new Set(sessions.keys()), calls = new Map()
    for (const event of recordsFor(journal || { events: [] }, id)) {
      const data = event.data || {}
      if (!allowed.has(data.agent_session_id)) continue
      if (event.type === eventName('start')) calls.set(data.call_id, { session_id: data.agent_session_id, ...data, outcome: 'unfinished', usage: null, usage_complete: false })
      else if ([eventName('end'), eventName('usage')].includes(event.type) && calls.has(data.call_id)) Object.assign(calls.get(data.call_id), data)
    }
    const records = [...calls.values()]
    return { enabled: !!profile, requested: enabled(this.config(), id), attached: this.engines.size > 0,
      scope: 'selected session and direct children', model: profile?.model ?? this.config().cmModel ?? CM_MODEL, profile,
      frozen_for_team: this.frozen.has(id), configuration_error: error, attempts: records.length,
      adopted: records.filter(r => r.outcome === 'adopted').length, unknown_usage_calls: records.filter(r => r.llm_stream_calls !== 0 && !r.usage_complete).length,
      active_calls: [...this.active.values()].filter(t => t.rootId === id).length, recent: records.slice(-12),
      accounting: 'sidecar audit ledger; native DSH session history is unchanged. Input and cache tokens are disjoint. Context estimates are not net token savings; cost is not computed.' }
  }
  shutdown() {
    this.closed = true
    for (const ticket of this.active.values()) ticket.abort.abort(cmError('CM_PLUGIN_DISPOSED'))
    this.frozen.clear()
  }
}
