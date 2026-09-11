import { createHash } from 'node:crypto'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'
import { teamModeFor, teamModeGuidance } from './role-guidance.js'
import { sessionEvents, forkBoundary } from './host-session-compat.js'

const host = resolveHostRoot()
const [{ createUserMessage }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
])

export const TEAM_REQUIRED_CODE = 'TEAM_REQUIRED'

const BOUND = 'dpswarm/team-required-bound'
const STARTED = 'dpswarm/team-required-started'
const FINISHED = 'dpswarm/team-required-finished'
const RETRYABLE_ADMISSION_FAILURE = 'retryable_admission_failure'
const VERSION = 1

const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value
const hash = value => createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex')
const error = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const clone = value => JSON.parse(JSON.stringify(value))

const enabled = (config, id) => Array.isArray(config?.enabledSessions) && config.enabledSessions.includes(id)
const isRoot = agent => !!agent?.session && agent.id === agent.session.id
  && !(agent.session.header?.origin === 'subagent') && (agent.session.header?.delegationDepth ?? 0) === 0

const messageFrom = event => event?.data?.message || event?.data
const directUserMessage = session => {
  const events = sessionEvents(session)
  // The fork boundary (old host header.seedLength, new host inheritedEventCount)
  // is durable: a fork must never inherit its parent's task binding from
  // copied transcript history.
  const firstOwn = forkBoundary(session)
  for (let index = events.length - 1; index >= firstOwn; index--) {
    if (events[index]?.type !== 'user/message') continue
    const message = messageFrom(events[index])
    if (message?.role === 'user' && message.source?.kind === 'user'
      && typeof message.id === 'string' && Array.isArray(message.content)) return message
  }
  return null
}

const bindingId = (rootId, message) => hash({ root_session_id: rootId, user_message_id: message.id, user_content_sha256: hash(message.content) })
const bindingData = (rootId, message) => ({
  version: VERSION,
  root_session_id: rootId,
  owner_session_id: rootId,
  binding_id: bindingId(rootId, message),
  user_message_id: message.id,
  user_content_sha256: hash(message.content),
})

const relevant = (snapshot, rootId) => (snapshot?.events || []).filter(event => event?.data?.root_session_id === rootId)

function stateFor(snapshot, binding) {
  const events = relevant(snapshot, binding.root_session_id)
  const bound = events.filter(event => event.type === BOUND && event.data?.binding_id === binding.binding_id)
  if (bound.length !== 1) throw error('TEAM_REQUIRED_BINDING_INVALID', 'The durable task binding is missing or ambiguous.')
  const own = event => event.data?.binding_id === binding.binding_id
  const allStarts = events.filter(event => event.type === STARTED && own(event))
  const finishes = events.filter(event => event.type === FINISHED && own(event))
  if (!allStarts.length) {
    if (finishes.length) throw error('TEAM_REQUIRED_BINDING_INVALID', 'A fixed-team terminal event requires a native child binding.')
    return { phase: 'required', binding, starts: [], allStarts: [], finish: null, retry: null, activeRunId: null }
  }
  const startsByRun = new Map()
  for (const start of allStarts) {
    const runId = start.data?.run_id
    if (typeof runId !== 'string' || !runId) throw error('TEAM_REQUIRED_BINDING_INVALID', 'A native child binding is missing its run id.')
    if (!startsByRun.has(runId)) startsByRun.set(runId, [])
    startsByRun.get(runId).push(start)
  }
  const terminalByRun = new Map()
  let finalFinish = null
  for (const finish of finishes) {
    const data = finish.data || {}, retryable = data.outcome === RETRYABLE_ADMISSION_FAILURE
    const runId = typeof data.run_id === 'string' && data.run_id ? data.run_id : null
    if (!runId || !startsByRun.has(runId) || terminalByRun.has(runId)
      || (!retryable && data.outcome !== 'completed' && data.outcome !== 'failed_takeover')
      || (!retryable && finalFinish)) {
      throw error('TEAM_REQUIRED_BINDING_INVALID', 'The durable fixed-team lifecycle is inconsistent.')
    }
    terminalByRun.set(runId, finish)
    if (!retryable) finalFinish = finish
  }
  const latestStart = allStarts[allStarts.length - 1], activeRunId = latestStart.data.run_id
  if (finalFinish) {
    if (finalFinish !== finishes[finishes.length - 1] || finalFinish.data.run_id !== activeRunId) {
      throw error('TEAM_REQUIRED_BINDING_INVALID', 'A settled fixed-team run cannot be followed by another attempt.')
    }
    return { phase: 'finished', binding, starts: startsByRun.get(activeRunId), allStarts, finish: finalFinish.data, retry: null, activeRunId }
  }
  const latestTerminal = terminalByRun.get(activeRunId)
  if (latestTerminal) {
    return { phase: 'required', binding, starts: [], allStarts, finish: null, retry: latestTerminal.data, activeRunId: null }
  }
  return { phase: 'started', binding, starts: startsByRun.get(activeRunId), allStarts, finish: null, retry: null, activeRunId }
}

