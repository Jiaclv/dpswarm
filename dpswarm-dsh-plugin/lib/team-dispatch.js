import { requireRootCaller } from './delegation.js'
import { sameTaskBinding, validateContinuationRequest } from './task-continuity.js'

const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const text = value => typeof value === 'string' && value.length > 0
const zeroCallAdmissionFailure = (result, sessions) => {
  const failed = result?.failed
  if (result?.stopped || !Array.isArray(result?.deliveries) || result.deliveries.length !== 0
    || !Array.isArray(failed) || failed.length !== sessions.size || sessions.size === 0) return false
  const observed = new Set()
  return failed.every(item => {
    const diagnostic = item?.diagnostic || item?.details?.worker_diagnostic, budget = diagnostic?.budget
    const sessionId = item?.execution_session_id || item?.details?.sessionId
    if (!sessions.has(sessionId) || observed.has(sessionId)) return false
    observed.add(sessionId)
    return item?.control_settlement?.ok === true && item?.details?.physicalCleanupConfirmed === true
      && diagnostic?.native_terminal && typeof diagnostic.native_terminal === 'object'
      && diagnostic?.cleanup?.physical_cleanup_confirmed === true && diagnostic?.failure?.code === 'WORKER_TOKEN_RESERVATION_DENIED'
      && diagnostic?.evidence?.native_terminal_available === true && !diagnostic?.evidence?.budget_error && !diagnostic?.evidence?.error
      && !diagnostic?.audit_error && !budget?.audit_warning
      && Number.isSafeInteger(budget?.calls) && budget.calls === 0
      && Number.isSafeInteger(budget?.unknown_usage_calls) && budget.unknown_usage_calls === 0
      && Number.isSafeInteger(budget?.active_calls) && budget.active_calls === 0
      && Number.isSafeInteger(budget?.committed_tokens) && budget.committed_tokens === 0
      && Number.isSafeInteger(budget?.observed_tokens_lower_bound) && budget.observed_tokens_lower_bound === 0
  })
}

/** Owns one dispatch lifecycle; only the owner of a published child may settle it. */
export class TeamDispatcher {
  constructor({ controller, requirement }) {
    this.controller = controller
    this.requirement = requirement
    this.running = new Set()
  }

