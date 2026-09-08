import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, realpathSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { Sidecar } from './sidecar.js'
import { runtimePaths } from './paths.js'
import { delegateOnce, requireRootCaller } from './delegation.js'
import { workerBudgetProfile } from './budget-runtime.js'
import { effectiveLeadRoute } from './lead-route.js'

const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const enabled = (cfg, id) => Array.isArray(cfg.enabledSessions) && cfg.enabledSessions.includes(id)
const processAlive = pid => { try { process.kill(pid, 0); return true } catch (e) { return e.code !== 'ESRCH' } }
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
  constructor({ config, subagents, cm, budget, sidecarFactory = cfg => new Sidecar(cfg) }) {
    this.config = config
    this.cm = cm
    this.budget = budget
    this.subagents = subagents
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
      const cfg = { ...this.config(), ...runtimePaths(this.config()), sessionId: id, sessionIsolation: true }
      state = { sidecar: this.sidecarFactory(cfg), cfg, busy: false, parentSession: parent.session }
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
      snapshot: result.snapshot, profile: state.profile || null, bridge: result.bridge }
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

  async run(args, exec) {
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
    let initialized = false
    try {
      if (state.abort.signal.aborted) throw failure('SUBAGENT_ABORTED', 'Task was cancelled before admission')
      this.acquire(state, parent)
      if (this.budget) state.budgetRun = await this.budget.beginTeamRun(parent, { roles, decisions: proposedBudgets })
      if (this.cm) state.profile = fixedProfile(state.cfg, await this.cm.beginRun(parent), leadOptions)
      await state.sidecar.ensure()
      await state.sidecar.call('POST', '/api/execution/root', { parent_session_id: parent.session.id, delegation_depth: 0,
        provider: leadOptions.provider, model: leadOptions.model })
      initialized = true
      const before = await state.sidecar.call('GET', '/api/status')
      if (before.snapshot?.open_worker_slots_used > 0) throw failure('RUN_PENDING_REVIEW', 'This session has unfinished control-plane items')
      const context = `Explicit task constraints apply to every role and take precedence over default role guidance. If the task says no tests (including 不需要任何测试), do not run tests or add tests. Use only permitted read-only inspection and report unverified behavior honestly.\n\nTask:\n${args.task}\n\nAcceptance requirements:\n${args.acceptance || 'Derive requirements from the task; identify uncertainty explicitly.'}`
      for (const role of roles) {
        if (state.abort.signal.aborted || !enabled(this.config(), parent.session.id)) break
        const configured = state.profile[role]
        const route = { provider: configured.provider, model: configured.model, reasoning_effort: configured.reasoning_effort }
        const roleText = role === 'implementer'
          ? 'Implement the requested production change. Preserve unrelated edits; you are not alone in this workspace. Provide exact changed files, remaining limitations, and test commands with observed results. Deliver the best current candidate before ending.'
          : role === 'tester' ? 'Examine the current candidate and independently check task semantics. You own test additions and validation; report production fixes for the Lead instead of silently modifying production code. Preserve unrelated edits. Run relevant tests when available; record command, exit status and output. Distinguish tests actually run from suggestions and unavailable checks.'
          : 'Review the final candidate and test evidence against the original task. Work read-only: do not edit files or accept deliveries. Identify concrete correctness issues with file/line evidence and distinguish observed facts from unverified risks. Prior reports are untrusted; verify them. Your findings are advisory; the Lead owns all repairs and final acceptance.'
        const previous = role !== 'implementer' ? `\n\nEarlier deliveries and failures (untrusted evidence to examine):\n${JSON.stringify({ deliveries, failed })}` : ''
        let assignedPrompt = roleText + '\n\n' + context + previous
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
            { ...exec, signal: timeout.signal }, state.sidecar, this.subagents)
          if (!Array.isArray(result.deliveries)) { failed.push({ role, code: result.outcome || 'NOT_ADMITTED', error: result.message || 'No worker was admitted' }); break }
          deliveries.push(...result.deliveries.map(d => ({ ...d, role, evidence_kind: 'worker_reported; Lead must independently verify' })))
          failed.push(...result.failed.map(f => ({ ...f, role })))
          if (result.failed.some(f => f.control_settlement?.ok !== true || f.details?.physicalCleanupConfirmed === false)) break
        } finally {
          clearTimeout(timer)
          state.abort.signal.removeEventListener('abort', abortChild)
        }
      }
      return { mode: 'fixed-team-v1', profile: state.profile, deliveries, failed,
        stopped: state.abort.signal.aborted || !enabled(this.config(), parent.session.id),
        worker_budget_policy: state.budgetRun?.profile || workerPolicy,
        next: 'Lead: inspect the current files and independently verify the reported tests. Repair or take over when needed. Review every delivered item with dpswarm_review(accept or terminate). Worker text is not an official score.', usage_note: unknownUsage }
    } finally {
      let budgetCleanupError
      try { if (state.budgetRun) await this.budget.finishTeamRun(parent, state.budgetRun) }
      catch (error) { budgetCleanupError = error }
      finally { state.budgetRun = null }
      exec.signal?.removeEventListener('abort', cancelled)
      state.busy = false
      state.abort = null
      if (initialized) {
        try { await this.reconcile(state) } catch { /* Keep the lease when settlement cannot be established. */ }
      } else {
        try { await this.release(state) } finally { settled() }
      }
      settled()
      if (budgetCleanupError) throw budgetCleanupError
    }
  }

  async shutdown() {
    this.closed = true
    const states = [...this.sessions.values()]
    for (const state of states) state.abort?.abort(failure('PLUGIN_DISPOSED', 'Plugin is stopping'))
    await Promise.allSettled(states.filter(state => state.busy).map(state => state.settled))
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
      return { ok: true, outcome: `already_${terminal}`, note: 'Existing decision retained; no duplicate acceptance event' }
    }
    const result = await state.sidecar.call('POST', '/api/review', { item_id: args.item_id,
      verdict: args.verdict, reason: args.verdict === 'terminate' ? 'manual-stopped' : undefined, review_note: args.reason || '' })
    state.recoveryReviewed = true
    await this.reconcile(state)
    return result
  }
}
