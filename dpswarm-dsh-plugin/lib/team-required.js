import { createHash } from 'node:crypto'
import { CONTINUED, resolveTaskContinuation, sameTaskBinding, validateContinuationRequest } from './task-continuity.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'
import { teamModeFor, teamModeGuidance } from './role-guidance.js'
import { sessionEvents, forkBoundary } from './host-session-compat.js'
import { inspectWorkspaceAdmission } from './workspace-admission.js'
import { InfrastructureBlockedStore, infrastructureAdmission, isInfrastructureFailure } from './infrastructure-blocked.js'

const host = resolveHostRoot()
const [{ createUserMessage }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
])

export const TEAM_REQUIRED_CODE = 'TEAM_REQUIRED'

const BOUND = 'dpswarm/team-required-bound'
const STARTED = 'dpswarm/team-required-started'
const FINISHED = 'dpswarm/team-required-finished'
const AMENDED = 'dpswarm/team-required-amended'
const ADMISSION = 'dpswarm/team-required-admission'
const RETRYABLE_ADMISSION_FAILURE = 'retryable_admission_failure'
const VERSION = 1

const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value
const hash = value => createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex')
const error = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const clone = value => JSON.parse(JSON.stringify(value))
const sameBinding = (a, b) => ['root_session_id','binding_id','user_message_id','user_content_sha256'].every(k => a?.[k] === b?.[k])

const enabled = (config, id) => Array.isArray(config?.enabledSessions) && config.enabledSessions.includes(id)
const isRoot = agent => !!agent?.session && agent.id === agent.session.id
  && !(agent.session.header?.origin === 'subagent') && (agent.session.header?.delegationDepth ?? 0) === 0

const messageFrom = event => event?.data?.message || event?.data
const isDirectUser = message => message?.role === 'user' && message.source?.kind === 'user'
  && typeof message.id === 'string' && Array.isArray(message.content)
const directUserMessages = session => {
  const events = sessionEvents(session), queues = { 'next-turn': [], 'next-step': [] }
  const messages = []
  const record = message => {
    const prior = messages.find(m => m.id === message.id)
    if (prior && hash(prior.content) !== hash(message.content)) throw error('TASK_SOURCE_MISMATCH', 'A durable user message id has conflicting content.')
    if (!prior) messages.push(message)
  }
  // Native pre-step runs before user/message commit. Replay only this fork's
  // durable inbox claims; queued, replaced and canceled input is not a task.
  for (const event of events.slice(forkBoundary(session))) {
    if (event?.type === 'user/message') {
      const message = messageFrom(event)
      if (isDirectUser(message)) record(message)
    }
    if (event?.type !== 'agent/inbox/spliced') continue
    const data = event.data, queue = queues[data?.target]
    if (!queue || !Array.isArray(data.inserted) || !Number.isSafeInteger(data.start)
      || data.start < 0 || data.start > queue.length) continue
    const count = data.removedCount ?? 0
    if (!Number.isSafeInteger(count) || count < 0 || data.start + count > queue.length) continue
    const removed = queue.splice(data.start, count, ...data.inserted)
    if (data.outcome === undefined) for (const message of removed) if (isDirectUser(message)) record(message)
  }
  return messages
}
const directUserMessage = session => directUserMessages(session).at(-1) || null

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
  const state = lifecycleFor(snapshot, binding)
  const records = relevant(snapshot, binding.root_session_id)
    .filter(event => event.type === ADMISSION && event.data?.binding_id === binding.binding_id)
  const infrastructure = records.filter(event => event.data.kind === 'infrastructure').at(-1)?.data
  if (state.phase !== 'finished' && infrastructure?.blocked === true) {
    return { ...state, lifecycle_phase: state.phase, phase: 'blocked', admission: infrastructure }
  }
  const admission = records.filter(event => event.data.kind !== 'infrastructure').at(-1)?.data
  return state.phase === 'required' && admission?.blocked === true
    ? { ...state, phase: 'blocked', admission } : state
}