  #acquire(exec) {
    requireRootCaller(exec?.agent)
    const rootId = exec.agent.session.id
    if (this.running.has(rootId)) throw failure('RUN_PENDING', 'This root session already has a team operation in progress.')
    // Acquire synchronously before any journal read, including Code Mode calls.
    this.running.add(rootId)
    return rootId
  }

  async run(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const binding = await this.requirement.beforeDispatch(exec.agent)
      let published = false, runId
      const publishedSessions = new Set(), publishedDetails = []
      const result = await this.controller.run(args, exec, {
        taskBinding: binding,
        taskSource: binding.required ? this.requirement.sourceFor?.(exec.agent, binding) : undefined,
        onChildStarted: async details => {
          await this.requirement.markStarted(binding, details)
          published = true
          runId = details.run_id
          publishedSessions.add(details.execution_session_id)
          publishedDetails.push(details)
        },
      }).catch(async error => {
        // A lease may appear between read-only preflight and atomic acquire.
        // Only fresh filesystem evidence can mark this task externally blocked.
        if (!published && error?.code === 'WORKSPACE_BUSY') await this.requirement.beforeRun(exec.agent)
        await this.requirement.markInfrastructureBlocked(exec.agent, error, { run_id: runId })
        throw error
      })
      // A native handle is not proof of a model request. On thrown errors or
      // uncertain physical cleanup, keep the durable requirement unsettled.
      const recoverySettled = !result?.execution_error || (typeof this.controller.reviewSettlementEvidence === 'function'
        && await this.controller.reviewSettlementEvidence(exec.agent, publishedDetails) === true)
      const safelySettled = recoverySettled && result && Array.isArray(result.failed) && result.failed.every(item =>
        // This trusted controller stage precedes child publication, so that
        // role has no physical worker or control-plane item to settle.
        item?.admission_stage === 'model_preflight'
        || (item?.control_settlement?.ok === true && item?.details?.physicalCleanupConfirmed !== false))
      let retryAllowed = false
      if (published && safelySettled) {
        if (zeroCallAdmissionFailure(result, publishedSessions)) {
          await this.requirement.finishAdmissionFailure(binding, {
            run_id: runId,
            execution_session_ids: [...publishedSessions],
            no_delivery: true,
          })
          retryAllowed = true
        } else {
          await this.requirement.finishRun(binding, {
            outcome: result.failed.length || result.stopped || result.execution_error ? 'failed_takeover' : 'completed',
            run_id: runId,
            ...(result.execution_error ? { error_code: result.execution_error.code } : {}),
          })
        }
      }
      return retryAllowed ? { ...result, retry_allowed: true,
        next: 'All published child sessions made zero model calls and were cleaned up. Correct the budget, then call dpswarm_run again for this same user task.' } : result
    } catch (error) {
      await this.requirement.markInfrastructureBlocked(exec.agent, error)
      throw error
    } finally {
      this.running.delete(rootId)
    }
  }

  async continueTask(args, exec) {
    requireRootCaller(exec?.agent)
    validateContinuationRequest(args)
    exec.signal?.throwIfAborted()
    // This operation only appends a task relationship. Do not acquire the long
    // dispatch lock or wait for workers: their owner still settles the old run.
    const state = this.controller.session(exec.agent)
    if (!state.busy && !this.running.has(exec.agent.session.id)) await this.controller.restoreAcceptance(state, exec.agent)
    await this.requirement.beforeRun(exec.agent)
    exec.signal?.throwIfAborted()
    const result = await this.requirement.continueTask(exec.agent, args, previous => {
      exec.signal?.throwIfAborted()
      const frozen = state.fixedTask, acceptance = state.acceptance
      if (frozen?.root_session_id !== exec.agent.session.id || frozen?.owner_session_id !== exec.agent.session.id
        || !sameTaskBinding(frozen?.task_binding, previous) || !text(frozen?.run_id)
        || !text(acceptance?.id) || frozen.acceptance_contract_id !== acceptance.id
        || acceptance.contract?.contract_id !== acceptance.id || !Number.isSafeInteger(acceptance.revision)) {
        throw failure('TASK_CONTINUITY_SOURCE_MISMATCH', 'The immediate previous task must match the existing frozen run and acceptance contract; no task is created or replaced by continuation.')
      }
      return { root_session_id: exec.agent.session.id, contract_id: acceptance.id,
        contract_revision: acceptance.revision, run_id: frozen.run_id }
    })
    return { ok: true, outcome: result.duplicate ? 'already_continued' : 'continued',
      phase: result.phase, ...(result.lifecycle_phase ? { lifecycle_phase: result.lifecycle_phase } : {}),
      binding: result.binding, message_binding: result.message_binding, continuation: result.continuation,
      native_child_bound_count: result.starts.length,
      ...(result.admission ? { admission: result.admission } : {}),
      effects: { starts_workers: false, allocates_budget: false, changes_acceptance: false },
      next: result.phase === 'started' ? 'The original run remains active or unsettled. Its owner must finish or recover it; no duplicate team was started.'
        : result.phase === 'blocked' ? 'The original task remains blocked. Continuation does not clear its admission or recovery conditions.'
        : 'Continue within the original task and contract; inspect existing reports and acceptance before deciding the next action.' }
  }

  async verifyRework(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const validateTask = async () => {
        const current = await this.requirement.beforeRun(exec.agent)
        if (!current.required || current.phase !== 'finished') throw failure('REWORK_TASK_NOT_SETTLED', 'Verification requires the original settled task and its deferred rework checkpoint.')
        return current
      }
      return await this.controller.verifyRework(args, exec, { taskBinding: await validateTask(), validateTask })
    } finally { this.running.delete(rootId) }
  }

  async amend(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const current = await this.requirement.beforeRun(exec.agent)
      const source = this.requirement.sourceFor(exec.agent, current)
      const state = this.controller.session(exec.agent)
      await this.controller.restoreAcceptance(state, exec.agent)
      const previous = state.fixedTask?.task_binding
      if (!previous) throw failure('TASK_AMENDMENT_SOURCE_REQUIRED', 'No previous fixed-team task can be amended.')
      const result = await this.controller.amendTask(args, exec, source)
      if (current.phase === 'finished' && previous.binding_id === current.binding?.binding_id && result.recovered) {
        await this.controller.persistAmendedBinding(state, exec.agent, current.binding)
        return result
      }
      const amended = await this.requirement.amendBinding(exec.agent, previous, current, result)
      await this.controller.persistAmendedBinding(state, exec.agent, amended.binding)
      return result
    } finally { this.running.delete(rootId) }
  }

  async repairReport(args, exec) {
    const rootId = this.#acquire(exec)
    try { return await this.controller.repairReport(args, exec) }
    finally { this.running.delete(rootId) }
  }

  async resume(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const validateTask = async () => {
        const current = await this.requirement.beforeRun(exec.agent)
        if (!current.required || current.phase !== 'finished') throw failure('RESUME_TASK_NOT_SETTLED', 'The original workers must be safely settled before recovery.')
        return current
      }
      return await this.controller.resume(args, exec, { validateTask })
    } finally { this.running.delete(rootId) }
  }

  async rework(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const validateTask = async () => {
        const current = await this.requirement.beforeRun(exec.agent)
        if (!current.required || current.phase !== 'finished') throw failure('REWORK_TASK_NOT_SETTLED', 'Rework requires the already settled fixed team for the current user task; start or settle that task first.')
        return current
      }
      return await this.controller.rework(args, exec, { taskBinding: await validateTask(), validateTask })
    } finally {
      this.running.delete(rootId)
    }
  }

  async review(args, exec) {
    const rootId = this.#acquire(exec)
    try {
      const state = this.controller.session(exec.agent)
      // A recovered spawn lease is trusted only because controller.session()
      // has established that its former host process is dead.
      const lease = state.lease
      const recovered = lease?.recovered === true && state.cfg?.subagentProvider === 'spawn'
        && lease.session_id === rootId && Number.isSafeInteger(lease.pid) && lease.pid > 0 && text(lease.run_id)
        ? { run_id: lease.run_id, pid: lease.pid } : null
      const result = await this.controller.review(args, exec)
      if (state.lease !== null || state.busy !== false) return result
      const binding = await this.requirement.beforeRun(exec.agent)
      if (!binding.required || binding.phase !== 'started') return result
      const starts = binding.starts.map(event => event.data)
      if (!starts.length || starts.some(start => start.run_id !== binding.activeRunId || !text(start.execution_session_id))) return result
      const recoveredSpawn = recovered?.run_id === binding.activeRunId
      const status = await this.controller.status(exec.agent)
      const snapshot = status?.snapshot
      if (!snapshot || snapshot.open_worker_slots_used !== 0 || !snapshot.nodes || !snapshot.work_items) return result
      const nodes = Object.values(snapshot.nodes), boundItems = []
      const terminal = starts.every(start => {
        const matches = nodes.filter(node => node.execution_session_id === start.execution_session_id)
        if (matches.length !== 1) return false
        const node = matches[0]
        boundItems.push({ ...start, item_id: node.item })
        return node.execution_parent_session_id === rootId
          && node.execution_provider === (recoveredSpawn ? 'spawn' : state.cfg?.subagentProvider)
          && (node.lifecycle === 'drained' || node.terminated === true) && text(node.item)
          && ['accepted', 'terminated'].includes(snapshot.work_items[node.item]?.acceptance)
      })
      const nativeCleanup = terminal && !recoveredSpawn && typeof this.controller.reviewSettlementEvidence === 'function'
        && await this.controller.reviewSettlementEvidence(exec.agent, boundItems) === true
      if (terminal && (recoveredSpawn || nativeCleanup)) {
        await this.requirement.finishRecovered(exec.agent, {
          run_id: binding.activeRunId,
          execution_session_ids: starts.map(start => start.execution_session_id),
          control_plane_confirmed: true,
          all_workers_terminal: true,
          physical_cleanup_confirmed: true,
          open_worker_slots_used: 0,
        })
      }
      return result
    } catch (error) {
      await this.requirement.markInfrastructureBlocked(exec.agent, error)
      throw error
    } finally {
      this.running.delete(rootId)
    }
  }
}
