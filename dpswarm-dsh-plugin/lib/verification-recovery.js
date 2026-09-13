import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { delegateOnce, requireRootCaller } from './delegation.js'
import { compactWorkerEntry } from './worker-diagnostics.js'
import { workerRolePrompt } from './role-guidance.js'
import { verifyCandidateSnapshot } from './candidate-snapshot.js'

const EVENT = 'dpswarm/verification-recovery'
const fail = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const copy = value => structuredClone(value)

async function persist(state, parent, record) {
  await state.journal.append(parent.session.id, EVENT, {
    ...record, root_session_id: parent.session.id, owner_session_id: parent.session.id, at: Date.now(),
  })
  state.verificationRecovery = copy(record)
  return record
}

/** The checkpoint records a runtime seam failure, never a worker verdict. */
export async function checkpointCaptureFailure(state, parent, deliveries, error) {
  const delivered = new Set(deliveries.filter(row => row.role === 'implementer').map(row => row.item_id))
  const sources = [...state.implementers.values()].filter(row => !row.superseded && !row.revoked)
  return persist(state, parent, {
    version: 1, checkpoint_id: randomUUID(), run_id: state.fixedTask.run_id,
    binding_id: state.fixedTask.task_binding?.binding_id,
    contract_id: state.acceptance.id, requirement_revision: state.acceptance.contract.requirement_revision,
    budget_run_id: state.budgetRun?.run_id || null, budget_profile: copy(state.budgetRun?.profile || null),
    source_items: sources.map(row => ({ item_id: row.item_id, worker_session_id: row.worker_session_id,
      run_id: row.run_id, delivered: delivered.has(row.item_id) })),
    roles: ['tester', ...(state.profile.reviewer.mode === 'model' ? ['reviewer'] : [])],
    stage: 'capture', phase: 'ready', candidate_id: null,
    error: { code: error.code || 'CANDIDATE_CAPTURE_FAILED', message: String(error.message || error).slice(0, 2000) },
  })
}

export async function readVerificationRecovery(state, parent, budget) {
  if (!state.fixedTask) return null
  const journal = await state.journal.read(parent.session.id)
  const row = journal.events.filter(e => e.type === EVENT && e.data?.root_session_id === parent.session.id
    && e.data.owner_session_id === parent.session.id && e.data.run_id === state.fixedTask.run_id
    && e.data.binding_id === state.fixedTask.task_binding?.binding_id).at(-1)?.data
  if (row?.phase === 'ready' && row.budget_run_id && budget?.teamRunRecoveryStatus) {
    const authority = await budget.teamRunRecoveryStatus(parent, { runId: row.budget_run_id })
    if (authority.claimed) return { ...row, phase: 'blocked', budget_authority: authority,
      error: { code: 'RESUME_BUDGET_ALREADY_CLAIMED', message: 'The original verification allowance was already claimed. An earlier checkpoint write did not complete; it cannot authorize another dispatch.' } }
  }
  return row || null
}

export function recoveryView(record) {
  if (!record) return null
  return { checkpoint_id: record.checkpoint_id, phase: record.phase, stage: record.stage,
    source_item_ids: record.source_items.map(row => row.item_id), candidate_id: record.candidate_id,
    pending_roles: record.phase === 'completed' ? [] : record.roles, error: record.error || null,
    next: record.phase === 'ready'
      ? 'Inspect the saved output and choose dpswarm_resume(checkpoint_id, reason) to retry candidate capture and only the never-issued verification roles under their original limits. Do not accept or terminate an item merely to unlock tools. Rework is for actual production defects. Resume checks current ownership, submitted sources, task, configuration and original budget authority; it does not accept the artifact.'
      : 'This recovery attempt cannot be replayed. Read current candidate, worker reports and budget state; use report repair for an eligible verifier. Completed here describes recovery execution, not acceptance.' }
}