const isZero = value => Number.isSafeInteger(value) && value === 0
const matchingDiagnostic = (event, { rootId, runId, sessionId }) => {
  const record = event?.data, diagnostic = record?.diagnostic, budget = diagnostic?.budget
  return record?.root_session_id === rootId && record?.run_id === runId
    && record?.worker_session_id === sessionId && diagnostic?.worker_session_id === sessionId
    && diagnostic?.native_terminal && typeof diagnostic.native_terminal === 'object' && diagnostic?.cleanup?.physical_cleanup_confirmed === true
    && diagnostic?.evidence?.native_terminal_available === true && !diagnostic?.evidence?.budget_error
    && !diagnostic?.audit_error && !diagnostic?.evidence?.error && !budget?.audit_warning
    && diagnostic?.failure?.code === 'WORKER_TOKEN_RESERVATION_DENIED'
    && budget?.root_session_id === rootId && budget?.worker_session_id === sessionId
    && isZero(budget.calls) && isZero(budget.unknown_usage_calls) && isZero(budget.active_calls)
    && isZero(budget.committed_tokens) && isZero(budget.observed_tokens_lower_bound)
}

const openTools = new Set(['dpswarm_status', 'dpswarm_models', 'dpswarm_run', 'dpswarm_review', 'dpswarm_report', 'dpswarm_mailbox', 'read', 'grep', 'glob', 'list'])

/**
 * Durable root-task gate for an explicitly enabled fixed team.  It never
 * chooses models or budgets: the existing controller still owns that work.
 */
export class TeamRequirement {
  constructor({ config, journal } = {}) {
    if (typeof config !== 'function') throw new TypeError('TEAM_REQUIRED_CONFIG_REQUIRED')
    if (!journal || typeof journal.read !== 'function' || typeof journal.transaction !== 'function') {
      throw new TypeError('TEAM_REQUIRED_JOURNAL_REQUIRED')
    }
    this.config = config
    this.journal = journal
    this.reminded = new Set()
  }

  applicable(agent) {
    return isRoot(agent) && enabled(this.config(), agent.session.id)
  }

  /** Read the newest durable user task and bind it atomically to this root. */
  async beforeRun(agent) {
    if (!this.applicable(agent)) return { required: false, phase: 'off', binding: null }
    const rootId = agent.session.id, message = directUserMessage(agent.session)
    if (!message) throw error('TEAM_REQUIRED_TASK_MISSING', 'The enabled team needs a durable user task before work can continue.')
    const binding = bindingData(rootId, message)
    const result = await this.journal.transaction(rootId, snapshot => {
      const events = relevant(snapshot, rootId)
      const same = events.filter(event => event.type === BOUND && event.data?.binding_id === binding.binding_id)
      if (same.length > 1) throw error('TEAM_REQUIRED_BINDING_INVALID', 'The task has more than one durable binding.')
      if (same.length === 1) {
        const prior = same[0].data
        if (prior.user_message_id !== binding.user_message_id || prior.user_content_sha256 !== binding.user_content_sha256) {
          throw error('TEAM_REQUIRED_BINDING_INVALID', 'The durable task binding does not match the original user message.')
        }
        return { events: [], binding }
      }
      return { events: [{ type: BOUND, data: { ...binding, bound_at: Date.now() } }], binding }
    })
    return { required: true, ...stateFor(result.journal, binding) }
  }

