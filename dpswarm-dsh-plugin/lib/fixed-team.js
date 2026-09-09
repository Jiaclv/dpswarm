import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { Sidecar } from './sidecar.js'
import { AuditJournal } from './audit.js'
import { compactWorkerEntry, compactDiagnosticRecords } from './worker-diagnostics.js'
import { runtimePaths } from './paths.js'
import { delegateOnce, requireRootCaller } from './delegation.js'
import { workerBudgetProfile, reworkBudgetProfile } from './budget-runtime.js'
import { effectiveLeadRoute } from './lead-route.js'
import { workerRolePrompt } from './role-guidance.js'

const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const enabled = (cfg, id) => Array.isArray(cfg.enabledSessions) && cfg.enabledSessions.includes(id)
const processAlive = pid => { try { process.kill(pid, 0); return true } catch (e) { return e.code !== 'ESRCH' } }
const clone = value => JSON.parse(JSON.stringify(value))
const configurationFingerprint = cfg => hash(Object.fromEntries(['sidecarUrl', 'workspace', 'dpswarmDir', 'pythonCmd', 'subagentProvider', 'implMode', 'implProvider', 'implModel', 'implEffort'].map(key => [key, cfg[key] ?? null])))
const sameTask = (a, b) => a && b && ['root_session_id', 'binding_id', 'user_message_id', 'user_content_sha256'].every(key => typeof a[key] === 'string' && a[key] === b[key])
const unknownUsage = 'Host subagent results do not provide a complete usage ledger; unknown values remain null.'

// An unset mode preserves a previously explicit route on upgrade; a fresh
// installation has no implementer provider and therefore follows the conversation.
export function implementerMode(cfg) {
  const mode = cfg.implMode || (typeof cfg.implProvider === 'string' && cfg.implProvider.trim() ? 'model' : 'lead')
  if (!['lead', 'model'].includes(mode)) throw failure('INVALID_IMPLEMENTER_MODE', 'Implementer must follow the conversation or use an explicitly configured model')
  return mode
}

export function fixedProfile(cfg, cm = { enabled: false, profile: null }, leadOptions) {
  const role = prefix => {
    const provider = cfg[`${prefix}Provider`], model = cfg[`${prefix}Model`]
    if (typeof provider !== 'string' || !provider.trim() || typeof model !== 'string' || !model.trim()) {
      throw failure('FIXED_ROUTE_REQUIRED', 'Configure an exact provider and model for each selected role in DPSwarm settings')
    }
    return { provider: provider.trim(), model: model.trim(), reasoning_effort: cfg[`${prefix}Effort`] || undefined }
  }
  const reviewerMode = cfg.reviewerMode ?? 'lead'
  if (!['lead', 'model'].includes(reviewerMode)) throw failure('INVALID_REVIEWER_MODE', 'Reviewer must follow Lead or use an explicitly configured model')
  const reviewer = reviewerMode === 'lead' ? { mode: 'lead' } : { mode: 'model', ...role('reviewer') }
  const timeout = cfg.workerTimeoutSeconds ?? 600
  if (!Number.isInteger(timeout) || timeout < 10 || timeout > 7200) throw failure('INVALID_WORKER_TIMEOUT', 'Worker timeout must be 10–7200 seconds')
  const implMode = implementerMode(cfg)
  let implementer
  if (implMode === 'lead') {
    if (!['provider', 'model'].every(key => typeof leadOptions?.[key] === 'string' && leadOptions[key].trim())) {
      throw failure('ROOT_MODEL_REQUIRED', 'The current conversation must supply its actual provider and model')
    }
    if (leadOptions.reasoningEffort != null && typeof leadOptions.reasoningEffort !== 'string') {
      throw failure('INVALID_ROOT_EFFORT', 'The conversation reasoning effort must be a string when set')
    }
    implementer = { mode: 'lead', provider: leadOptions.provider, model: leadOptions.model,
      reasoning_effort: leadOptions.reasoningEffort || undefined }
  } else implementer = { mode: 'model', ...role('impl') }
  const profile = { version: 'fixed-team-v1', implementer, tester: role('test'), reviewer, workerTimeoutSeconds: timeout,
    cm }
  return Object.freeze({ ...profile, id: hash(profile) })
}

/** Own the project lease and configured sequential workers; Lead owns the main turn and final decisions. */
export class FixedTeamController {
  constructor({ config, subagents, cm, budget, modelRegistry, resolveSession, sidecarFactory = cfg => new Sidecar(cfg) }) {
    this.config = config
    this.cm = cm
    this.budget = budget
    this.modelRegistry = modelRegistry
    this.subagents = subagents
    this.resolveSession = resolveSession
    this.sidecarFactory = sidecarFactory
    this.sessions = new Map()
  }

  settingsChanged() {
    const cfg = this.config()
    for (const [id, state] of this.sessions) {
      if (state.abort && !enabled(cfg, id)) state.abort.abort(failure('DPSWARM_DISABLED', 'User disabled collaboration for this session'))
    }
  }