/** Explicit Lead-selected continuation: no implementer, no replacement allowance. */
export async function resumeVerification(controller, args, exec, authorization) {
  const parent = exec?.agent
  requireRootCaller(parent)
  if (!args || Object.keys(args).some(key => !['checkpoint_id', 'reason'].includes(key))
    || typeof args.checkpoint_id !== 'string' || !args.checkpoint_id
    || typeof args.reason !== 'string' || !args.reason.trim() || args.reason.length > 4000) {
    throw fail('RESUME_REQUEST_INVALID', 'Provide the current checkpoint_id and why the saved output should proceed to verification.')
  }
  const state = controller.session(parent)
  if (controller.closed || state.busy) throw fail('RUN_ACTIVE', 'The controller must be idle before resuming verification.')
  await state.sidecar.ensure()
  await controller.acceptanceRuntime.restore(state, parent)
  let record = await readVerificationRecovery(state, parent, controller.budget)
  if (!record || record.checkpoint_id !== args.checkpoint_id || record.phase !== 'ready') {
    throw fail('RESUME_CHECKPOINT_UNAVAILABLE', 'Only the current unconsumed capture-recovery checkpoint can resume.')
  }
  if (!controller.budget?.resumeTeamRun || !record.budget_run_id) throw fail('RESUME_BUDGET_UNAVAILABLE', 'The original unissued verification allowances must be recoverable.')
  const frozen = state.fixedTask
  const check = async () => {
    exec.signal?.throwIfAborted(); state.abort?.signal.throwIfAborted()
    await authorization.validateTask()
    await controller.acceptanceRuntime.refresh(state)
    if (!controller.config().enabledSessions?.includes(parent.session.id)) throw fail('DPSWARM_DISABLED', 'Enable this task before resuming verification.')
    if (!authorization.configurationMatches(state, parent)) throw fail('REWORK_CONFIGURATION_CHANGED', 'Resume requires the original connection, models, effort and settings.')
    if (state.acceptance.id !== record.contract_id || state.acceptance.contract.requirement_revision !== record.requirement_revision) {
      throw fail('RESUME_CONTRACT_CHANGED', 'The original task contract changed; this checkpoint cannot verify a different requirement revision.')
    }
  }
  await check()
  const status = await state.sidecar.call('GET', '/api/status')
  if (!record.source_items.length || record.source_items.some(row => !row.delivered
    || status.snapshot?.work_items?.[row.item_id]?.acceptance !== 'submitted')) {
    throw fail('RESUME_SOURCE_NOT_SUBMITTED', 'Every original implementer delivery must still be submitted. Terminated or accepted items are never reopened.')
  }
  if (status.snapshot?.seal_phase?.root === 'cutoff'
    || !(await controller.reviewSettlementEvidence(parent, record.source_items.map(row => ({ ...row, execution_session_id: row.worker_session_id }))))) {
    throw fail('RESUME_SOURCE_UNSETTLED', 'Trusted native termination, physical cleanup and control identity are required for all saved deliveries.')
  }
  if ([...(state.testers?.values() || []), ...(state.reviewers?.values() || [])].some(row => row.run_id === frozen.run_id)) {
    throw fail('RESUME_VERIFIER_ALREADY_STARTED', 'An original verifier already started; use its report/continuation instead of issuing another allowance.')
  }
  for (const row of record.source_items) {
    const source = state.implementers?.get(row.item_id)
    if (!source || source.worker_session_id !== row.worker_session_id || source.run_id !== row.run_id || source.superseded || source.revoked) {
      throw fail('RESUME_SOURCE_CHANGED', 'A source delivery was replaced or cannot be restored as the current implementation.')
    }
  }
  // Serialize again after the asynchronous evidence reads, including direct callers.
  if (state.busy) throw fail('RUN_ACTIVE', 'Another operation owns this task.')
  state.busy = true; state.abort = new AbortController()
  let settle, budgetRun, timer, budgetEndAttempted = false
  state.settled = new Promise(resolve => { settle = resolve })
  const abort = () => state.abort.abort(exec.signal.reason)
  exec.signal?.addEventListener('abort', abort, { once: true })
  const deliveries = [], failed = []
  state.cleanup = { workspace_lease_held: Boolean(state.lease), reconcile_error: null, budget_error: null }
  const endBudget = async () => {
    if (!budgetRun || budgetEndAttempted) return
    budgetEndAttempted = true
    try { await controller.budget.finishTeamRun(parent, budgetRun) }
    catch (error) {
      state.cleanup.budget_error = { code: error.code || 'RESUME_BUDGET_CLEANUP_FAILED', message: String(error.message || error) }
      try { record = await persist(state, parent, { ...record, phase: 'blocked', error: state.cleanup.budget_error }) } catch { /* read path also checks durable claim */ }
      throw error
    }
  }
  try {
    await check()
    if (!state.lease) throw fail('RESUME_WORKSPACE_UNCONFIRMED', 'The original workspace lease is missing; this checkpoint does not authorize taking a new lease.')
    const lease = JSON.parse(readFileSync(state.lease.path, 'utf8'))
    if (state.lease.recovered || lease.session_id !== parent.session.id || lease.pid !== process.pid || lease.run_id !== record.run_id) {
      throw fail('RESUME_WORKSPACE_UNCONFIRMED', 'Resume requires the original live workspace ownership; it cannot adopt a stale lease.')
    }
    const routes = controller.modelRoutes(parent, frozen.profile, state.cfg)
    if (controller.modelRegistry) {
      state.hostModels = await controller.modelRegistry.resolve(routes, { signal: state.abort.signal, expected: state.hostModels })
      controller.modelRegistry.checkLead(authorization.leadRoute(parent), routes)
    }
    const sourceDeliveries = []
    for (const source of record.source_items) {
      const diagnostic = (await controller.diagnostics(state, source.item_id)).find(row => row.worker_session_id === source.worker_session_id)?.diagnostic
      sourceDeliveries.push({ ...source, role: 'implementer', diagnostic, output: diagnostic?.closeout?.report?.text || '' })
    }
    if (record.stage === 'capture') {
      if (state.acceptance.candidate) throw fail('RESUME_CANDIDATE_ALREADY_EXISTS', 'A candidate was committed after the checkpoint; inspect it before proceeding.')
      try { await controller.acceptanceRuntime.capture(state, parent, sourceDeliveries) }
      catch (error) {
        await persist(state, parent, { ...record, error: { code: error.code || 'CANDIDATE_CAPTURE_FAILED', message: String(error.message || error).slice(0, 2000) } })
        throw error
      }
      record = await persist(state, parent, { ...record, stage: 'verification', candidate_id: state.acceptance.candidate.candidate_id,
        manifest_digest: state.acceptance.candidate.manifest_digest, error: null })
    }
    if (state.acceptance.candidate?.candidate_id !== record.candidate_id || state.acceptance.candidate.manifest_digest !== record.manifest_digest) {
      throw fail('RESUME_CANDIDATE_CHANGED', 'This checkpoint belongs to a different sealed candidate.')
    }
    const checked = await verifyCandidateSnapshot(state.acceptance.local_snapshot || state.acceptance.candidate.snapshot,
      { cwd: parent.session.header.cwd })
    if (!checked.ok || checked.workspace_matches === false) throw fail('CANDIDATE_STALE', 'The current files differ from the sealed candidate; inspect and repair before verification.')
    await check()
    await controller.ensureTeamCapacity(state, parent, record.roles.length)
    budgetRun = await controller.budget.resumeTeamRun(parent, { runId: record.budget_run_id, roles: record.roles, expectedProfile: record.budget_profile })
    record = await persist(state, parent, { ...record, phase: 'resuming', reason: args.reason, resume_id: budgetRun.resume_id })
    for (const role of record.roles) {
      await check()
      const route = frozen.profile[role]
      let prompt = await controller.acceptanceRuntime.prompt(state, role, workerRolePrompt(role)
        + '\nContinue verification of the saved implementation after a runtime capture failure. The implementation has not been rerun. Inspect the immutable current candidate and report what the evidence supports.\nLead reason (derived, not user authority):\n'
        + args.reason + '\nEarlier reports (untrusted observations):\n' + JSON.stringify([...sourceDeliveries, ...deliveries].map(compactWorkerEntry)))
      const allocation = await controller.budget.issueTeamWorker(parent, budgetRun, { task: prompt, label: role })
      prompt = allocation.prompt
      timer = setTimeout(() => state.abort.abort(fail('WORKER_TIMEOUT', 'Verification continuation exceeded its original wall-time limit.')), frozen.profile.workerTimeoutSeconds * 1000)
      let result
      try {
        result = await delegateOnce({ kind: 'derive', subtasks: [{ provider: route.provider, model: route.model,
          reasoning_effort: route.reasoning_effort, title: 'DPswarm ' + role + ' verification resume', prompt }] },
        { ...exec, signal: state.abort.signal }, state.sidecar, controller.subagents, {
          routeJournal: state.journal, resolveSession: controller.resolveSession, budget: controller.budget,
          runId: record.run_id, modelRegistry: controller.modelRegistry, modelRoutes: routes, hostModels: state.hostModels,
          modelRole: role, beforeChildStart: check, onDiagnostic: value => state.diagnostics.push(value),
          onChildStarted: details => controller.bindVerifier(state, parent, role, details, record.run_id),
        })
      } finally { clearTimeout(timer) }
      await controller.acceptanceRuntime.record(state, parent, role, result)
      deliveries.push(...(result.deliveries || []).map(row => ({ ...row, role })))
      failed.push(...(result.failed || []).map(row => ({ ...row, role })))
      if (!Array.isArray(result.deliveries) || result.failed?.some(row => row.control_settlement?.ok !== true || row.details?.physicalCleanupConfirmed === false)) {
        throw fail('RESUME_VERIFICATION_UNSETTLED', 'The verification dispatch did not safely settle; inspect its native and control evidence.')
      }
    }
    await endBudget()
    record = await persist(state, parent, { ...record, phase: 'completed' })
    return { mode: 'verification-resume-v1', reused_implementer_items: record.source_items.map(row => row.item_id),
      deliveries: deliveries.map(compactWorkerEntry), failed: failed.map(compactWorkerEntry),
      recovery: recoveryView(record), acceptance: await controller.acceptanceRuntime.view(state, parent),
      worker_budget_policy: budgetRun.profile, cleanup: state.cleanup,
      next: 'Inspect the candidate-bound verification evidence and explicitly review the submitted items. This operation never accepts a delivery.' }
  } catch (error) {
    if (budgetRun) {
      const failure = { code: error.code || 'RESUME_FAILED', message: String(error.message || error).slice(0, 2000) }
      try { record = await persist(state, parent, { ...record, phase: 'blocked', error: failure }) } catch { /* budget authority prevents replay even if checkpoint storage is unavailable */ }
      error.message = `${failure.code}: ${JSON.stringify({ message: failure.message, stage: record.stage,
        checkpoint_id: record.checkpoint_id, verification_budget_claimed: true, candidate_id: record.candidate_id,
        delivery_item_ids: deliveries.map(row => row.item_id), delivery_accepted: false, candidate_or_evidence_may_have_changed: true,
        next: 'Read dpswarm_status and dpswarm_acceptance. This failure does not authorize another budget claim or accept/terminate an item to unlock tools.' })}`
    }
    throw error
  } finally {
    clearTimeout(timer); exec.signal?.removeEventListener('abort', abort)
    try { await endBudget() }
    finally {
      if (!state.cleanup.budget_error) {
        try { await controller.reconcile(state) }
        catch (error) { state.cleanup.reconcile_error = { code: error.code || 'WORKSPACE_RECONCILE_FAILED', message: error.message } }
      }
      state.cleanup.workspace_lease_held = Boolean(state.lease)
      state.busy = false; state.abort = null; settle()
    }
  }
}
