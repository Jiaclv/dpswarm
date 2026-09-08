import { createHash, randomUUID } from 'node:crypto'

export const budgetError = (code, message = code) => Object.assign(new Error(message), { code })
export const isWorkerSession = session => session?.header?.origin === 'subagent' || (session?.header?.delegationDepth || 0) > 0
const positive = value => Number.isSafeInteger(value) && value > 0
const plain = value => JSON.parse(JSON.stringify(value))
const canonical = value => {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]))
  return value
}
const same = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right))
const hash = text => createHash('sha256').update(text, 'utf8').digest('hex')
const marker = id => `[DPSWARM_WORKER_BUDGET_V1:${id}]`
const fixedMarker = id => `[DPSWARM_FIXED_WORKER_BUDGET_V1:${id}]`
const eventName = suffix => `dpswarm/worker-budget-${suffix}`
const decisionRequired = message => budgetError('WORKER_BUDGET_DECISION_REQUIRED', message || 'Auto requires the current Lead to decide this worker allocation before delegation.')
const nativeProgress = session => (session.events || []).slice(session.header?.seedLength || 0)
  .some(event => ['assistant/message', 'tool/result', 'step/end', 'turn/end'].includes(event.type))
const callsFrom = (events, workerId) => {
  const calls = new Map()
  for (const event of events) {
    const data = event?.data || {}
    if (data.worker_session_id !== workerId) continue
    if (event.type.endsWith('-admitted')) calls.set(data.call_id, { ...data, status: 'restored_unknown', usage: null, usage_complete: false, observed_tokens: null })
    if (event.type.endsWith('-settled') && calls.has(data.call_id)) Object.assign(calls.get(data.call_id), data)
  }
  return calls
}
const totalsFor = calls => {
  let observed = 0, committed = 0, unknown = 0, active = 0
  for (const call of calls.values()) {
    observed += call.observed_tokens ?? 0
    committed += call.usage_complete ? call.observed_tokens : Math.max(call.reserved_tokens || 0, call.observed_tokens || 0)
    if (!call.usage_complete) unknown++
    if (call.status === 'active') active++
  }
  return { calls: calls.size, observed_tokens_lower_bound: observed, committed_tokens: committed, unknown_usage_calls: unknown, active_calls: active }
}

function assignment(session, incoming) {
  const own = (session.events || []).slice(session.header?.seedLength || 0)
  const message = own.find(e => e.type === 'user/message')?.data || incoming.find(m => m?.role === 'user' && m?.source?.kind === 'user')
  if (!message || message.role !== 'user' || message.source?.kind !== 'user' || !Array.isArray(message.content)
    || message.content.length !== 1 || message.content[0]?.type !== 'text' || typeof message.content[0].text !== 'string') throw decisionRequired()
  return message.content[0].text
}
export function validateWorkerDecision(value) {
  if (!value || typeof value.task !== 'string' || !value.task.trim() || !positive(value.tokenLimit) || !positive(value.callLimit)
    || typeof value.reason !== 'string' || !value.reason.trim() || (value.label !== undefined && (typeof value.label !== 'string' || !value.label.trim()))) throw budgetError('WORKER_BUDGET_DECISION_INVALID')
  return { task: value.task, tokenLimit: value.tokenLimit, callLimit: value.callLimit, reason: value.reason.trim(), ...(value.label === undefined ? {} : { label: value.label.trim() }) }
}
export function workerBudgetProfile(config, rootId) {
  const matches = (config.workerBudgetSessionOverrides || []).filter(item => item?.sessionId === rootId)
  if (matches.length > 1) throw budgetError('WORKER_BUDGET_DUPLICATE_OVERRIDE')
  const selected = matches[0], mode = selected?.mode ?? config.workerBudgetMode ?? 'unlimited'
  if (!['unlimited', 'manual', 'auto'].includes(mode)) throw budgetError('WORKER_BUDGET_MODE_INVALID')
  if (mode !== 'manual') return { mode }
  const tokenLimit = selected?.tokenLimit ?? config.workerTokenLimit, callLimit = selected?.callLimit ?? config.workerCallLimit
  if (!positive(tokenLimit) || !positive(callLimit)) throw budgetError('WORKER_BUDGET_LIMIT_INVALID')
  return { mode, tokenLimit, callLimit }
}
export function budgetUsage(raw) {
  const usage = raw && typeof raw === 'object' ? Object.fromEntries(['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens'].filter(k => Number.isSafeInteger(raw[k]) && raw[k] >= 0).map(k => [k, raw[k]])) : null
  const complete = !!usage && 'inputTokens' in usage && 'outputTokens' in usage && Object.values(raw).every(n => Number.isSafeInteger(n) && n >= 0)
  const total = usage ? ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'].reduce((n, k) => n + (usage[k] ?? 0), 0) : null
  return { usage, complete, total }
}
export function estimateRequestTokens(options) {
  return { input: Math.max(1, Math.ceil(JSON.stringify({ system: options.system || '', messages: options.messages || [], tools: options.tools || [] }).length / 3)), output: positive(options.maxTokens) ? options.maxTokens : null }
}