  async status(agent) {
    const state = await this.beforeRun(agent)
    if (!state.required) return { enabled: false, phase: 'off' }
    return {
      enabled: true,
      phase: state.phase,
      binding: {
        root_session_id: state.binding.root_session_id,
        user_message_id: state.binding.user_message_id,
        user_content_sha256: state.binding.user_content_sha256,
        binding_id: state.binding.binding_id,
      },
      native_child_bound_count: state.starts.length,
      model_request_observed: null,
            ...(state.finish ? { finish: clone(state.finish) } : {}),
      ...(state.retry ? { retry: clone(state.retry) } : {}),
      next: state.phase === 'required' ? state.retry
        ? 'All published child sessions settled with durable zero-call reservation denials and no delivery. Correct the budget, then call dpswarm_run again for this same user task.'
        : `Call dpswarm_run for this user task. ${teamModeGuidance(teamModeFor(this.config(), agent.session.id))}`
        : state.phase === 'started' ? 'A native child is bound but the team run is not settled; restore/review it before new work.'
          : 'The fixed-team run settled; Lead follow-up is permitted.',
    }
  }

  async preExecute(exec, next) {
    const agent = exec?.agent
    if (!this.applicable(agent)) return next()
    const state = await this.beforeRun(agent)
    if (state.phase === 'finished') {
      if (exec.name === 'dpswarm_run') return { kind: 'deny', reason: 'TEAM_REQUIRED_ALREADY_FULFILLED: The current user task already has a settled fixed-team run. A new user task creates a new requirement.' }
      return next()
    }
    // Code Mode's outer transport must remain callable so its SDK can invoke
    // dpswarm_run. Every SDK sub-dispatch has parent set and re-enters here.
    if (exec.name === 'run_code' && exec.parent === undefined) return next()
    if (state.phase === 'started' && exec.name === 'dpswarm_run') {
      return { kind: 'deny', reason: 'TEAM_REQUIRED_REVIEW_REQUIRED: A native child is already bound for this task; restore/review the existing run instead of starting another.' }
    }
    if (openTools.has(exec.name)) return next()
    const recovery = state.phase === 'started'
      ? 'A native child is already bound and the fixed-team run is unfinished; restore or review it before other work.'
      : 'Call dpswarm_run for the current user task before using other tools.'
    return { kind: 'deny', reason: `${TEAM_REQUIRED_CODE}: ${recovery}` }
  }

  async beforeDispatch(agent) {
    const state = await this.beforeRun(agent)
    if (!state.required) return state
    if (state.phase === 'finished') throw error('TEAM_REQUIRED_ALREADY_FULFILLED', 'The current user task already has a settled fixed-team run.')
    if (state.phase === 'started') throw error('TEAM_REQUIRED_REVIEW_REQUIRED', 'A native child is already bound; restore/review that run instead of starting another.')
    return state
  }

  async markStarted(binding, details = {}) {
    if (binding?.required === false) return binding
    const normalized = this.#normalizeBinding(binding)
    const execution = {
      run_id: this.#requiredString(details.run_id, 'TEAM_REQUIRED_RUN_ID_REQUIRED'),
      execution_session_id: this.#requiredString(details.execution_session_id, 'TEAM_REQUIRED_EXECUTION_SESSION_REQUIRED'),
      role: this.#requiredString(details.role, 'TEAM_REQUIRED_ROLE_REQUIRED'),
    }
    const result = await this.journal.transaction(normalized.root_session_id, snapshot => {
      const state = stateFor(snapshot, normalized)
      if (state.phase === 'finished') throw error('TEAM_REQUIRED_ALREADY_FINISHED', 'This task was already settled.')
      if (state.phase === 'started' && state.activeRunId !== execution.run_id) {
        throw error('TEAM_REQUIRED_ATTEMPT_ACTIVE', 'Another native attempt is already bound for this task.')
      }
      if (state.phase !== 'started' && state.allStarts.some(event => event.data.run_id === execution.run_id)) {
        throw error('TEAM_REQUIRED_ATTEMPT_CLOSED', 'A closed zero-call attempt cannot publish a late child.')
      }
      const duplicate = state.allStarts.find(event => event.data.execution_session_id === execution.execution_session_id)
      if (duplicate) {
        if (duplicate.data.run_id !== execution.run_id || duplicate.data.role !== execution.role || state.phase !== 'started') {
          throw error('TEAM_REQUIRED_START_INVALID', 'A child session cannot be rebound to a different attempt or role.')
        }
        return { events: [], state }
      }
      return { events: [{ type: STARTED, data: { version: VERSION, root_session_id: normalized.root_session_id,
        owner_session_id: normalized.root_session_id, binding_id: normalized.binding_id,
        execution_kind: 'native_child_bound', ...execution, started_at: Date.now() } }], state: null }
    })
    return result.state || stateFor(result.journal, normalized)
  }