  session(parent) {
    requireRootCaller(parent)
    const id = parent.session.id
    let state = this.sessions.get(id)
    if (!state) {
      const cfg = { ...this.config(), ...runtimePaths(this.config()), sessionId: id, sessionIsolation: true, ...(this.modelRegistry ? { hostCatalogRequired: true } : {}) }
      state = { sidecar: this.sidecarFactory(cfg), cfg, busy: false, parentSession: parent.session, diagnostics: [] }
      state.journal = new AuditJournal({ sidecarFactory: () => state.sidecar })
      // Reconnect to a persisted lease only after the old host process ended.
      const cwd = parent.session.header.cwd
      if (typeof cwd === 'string' && cwd) {
        const actual = realpathSync(cwd), identity = process.platform === 'win32' ? actual.toLowerCase() : actual
        const path = join(cfg.workspace, 'workspace-leases', `${hash(identity)}.json`)
        if (existsSync(path)) {
          const lease = JSON.parse(readFileSync(path, 'utf8'))
          if (lease.session_id === id && Number.isSafeInteger(lease.pid) && !processAlive(lease.pid)) {
            state.lease = { ...lease, path, recovered: true }
          }
        }
      }
      this.sessions.set(id, state)
    }
    return state
  }

  async status(parent) {
    requireRootCaller(parent)
    const cfg = this.config(), on = enabled(cfg, parent.session.id)
    const existing = this.sessions.get(parent.session.id)
    const base = { enabled: on, mode: 'fixed-team-v1', session_id: parent.session.id,
      cm: (await this.cm?.status(parent)) || { enabled: false, attached: false }, usage_note: unknownUsage }
    if (!on && !existing) return { ...base, state: 'off' }
    const state = existing || this.session(parent)
    await state.sidecar.ensure()
    const result = await state.sidecar.call('GET', '/api/status')
    return { ...base, state: state.busy ? 'running' : result.state || 'ready',
      snapshot: result.snapshot, profile: state.profile || null, bridge: result.bridge,
      worker_diagnostics_scope: 'latest 6 recorded workers; complete records remain in authenticated plugin audit',
      worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state)), cleanup: state.cleanup || null,
      rework_recovery: state.reworkRecovery || null }
  }

  async diagnostics(state, itemId) {
    const snapshot = await state.journal.read(state.cfg.sessionId)
    const records = snapshot.events.filter(e => e.type === 'dpswarm/worker-diagnostic'
      && e.data?.root_session_id === state.cfg.sessionId
      && e.data.owner_session_id === e.data.worker_session_id
      && e.data.diagnostic?.worker_session_id === e.data.worker_session_id
      && e.data.diagnostic.root_session_id === state.cfg.sessionId
      && (!itemId || e.data.item_id === itemId)).map(e => e.data)
    // An explicitly visible local audit error is useful until durable recovery;
    // never manufacture a successful persistent write from this fallback.
    return [...records, ...state.diagnostics.filter(d => d.diagnostic.audit_error && (!itemId || d.item_id === itemId))]
  }

  acquire(state, parent) {
    const cwd = parent.session.header.cwd
    if (typeof cwd !== 'string' || !cwd) throw failure('WORKSPACE_REQUIRED', 'The host session must supply a project directory')
    const actual = realpathSync(cwd)
    const identity = process.platform === 'win32' ? actual.toLowerCase() : actual
    const directory = join(state.cfg.workspace, 'workspace-leases')
    mkdirSync(directory, { recursive: true })
    const path = join(directory, `${hash(identity)}.json`)
    const lease = { version: 1, run_id: randomUUID(), session_id: parent.session.id, cwd: actual, pid: process.pid }
    let fd
    try { fd = openSync(path, 'wx') } catch (error) {
      if (error.code === 'EEXIST') throw failure('WORKSPACE_BUSY', 'This project has an unfinished DPSwarm run. Review or terminate its deliveries first; uncertain cleanup is not cleared automatically.')
      throw error
    }
    try { writeFileSync(fd, JSON.stringify(lease)) } catch (error) { closeSync(fd); unlinkSync(path); throw error }
    closeSync(fd)
    state.lease = { path, ...lease }
  }

  async release(state) {
    if (!state.lease) return
    const current = JSON.parse(readFileSync(state.lease.path, 'utf8'))
    if (current.run_id !== state.lease.run_id) throw failure('WORKSPACE_LEASE_CHANGED', 'Workspace ownership changed')
    // Persist lifecycle closure before relinquishing the workspace. An audit
    // failure must leave the lease available for explicit recovery.
    await this.cm?.finishRun(current.session_id, state.parentSession)
    unlinkSync(state.lease.path)
    state.lease = null
  }

  async reconcile(state) {
    const status = await state.sidecar.call('GET', '/api/status')
    if (!status.snapshot) return false
    const snapshot = status.snapshot
    if (snapshot.seal_phase?.root === 'cutoff') return false
    if (snapshot.open_worker_slots_used === 0 && (!state.lease?.recovered || state.recoveryReviewed)) { await this.release(state); return true }
    return false
  }

  modelRoutes(parent, profile, cfg) {
    const routes = [{ role: 'lead', ...effectiveLeadRoute(parent) }]
    for (const role of ['implementer', 'tester', ...(profile.reviewer.mode === 'model' ? ['reviewer'] : [])]) {
      const selected = profile[role]
      routes.push({ role, provider: selected.provider, model: selected.model,
        ...(selected.reasoning_effort ? { reasoningEffort: selected.reasoning_effort } : {}) })
    }
    if (cfg.cmEnabledSessions?.includes(parent.session.id)) routes.push({ role: 'cm', provider: cfg.cmProvider, model: cfg.cmModel,
      ...(cfg.cmEffort ? { reasoningEffort: cfg.cmEffort } : {}) })
    return routes
  }

  async modelAvailability(parent) {
    if (!this.modelRegistry) return { source: 'legacy', ready: null }
    try {
      const cfg = this.config(), profile = fixedProfile(cfg, undefined, effectiveLeadRoute(parent))
      return { ready: true, ...await this.modelRegistry.resolve(this.modelRoutes(parent, profile, cfg)),
        note: 'DPH exact route preflight; provider calls are still checked at dispatch. No model generation was made.' }
    } catch (error) { return { source: 'dph', ready: false, error: error.code || 'HOST_MODEL_REGISTRY_UNAVAILABLE', message: error.message } }
  }

  async run(args, exec, { onChildStarted, taskBinding } = {}) {
    const parent = exec?.agent
    requireRootCaller(parent)
    if (this.closed) throw failure('PLUGIN_DISPOSED', 'Plugin is stopping')
    if (!enabled(this.config(), parent.session.id)) throw failure('DPSWARM_DISABLED', 'Enable DPSwarm for this task in the input toolbar first')
    if (!args || typeof args.task !== 'string' || !args.task.trim() || args.task.length > 100000) throw failure('TASK_REQUIRED', 'A nonempty bounded task description is required')
    const allowed = new Set(['task', 'acceptance', 'worker_budgets'])
    if (Object.keys(args).some(key => !allowed.has(key))) throw failure('FIXED_MODE_ONLY', 'Models, roles and topology come from user settings')
    if (args.acceptance != null && (typeof args.acceptance !== 'string' || args.acceptance.length > 40000)) throw failure('INVALID_ACCEPTANCE', 'Acceptance requirements must be text')
    let state = this.session(parent)
    if (state.busy || state.lease) throw failure('RUN_PENDING', 'Finish or review the current fixed-team run first')
    // Configuration edits apply to a new run; active execution and review keep their connection snapshot.
    this.sessions.delete(parent.session.id)
    state = this.session(parent)
    const leadOptions = effectiveLeadRoute(parent)
    state.profile = fixedProfile(state.cfg, undefined, leadOptions)
    const workerPolicy = workerBudgetProfile(state.cfg, parent.session.id)
    if (!this.budget && workerPolicy.mode !== 'unlimited') throw failure('WORKER_BUDGET_UNAVAILABLE', 'Configured worker limits require the budget runtime before any child starts.')
    const roles = ['implementer', 'tester', ...(state.profile.reviewer.mode === 'model' ? ['reviewer'] : [])]
    const proposedBudgets = args.worker_budgets
    if (workerPolicy.mode === 'auto') {
      if (!this.budget || !proposedBudgets || typeof proposedBudgets !== 'object' || Array.isArray(proposedBudgets)
        || Object.keys(proposedBudgets).some(role => !roles.includes(role))) throw failure('WORKER_BUDGET_DECISION_REQUIRED', 'Read the task and provide your own worker_budgets for every enabled role before dispatch.')
      for (const role of roles) {
        const plan=proposedBudgets[role]
        if (!plan || !Number.isSafeInteger(plan.tokenLimit) || plan.tokenLimit < 1 || !Number.isSafeInteger(plan.callLimit) || plan.callLimit < 1
          || typeof plan.reason !== 'string' || !plan.reason.trim() || plan.reason.length > 4000
          || Object.keys(plan).some(k => !['tokenLimit','callLimit','reason'].includes(k))) {
          throw failure('WORKER_BUDGET_DECISION_REQUIRED', `Lead must provide positive tokenLimit, callLimit and a short reason for ${role}.`)
        }
      }
    } else if (proposedBudgets !== undefined) {
      throw failure('USER_WORKER_LIMITS_AUTHORITATIVE', 'Only Auto mode accepts Lead-selected worker_budgets; manual and unlimited come from user settings.')
    }
    state.busy = true
    let settled
    state.settled = new Promise(resolve => { settled = resolve })
    state.abort = new AbortController()
    const cancelled = () => state.abort?.abort(exec.signal.reason)
    exec.signal?.addEventListener('abort', cancelled, { once: true })
    if (exec.signal?.aborted) cancelled()
    const deliveries = [], failed = []
    state.cleanup = { workspace_lease_held: false, reconcile_error: null, budget_error: null }
    let initialized = false
    try {
      if (state.abort.signal.aborted) throw failure('SUBAGENT_ABORTED', 'Task was cancelled before admission')
      if (this.modelRegistry) {
        state.modelRoutes = this.modelRoutes(parent, state.profile, state.cfg)
        state.hostModels = await this.modelRegistry.resolve(state.modelRoutes, { signal: state.abort.signal })
        this.modelRegistry.checkLead(effectiveLeadRoute(parent), state.modelRoutes)
      }
      if (state.abort.signal.aborted) throw failure('SUBAGENT_ABORTED', 'Task was cancelled before admission')
      this.acquire(state, parent)
      if (this.budget) state.budgetRun = await this.budget.beginTeamRun(parent, { roles, decisions: proposedBudgets, expectedProfile: workerPolicy })
      if (this.cm) state.profile = fixedProfile(state.cfg, await this.cm.beginRun(parent), leadOptions)
      await state.sidecar.ensure()
      if (this.modelRegistry) await this.modelRegistry.publish(state.sidecar, state.hostModels)
      await state.sidecar.call('POST', '/api/execution/root', { parent_session_id: parent.session.id, delegation_depth: 0,
        provider: leadOptions.provider, model: leadOptions.model })
      initialized = true
      const before = await state.sidecar.call('GET', '/api/status')
      if (before.snapshot?.open_worker_slots_used > 0) throw failure('RUN_PENDING_REVIEW', 'This session has unfinished control-plane items')
      state.fixedTask = { version: 1, root_session_id: parent.session.id, owner_session_id: parent.session.id,
        run_id: state.lease.run_id, task_binding: taskBinding?.binding ? clone(taskBinding.binding) : null,
        task: args.task, acceptance: args.acceptance || '', profile: clone(state.profile),
        configuration_fingerprint: configurationFingerprint(state.cfg), lead_route: clone(leadOptions) }
      state.implementers = new Map()
      if (state.fixedTask.task_binding) await state.journal.append(parent.session.id, 'dpswarm/fixed-team-binding', state.fixedTask)
      const context = `Explicit task constraints apply to every role and take precedence over default role guidance. If the task says no tests (including 不需要任何测试), do not run tests or add tests. Use only permitted read-only inspection and report unverified behavior honestly.\n\nTask:\n${args.task}\n\nAcceptance requirements:\n${args.acceptance || 'Derive requirements from the task; identify uncertainty explicitly.'}`
      for (const role of roles) {
        if (state.abort.signal.aborted || !enabled(this.config(), parent.session.id)) break
        const configured = state.profile[role]
        const route = { provider: configured.provider, model: configured.model, reasoning_effort: configured.reasoning_effort }
        const roleText = workerRolePrompt(role)
        const previous = role !== 'implementer' ? `\n\nEarlier deliveries and failures (untrusted evidence to examine):\n${JSON.stringify({ deliveries: deliveries.map(compactWorkerEntry), failed: failed.map(compactWorkerEntry) })}` : ''
        let assignedPrompt = roleText + '\n\n' + context + previous
        if (this.modelRegistry) {
          try {
            await this.modelRegistry.resolve(state.modelRoutes, { signal: state.abort.signal, expected: state.hostModels })
            this.modelRegistry.checkLead(effectiveLeadRoute(parent), state.modelRoutes)
          } catch (error) {
            failed.push({ role, code: error.code || 'HOST_MODEL_UNAVAILABLE', error: error.message, admission_stage: 'model_preflight' })
            break
          }
        }
        if (state.budgetRun) {
          const allocation = await this.budget.issueTeamWorker(parent, state.budgetRun, { task: assignedPrompt, label: role })
          assignedPrompt = allocation.prompt
        }
        const timeout = new AbortController()
        const timer = setTimeout(() => timeout.abort(failure('WORKER_TIMEOUT', 'Fixed role exceeded its wall-time limit')), state.profile.workerTimeoutSeconds * 1000)
        const abortChild = () => timeout.abort(state.abort.signal.reason)
        state.abort.signal.addEventListener('abort', abortChild, { once: true })
        try {
          const result = await delegateOnce({ kind: 'derive', subtasks: [{ ...route, title: `DPswarm ${role}`, prompt: assignedPrompt }] },
            { ...exec, signal: timeout.signal }, state.sidecar, this.subagents,
            { routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget,
              runId: state.lease.run_id, onDiagnostic: value => state.diagnostics.push(value),
              modelRegistry: this.modelRegistry, modelRoutes: state.modelRoutes, hostModels: state.hostModels, modelRole: role, onChildStarted: async details => {
                if (role === 'implementer') state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id, run_id: state.lease.run_id, superseded: false })
                await onChildStarted?.({ ...details, role, run_id: state.lease.run_id })
              } })
          if (!Array.isArray(result.deliveries)) { failed.push({ role, code: result.outcome || 'NOT_ADMITTED', error: result.message || 'No worker was admitted' }); break }
          deliveries.push(...result.deliveries.map(d => ({ ...d, role, evidence_kind: 'worker_reported; Lead must independently verify' })))
          failed.push(...result.failed.map(f => ({ ...f, role })))
          if (result.failed.some(f => f.control_settlement?.ok !== true || f.details?.physicalCleanupConfirmed === false)) break
        } catch (error) {
          if (!error.code?.startsWith('HOST_MODEL_')) throw error
          failed.push({ role, code: error.code, error: error.message, admission_stage: 'model_preflight' })
          break
        } finally {
          clearTimeout(timer)
          state.abort.signal.removeEventListener('abort', abortChild)
        }
      }
      return { mode: 'fixed-team-v1', profile: state.profile, model_registry: state.hostModels || null, deliveries: deliveries.map(compactWorkerEntry), failed: failed.map(compactWorkerEntry),
        diagnostic_detail_source: 'Unabridged reports page through dpswarm_report(item_id); full records remain in authenticated /api/plugin-audit; model view is bounded.', cleanup: state.cleanup,
        stopped: state.abort.signal.aborted || !enabled(this.config(), parent.session.id),
        worker_budget_policy: state.budgetRun?.profile || workerPolicy,
        next: 'Lead: inspect the current files and verify within the user-permitted scope. If the user forbids tests, do not run or add tests. For concrete production defects, call dpswarm_rework on the implementer item; keep corrections within the original task. Review every submitted delivered item with dpswarm_review(accept or terminate). Worker text is not an official score.', usage_note: unknownUsage }
    } finally {
      let budgetCleanupError
      try { if (state.budgetRun) await this.budget.finishTeamRun(parent, state.budgetRun) }
      catch (error) {
        budgetCleanupError = error
        state.cleanup.budget_error = { code: error?.code || null, message: String(error?.message ?? error) }
        if (error && typeof error === 'object') error.details = { ...error.details, deliveries, failed, worker_diagnostics: state.diagnostics }
      }
      finally { state.budgetRun = null }
      exec.signal?.removeEventListener('abort', cancelled)
      state.busy = false
      state.abort = null
      if (initialized) {
        try { await this.reconcile(state) } catch (error) {
          // Keep the lease and retain the cleanup failure for status/review.
          state.cleanup.reconcile_error = { code: error?.code || 'WORKSPACE_RECONCILE_FAILED', message: String(error?.message ?? error) }
        }
      } else {
        try { await this.release(state) } finally { settled() }
      }
      state.cleanup.workspace_lease_held = Boolean(state.lease)
      settled()
      if (budgetCleanupError) throw budgetCleanupError
    }
  }

  async rework(args, exec, authorization = {}) {
    const parent = exec?.agent
    requireRootCaller(parent)
    if (this.closed) throw failure('PLUGIN_DISPOSED', 'Plugin is stopping')
    if (!args || typeof args.item_id !== 'string' || !args.item_id.trim()
      || typeof args.feedback !== 'string' || !args.feedback.trim() || args.feedback.length > 20000
      || Object.keys(args).some(key => !['item_id', 'feedback'].includes(key))) {
      throw failure('REWORK_FEEDBACK_REQUIRED', 'Provide only an existing implementer item_id and concrete necessary corrections within the original task.')
    }
    if (!enabled(this.config(), parent.session.id)) throw failure('DPSWARM_DISABLED', 'Enable the team switch before requesting implementer rework.')
    const state = this.session(parent)
    if (state.busy) throw failure('RUN_ACTIVE', 'Wait for the current worker operation to settle.')
    // A ledger record remains readable after restart, but this release does not
    // reconstruct a live one-shot continuation or guess a changed connection.
    const source = state.implementers?.get(args.item_id), frozen = state.fixedTask
    if (!source || !frozen) throw failure('REWORK_RESTORE_REQUIRED', 'No verified live implementer continuation exists for this item. Historical records remain readable; restart recovery is not supported for rework.')
    if (source.revoked) throw failure('REWORK_ALLOCATION_REVOKED', 'This published attempt never bound its budget allocation. Use the reported original source item only after cleanup is confirmed.')
    if (source.superseded) throw failure('REWORK_SOURCE_SUPERSEDED', 'This implementer already has a linked continuation; use its latest item identifier.')
    if (!this.budget?.issueRework || !this.budget?.revokeRework) throw failure('WORKER_REWORK_BUDGET_UNAVAILABLE', 'Rework requires the independent worker budget service, including unlimited mode.')
    const check = async () => {
      if (state.abort?.signal.aborted || exec.signal?.aborted) throw failure('SUBAGENT_ABORTED', 'Rework was cancelled.')
      if (!enabled(this.config(), parent.session.id)) throw failure('DPSWARM_DISABLED', 'The team switch was disabled.')
      const current = authorization.validateTask ? await authorization.validateTask() : authorization.taskBinding
      if (!current?.required || current.phase !== 'finished' || !sameTask(current.binding, frozen.task_binding)
          || current.binding.root_session_id !== parent.session.id) throw failure('REWORK_TASK_MISMATCH', 'Only the original settled user task can request this implementer continuation.')
      const cfg = this.config()
      if (configurationFingerprint({ ...cfg, ...runtimePaths(cfg) }) !== frozen.configuration_fingerprint
          || hash(effectiveLeadRoute(parent)) !== hash(frozen.lead_route)) throw failure('REWORK_CONFIGURATION_CHANGED', 'The original connection or fixed model configuration changed; rework will not substitute a model or effort.')
    }
    await check()
    const records = await this.diagnostics(state, source.item_id)
    const evidence = records.filter(row => row.worker_session_id === source.worker_session_id && row.run_id === source.run_id && row.role === 'implementer')
    if (evidence.length !== 1 || !evidence[0].diagnostic.native_terminal
      || evidence[0].diagnostic.cleanup?.physical_cleanup_confirmed !== true || evidence[0].diagnostic.audit_error) {
      throw failure('REWORK_TERMINAL_EVIDENCE_REQUIRED', 'A trusted terminal worker and confirmed physical cleanup are required before rework.')
    }
    // Serialize before any asynchronous route/budget preflight. The dispatcher
    // also serializes tools; this guard protects direct internal controller use.
    if (state.busy) throw failure('RUN_ACTIVE', 'Another operation claimed this root.')
    state.busy = true
    state.abort = new AbortController()
    let settled
    state.settled = new Promise(resolve => { settled = resolve })
    const cancelled = () => state.abort?.abort(exec.signal.reason)
    exec.signal?.addEventListener('abort', cancelled, { once: true })
    if (exec.signal?.aborted) cancelled()
    let allocation, publishedChild, published = false, prepared = false, reworkId = randomUUID()
    const deliveries = [], failed = []
    state.cleanup = { workspace_lease_held: Boolean(state.lease), reconcile_error: null, budget_error: null }
    const recovery = state.reworkRecovery = { allocation_revoked: false, source_retry_allowed: false, retry_source_item_id: null, published_child_eligible: null }
    const record = (phase, extra = {}) => state.journal.append(parent.session.id, 'dpswarm/worker-rework', {
      version: 1, root_session_id: parent.session.id, owner_session_id: parent.session.id,
      run_id: reworkId, binding_id: frozen.task_binding.binding_id, phase,
      source_item_id: source.item_id, source_worker_session_id: source.worker_session_id,
      allocation_id: allocation?.allocation_id || null, feedback: args.feedback, at: Date.now(), ...extra,
    })
    try {
      await check()
      // Never claim budget while another root owns this workspace. An existing
      // same-root lease can contain tester/reviewer deliveries and stays held.
      if (!state.lease) this.acquire(state, parent)
      else {
        const lease = JSON.parse(readFileSync(state.lease.path, 'utf8'))
        if (state.lease.recovered || lease.run_id !== state.lease.run_id || lease.session_id !== parent.session.id || lease.pid !== process.pid) {
          throw failure('REWORK_WORKSPACE_UNCONFIRMED', 'Workspace ownership must be confirmed in this host before rework.')
        }
      }
      await state.sidecar.ensure()
      const before = await state.sidecar.call('GET', '/api/status'), item = before.snapshot?.work_items?.[source.item_id]
      if (!item || !['submitted', 'terminated'].includes(item.acceptance)) throw failure('REWORK_ITEM_NOT_ELIGIBLE', 'Only a submitted or terminated original implementer item is eligible; accepted deliveries are final.')
      if (before.snapshot.seal_phase?.root === 'cutoff') throw failure('REWORK_WORKSPACE_UNCONFIRMED', 'The root is sealed because cleanup is uncertain.')
      const routes = [{ role: 'lead', ...frozen.lead_route }, { role: 'implementer', provider: frozen.profile.implementer.provider,
        model: frozen.profile.implementer.model, ...(frozen.profile.implementer.reasoning_effort ? { reasoningEffort: frozen.profile.implementer.reasoning_effort } : {}) }]
      const expectedModels = state.hostModels ? { ...state.hostModels, models: state.hostModels.models.filter(row => ['lead', 'implementer'].includes(row.role)) } : undefined
      if (this.modelRegistry) {
        await this.modelRegistry.resolve(routes, { signal: state.abort.signal, expected: expectedModels })
        this.modelRegistry.checkLead(effectiveLeadRoute(parent), routes)
      }
      // The rework allowance reads only the rework settings, never the frozen
      // initial-worker policy; the issued grant must match what was announced.
      const reworkProfile = reworkBudgetProfile(this.config())
      const allowanceNote = reworkProfile.mode === 'unlimited'
        ? 'This is a linked rework of the original implementer without a token or call cap.'
        : `This is a linked rework of the original implementer with a fixed allowance of ${reworkProfile.tokenLimit} cumulative tokens and ${reworkProfile.callLimit} model calls.`
      const task = `${workerRolePrompt('implementer')}\n\n${allowanceNote} Earlier usage remains separately recorded. Repair the specified defects until the original requirements are met; do not polish beyond the task. Fix only the concrete defects below within the original scope. Preserve unrelated work; do not add requirements or optional validation. Explicit no-tests instructions take precedence. Deliver the current candidate promptly.\n\nOriginal task:\n${frozen.task}\n\nOriginal acceptance:\n${frozen.acceptance || 'Use only the original task requirements.'}\n\nNecessary corrections:\n${args.feedback}`
      await check()
      allocation = await this.budget.issueRework(parent, { workerSessionId: source.worker_session_id, task })
      const sameProfile = (a, b) => !!a && !!b && a.mode === b.mode && a.tokenLimit === b.tokenLimit && a.callLimit === b.callLimit
      if (allocation?.source_worker_session_id !== source.worker_session_id || allocation.role !== 'implementer' || !sameProfile(allocation.profile, reworkProfile)
          || typeof allocation.allocation_id !== 'string' || typeof allocation.prompt !== 'string') throw failure('WORKER_REWORK_ALLOCATION_INVALID', 'Budget continuation identity did not match the original implementer.')
      await check()
      await record('prepared', { profile: allocation.profile, route: frozen.profile.implementer })
      prepared = true
      // Route and new-allocation preflight succeeded before changing the old
      // submitted item. This is supersession, never acceptance of failed work.
      if (item.acceptance === 'submitted') await state.sidecar.call('POST', '/api/review', { item_id: source.item_id,
        verdict: 'terminate', reason: 'manual-stopped', review_note: `Superseded by linked implementer rework ${reworkId}; original report remains preserved.` })
      await check()
      const timeout = new AbortController()
      const abortChild = () => timeout.abort(state.abort.signal.reason)
      const timer = setTimeout(() => timeout.abort(failure('WORKER_TIMEOUT', 'Fixed implementer rework exceeded its original wall-time limit')), frozen.profile.workerTimeoutSeconds * 1000)
      state.abort.signal.addEventListener('abort', abortChild, { once: true })
      try {
        const route = frozen.profile.implementer
        const result = await delegateOnce({ kind: 'derive', subtasks: [{ provider: route.provider, model: route.model,
          reasoning_effort: route.reasoning_effort, title: 'DPswarm implementer rework', prompt: allocation.prompt }] },
        { ...exec, signal: timeout.signal }, state.sidecar, this.subagents, {
          routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget, runId: reworkId,
          onDiagnostic: value => state.diagnostics.push(value), modelRegistry: this.modelRegistry,
          modelRoutes: routes, hostModels: expectedModels, modelRole: 'implementer', beforeChildStart: check,
          onChildStarted: async details => {
            published = true
            publishedChild = { item_id: details.item_id, worker_session_id: details.execution_session_id }
            source.superseded = true
            state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id,
              run_id: reworkId, superseded: false })
            await record('published', { item_id: details.item_id, worker_session_id: details.execution_session_id })
          },
        })
        if (!Array.isArray(result.deliveries)) failed.push({ role: 'implementer', code: result.outcome || 'NOT_ADMITTED', error: result.message || 'No rework worker was admitted' })
        else {
          deliveries.push(...result.deliveries.map(value => ({ ...value, role: 'implementer', source_item_id: source.item_id })))
          failed.push(...result.failed.map(value => ({ ...value, role: 'implementer', source_item_id: source.item_id })))
        }
      } finally {
        clearTimeout(timer)
        state.abort.signal.removeEventListener('abort', abortChild)
      }
      await record('finished', { published, delivery_item_ids: deliveries.map(value => value.item_id),
        failed_item_ids: failed.map(value => value.item_id).filter(Boolean), failure_codes: failed.map(value => value.code) })
      return { mode: 'fixed-implementer-rework-v1', source_item_id: source.item_id, source_worker_session_id: source.worker_session_id,
        worker_budget_policy: allocation.profile, deliveries: deliveries.map(compactWorkerEntry), failed: failed.map(compactWorkerEntry),
        stopped: state.abort.signal.aborted || !enabled(this.config(), parent.session.id), cleanup: state.cleanup, rework_recovery: recovery,
        next: 'Inspect the necessary corrections within the original permitted scope. Accept only an item in deliveries. A failed or partial candidate is not an accepted worker delivery; use the latest implementer item for any further necessary rework.' }
    } catch (error) {
      if (prepared) {
        try { await record('failed', { published, error_code: error?.code || 'REWORK_FAILED' }) }
        catch (auditError) { state.cleanup.reconcile_error = { code: auditError?.code || 'REWORK_AUDIT_FAILED', message: String(auditError?.message || auditError) } }
      }
      throw error
    } finally {
      let cleanupError
      try {
        if (allocation?.allocation_id) {
          const revoked = await this.budget.revokeRework(parent, allocation.allocation_id)
          if (revoked?.revoked === true && publishedChild) {
            const attempt = state.implementers.get(publishedChild.item_id)
            if (attempt) attempt.revoked = true
            const diagnostic = state.diagnostics.findLast(row => row.item_id === publishedChild.item_id
              && row.execution_session_id === publishedChild.worker_session_id)?.diagnostic
            const failedChild = failed.find(row => row.item_id === publishedChild.item_id)
            const safe = diagnostic?.native_terminal != null && diagnostic?.cleanup?.physical_cleanup_confirmed === true
              && failedChild?.control_settlement?.ok === true && failedChild?.details?.physicalCleanupConfirmed !== false
            // Publication binds execution/route, not necessarily a budget. Only
            // an authenticated unbound revocation plus terminal cleanup can
            // reopen the old source. A consumed allocation stays on its chain.
            await record('revoked', { item_id: publishedChild.item_id, worker_session_id: publishedChild.worker_session_id,
              source_retry_allowed: safe, published_child_eligible: false })
            recovery.allocation_revoked = true
            recovery.published_child_eligible = false
            if (safe) {
              source.superseded = false
              recovery.source_retry_allowed = true
              recovery.retry_source_item_id = source.item_id
            }
          } else if (revoked?.bound === true) recovery.published_child_eligible = true
        }
      }
      catch (error) { cleanupError = error; state.cleanup.budget_error = { code: error.code || 'REWORK_REVOKE_FAILED', message: String(error.message || error) } }
      exec.signal?.removeEventListener('abort', cancelled)
      // Keep the lease on an unconfirmed revoke or failed control cleanup.
      if (!cleanupError && !state.cleanup.reconcile_error) {
        try { await this.reconcile(state) }
        catch (error) { state.cleanup.reconcile_error = { code: error.code || 'WORKSPACE_RECONCILE_FAILED', message: String(error.message || error) } }
      }
      state.cleanup.workspace_lease_held = Boolean(state.lease)
      state.busy = false
      state.abort = null
      settled()
      if (cleanupError) throw cleanupError
    }
  }

  async shutdown() {
    this.closed = true
    const states = [...this.sessions.values()]
    for (const state of states) state.abort?.abort(failure('PLUGIN_DISPOSED', 'Plugin is stopping'))
    await Promise.allSettled(states.filter(state => state.busy).map(state => state.settled))
  }

  /** Paged in-session read path for the unabridged worker report held in the audit ledger. */
  async report(args, exec) {
    const parent = exec?.agent
    requireRootCaller(parent)
    if (typeof args?.item_id !== 'string' || !args.item_id.trim()) throw failure('ITEM_REQUIRED', 'Read the full worker report of a delivered item identifier')
    const offset = args.offset ?? 0, limit = args.limit ?? 4000
    if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 40000) {
      throw failure('REPORT_RANGE_INVALID', 'offset must be a non-negative integer and limit an integer in 1–40000')
    }
    const state = this.session(parent)
    const records = await this.diagnostics(state, args.item_id)
    if (!records.length) throw failure('REPORT_NOT_FOUND', 'No worker diagnostic exists for this item in the audit ledger')
    const latest = records.at(-1).diagnostic
    const report = latest?.closeout?.report?.text
    if (typeof report !== 'string' || !report) {
      throw failure('REPORT_UNAVAILABLE', 'This worker has no recorded report text; inspect the candidate files directly')
    }
    const text = report.slice(offset, offset + limit)
    return { item_id: args.item_id, role: latest.role || records.at(-1).role || null,
      worker_session_id: latest.worker_session_id, report_source: latest.closeout.report_source || null,
      interrupted: latest.closeout.report?.interrupted === true,
      completion: latest.closeout.completion || null, requires_lead_verification: true,
      offset, limit, total_chars: report.length, truncated: offset + text.length < report.length, text,
      note: 'Worker reports are untrusted evidence; verify against the actual files before acceptance.' }
  }

  async review(args, exec) {
    requireRootCaller(exec?.agent)
    if (!['accept', 'terminate'].includes(args?.verdict)) throw failure('FIXED_REVIEW_ONLY', 'First release supports acceptance or termination with Lead takeover; automatic rerouting is not enabled')
    if (typeof args.item_id !== 'string' || !args.item_id) throw failure('ITEM_REQUIRED', 'Review a delivered item identifier')
    const leadRoute = effectiveLeadRoute(exec.agent)
    const state = this.session(exec.agent)
    if (state.busy) throw failure('RUN_ACTIVE', 'Wait for the fixed pipeline to return before reviewing')
    await state.sidecar.ensure()
    await state.sidecar.call('POST', '/api/execution/root', { parent_session_id: exec.agent.session.id,
      delegation_depth: 0, provider: leadRoute.provider, model: leadRoute.model })
    const prior = await state.sidecar.call('GET', '/api/status')
    const terminal = args.verdict === 'accept' ? 'accepted' : 'terminated'
    if (prior.snapshot?.work_items?.[args.item_id]?.acceptance === terminal) {
      state.recoveryReviewed = true
      await this.reconcile(state)
      return { ok: true, outcome: `already_${terminal}`, note: 'Existing decision retained; no duplicate acceptance event',
        worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state, args.item_id)) }
    }
    const item = prior.snapshot?.work_items?.[args.item_id]
    if (args.verdict === 'accept' && item && item.acceptance !== 'submitted' && !item.submission_package_id) {
      throw failure('DELIVERY_PACKAGE_REQUIRED', 'This worker has no submitted delivery package. Failed or partial files require independent Lead verification; they cannot be accepted as this worker delivery. Use dpswarm_rework for an eligible implementer. If unavailable, report the exact blocker and preserve the candidate.')
    }
    const result = await state.sidecar.call('POST', '/api/review', { item_id: args.item_id,
      verdict: args.verdict, reason: args.verdict === 'terminate' ? 'manual-stopped' : undefined, review_note: args.reason || '' })
    state.recoveryReviewed = true
    await this.reconcile(state)
    return { ...result, worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state, args.item_id)) }
  }
}