function lifecycleFor(snapshot, binding, seen = new Set()) {
  if (seen.has(binding.binding_id)) throw error('TEAM_REQUIRED_BINDING_INVALID', 'Task amendment cycle.')
  seen.add(binding.binding_id)
  const events = relevant(snapshot, binding.root_session_id)
  const bound = events.filter(event => event.type === BOUND && event.data?.binding_id === binding.binding_id)
  if (bound.length !== 1) throw error('TEAM_REQUIRED_BINDING_INVALID', 'The durable task binding is missing or ambiguous.')
  const own = event => event.data?.binding_id === binding.binding_id
  const allStarts = events.filter(event => event.type === STARTED && own(event))
  const finishes = events.filter(event => event.type === FINISHED && own(event))
  const amendments = events.filter(event => event.type === AMENDED && own(event))
  if (amendments.length) {
    if (amendments.length !== 1 || allStarts.length || finishes.length) throw error('TEAM_REQUIRED_BINDING_INVALID', 'An amendment cannot create a second team lifecycle.')
    const amendment = amendments[0].data
    const previous = events.filter(event => event.type === BOUND && event.data.binding_id === amendment.previous_binding_id)
    if (previous.length !== 1 || !amendment.contract_id || !Number.isSafeInteger(amendment.contract_revision)) throw error('TEAM_REQUIRED_BINDING_INVALID', 'Task amendment evidence is missing.')
    const prior = lifecycleFor(snapshot, previous[0].data, seen)
    if (prior.phase !== 'finished') throw error('TEAM_REQUIRED_BINDING_INVALID', 'Amended task predecessor is not settled.')
    return { ...prior, binding, amended_from: amendment.previous_binding_id, contract_id: amendment.contract_id,
      contract_revision: amendment.contract_revision }
  }
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

const openTools = new Set(['dpswarm_status', 'dpswarm_models', 'dpswarm_run', 'dpswarm_review', 'dpswarm_report', 'dpswarm_mailbox', 'dpswarm_amend_task', 'dpswarm_continue_task', 'dpswarm_verify_rework', 'dpswarm_read_evidence', 'dpswarm_repair_report', 'dpswarm_acceptance', 'read', 'grep', 'glob', 'list'])

/**
 * Durable root-task gate for an explicitly enabled fixed team.  It never
 * chooses models or budgets: the existing controller still owns that work.
 */
export class TeamRequirement {
  constructor({ config, journal, inspectAdmission = inspectWorkspaceAdmission, checkRuntime, readCompletion, infrastructureStateDirectory } = {}) {
    if (typeof config !== 'function') throw new TypeError('TEAM_REQUIRED_CONFIG_REQUIRED')
    if (!journal || typeof journal.read !== 'function' || typeof journal.transaction !== 'function') {
      throw new TypeError('TEAM_REQUIRED_JOURNAL_REQUIRED')
    }
    this.config = config
    this.journal = journal
    this.inspectAdmission = inspectAdmission
    this.checkRuntime = checkRuntime
    this.readCompletion = readCompletion
    this.infrastructureStateDirectory = infrastructureStateDirectory
    this.knownStates = new Map()
    this.runtimeHealth = new Map()
    this.reminded = new Set()
    this.completionReminded = new Set()
  }

  sourceFor(agent, value) {
    const binding = value?.binding || value
    const latest = directUserMessage(agent?.session)
    const current = latest && bindingData(agent.session.id, latest)
    const known = current && this.knownStates.get(current.binding_id)
    const linked = current && known?.continuation && sameBinding(known.binding, binding) && sameBinding(known.message_binding, current)
    const message = linked ? directUserMessages(agent.session).find(m => sameBinding(bindingData(agent.session.id, m), binding)) : latest
    if (!isRoot(agent) || !binding || !message || !sameBinding(bindingData(agent.session.id, message), binding)) {
      throw error('TASK_SOURCE_MISMATCH', 'The task source must match the current durable direct-user message.')
    }
    return { session_id: agent.session.id, message_id: message.id, content: clone(message.content),
      content_hash: hash(message.content), source: 'dph-user-message' }
  }

  async continuityContext(agent) {
    if (!this.applicable(agent)) return { available: false, reason: 'team-disabled' }
    const messages = directUserMessages(agent.session), current = messages.at(-1), previous = messages.at(-2)
    if (!current || !previous) return { available: false, reason: 'no-previous-direct-user-task' }
    const currentBinding = bindingData(agent.session.id, current), previousBinding = bindingData(agent.session.id, previous)
    let snapshot
    try { snapshot = await this.journal.read(agent.session.id) }
    catch (caught) { return { available: false, reason: 'journal-unavailable', code: caught.code || 'JOURNAL_UNAVAILABLE' } }
    const exists = relevant(snapshot, agent.session.id).some(e => e.type === BOUND && sameBinding(e.data, previousBinding))
    if (!exists) return { available: false, reason: 'previous-message-has-no-task-binding', current_binding: currentBinding, previous_binding: previousBinding }
    const prior = resolveTaskContinuation(snapshot, previousBinding), priorState = stateFor(snapshot, prior.binding)
    const linked = relevant(snapshot, agent.session.id).find(e => e.type === CONTINUED && e.data.binding_id === currentBinding.binding_id)
    const preview = message => message.content.filter(c => c.type === 'text').map(c => c.text).join('\n').slice(0, 600)
    return { available: true, decision_required: !linked, current_binding: currentBinding, previous_binding: previousBinding,
      previous_task_binding: prior.binding, previous_phase: priorState.phase,
      previous_run_id: priorState.activeRunId || priorState.finish?.run_id || null,
      ...(priorState.admission ? { previous_admission: clone(priorState.admission) } : {}),
      current_user_text_preview: preview(current), previous_user_text_preview: preview(previous),
      ...(linked ? { continued: clone(linked.data) } : {}),
      next: 'Lead must decide whether this trusted user message continues the same task. If so call dpswarm_continue_task with both message binding ids and a reason. This records continuity only; existing execution, budgets, contract, findings and acceptance stay unchanged. Otherwise start a new task or amend changed requirements explicitly.' }
  }

  /** Explicit root decision only. The transaction never waits on worker completion. */
  async continueTask(agent, args, validatePrevious) {
    if (!this.applicable(agent)) throw error('TASK_CONTINUITY_ROOT_REQUIRED', 'Only an enabled root can continue its task.')
    validateContinuationRequest(args)
    if (typeof validatePrevious !== 'function') throw error('TASK_CONTINUITY_PROOF_REQUIRED', 'The controller must verify the original task and contract.')
    const tx = await this.journal.transaction(agent.session.id, snapshot => {
      const messages = directUserMessages(agent.session), current = messages.at(-1), previous = messages.at(-2)
      if (!current || !previous) throw error('TASK_SOURCE_MISMATCH', 'Two durable direct-user messages are required.')
      const currentBinding = bindingData(agent.session.id, current), previousBinding = bindingData(agent.session.id, previous)
      if (args.current_binding_id !== currentBinding.binding_id || args.previous_binding_id !== previousBinding.binding_id) {
        throw error('TASK_SOURCE_MISMATCH', 'Continue only the newest direct-user message from its immediate predecessor; refresh status after new input.')
      }
      const prior = resolveTaskContinuation(snapshot, previousBinding)
      const proof = validatePrevious(prior.binding)
      if (!proof || proof.root_session_id !== agent.session.id || !proof.contract_id
        || !Number.isSafeInteger(proof.contract_revision) || proof.contract_revision < 1 || !proof.run_id) {
        throw error('TASK_CONTINUITY_PROOF_REQUIRED', 'An existing task contract and run are required; continuation does not create either.')
      }
      const currentResolved = resolveTaskContinuation(snapshot, currentBinding)
      const duplicate = currentResolved.continuation
      if (duplicate) {
        if (duplicate.previous_binding_id !== previousBinding.binding_id || !sameTaskBinding(currentResolved.binding, prior.binding)
          || duplicate.contract_id !== proof.contract_id || duplicate.run_id !== proof.run_id) throw error('TASK_CONTINUITY_CONFLICT', 'The message already continues another task.')
        return { events: [], duplicate: true, message_binding: currentBinding }
      }
      const own = stateFor(snapshot, currentBinding)
      if (own.allStarts.length || own.finish || own.amended_from || !['required', 'blocked'].includes(own.phase)) {
        throw error('TASK_CONTINUITY_CONFLICT', 'The current user message already owns a task execution or amendment.')
      }
      return { events: [{ type: CONTINUED, data: { ...currentBinding, previous_binding_id: previousBinding.binding_id,
        effective_binding_id: prior.binding.binding_id, source_kind: 'direct-user', decision: 'continue_same_task',
        reason: args.reason.trim(), contract_id: proof.contract_id, contract_revision: proof.contract_revision,
        run_id: proof.run_id, continued_at: Date.now() } }], duplicate: false, message_binding: currentBinding }
    })
    const resolved = resolveTaskContinuation(tx.journal, tx.message_binding)
    const state = { required: true, ...stateFor(tx.journal, resolved.binding), message_binding: tx.message_binding,
      continuation: resolved.continuation, journal_anchor: { revision: tx.journal.revision, head_hash: tx.journal.head_hash } }
    this.knownStates.set(state.binding.binding_id, state); this.knownStates.set(tx.message_binding.binding_id, state)
    return { ...state, duplicate: tx.duplicate }
  }

  async amendBinding(agent, previousBinding, current, proof) {
    if (!proof?.contract_id || !Number.isSafeInteger(proof.contract_revision)) throw error('TASK_AMENDMENT_PROOF_REQUIRED', 'A committed acceptance amendment is required.')
    this.sourceFor(agent, current)
    const binding = current.binding || current
    const previous = previousBinding.binding || previousBinding
    const tx = await this.journal.transaction(agent.session.id, snapshot => {
      const prior = stateFor(snapshot, previous)
      if (prior.phase !== 'finished') throw error('REWORK_TASK_NOT_SETTLED', 'Settle the previous execution before amending its requirements.')
      const duplicate = relevant(snapshot, agent.session.id).find(e => e.type === AMENDED && e.data.binding_id === binding.binding_id)
      if (duplicate) {
        if (duplicate.data.previous_binding_id !== previous.binding_id || duplicate.data.contract_id !== proof.contract_id) throw error('TASK_AMENDMENT_CONFLICT', 'The message already amends another task.')
        return { events: [] }
      }
      const next = stateFor(snapshot, binding)
      if (next.phase !== 'required') throw error('TASK_AMENDMENT_CONFLICT', 'The new user message already has an execution.')
      return { events: [{ type: AMENDED, data: { ...binding, previous_binding_id: previous.binding_id,
        contract_id: proof.contract_id, contract_revision: proof.contract_revision, at: Date.now() } }] }
    })
    return { required: true, ...stateFor(tx.journal, binding) }
  }

  applicable(agent) {
    return isRoot(agent) && enabled(this.config(), agent.session.id)
  }

  /** Read the newest durable user task and bind it atomically to this root. */
  async beforeRun(agent) {
    if (!this.applicable(agent)) return { required: false, phase: 'off', binding: null }
    const rootId = agent.session.id, message = directUserMessage(agent.session)
    if (!message) throw error('TEAM_REQUIRED_TASK_MISSING', 'The enabled team needs a durable user task before work can continue.')
    const messageBinding = bindingData(rootId, message)
    let binding = messageBinding
    let health, runtimeError
    if (this.checkRuntime) {
      try { health = await this.checkRuntime(agent); this.runtimeHealth.set(rootId, health) }
      catch (caught) {
        if (['SIDECAR_RUNTIME_INCOMPATIBLE', 'HOST_RUNTIME_INCOMPATIBLE'].includes(caught?.code)) runtimeError = caught
        // Before first use, ordinary ensure() handles connection refusal.
        // An unavailable probe never clears an existing blocker.
      }
    }
    const config = this.config(), admission = await this.inspectAdmission(config, agent)
    const fingerprint = admission ? hash({ config, lease: admission.fingerprint }) : null
    let fallback = this.#store()?.read(binding)
    let observed, result
    try { result = await this.journal.transaction(rootId, snapshot => {
      const events = relevant(snapshot, rootId)
      if (fallback) this.#requireCheckpointContinuity(snapshot, fallback)
      const same = events.filter(event => event.type === BOUND && event.data?.binding_id === messageBinding.binding_id)
      if (same.length > 1) throw error('TEAM_REQUIRED_BINDING_INVALID', 'The task has more than one durable binding.')
      if (same.length === 1) {
        const prior = same[0].data
        if (prior.user_message_id !== messageBinding.user_message_id || prior.user_content_sha256 !== messageBinding.user_content_sha256) {
          throw error('TEAM_REQUIRED_BINDING_INVALID', 'The durable task binding does not match the original user message.')
        }
      }
      const additions = same.length ? [] : [{ type: BOUND, data: { ...messageBinding, bound_at: Date.now() } }]
      const projected = { ...snapshot, events: [...snapshot.events, ...additions] }
      binding = resolveTaskContinuation(projected, messageBinding).binding
      fallback = this.#store()?.read(binding) || fallback
      if (fallback) this.#requireCheckpointContinuity(snapshot, fallback)
      const state = lifecycleFor(projected, binding)
      observed = { required: true, ...state, journal_anchor: { revision: snapshot.revision, head_hash: snapshot.head_hash } }
      const priorInfrastructure = events.filter(event => event.type === ADMISSION && event.data?.binding_id === binding.binding_id
        && event.data?.kind === 'infrastructure').at(-1)?.data || fallback?.admission
      if (state.phase !== 'finished') {
        if (runtimeError) {
          const blocked = infrastructureAdmission(runtimeError)
          if (!priorInfrastructure?.blocked || priorInfrastructure.code !== blocked.code || priorInfrastructure.fingerprint !== blocked.fingerprint) {
            additions.push({ type: ADMISSION, data: { ...binding, ...blocked, run_id: state.activeRunId } })
          }
        } else if (priorInfrastructure?.blocked && this.#runtimeRecovered(priorInfrastructure, health)) {
          // This durable append also proves audit health; failed writes leave
          // the checkpoint blocked and never finish the pending requirement.
          additions.push({ type: ADMISSION, data: { ...binding, kind: 'infrastructure', blocked: false,
            run_id: state.activeRunId, fingerprint: health.fingerprint, recovered_at: Date.now() } })
        }
      }
      if (state.phase === 'required') {
        const prior = events.filter(event => event.type === ADMISSION && event.data?.binding_id === binding.binding_id
          && event.data.kind !== 'infrastructure').at(-1)?.data
        if (admission && (!prior?.blocked || prior.fingerprint !== fingerprint)) additions.push({ type: ADMISSION,
          data: { ...binding, blocked: true, ...admission, fingerprint, observed_at: Date.now() } })
        else if (!admission && prior?.blocked) additions.push({ type: ADMISSION,
          data: { ...binding, blocked: false, observed_at: Date.now() } })
      }
      return { events: additions, binding }
    }) } catch (caught) {
      if (!isInfrastructureFailure(caught)) throw caught
      const prior = observed || fallback || this.knownStates.get(binding.binding_id)
      return this.#checkpoint(binding, prior, runtimeError || caught)
    }
    let state = { required: true, ...stateFor(result.journal, binding),
      journal_anchor: { revision: result.journal.revision, head_hash: result.journal.head_hash } }
    const latestInfrastructure = relevant(result.journal, rootId).filter(event => event.type === ADMISSION
      && event.data?.binding_id === binding.binding_id && event.data.kind === 'infrastructure').at(-1)?.data
    if (fallback && state.phase !== 'finished' && latestInfrastructure?.blocked !== false && state.admission?.kind !== 'infrastructure') {
      state = { ...state, lifecycle_phase: state.phase, phase: 'blocked', admission: fallback.admission }
    } else if (fallback && (state.phase === 'finished' || latestInfrastructure?.blocked === false)) this.#store()?.clear(binding)
    const continuity = resolveTaskContinuation(result.journal, messageBinding)
    state = { ...state, message_binding: messageBinding, continuation: continuity.continuation }
    this.knownStates.set(binding.binding_id, state)
    this.knownStates.set(messageBinding.binding_id, state)
    return state
  }

  async status(agent) {
    const state = await this.beforeRun(agent)
    if (!state.required) return { enabled: false, phase: 'off' }
    return {
      enabled: true,
      phase: state.phase,
      ...(state.lifecycle_phase ? { lifecycle_phase: state.lifecycle_phase } : {}),
      binding: {
        root_session_id: state.binding.root_session_id,
        user_message_id: state.binding.user_message_id,
        user_content_sha256: state.binding.user_content_sha256,
        binding_id: state.binding.binding_id,
      },
      continuity: await this.continuityContext(agent),
      native_child_bound_count: state.starts.length,
      model_request_observed: null,
            ...(state.finish ? { finish: clone(state.finish) } : {}),
      ...(state.retry ? { retry: clone(state.retry) } : {}),
      ...(state.admission ? { admission: clone(state.admission) } : {}),
      next: state.phase === 'blocked' ? `${state.admission.code}: ${state.admission.message} The team requirement remains pending. ${state.admission.kind === 'infrastructure' ? 'Repair the runtime/audit service before resuming this pending task. Report this blocker without retrying model calls or starting a duplicate run.' : 'Do not implement a substitute or repeatedly dispatch while ownership is unchanged.'}` : state.phase === 'required' ? state.retry
        ? 'All published child sessions settled with durable zero-call reservation denials and no delivery. Correct the budget, then call dpswarm_run again for this same user task.'
        : `Call dpswarm_run for this user task. ${teamModeGuidance(teamModeFor(this.config(), agent.session.id))}`
        : state.phase === 'started' ? 'A native child is bound but the team run is not settled; restore/review it before new work.'
          : 'The fixed-team execution settled; Lead follow-up is permitted. This is not delivery acceptance. Read dpswarm_acceptance and settle or explicitly preserve pending items; disclose any held workspace lease.',
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
    if (state.phase === 'blocked' && exec.name === 'dpswarm_run') return { kind: 'deny', reason: `${state.admission.code}: ${state.admission.message}` }
    if (state.phase === 'blocked' && exec.name === 'ask_user_question') return next()
    if (openTools.has(exec.name)) return next()
    const recovery = state.phase === 'blocked'
      ? `${state.admission.code}: ${state.admission.message} Ask the user or report the blockage; keep this task pending until verified recovery.`
      : state.phase === 'started'
      ? 'A native child is already bound and the fixed-team run is unfinished; restore or review it before other work.'
      : 'Call dpswarm_run for the current user task before using other tools.'
    return { kind: 'deny', reason: `${TEAM_REQUIRED_CODE}: ${recovery}` }
  }

  async beforeDispatch(agent) {
    const state = await this.beforeRun(agent)
    if (!state.required) return state
    if (state.phase === 'finished') throw error('TEAM_REQUIRED_ALREADY_FULFILLED', 'The current user task already has a settled fixed-team run.')
    if (state.phase === 'started') throw error('TEAM_REQUIRED_REVIEW_REQUIRED', 'A native child is already bound; restore/review that run instead of starting another.')
    if (state.phase === 'blocked') {
      const blocked = error(state.admission.code, state.admission.message)
      // Dispatcher catch must retain the original blocker identity, not add a
      // fresh admission each time an already-blocked run is denied.
      if (state.admission.kind === 'infrastructure') blocked.details = { fingerprint: state.admission.fingerprint }
      throw blocked
    }
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
    const state = result.state || stateFor(result.journal, normalized)
    this.knownStates.set(normalized.binding_id, { required: true, ...state,
      journal_anchor: { revision: result.journal.revision, head_hash: result.journal.head_hash } })
    return state
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

  /** Native rejection closes the turn as blocked before prepareCall/stream. */
  async preStep({ agent, signal }, next) {
    if (signal?.aborted || !this.applicable(agent) || !directUserMessage(agent.session)) return next()
    const state = await this.beforeRun(agent)
    if (signal?.aborted) return next()
    return state.phase === 'blocked' ? { kind: 'reject' } : next()
  }

  async turnStopping({ agent, turn, signal }) {
    if (signal?.aborted || !this.applicable(agent)) return
    const state = await this.beforeRun(agent)
    if (!state.required || state.phase === 'blocked') return
    if (state.phase === 'finished') {
      // One factual closeout notice, not a new acceptance gate or retry loop.
      // A Lead may finish honestly with partial/blocked work after reading it.
      const key = `${state.binding.binding_id}:${turn}`
      if (!this.readCompletion || this.completionReminded.has(key)) return
      this.completionReminded.add(key)
      let completion
      try { completion = await this.readCompletion(agent, state.binding) }
      catch (caught) { completion = { available: false, attention_required: true,
        reason: caught.code || 'COMPLETION_STATE_UNAVAILABLE', message: String(caught.message || caught) } }
      if (signal?.aborted || !completion?.attention_required) return
      agent.steer(createUserMessage({ source: { kind: 'plugin', plugin: 'dpswarm', form: 'notice', summary: 'Delivery acceptance and remaining recovery work.' },
        content: [{ type: 'text', text: 'The team execution ended, but completion must be described from the current control state below. Decide whether to continue necessary verification, read the report schema and repair the report, explicitly terminate, or report a partial/blocked handoff. You may finish with the exact remaining blocker; do not claim acceptance from native completed or file presentation. Pending items and a held lease are real recovery work. This notice does not change acceptance, budgets, models or ownership.\n' + JSON.stringify(completion) }] }))
      return
    }
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

  /** Internal controller error path; no model/tool argument can set this state. */
  async markInfrastructureBlocked(agent, caught, { run_id: runId } = {}) {
    if (!this.applicable(agent) || !isInfrastructureFailure(caught)) return false
    const message = directUserMessage(agent.session)
    if (!message) return false
    const messageBinding = bindingData(agent.session.id, message)
    let binding = messageBinding
    let checkpoint = this.#store()?.read(binding)
    let observed = checkpoint || this.knownStates.get(binding.binding_id)
    const admission = infrastructureAdmission(caught, this.runtimeHealth.get(agent.session.id)?.fingerprint || null)
    try {
      const result = await this.journal.transaction(agent.session.id, snapshot => {
        binding = resolveTaskContinuation(snapshot, messageBinding).binding
        checkpoint = this.#store()?.read(binding) || checkpoint
        if (checkpoint) this.#requireCheckpointContinuity(snapshot, checkpoint)
        const state = lifecycleFor(snapshot, binding)
        if (state.phase === 'finished' || (runId && state.activeRunId !== runId)) return { events: [], state }
        observed = { required: true, ...state, journal_anchor: { revision: snapshot.revision, head_hash: snapshot.head_hash } }
        const prior = relevant(snapshot, agent.session.id).filter(event => event.type === ADMISSION
          && event.data?.binding_id === binding.binding_id && event.data.kind === 'infrastructure').at(-1)?.data
        return { events: prior?.blocked && prior.code === admission.code && prior.fingerprint === admission.fingerprint ? []
          : [{ type: ADMISSION, data: { ...binding, ...admission, run_id: state.activeRunId } }] }
      })
      const state = { required: true, ...stateFor(result.journal, binding),
        journal_anchor: { revision: result.journal.revision, head_hash: result.journal.head_hash } }
      this.knownStates.set(binding.binding_id, state)
      return state.phase === 'blocked'
    } catch (auditError) {
      if (!isInfrastructureFailure(auditError)) throw auditError
      if (runId && observed?.activeRunId !== runId) throw auditError
      this.#checkpoint(binding, observed, caught)
      return true
    }
  }

  #store() {
    const directory = typeof this.infrastructureStateDirectory === 'function' ? this.infrastructureStateDirectory() : this.infrastructureStateDirectory
    return directory ? new InfrastructureBlockedStore(directory) : null
  }

  #requireCheckpointContinuity(snapshot, checkpoint) {
    const anchor = checkpoint.journal_anchor, expected = checkpoint.allStarts || checkpoint.starts || []
    // Recovery must extend the ledger that actually published these children.
    // A valid but rolled-back/restored ledger cannot silently erase ownership.
    const sameBinding = relevant(snapshot, checkpoint.binding.root_session_id).some(event => event.type === BOUND
      && event.data?.binding_id === checkpoint.binding.binding_id
      && event.data.user_message_id === checkpoint.binding.user_message_id
      && event.data.user_content_sha256 === checkpoint.binding.user_content_sha256)
    const anchored = !anchor || (Number.isSafeInteger(snapshot.revision) && snapshot.revision >= anchor.revision
      && (snapshot.revision !== anchor.revision || snapshot.head_hash === anchor.head_hash))
    const startsRetained = expected.every(start => (snapshot.events || []).some(event => event.type === STARTED && hash(event) === hash(start)))
    if (!anchored || !startsRetained || (expected.length > 0 && (!sameBinding || !anchor))) {
      throw error('PLUGIN_AUDIT_CONTINUITY_LOST', 'The audit no longer contains the authenticated task and published child history. Restore that ledger before reviewing the existing run.')
    }
  }

  #runtimeRecovered(admission, health) {
    if (health?.compatible !== true || typeof health.fingerprint !== 'string') return false
    // A healthy Python service says nothing about the host's composed services.
    // Only the host capability probe can clear its own typed failure.
    if (admission.code === 'HOST_RUNTIME_INCOMPATIBLE') return health.host_runtime?.compatible === true
    // Capability rejection is repaired by a fully compatible live runtime.
    if (admission.code === 'SIDECAR_RUNTIME_INCOMPATIBLE') return true
    // An invalid event/schema on an otherwise compatible runtime needs an
    // actual runtime change; a successful unrelated write is not its repair.
    if (['PLUGIN_AUDIT_INVALID_EVENT', 'PLUGIN_AUDIT_INVALID_READ', 'PLUGIN_AUDIT_INVALID_WRITE'].includes(admission.code)) {
      return admission.fingerprint !== health.fingerprint
    }
    // Other durable-audit failures additionally require the clearing append
    // to succeed, proving actual repair even when the server is unchanged.
    return true
  }

  #checkpoint(binding, prior, caught) {
    if (prior?.continuation && prior.message_binding?.binding_id === binding.binding_id) binding = prior.binding
    const state = prior && prior.binding?.binding_id === binding.binding_id ? prior : {
      required: true, phase: 'required', binding, starts: [], allStarts: [], finish: null, retry: null, activeRunId: null,
      lifecycle_unknown: true,
    }
    const admission = infrastructureAdmission(caught, this.runtimeHealth.get(binding.root_session_id)?.fingerprint || null)
    const same = state.admission?.kind === 'infrastructure' && state.admission.code === admission.code
      && state.admission.fingerprint === admission.fingerprint
    const blocked = { ...state, required: true, lifecycle_phase: state.lifecycle_phase || state.phase,
      phase: 'blocked', admission: { ...(same ? state.admission : admission), run_id: state.activeRunId } }
    this.knownStates.set(binding.binding_id, blocked)
    const store = this.#store()
    if (store) {
      const existing = store.read(binding)
      if (JSON.stringify(existing) !== JSON.stringify(blocked)) store.write(blocked)
    }
    return blocked
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
  ctx.on('agent/pre-step', (payload, next) => requirement.preStep(payload, next), { prepend: true, global: true })
  ctx.on('tools/pre-execute', (exec, next) => requirement.preExecute(exec, next), { prepend: true, global: true })
  ctx.on('agent/turn-stopping', payload => requirement.turnStopping(payload), { prepend: true, global: true })
  return requirement
}