  async finishRun(binding, details = {}) {
    if (binding?.required === false) return binding
    const normalized = this.#normalizeBinding(binding)
    const outcome = details.outcome === 'failed_takeover' ? 'failed_takeover' : details.outcome === 'completed' ? 'completed' : null
    if (!outcome) throw error('TEAM_REQUIRED_FINISH_OUTCOME_REQUIRED', 'Finish outcome must be completed or failed_takeover.')
    const runId = typeof details.run_id === 'string' && details.run_id.trim() ? details.run_id : null
    const result = await this.journal.transaction(normalized.root_session_id, snapshot => {
      const state = stateFor(snapshot, normalized)
      if (state.phase === 'finished') {
        if (state.finish?.outcome === outcome && state.finish?.run_id === runId) return { events: [], state }
        throw error('TEAM_REQUIRED_FINISH_INVALID', 'The fixed-team run already has a different terminal outcome.')
      }
      if (state.starts.length === 0) {
        if (state.retry) throw error('TEAM_REQUIRED_FINISH_INVALID', 'A closed retryable attempt cannot settle this task.')
        return { events: [], state: { ...state, blocked: true } }
      }
      if (!runId) throw error('TEAM_REQUIRED_RUN_ID_REQUIRED', 'Trusted lifecycle detail is required.')
      if (state.activeRunId !== runId) throw error('TEAM_REQUIRED_FINISH_INVALID', 'Only the active attempt can settle this task.')
      return { events: [{ type: FINISHED, data: { version: VERSION, root_session_id: normalized.root_session_id,
        owner_session_id: normalized.root_session_id, binding_id: normalized.binding_id, outcome, run_id: runId,
        ...(typeof details.error_code === 'string' && details.error_code ? { error_code: details.error_code } : {}),
        finished_at: Date.now() } }], state: null }
    })
    return result.state || stateFor(result.journal, normalized)
  }