export class WorkerBudgetRuntime {
  constructor({ config, resolveSession = () => null, listSessions = () => [], journal }) {
    if (!journal) throw new TypeError('WORKER_BUDGET_JOURNAL_REQUIRED')
    Object.assign(this, { config, resolveSession, listSessions, journal })
    this.states = new Map(); this.ensuring = new Map(); this.teamRuns = new WeakMap(); this.closed = false
  }
  root(session) {
    if (!isWorkerSession(session)) return session
    const seen = new Set(), original = session
    while (isWorkerSession(session)) {
      if (seen.has(session.id)) throw budgetError('WORKER_BUDGET_ANCESTRY_CYCLE')
      seen.add(session.id); const parent = session.header?.parentSession
      if (!parent || !this.resolveSession(parent)) throw budgetError('WORKER_BUDGET_PARENT_UNAVAILABLE', `Cannot resolve trusted parent of ${original.id}.`)
      session = this.resolveSession(parent)
    }
    return session
  }
  trustedLead(parent) {
    const root = parent?.session
    if (this.closed) throw budgetError('WORKER_BUDGET_DISPOSED')
    if (!root || isWorkerSession(root) || this.resolveSession(root.id) !== root) throw budgetError('WORKER_BUDGET_LEAD_REQUIRED')
    return root
  }
  async events(root) { return (await this.journal.read(root.id)).events.filter(e => e?.data?.root_session_id === root.id) }
  append(rootId, suffix, data) { return this.journal.append(rootId, eventName(suffix), { version: 3, root_session_id: rootId, owner_session_id: rootId, ...plain(data) }) }
  async restore(session, root, snapshot = null) {
    if (this.states.has(session.id)) return this.states.get(session.id)
    const journal = snapshot || await this.journal.read(root.id)
    const events = records => records.filter(e => e.data?.worker_session_id === session.id)
    const workerEvents = events(journal.events || []), frozen = workerEvents.find(e => e.type === eventName('frozen'))
    if (!frozen) return null
    const state = { session, rootId: root.id, profile: frozen.data.profile, phase: frozen.data.phase,
      calls: callsFrom(workerEvents, session.id), decision: frozen.data.decision || null,
      policyBinding: frozen.data.policy_binding || null, failure: frozen.data.failure || null,
      restored: true, serial: Promise.resolve() }
    for (const event of workerEvents) if (event.type === eventName('failure')) {
      state.phase = 'failed'; state.failure = event.data.failure
    }
    if (state.profile?.mode === 'auto' && !state.decision) {
      state.phase = 'failed'; state.failure = 'WORKER_BUDGET_DECISION_REQUIRED'
    }
    this.states.set(session.id, state)
    return state
  }
  async beginTeamRun(parent, { roles, decisions }) {
    const root = this.trustedLead(parent), profile = workerBudgetProfile(this.config(), root.id)
    if (!Array.isArray(roles) || !roles.length || new Set(roles).size !== roles.length || roles.some(r => !['implementer', 'tester', 'reviewer'].includes(r))) throw budgetError('WORKER_BUDGET_ROLES_INVALID')
    const chosen = {}
    if (profile.mode === 'auto') {
      if (!decisions || typeof decisions !== 'object' || Object.keys(decisions).some(r => !roles.includes(r))) throw decisionRequired()
      for (const role of roles) { const d = validateWorkerDecision({ ...decisions[role], task: role }); chosen[role] = { tokenLimit: d.tokenLimit, callLimit: d.callLimit, reason: d.reason } }
    } else if (decisions !== undefined) throw budgetError('USER_WORKER_LIMITS_AUTHORITATIVE')
    const record = { run_id: randomUUID(), profile: plain(profile), roles: [...roles], decisions: chosen, frozen_at: Date.now() }
    await this.append(root.id, 'team-run', record)
    const handle = Object.freeze({ run_id: record.run_id, profile: Object.freeze({ ...profile }) })
    this.teamRuns.set(handle, { root, record, issued: new Set(), closed: false }); return handle
  }
  async issueTeamWorker(parent, handle, { task, label }) {
    const root = this.trustedLead(parent), team = this.teamRuns.get(handle)
    if (!team || team.closed || team.root !== root) throw budgetError('WORKER_BUDGET_TEAM_HANDLE_INVALID')
    if (!team.record.roles.includes(label) || team.issued.has(label) || typeof task !== 'string' || !task.trim()) throw budgetError('WORKER_BUDGET_ROLE_ALREADY_ISSUED')
    const choice = team.record.decisions[label], profile = team.record.profile.mode === 'auto' ? { mode: 'auto', tokenLimit: choice.tokenLimit, callLimit: choice.callLimit } : team.record.profile
    const allocation_id = randomUUID(), prompt = `${fixedMarker(allocation_id)}\n${task}`
    const data = { authority: 'fixed-team-run', allocation_id, run_id: team.record.run_id, task, task_sha256: hash(task), prompt_sha256: hash(prompt), prompt, label, profile, ...(choice || {}), decided_at: team.record.frozen_at, decided_by: team.record.profile.mode === 'auto' ? 'current_lead_tool_call' : 'user_settings_at_team_start' }
    await this.append(root.id, 'allocation', data); team.issued.add(label)
    return { allocation_id, prompt, profile: { ...profile }, ...(choice || {}) }
  }
  async finishTeamRun(parent, handle) {
    const team = this.teamRuns.get(handle)
    if (!team || team.closed) return
    if (parent?.session !== team.root) throw budgetError('WORKER_BUDGET_LEAD_REQUIRED')
    team.closed = true
    try { await this.append(team.root.id, 'team-run-ended', { run_id: team.record.run_id, ended_at: Date.now(), unbound_allocations: 'revoked' }) } finally { this.teamRuns.delete(handle) }
  }
  async plan(parent, requested) {
    const root = this.trustedLead(parent)
    if (workerBudgetProfile(this.config(), root.id).mode !== 'auto') throw budgetError('WORKER_BUDGET_AUTO_REQUIRED')
    const d = validateWorkerDecision(requested), allocation_id = randomUUID(), prompt = `${marker(allocation_id)}\n${d.task}`
    const data = { allocation_id, task_sha256: hash(d.task), prompt_sha256: hash(prompt), ...d, prompt, decided_at: Date.now(), decided_by: 'current_lead_tool_call' }
    const result = await this.append(root.id, 'allocation', data)
    return { allocation_id, prompt, tokenLimit: d.tokenLimit, callLimit: d.callLimit, reason: d.reason, audit: { revision: result.journal.revision, head_hash: result.journal.head_hash } }
  }
  async allocation(root, session, incoming, fixed) {
    if (session.header?.parentSession !== root.id) throw decisionRequired()
    const prompt = assignment(session, incoming), found = (fixed ? /^\[DPSWARM_FIXED_WORKER_BUDGET_V1:([0-9a-f-]{36})\]\n/ : /^\[DPSWARM_WORKER_BUDGET_V1:([0-9a-f-]{36})\]\n/).exec(prompt)
    if (!found) throw decisionRequired()
    const allocationId = found[1], task = prompt.slice(found[0].length)
    const tx = await this.journal.transaction(root.id, snapshot => {
      const events = snapshot.events.filter(e => e?.data?.root_session_id === root.id), issued = events.filter(e => e.type === eventName('allocation') && e.data.allocation_id === allocationId)
      if (issued.length !== 1) throw decisionRequired()
      const a = issued[0].data
      if (a.task_sha256 !== hash(task) || a.prompt_sha256 !== hash(prompt) || a.prompt !== prompt || fixed !== (a.authority === 'fixed-team-run')) throw decisionRequired()
      let profile = { mode: 'auto', tokenLimit: a.tokenLimit, callLimit: a.callLimit }
      if (fixed) {
        const runs = events.filter(e => e.type === eventName('team-run') && e.data.run_id === a.run_id)
        if (runs.length !== 1 || !runs[0].data.roles.includes(a.label)) throw decisionRequired()
        const record = runs[0].data, selected = record.profile.mode === 'auto' ? record.decisions[a.label] : record.profile
        profile = record.profile.mode === 'unlimited' ? { mode: 'unlimited' } : { mode: record.profile.mode, tokenLimit: selected.tokenLimit, callLimit: selected.callLimit }
        if (!same(profile, a.profile) || (profile.mode === 'auto' && a.reason !== selected.reason)) throw decisionRequired()
      }
      if (profile.mode === 'auto') validateWorkerDecision(a)
      const bindings = events.filter(e => e.type === eventName('allocation-bound') && e.data.allocation_id === allocationId)
      if (bindings.some(e => e.data.worker_session_id !== session.id) || bindings.length > 1) throw decisionRequired()
      if (fixed && !bindings.length && events.some(e => e.type === eventName('team-run-ended') && e.data.run_id === a.run_id)) throw decisionRequired()
      const result = { allocation_id: allocationId, task_sha256: a.task_sha256, profile, ...(profile.mode === 'unlimited' ? {} : { tokenLimit: profile.tokenLimit, callLimit: profile.callLimit }), ...(a.reason === undefined ? {} : { reason: a.reason }), ...(fixed ? { run_id: a.run_id, authority: a.authority } : {}), ...(a.label === undefined ? {} : { label: a.label }), decided_by: a.decided_by, decided_at: a.decided_at }
      return { events: bindings.length ? [] : [{ type: eventName('allocation-bound'), data: { version: 3, root_session_id: root.id, allocation_id: allocationId, worker_session_id: session.id, owner_session_id: session.id, task_sha256: a.task_sha256, bound_at: Date.now() } }], result }
    })
    return tx.result
  }
  async ensure(agent, signal, incoming = []) {
    if (!isWorkerSession(agent.session)) return null
    signal?.throwIfAborted(); if (this.closed) throw budgetError('WORKER_BUDGET_DISPOSED')
    if (this.ensuring.has(agent.session.id)) return this.ensuring.get(agent.session.id)
    const pending = this.initialize(agent.session, incoming, signal); this.ensuring.set(agent.session.id, pending)
    try { return await pending } finally { this.ensuring.delete(agent.session.id) }
  }
async initialize(session, incoming, signal) {
    let first = ''; try { first = assignment(session, incoming) } catch {}
    const fixed = first.startsWith('[DPSWARM_FIXED_WORKER_BUDGET_V1:')
    let root
    try { root = this.root(session) } catch (error) {
      if (!fixed && (this.config().workerBudgetMode ?? 'unlimited') === 'unlimited'
        && !(this.config().workerBudgetSessionOverrides || []).some(o => o.mode !== 'unlimited')) return null
      throw error
    }
    let configured = fixed ? null : workerBudgetProfile(this.config(), root.id)
    let snapshot
    try { snapshot = await this.journal.read(root.id) }
    catch (error) {
      if (configured?.mode === 'unlimited' && !fixed) {
        const state = { session, rootId: root.id, profile: configured, decision: null, policyBinding: null,
          phase: 'ready', calls: new Map(), failure: null, restored: false, serial: Promise.resolve(),
          auditFailure: 'UNLIMITED_OBSERVATION_NOT_PERSISTED' }
        this.states.set(session.id, state)
        return state
      }
      throw budgetError(error.code || 'WORKER_BUDGET_LEDGER_UNAVAILABLE')
    }
    const restored = await this.restore(session, root, snapshot)
    if (restored) {
      if (restored.phase === 'failed') throw budgetError(restored.failure || 'WORKER_BUDGET_NOT_READY')
      return restored
    }
    // A missing ledger may initialize a new child only. Native evidence of an
    // already-running child is never treated as an unlimited fresh start.
    if (snapshot.missing && nativeProgress(session) && (fixed || configured?.mode !== 'unlimited')) {
      throw budgetError('WORKER_BUDGET_LEDGER_MISSING')
    }
    let profile = configured, decision = null, policyBinding = null
    if (fixed || profile.mode === 'auto') {
      const allocation = await this.allocation(root, session, incoming, fixed); profile = allocation.profile
      if (profile.mode === 'auto') decision = allocation
      if (fixed) policyBinding = { authority: allocation.authority, run_id: allocation.run_id,
        allocation_id: allocation.allocation_id, label: allocation.label, profile }
    }
    const state = { session, rootId: root.id, profile, decision, policyBinding, phase: 'ready', calls: new Map(),
      failure: null, restored: false, serial: Promise.resolve() }
    try {
      const frozen = await this.journal.transaction(root.id, latest => {
        const prior = (latest.events || []).filter(event => event.type === eventName('frozen')
          && event.data?.worker_session_id === session.id)
        if (prior.length > 1) throw budgetError('WORKER_BUDGET_FROZEN_CONFLICT')
        if (prior.length === 1) {
          const data = prior[0].data
          if ( !same(data.profile, profile)
            ||  !same(data.decision || null, decision || null)
            ||  !same(data.policy_binding || null, policyBinding || null)) {
            throw budgetError('WORKER_BUDGET_FROZEN_CONFLICT')
          }
          return { events: [], restored: true }
        }
        return { events: [{ type: eventName('frozen'), data: { version: 3, root_session_id: root.id,
          owner_session_id: session.id, worker_session_id: session.id, profile, decision,
          policy_binding: policyBinding, phase: state.phase } }] }
      })
      if (frozen.restored) {
        const durable = await this.restore(session, root, frozen.journal)
        if (!durable) throw budgetError('WORKER_BUDGET_FROZEN_MISSING')
        return durable
      }
    } catch (error) {
      if (profile.mode !== 'unlimited') throw budgetError(error.code || 'WORKER_BUDGET_BINDING_NOT_DURABLE')
      state.auditFailure = 'UNLIMITED_OBSERVATION_NOT_PERSISTED'
    }
    this.states.set(session.id, state)
    signal?.throwIfAborted()
    if (this.closed) throw budgetError('WORKER_BUDGET_DISPOSED')
    return state
  }
  totals(state) { return totalsFor(state.calls) }
  guard(state) {
    if (!state || state.profile.mode === 'unlimited') return
    if (state.phase !== 'ready') throw budgetError(state.failure || 'WORKER_BUDGET_NOT_READY')
    const t = this.totals(state); if (t.calls >= state.profile.callLimit) throw budgetError('WORKER_CALL_LIMIT_REACHED'); if (t.committed_tokens >= state.profile.tokenLimit) throw budgetError('WORKER_TOKEN_LIMIT_REACHED')
  }
  outputLimit(state, inputEstimate, requested) {
    if (!state || state.profile.mode === 'unlimited') return requested
    this.guard(state); const remain = state.profile.tokenLimit - this.totals(state).committed_tokens - Math.max(1, inputEstimate)
    if (remain < 1) throw budgetError('WORKER_TOKEN_RESERVATION_DENIED')
    return Math.min(positive(requested) ? requested : remain, remain)
  }
  serial(state, work) { const next = state.serial.catch(() => undefined).then(work); state.serial = next; return next }
async admit(state, options) {
    if (!state) return null
    return this.serial(state, async () => {
      const unlimited = state.profile.mode === 'unlimited'
      if (!unlimited && state.phase !== 'ready') throw budgetError(state.failure || 'WORKER_BUDGET_NOT_READY')
      const estimate = estimateRequestTokens(options)
      if (!unlimited && estimate.output === null) throw budgetError('WORKER_OUTPUT_LIMIT_REQUIRED')
      const reservation = unlimited ? null : estimate.input + estimate.output
      const call = { call_id: randomUUID(), worker_session_id: state.session.id, owner_session_id: state.session.id,
        root_session_id: state.rootId, purpose: options.purpose || 'worker', provider: options.provider, model: options.model,
        reserved_tokens: reservation, input_estimate: estimate.input, output_limit: estimate.output, started_at: Date.now(),
        status: 'active', usage: null, usage_complete: false, observed_tokens: null }
      try {
        const committed = await this.journal.transaction(state.rootId, snapshot => {
          const events = (snapshot.events || []).filter(event => event?.data?.root_session_id === state.rootId)
          const frozen = events.filter(event => event.type === eventName('frozen')
            && event.data?.worker_session_id === state.session.id)
          if (!unlimited && (snapshot.missing || frozen.length !== 1
            || !same(frozen[0].data.profile, state.profile))) {
            throw budgetError('WORKER_BUDGET_FROZEN_MISSING')
          }
          const calls = callsFrom(events, state.session.id), totals = totalsFor(calls)
          if (!unlimited) {
            if (totals.calls >= state.profile.callLimit) throw budgetError('WORKER_CALL_LIMIT_REACHED')
            if (totals.committed_tokens >= state.profile.tokenLimit || totals.committed_tokens + reservation > state.profile.tokenLimit) {
              throw budgetError('WORKER_TOKEN_RESERVATION_DENIED')
            }
          }
          calls.set(call.call_id, call)
          return { events: [{ type: eventName('admitted'), data: { version: 3, ...call } }], calls }
        })
        state.calls = committed.calls
        return { state, call }
      } catch (error) {
        // A limited worker must never dispatch after an uncertain admission.
        // The next attempt folds the authoritative ledger, including a POST
        // that committed before a transport failure.
        if (!unlimited) throw budgetError(error.code || 'WORKER_BUDGET_ADMISSION_NOT_DURABLE')
        state.auditFailure = 'UNLIMITED_OBSERVATION_NOT_PERSISTED'
        state.calls.set(call.call_id, call)
        return { state, call }
      }
    })
  }
  async settle(ticket, rawUsage, outcome, failure) {
    if (!ticket) return
    const { state, call } = ticket
    return this.serial(state, async () => {
      const u = budgetUsage(rawUsage), data = { worker_session_id: state.session.id, owner_session_id: state.session.id, call_id: call.call_id, status: outcome || 'completed', usage: u.usage, usage_complete: u.complete, observed_tokens: u.total, finished_at: Date.now(), failure: failure || null }
      try { await this.append(state.rootId, 'settled', data) } catch (error) { if (state.profile.mode !== 'unlimited') throw budgetError(error.code || 'WORKER_BUDGET_SETTLEMENT_NOT_DURABLE'); state.auditFailure = 'UNLIMITED_OBSERVATION_NOT_PERSISTED' }
      Object.assign(call, data)
    })
  }
  describe(state) {
    const totals = this.totals(state), limited = state.profile.mode !== 'unlimited'
    return { worker_session_id: state.session.id, root_session_id: state.rootId, ...state.profile, phase: state.phase, frozen: true, ...totals, remaining_tokens: limited ? Math.max(0, state.profile.tokenLimit - totals.committed_tokens) : null, remaining_calls: limited ? Math.max(0, state.profile.callLimit - totals.calls) : null, decision: state.decision, policy_binding: state.policyBinding || null, failure: state.failure, audit_warning: state.auditFailure || null, recent: [...state.calls.values()].slice(-8).map(c => ({ ...c })) }
  }
  async status(agent) {
    if (isWorkerSession(agent.session)) { try { const state = await this.restore(agent.session, this.root(agent.session)); return state ? this.describe(state) : { worker_session_id: agent.session.id, frozen: false } } catch (e) { return { worker_session_id: agent.session.id, frozen: false, error: e.code || 'WORKER_BUDGET_STATUS_UNAVAILABLE' } } }
    const children = [], seen = new Set()
    for (const s of this.listSessions()) if (isWorkerSession(s)) try {
      const state = await this.restore(s, this.root(s))
      if (state?.rootId === agent.session.id) { children.push(this.describe(state)); seen.add(s.id) }
    } catch {}
    // A host may dispose a completed child before the Lead reads status. Keep
    // previously observed state visible for this process; cold restart rebuilds
    // it from the durable ledger.
    for (const state of this.states.values()) if (state.rootId === agent.session.id && !seen.has(state.session.id)) children.push(this.describe(state))
    return { scope: 'independent worker limits; no team pool and no Lead limit', lead_limited: false, configured: workerBudgetProfile(this.config(), agent.session.id), children, coverage: 'sidecar audit ledger; legacy DSH custom records are export-only' }
  }
  shutdown() { this.closed = true }
}