  async finishAdmissionFailure(binding, evidence = {}) {
    if (binding?.required === false) return binding
    const normalized = this.#normalizeBinding(binding)
    const runId = this.#requiredString(evidence.run_id, 'TEAM_REQUIRED_RUN_ID_REQUIRED')
    const supplied = Array.isArray(evidence.execution_session_ids) ? [...new Set(evidence.execution_session_ids)].sort() : []
    const result = await this.journal.transaction(normalized.root_session_id, snapshot => {
      const state = stateFor(snapshot, normalized)
      if (state.phase === 'required' && state.retry?.run_id === runId) {
        const prior = Array.isArray(state.retry.execution_session_ids) ? state.retry.execution_session_ids : []
        if (evidence.no_delivery === true && prior.length === supplied.length && prior.every((id, index) => id === supplied[index])) return { events: [], state }
        throw error('TEAM_REQUIRED_RETRY_EVIDENCE_REQUIRED', 'A closed retryable attempt cannot be altered.')
      }
      const expected = state.starts.map(event => event.data.execution_session_id).sort()
      const sameSessions = expected.length > 0 && expected.length === supplied.length && expected.every((id, index) => id === supplied[index])
      if (state.phase !== 'started' || state.activeRunId !== runId || evidence.no_delivery !== true || !sameSessions) {
        throw error('TEAM_REQUIRED_RETRY_EVIDENCE_REQUIRED', 'A retry requires every active child, no delivery, and an active attempt.')
      }
      const diagnostics = relevant(snapshot, normalized.root_session_id).filter(event => event.type === 'dpswarm/worker-diagnostic')
      if (!expected.every(sessionId => diagnostics.filter(event => matchingDiagnostic(event, { rootId: normalized.root_session_id, runId, sessionId })).length === 1)) {
        throw error('TEAM_REQUIRED_RETRY_EVIDENCE_REQUIRED', 'Every child needs one durable terminal zero-call diagnostic before retry.')
      }
      return { events: [{ type: FINISHED, data: { version: VERSION, root_session_id: normalized.root_session_id,
        owner_session_id: normalized.root_session_id, binding_id: normalized.binding_id, outcome: RETRYABLE_ADMISSION_FAILURE,
        run_id: runId, execution_session_ids: supplied, no_delivery: true, finished_at: Date.now() } }], state: null }
    })
    return result.state || stateFor(result.journal, normalized)
  }
  async finishRecovered(agent, evidence = {}) {
    const state = await this.beforeRun(agent)
    if (!state.required || state.phase === 'finished') return state
    if (state.phase !== 'started') throw error('TEAM_REQUIRED_START_REQUIRED', 'There is no native child binding to reconcile.')
    const expectedSessions = state.starts.map(event => event.data.execution_session_id).sort()
    const suppliedSessions = Array.isArray(evidence.execution_session_ids) ? [...new Set(evidence.execution_session_ids)].sort() : []
    const sameSessions = expectedSessions.length === suppliedSessions.length && expectedSessions.every((id, index) => id === suppliedSessions[index])
    const sameRun = typeof evidence.run_id === 'string' && evidence.run_id && state.starts.every(event => event.data.run_id === evidence.run_id)
    if (evidence.control_plane_confirmed !== true || evidence.open_worker_slots_used !== 0 || evidence.all_workers_terminal !== true
      || evidence.physical_cleanup_confirmed !== true || !sameRun || !sameSessions) {
      throw error('TEAM_REQUIRED_RECOVERY_EVIDENCE_REQUIRED', 'Only trusted terminal control-plane evidence for every bound native child can settle a recovered run.')
    }
    return this.finishRun(state.binding, { outcome: 'failed_takeover', run_id: evidence.run_id, error_code: 'RECOVERED_TERMINAL_REVIEW' })
  }

  async turnStopping({ agent, turn, signal }) {
    if (signal?.aborted || !this.applicable(agent)) return
    const state = await this.beforeRun(agent)
    if (!state.required || state.phase === 'finished') return
    const key = `${state.binding.binding_id}:${turn}`
    if (this.reminded.has(key)) {
      const recovery = state.phase === 'started'
        ? 'A native child is bound but the fixed-team run is not settled. Restore/review it before ending this task.'
        : 'The enabled fixed team has not started for this user task. Call dpswarm_run before ending.'
      throw error(TEAM_REQUIRED_CODE, recovery)
    }
    this.reminded.add(key)
    const recovery = state.phase === 'started'
      ? 'DPSwarm is enabled. A native child was bound but the team run is unfinished. Restore or review the existing run; do not start a duplicate.'
      : 'DPSwarm is enabled for this user task. Call dpswarm_run now; do not complete the task yourself.'
    agent.steer(createUserMessage({ source: { kind: 'plugin', plugin: 'dpswarm', form: 'notice', summary: 'Fixed team is required before task completion.' },
      content: [{ type: 'text', text: recovery }] }))
  }

  #normalizeBinding(binding) {
    const value = binding?.binding || binding
    if (!value || typeof value.root_session_id !== 'string' || typeof value.binding_id !== 'string'
      || typeof value.user_message_id !== 'string' || typeof value.user_content_sha256 !== 'string') {
      throw error('TEAM_REQUIRED_BINDING_REQUIRED', 'Use the binding returned by beforeRun.')
    }
    return { root_session_id: value.root_session_id, binding_id: value.binding_id,
      user_message_id: value.user_message_id, user_content_sha256: value.user_content_sha256 }
  }

  #requiredString(value, code) {
    if (typeof value !== 'string' || !value.trim()) throw error(code, 'Trusted lifecycle detail is required.')
    return value
  }
}

/** Install root-only native and Code Mode gates. */
export function installTeamRequirement(ctx, options = {}) {
  const requirement = options.requirement || new TeamRequirement(options)
  ctx.provide?.('dpswarmTeamRequirement', requirement)
  ctx.on('tools/pre-execute', (exec, next) => requirement.preExecute(exec, next), { prepend: true, global: true })
  ctx.on('agent/turn-stopping', payload => requirement.turnStopping(payload), { prepend: true, global: true })
  return requirement
}
