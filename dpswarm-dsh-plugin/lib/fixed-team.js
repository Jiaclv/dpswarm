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
import { workerRolePrompt, teamModeFor, teamModeGuidance } from './role-guidance.js'
import { scopesOverlap } from './write-scope.js'

/** Parallel implementer split: 1–3 disjoint write scopes, Lead-authored. */
function validateSubtasks(value) {
  if (!Array.isArray(value) || !value.length || value.length > 3) {
    throw failure('PARALLEL_SUBTASKS_INVALID', 'subtasks must be an array of 1–3 entries (parallel implementer phase; omit for the sequential team)')
  }
  const ids = new Set()
  for (const [index, st] of value.entries()) {
    if (!st || typeof st !== 'object' || Array.isArray(st)
      || typeof st.id !== 'string' || !st.id.trim() || ids.has(st.id)
      || typeof st.task !== 'string' || !st.task.trim()
      || !Array.isArray(st.write_scope) || !st.write_scope.length || st.write_scope.some(s => typeof s !== 'string' || !s.trim())
      || (st.acceptance !== undefined && typeof st.acceptance !== 'string')
      || Object.keys(st).some(key => !['id', 'task', 'write_scope', 'acceptance'].includes(key))) {
      throw failure('PARALLEL_SUBTASKS_INVALID', `subtasks[${index}] needs a unique id, a task and a nonempty write_scope glob list`)
    }
    ids.add(st.id)
  }
  for (let i = 0; i < value.length; i++) for (let j = i + 1; j < value.length; j++) {
    if (scopesOverlap(value[i].write_scope, value[j].write_scope)) {
      throw failure('WORKER_SCOPE_OVERLAP', `write_scope of "${value[i].id}" overlaps with "${value[j].id}"; split disjoint file regions before dispatch`)
    }
  }
  return value.map(st => ({ id: st.id.trim(), task: st.task, write_scope: st.write_scope.map(s => s.trim()),
    ...(st.acceptance ? { acceptance: st.acceptance } : {}) }))
}

const scopeClause = st => `\n\n写范围（工具层强制）：只能写入或修改匹配 ${st.write_scope.join('、')} 的文件；读取不限。其他实现者正在并行处理其余子任务——他们的范围不属于你，越界写会被拒绝并记录。`

/** Staged mode (fixed-team-v3): Lead-authored artifact board + phases. */
function validateStaged(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some(k => !['artifacts', 'phases'].includes(k))) {
    throw failure('STAGED_INVALID', 'staged must be an object with artifacts and phases')
  }
  const { artifacts, phases } = value
  if (!Array.isArray(phases) || !phases.length || phases.length > 4) throw failure('STAGED_INVALID', 'phases: 1–4 entries')
  if (!Array.isArray(artifacts) || !artifacts.length || artifacts.length > 8) throw failure('STAGED_INVALID', 'artifacts: 1–8 entries')
  const phaseIds = new Set()
  for (const [i, ph] of phases.entries()) {
    if (!ph || typeof ph !== 'object' || typeof ph.id !== 'string' || !ph.id.trim() || phaseIds.has(ph.id)
      || typeof ph.task !== 'string' || !ph.task.trim()
      || Object.keys(ph).some(k => !['id', 'task'].includes(k))) throw failure('STAGED_INVALID', `phases[${i}] needs a unique id and a task`)
    phaseIds.add(ph.id)
  }
  const phaseOrder = new Map([...phaseIds].map((id, i) => [id, i]))
  const ids = new Set()
  for (const [i, a] of artifacts.entries()) {
    if (!a || typeof a !== 'object' || Array.isArray(a)
      || typeof a.id !== 'string' || !a.id.trim() || ids.has(a.id)
      || typeof a.title !== 'string' || !a.title.trim()
      || typeof a.task !== 'string' || !a.task.trim()
      || !Array.isArray(a.write_globs) || !a.write_globs.length || a.write_globs.some(g => typeof g !== 'string' || !g.trim())
      || typeof a.phase !== 'string' || !phaseIds.has(a.phase)
      || (a.deps !== undefined && (!Array.isArray(a.deps) || a.deps.some(d => typeof d !== 'string')))
      || Object.keys(a).some(k => !['id', 'title', 'task', 'write_globs', 'phase', 'deps'].includes(k))) {
      throw failure('STAGED_INVALID', `artifacts[${i}] needs unique id, title, task, write_globs, a valid phase and optional deps`)
    }
    ids.add(a.id)
  }
  const artifactPhase = new Map(artifacts.map(a => [a.id, a.phase]))
  for (const a of artifacts) for (const d of a.deps || []) {
    if (!ids.has(d)) throw failure('STAGED_INVALID', `artifact ${a.id} depends on unknown artifact ${d}`)
    if (phaseOrder.get(artifactPhase.get(d)) > phaseOrder.get(a.phase)) throw failure('STAGED_INVALID', `artifact ${a.id} depends on ${d} from a later phase`)
  }
  // Design-time deadlock removal: the artifact graph must be acyclic.
  const remaining = new Map(artifacts.map(a => [a.id, new Set(a.deps || [])]))
  const done = new Set()
  while (true) {
    const ready = [...remaining.keys()].filter(id => [...remaining.get(id)].every(d => done.has(d)))
    if (!ready.length) break
    for (const id of ready) { remaining.delete(id); done.add(id) }
  }
  if (remaining.size) throw failure('ARTIFACT_CYCLE', `artifact deps form a cycle: ${[...remaining.keys()].join(', ')}`)
  for (let i = 0; i < artifacts.length; i++) for (let j = i + 1; j < artifacts.length; j++) {
    if (scopesOverlap(artifacts[i].write_globs, artifacts[j].write_globs)) {
      throw failure('WORKER_SCOPE_OVERLAP', `write_globs of "${artifacts[i].id}" overlap "${artifacts[j].id}"`)
    }
  }
  return {
    phases: phases.map(ph => ({ id: ph.id.trim(), task: ph.task })),
    artifacts: artifacts.map(a => ({ id: a.id.trim(), title: a.title.trim(), task: a.task,
      write_globs: a.write_globs.map(g => g.trim()), phase: a.phase, deps: [...(a.deps || [])] })),
  }
}

/** Wave plan: phases in order; intra-phase dependency waves (deps from earlier phases are settled). */
function planWaves(staged) {
  const phaseIndex = new Map(staged.phases.map((p, i) => [p.id, i]))
  const artifactPhase = new Map(staged.artifacts.map(a => [a.id, phaseIndex.get(a.phase)]))
  const waves = [], done = new Set()
  for (const phase of staged.phases) {
    const mine = staged.artifacts.filter(a => a.phase === phase.id)
    const pending = new Set(mine.map(a => a.id))
    while (pending.size) {
      const ready = mine.filter(a => pending.has(a.id)
        && (a.deps || []).every(d => done.has(d) || artifactPhase.get(d) < phaseIndex.get(phase.id)))
      if (!ready.length) break
      waves.push(ready.map(a => ({ id: a.id, title: a.title, task: a.task, write_scope: a.write_globs, phase: a.phase })))
      for (const a of ready) { pending.delete(a.id); done.add(a.id) }
    }
  }
  return waves
}

const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const enabled = (cfg, id) => Array.isArray(cfg.enabledSessions) && cfg.enabledSessions.includes(id)
const processAlive = pid => { try { process.kill(pid, 0); return true } catch (e) { return e.code !== 'ESRCH' } }
const clone = value => JSON.parse(JSON.stringify(value))
const configurationFingerprint = cfg => hash(Object.fromEntries(['sidecarUrl', 'workspace', 'dpswarmDir', 'pythonCmd', 'subagentProvider', 'implMode', 'implProvider', 'implModel', 'implEffort'].map(key => [key, cfg[key] ?? null])))
const sameTask = (a, b) => a && b && ['root_session_id', 'binding_id', 'user_message_id', 'user_content_sha256'].every(key => typeof a[key] === 'string' && a[key] === b[key])
const unknownUsage = 'Host subagent results do not provide a complete usage ledger; unknown values remain null.'

// Mirrors Projection.team_open_workers: non-terminal derive/fission items hold
// against spec.max_team_workers. Acceptance values follow AcceptanceState.
const TERMINAL_ACCEPTANCE = new Set(['accepted', 'terminated', 'escalated', 'aborted-finalize'])
const openWorkerItems = snapshot => Object.values(snapshot?.work_items || {})
  .filter(item => ['derive', 'fission'].includes(item.kind) && !TERMINAL_ACCEPTANCE.has(item.acceptance ?? 'active')).length

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
  constructor({ config, subagents, cm, budget, modelRegistry, resolveSession, sidecarFactory = cfg => new Sidecar(cfg), writeScope = null }) {
    this.config = config
    this.cm = cm
    this.budget = budget
    this.modelRegistry = modelRegistry
    this.subagents = subagents
    this.resolveSession = resolveSession
    this.sidecarFactory = sidecarFactory
    this.writeScope = writeScope
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
    const teamMode = teamModeFor(cfg, parent.session.id)
    const base = { enabled: on, mode: 'fixed-team-v1', session_id: parent.session.id,
      team_mode: { mode: teamMode,
        source: (cfg.teamModeOverrides || []).some(row => row?.sessionId === parent.session.id) ? 'user popover override' : 'default',
        instruction: teamModeGuidance(teamMode) },
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
    // Restore the raised team-worker cap once every item reached a terminal
    // state; a crash in between simply leaves the raised (audited) spec.
    const raisedFrom = state.fixedTask?.raised_team_workers
    if (raisedFrom !== undefined && openWorkerItems(snapshot) === 0) {
      try {
        await state.sidecar.call('POST', '/api/spec', { max_team_workers: raisedFrom })
        state.fixedTask.raised_team_workers = undefined
      } catch (error) {
        state.cleanup.reconcile_error = { code: error?.code || 'SPEC_RESTORE_FAILED', message: String(error?.message ?? error) }
      }
    }
    if (snapshot.open_worker_slots_used === 0 && (!state.lease?.recovered || state.recoveryReviewed)) { await this.release(state); return true }
    return false
  }

  /**
   * The fixed topology must fit the §7 team-worker cap (non-terminal items
   * count). Publish an audited spec revision raising only max_team_workers to
   * the needed level; reconcile() restores the original value after settle.
   */
  async ensureTeamCapacity(state, parent, need) {
    const status = await state.sidecar.call('GET', '/api/status')
    const current = status.spec?.max_team_workers
    if (!Number.isSafeInteger(current)) return
    const required = Math.min(8, openWorkerItems(status.snapshot) + need)
    if (current >= required) return
    await state.sidecar.call('POST', '/api/spec', { max_team_workers: required })
    if (state.fixedTask && state.fixedTask.raised_team_workers === undefined) state.fixedTask.raised_team_workers = current
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
    const allowed = new Set(['task', 'acceptance', 'worker_budgets', 'subtasks', 'staged'])
    if (Object.keys(args).some(key => !allowed.has(key))) throw failure('FIXED_MODE_ONLY', 'Models, roles and topology come from user settings')
    if (args.acceptance != null && (typeof args.acceptance !== 'string' || args.acceptance.length > 40000)) throw failure('INVALID_ACCEPTANCE', 'Acceptance requirements must be text')
    const subtasks = args.subtasks === undefined ? null : validateSubtasks(args.subtasks)
    const staged = args.staged === undefined ? null : validateStaged(args.staged)
    if (subtasks && staged) throw failure('STAGED_INVALID', 'staged and subtasks are mutually exclusive; staged carries its own artifact split')
    const waves = staged ? planWaves(staged) : null
    const parallelCount = subtasks ? subtasks.length : staged ? staged.artifacts.length : 0
    let state = this.session(parent)
    if (state.busy || state.lease) throw failure('RUN_PENDING', 'Finish or review the current fixed-team run first')
    // Configuration edits apply to a new run; active execution and review keep their connection snapshot.
    this.sessions.delete(parent.session.id)
    state = this.session(parent)
    // The user picks the team mode per task (popover). The Lead may not exceed it.
    const teamMode = teamModeFor(state.cfg, parent.session.id)
    if (teamMode === 'serial' && (subtasks || staged)) throw failure('USER_TEAM_MODE_SERIAL', 'This task is set to serial by the user; the Lead must not split it. Switch the mode in the compass menu first.')
    if (teamMode === 'parallel' && staged) throw failure('USER_TEAM_MODE_PARALLEL', 'This task is set to parallel by the user; staged needs the user to switch the mode first.')
    if (teamMode === 'staged' && subtasks) throw failure('USER_TEAM_MODE_STAGED', 'This task is set to staged by the user; subtasks need the user to switch to parallel.')
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
        const plan = proposedBudgets[role]
        const plans = role === 'implementer' && parallelCount ? plan : [plan]
        if (role === 'implementer' && parallelCount && (!Array.isArray(plan) || plan.length !== parallelCount)) {
          throw failure('WORKER_BUDGET_DECISION_REQUIRED', 'Parallel implementers need one index-aligned worker_budgets.implementer entry per subtask or staged artifact.')
        }
        if (!(role === 'implementer' && parallelCount) && Array.isArray(plan)) {
          throw failure('WORKER_BUDGET_DECISION_REQUIRED', `worker_budgets.${role} must be a single decision object outside a parallel implementer phase.`)
        }
        for (const entry of plans) {
          if (!entry || !Number.isSafeInteger(entry.tokenLimit) || entry.tokenLimit < 1 || !Number.isSafeInteger(entry.callLimit) || entry.callLimit < 1
            || typeof entry.reason !== 'string' || !entry.reason.trim() || entry.reason.length > 4000
            || Object.keys(entry).some(k => !['tokenLimit','callLimit','reason'].includes(k))) {
            throw failure('WORKER_BUDGET_DECISION_REQUIRED', `Lead must provide positive tokenLimit, callLimit and a short reason for ${role}.`)
          }
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
        ...(subtasks ? { subtasks: clone(subtasks) } : {}),
        ...(staged ? { staged: clone(staged) } : {}),
        configuration_fingerprint: configurationFingerprint(state.cfg), lead_route: clone(leadOptions) }
      state.implementers = new Map()
      state.testers = new Map()
      state.reviewers = new Map()
      this.writeScope?.clear(parent.session.id)
      if (state.fixedTask.task_binding) await state.journal.append(parent.session.id, 'dpswarm/fixed-team-binding', state.fixedTask)
      // The fixed topology must fit the §7 team-worker cap before dispatch;
      // rework headroom is raised on demand in rework().
      await this.ensureTeamCapacity(state, parent, (parallelCount ? (staged ? Math.max(...waves.map(w => w.length)) : parallelCount) : 1) + roles.length - 1)
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
        const parallelImplementers = role === 'implementer' && (subtasks || staged)
        if (state.budgetRun && !parallelImplementers) {
          const allocation = await this.budget.issueTeamWorker(parent, state.budgetRun, { task: assignedPrompt, label: role })
          assignedPrompt = allocation.prompt
        }
        const timeout = new AbortController()
        const timer = setTimeout(() => timeout.abort(failure('WORKER_TIMEOUT', 'Fixed role exceeded its wall-time limit')), state.profile.workerTimeoutSeconds * 1000)
        const abortChild = () => timeout.abort(state.abort.signal.reason)
        state.abort.signal.addEventListener('abort', abortChild, { once: true })
        try {
          if (parallelImplementers) {
            // Parallel implementer phase: one derive dispatch fans out N children
            // with per-subtask allocations and enforced write-scope claims.
            // Staged mode registers the artifact board first, then dispatches
            // dependency waves (each wave is one parallel derive fan-out).
            const waveList = staged ? waves : [subtasks]
            if (staged) {
              for (const artifact of staged.artifacts) {
                try {
                  await state.sidecar.call('POST', '/api/artifact/register', { id: artifact.id, title: artifact.title,
                    write_globs: artifact.write_globs, owner_item: null, phase: artifact.phase, deps: artifact.deps })
                } catch (error) {
                  throw failure('ARTIFACT_REGISTER_FAILED', `artifact ${artifact.id}: ${error.message || error}`)
                }
                this.writeScope?.registerArtifact(parent.session.id, { ...artifact, state: 'pending', version: 1 })
              }
            }
            let waveFailed = false
            const pendingWaits = []
            let prevWavePhase = null
            for (const wave of waveList) {
              if (state.abort.signal.aborted || !enabled(this.config(), parent.session.id) || waveFailed) break
              // Phase gate handoff: a wave opening a new phase carries a bounded
              // digest of the earlier phases' deliveries (untrusted; files are
              // authoritative). The CM-curated digest is the later upgrade.
              const phaseHandoff = staged && prevWavePhase !== null && wave[0]?.phase !== prevWavePhase && deliveries.length
                ? '\n\n前序相位交付摘要（不可信摘要，以实际文件为准；状态板见各产物状态）：\n'
                  + deliveries.map(d => `【${d.subtask ?? d.role}】${(d.output || '').slice(0, 2000)}`).join('\n\n')
                : ''
              const assigned = []
              for (const [index, st] of wave.entries()) {
                let prompt = roleText + '\n\n' + context + phaseHandoff
                  + `\n\n你负责的子任务（${st.id}）：\n${st.task}`
                  + (st.acceptance ? `\n\n本子任务验收：\n${st.acceptance}` : '')
                  + scopeClause(st)
                  + (staged ? `\n\n你管理的产物是「${st.title ?? st.id}」（id: ${st.id}）：开始写入时用 dpswarm_artifact 把它推进到 draft，完成并自查后推进到 ready；读取他人未 ready 的产物会被工具层拒绝（先做自己部分）。若你必须等他人产物才能继续，就先保存当前进度，并在最终回复的最后一行单独写 [DPSWARM_WAITING: <那个产物的 id>]——它就绪后你会被唤醒继续。` : '')
                if (state.budgetRun) {
                  const allocation = await this.budget.issueTeamWorker(parent, state.budgetRun, { task: prompt, label: 'implementer',
                    subtask: st.id, subtaskIndex: staged ? staged.artifacts.findIndex(a => a.id === st.id) : index })
                  prompt = allocation.prompt
                }
                assigned.push({ ...route, title: `DPswarm implementer · ${st.id}`, prompt })
              }
              const result = await delegateOnce({ kind: 'derive', subtasks: assigned },
                { ...exec, signal: timeout.signal }, state.sidecar, this.subagents,
                { routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget,
                  runId: state.lease.run_id, onDiagnostic: value => state.diagnostics.push(value),
                  modelRegistry: this.modelRegistry, modelRoutes: state.modelRoutes, hostModels: state.hostModels, modelRole: role, onChildStarted: async details => {
                    const st = wave[details.subtask_index]
                    if (st && this.writeScope) {
                      // A rework continuation re-claims the same subtask region; the
                      // source worker is provably terminal before rework starts.
                      await this.writeScope.claim({ rootId: parent.session.id, sessionId: details.execution_session_id,
                        subtask: st.id, scopes: st.write_scope, runId: state.lease.run_id })
                      if (staged) {
                        this.writeScope.updateArtifactState(parent.session.id, st.id, 'claimed')
                        try { await state.sidecar.call('POST', '/api/artifact/state', { artifact_id: st.id, to: 'claimed' }) }
                        catch (error) { state.cleanup.reconcile_error = { code: error?.code || 'ARTIFACT_STATE_FAILED', message: String(error?.message ?? error) } }
                      }
                    }
                    state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id,
                      run_id: state.lease.run_id, subtask: st?.id ?? null, superseded: false })
                    await onChildStarted?.({ ...details, role, run_id: state.lease.run_id })
                  } })
              if (!Array.isArray(result.deliveries)) { failed.push({ role, code: result.outcome || 'NOT_ADMITTED', error: result.message || 'No worker was admitted' }); waveFailed = true; break }
              for (const d of result.deliveries) {
                const wait = staged ? /\[DPSWARM_WAITING:\s*([\w.-]+)\]\s*$/m.exec(d.output || '') : null
                if (wait) pendingWaits.push({ subtask: wave[d.subtask_index]?.id ?? null, item_id: d.item_id,
                  worker_session_id: d.execution_session_id, awaiting: wait[1], report: d.output || '', attempt: 0 })
                else deliveries.push({ ...d, role, subtask: wave[d.subtask_index]?.id ?? null, evidence_kind: 'worker_reported; Lead must independently verify' })
              }
              failed.push(...result.failed.map(f => ({ ...f, role, subtask: wave[f.subtask_index]?.id ?? null })))
              // A failed artifact never satisfies its dependents; stop further
              // waves and let the Lead rework the failed item instead.
              if (result.failed.length) waveFailed = true
              if (result.failed.some(f => f.control_settlement?.ok !== true || f.details?.physicalCleanupConfirmed === false)) waveFailed = true
              prevWavePhase = wave[0]?.phase ?? prevWavePhase
            }
            // Staged wake loop: a waiting worker's continuation starts when its
            // awaited artifact reaches ready/frozen/done. The linked continuation
            // carries the prior report and keeps the same subtask claim; the
            // budget grant gets a wake-attempt key (same lineage fields).
            while (!waveFailed && pendingWaits.length && !state.abort.signal.aborted && enabled(this.config(), parent.session.id)) {
              const wakeable = pendingWaits.filter(w => {
                const awaited = this.writeScope?.artifactsFor(parent.session.id).find(a => a.id === w.awaiting)
                return awaited && ['ready', 'frozen', 'done'].includes(awaited.state || 'pending')
              })
              if (!wakeable.length) break
              for (const w of wakeable) {
                pendingWaits.splice(pendingWaits.indexOf(w), 1)
                w.attempt += 1
                const artifact = this.writeScope.artifactsFor(parent.session.id).find(a => a.id === w.subtask)
                let wakePrompt = roleText + '\n\n' + context
                  + `\n\n唤醒继续（${w.subtask}）：你等待的产物「${w.awaiting}」已就绪。读取它并完成你的子任务。\n\n你此前的进度（不可信，以实际文件为准）：\n${w.report.slice(0, 8000)}`
                  + scopeClause({ write_scope: artifact?.write_globs || [] })
                if (state.budgetRun) {
                  const allocation = await this.budget.issueTeamWorker(parent, state.budgetRun, { task: wakePrompt, label: 'implementer',
                    subtask: w.subtask, subtaskIndex: staged.artifacts.findIndex(a => a.id === w.subtask), attempt: w.attempt })
                  wakePrompt = allocation.prompt
                }
                const wakeResult = await delegateOnce({ kind: 'derive', subtasks: [{ ...route, title: `DPswarm implementer · ${w.subtask} · wake${w.attempt}`, prompt: wakePrompt }] },
                  { ...exec, signal: timeout.signal }, state.sidecar, this.subagents,
                  { routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget,
                    runId: state.lease.run_id, onDiagnostic: value => state.diagnostics.push(value),
                    modelRegistry: this.modelRegistry, modelRoutes: state.modelRoutes, hostModels: state.hostModels, modelRole: role, onChildStarted: async details => {
                      if (this.writeScope) {
                        await this.writeScope.claim({ rootId: parent.session.id, sessionId: details.execution_session_id,
                          subtask: w.subtask, scopes: artifact?.write_globs || [], runId: state.lease.run_id })
                      }
                      state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id,
                        run_id: state.lease.run_id, subtask: w.subtask, superseded: false })
                      await onChildStarted?.({ ...details, role, run_id: state.lease.run_id })
                    } })
                if (!Array.isArray(wakeResult.deliveries)) { failed.push({ role, code: wakeResult.outcome || 'NOT_ADMITTED', error: wakeResult.message || 'No wake worker was admitted', subtask: w.subtask }); waveFailed = true; break }
                for (const d of wakeResult.deliveries) {
                  const again = /\[DPSWARM_WAITING:\s*([\w.-]+)\]\s*$/m.exec(d.output || '')
                  if (again && w.attempt < 3) pendingWaits.push({ ...w, awaiting: again[1], report: d.output || '', item_id: d.item_id, worker_session_id: d.execution_session_id })
                  else if (again) failed.push({ role, code: 'ARTIFACT_WAIT_TIMEOUT', error: `artifact ${again[1]} still not ready after ${w.attempt} wake attempts`, subtask: w.subtask, item_id: d.item_id })
                  else deliveries.push({ ...d, role, subtask: w.subtask, evidence_kind: 'worker_reported; Lead must independently verify' })
                }
                failed.push(...wakeResult.failed.map(f => ({ ...f, role, subtask: w.subtask })))
                if (wakeResult.failed.length) waveFailed = true
              }
            }
            // Remaining waits with still-unready artifacts are honest failures.
            for (const w of pendingWaits) failed.push({ role, code: 'ARTIFACT_WAIT_TIMEOUT', error: `artifact ${w.awaiting} never became ready; the item stays open for Lead review`, subtask: w.subtask, item_id: w.item_id })
            pendingWaits.length = 0
            continue
          }
          const result = await delegateOnce({ kind: 'derive', subtasks: [{ ...route, title: `DPswarm ${role}`, prompt: assignedPrompt }] },
            { ...exec, signal: timeout.signal }, state.sidecar, this.subagents,
            { routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget,
              runId: state.lease.run_id, onDiagnostic: value => state.diagnostics.push(value),
              modelRegistry: this.modelRegistry, modelRoutes: state.modelRoutes, hostModels: state.hostModels, modelRole: role, onChildStarted: async details => {
                if (role === 'implementer') state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id, run_id: state.lease.run_id, superseded: false })
                if (role === 'tester') state.testers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id, run_id: state.lease.run_id })
                if (role === 'reviewer') state.reviewers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id, run_id: state.lease.run_id })
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
        team_mode: { mode: teamMode, split_form: subtasks ? 'parallel' : staged ? 'staged' : 'serial',
          ...(teamMode !== 'serial' && !subtasks && !staged
            ? { note: `The user set this task to ${teamMode}, but no matching split form was passed, so the sequential team ran. Prefer subtasks (parallel) or the staged board (staged) when the task is divisible.` }
            : {}) },
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
      // Rework adds an implementer plus the tester re-verification; both count
      // against the §7 team-worker cap alongside the still-open original items.
      await this.ensureTeamCapacity(state, parent, 2)
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
      const sourceScope = source.subtask != null ? frozen.subtasks?.find(row => row.id === source.subtask) : null
      // Carry the predecessor's own conclusions into the linked session: final
      // report + provably saved candidate paths. Untrusted evidence, but the
      // continuation no longer restarts blind. (Full session resume needs host
      // fork/seed plumbing; tracked as a later mechanism upgrade.)
      const priorDiagnostic = evidence[0].diagnostic
      const priorReport = priorDiagnostic.closeout?.report?.text || ''
      const priorCandidates = (priorDiagnostic.closeout?.candidates || []).map(c => c?.path).filter(Boolean)
      const priorContext = '\n\nPrior attempt context (same task lineage; untrusted evidence, verify before relying):\n'
        + (priorReport ? priorReport.slice(0, 12000) + (priorReport.length > 12000 ? '\n… [prior report truncated]' : '') : '(the prior attempt produced no report)')
        + (priorCandidates.length ? `\n\nPreviously saved candidate files:\n${priorCandidates.join('\n')}` : '\n\nNo files were provably saved by the prior attempt.')
      const task = `${workerRolePrompt('implementer')}\n\n${allowanceNote} Earlier usage remains separately recorded. Repair the specified defects until the original requirements are met; do not polish beyond the task. Fix only the concrete defects below within the original scope. Preserve unrelated work; do not add requirements or optional validation. Explicit no-tests instructions take precedence. Deliver the current candidate promptly.${sourceScope ? scopeClause(sourceScope) : ''}\n\nOriginal task:\n${frozen.task}\n\nOriginal acceptance:\n${frozen.acceptance || 'Use only the original task requirements.'}\n\nNecessary corrections:\n${args.feedback}${priorContext}`
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
            if (sourceScope && this.writeScope) {
              await this.writeScope.claim({ rootId: parent.session.id, sessionId: details.execution_session_id,
                subtask: sourceScope.id, scopes: sourceScope.write_scope, runId: reworkId })
            }
            state.implementers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id,
              run_id: reworkId, subtask: source.subtask ?? null, superseded: false })
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
      // Same-lineage re-verification: the original tester continues against the
      // reworked candidate with its own rework-chain allocation; its earlier
      // report carries over as untrusted context. Skipped silently when the
      // original run had no tester or the rework delivered nothing.
      let testerAllocation = null
      try {
        const testerSource = state.testers?.size ? [...state.testers.values()].at(-1) : null
        if (deliveries.length && testerSource) {
          const testerRecords = await this.diagnostics(state, testerSource.item_id)
          const testerEvidence = testerRecords.filter(row => row.worker_session_id === testerSource.worker_session_id && row.run_id === testerSource.run_id && row.role === 'tester')
          const testerDiagnostic = testerEvidence.length === 1 ? testerEvidence[0].diagnostic : null
          const testerTerminal = testerDiagnostic?.native_terminal != null
            && testerDiagnostic?.cleanup?.physical_cleanup_confirmed === true && !testerDiagnostic?.audit_error
          if (!testerTerminal) {
            failed.push({ role: 'tester', code: 'REVERIFY_SOURCE_UNAVAILABLE', error: 'The original tester has no trusted terminal record; Lead verifies the reworked candidate directly.' })
          } else {
            const priorTesterReport = testerDiagnostic.closeout?.report?.text || ''
            const reworkReport = deliveries[0]?.output || ''
            const testerRoute = frozen.profile.tester
            const verifyPrompt = `${workerRolePrompt('tester')}\n\nThis is a linked re-verification after implementer rework, continuing the original tester's assignment. Re-verify the CURRENT candidate: the implementer just repaired the defects below. Original constraints (including any no-tests instruction) apply unchanged. Report a verdict with evidence.\n\nOriginal task:\n${frozen.task}\n\nOriginal acceptance:\n${frozen.acceptance || 'Use only the original task requirements.'}\n\nCorrections the implementer was asked to make:\n${args.feedback}\n\nRework delivery report (untrusted; verify against the actual files):\n${reworkReport.slice(0, 12000)}${reworkReport.length > 12000 ? '\n… [truncated]' : ''}\n\nYour earlier pass's final report (untrusted context; the files have changed since):\n${priorTesterReport ? priorTesterReport.slice(0, 12000) : '(no earlier report)'}`
            await check()
            testerAllocation = await this.budget.issueRework(parent, { workerSessionId: testerSource.worker_session_id, task: verifyPrompt })
            const verifyRoutes = [{ role: 'lead', ...frozen.lead_route }, { role: 'tester', provider: testerRoute.provider,
              model: testerRoute.model, ...(testerRoute.reasoning_effort ? { reasoningEffort: testerRoute.reasoning_effort } : {}) }]
            if (this.modelRegistry) await this.modelRegistry.resolve(verifyRoutes, { signal: state.abort.signal })
            const verifyTimeout = new AbortController()
            const verifyAbortChild = () => verifyTimeout.abort(state.abort.signal.reason)
            const verifyTimer = setTimeout(() => verifyTimeout.abort(failure('WORKER_TIMEOUT', 'Tester re-verification exceeded its wall-time limit')), frozen.profile.workerTimeoutSeconds * 1000)
            state.abort.signal.addEventListener('abort', verifyAbortChild, { once: true })
            try {
              const result = await delegateOnce({ kind: 'derive', subtasks: [{ provider: testerRoute.provider, model: testerRoute.model,
                reasoning_effort: testerRoute.reasoning_effort, title: 'DPswarm tester re-verify', prompt: testerAllocation.prompt }] },
              { ...exec, signal: verifyTimeout.signal }, state.sidecar, this.subagents, {
                routeJournal: state.journal, resolveSession: this.resolveSession, budget: this.budget, runId: reworkId,
                onDiagnostic: value => state.diagnostics.push(value), modelRegistry: this.modelRegistry,
                modelRoutes: verifyRoutes, hostModels: undefined, modelRole: 'tester', beforeChildStart: check,
                onChildStarted: async details => {
                  // The continuation becomes the latest tester lineage; the next
                  // rework re-verifies from it.
                  state.testers.set(details.item_id, { item_id: details.item_id, worker_session_id: details.execution_session_id, run_id: reworkId })
                  await record('verification-published', { item_id: details.item_id, worker_session_id: details.execution_session_id })
                } })
              if (!Array.isArray(result.deliveries)) failed.push({ role: 'tester', code: result.outcome || 'NOT_ADMITTED', error: result.message || 'No re-verification worker was admitted' })
              else {
                deliveries.push(...result.deliveries.map(value => ({ ...value, role: 'tester', verification_of: deliveries[0]?.item_id ?? null })))
                failed.push(...result.failed.map(value => ({ ...value, role: 'tester', verification_of: deliveries[0]?.item_id ?? null })))
              }
            } finally {
              clearTimeout(verifyTimer)
              state.abort.signal.removeEventListener('abort', verifyAbortChild)
            }
          }
        }
      } catch (error) {
        failed.push({ role: 'tester', code: error?.code || 'REVERIFY_FAILED', error: String(error?.message ?? error) })
      } finally {
        // Observation-only revoke: an unbound tester allocation is released; a
        // bound one stays consumed on its chain, same rule as the implementer.
        if (testerAllocation?.allocation_id) {
          try { await this.budget.revokeRework(parent, testerAllocation.allocation_id) }
          catch (error) { state.cleanup.budget_error = { code: error?.code || 'REWORK_REVOKE_FAILED', message: String(error?.message || error) } }
        }
      }
      await record('finished', { published, delivery_item_ids: deliveries.map(value => value.item_id),
        failed_item_ids: failed.map(value => value.item_id).filter(Boolean), failure_codes: failed.map(value => value.code) })
      return { mode: 'fixed-implementer-rework-v1', source_item_id: source.item_id, source_worker_session_id: source.worker_session_id,
        worker_budget_policy: allocation.profile, deliveries: deliveries.map(compactWorkerEntry), failed: failed.map(compactWorkerEntry),
        stopped: state.abort.signal.aborted || !enabled(this.config(), parent.session.id), cleanup: state.cleanup, rework_recovery: recovery,
        next: 'Inspect the necessary corrections within the original permitted scope. The source item was terminated by this rework dispatch; review only items in this delivery. Accept only an implementer item in deliveries. When a tester re-verification item is present, treat its report as advisory evidence for your review. A failed or partial candidate is not an accepted worker delivery; use the latest implementer item for any further necessary rework.' }
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

  /** Worker-side staged tool: advance the artifact owned by the caller's claim. */
  async artifactState(args, exec) {
    const session = exec?.agent?.session
    if (!session || !this.writeScope) throw failure('ARTIFACT_UNAVAILABLE', 'The artifact board is unavailable')
    const claim = this.writeScope.forSession(session.id)
    if (!claim || session.header?.parentSession !== claim.root_session_id) {
      throw failure('ARTIFACT_NOT_CLAIMED', 'Only a staged worker advancing its own claimed artifact may call this')
    }
    const artifact = this.writeScope.artifactsFor(claim.root_session_id).find(a => a.id === claim.subtask)
    if (!artifact) throw failure('ARTIFACT_UNKNOWN', `No artifact board entry for ${claim.subtask}`)
    const to = args?.to
    if (typeof to !== 'string' || !to.trim()) throw failure('ARTIFACT_STATE_REQUIRED', 'to is required (target state)')
    const note = typeof args?.note === 'string' ? args.note.slice(0, 2000) : undefined
    const state = this.sessions.get(claim.root_session_id)
    if (!state) throw failure('ARTIFACT_RUN_ENDED', 'The owning run state is gone')
    // The control plane is the authoritative transition validator; the mirror
    // only follows accepted changes.
    const result = await state.sidecar.call('POST', '/api/artifact/state', { artifact_id: artifact.id, to, ...(note ? { note } : {}) })
    artifact.state = to
    if (Number.isSafeInteger(artifact.version)) artifact.version += 1
    return result
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
    const item = prior.snapshot?.work_items?.[args.item_id]
    const existingAcceptance = item?.acceptance
    if (existingAcceptance === terminal) {
      state.recoveryReviewed = true
      await this.reconcile(state)
      return { ok: true, outcome: `already_${terminal}`, note: 'Existing decision retained; no duplicate acceptance event',
        worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state, args.item_id)) }
    }
    // Terminal the other way (e.g. superseded by a rework dispatch): answer
    // idempotently instead of letting the sidecar reject the transition. A
    // failed worker has no delivery package, so an accept on it still falls
    // through to the no-package guard — a false acceptance stays impossible.
    if (['accepted', 'terminated'].includes(existingAcceptance)
      && (args.verdict === 'terminate' || item?.submission_package_id)) {
      return { ok: true, outcome: `already_${existingAcceptance}`, note: `Item is already ${existingAcceptance}; no transition attempted. Review the latest implementer item instead.`,
        worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state, args.item_id)) }
    }
    if (args.verdict === 'accept' && item && item.acceptance !== 'submitted' && !item.submission_package_id) {
      throw failure('DELIVERY_PACKAGE_REQUIRED', 'This worker has no submitted delivery package. Failed or partial files require independent Lead verification; they cannot be accepted as this worker delivery. Use dpswarm_rework for an eligible implementer. If unavailable, report the exact blocker and preserve the candidate.')
    }
    // Role separation: with a configured independent reviewer, verification
    // judgment belongs to it. The Lead cannot accept a production delivery while
    // the reviewer verdict is pending; review the reviewer item first, or
    // terminate it with a documented takeover reason and verify yourself.
    if (args.verdict === 'accept' && state.fixedTask?.profile?.reviewer?.mode === 'model'
        && state.implementers?.has(args.item_id)) {
      const openReviewer = [...(state.reviewers?.values() || [])].find(r => {
        const acceptance = prior.snapshot?.work_items?.[r.item_id]?.acceptance
        return acceptance && !['accepted', 'terminated'].includes(acceptance)
      })
      if (openReviewer) throw failure('REVIEWER_PENDING', 'The configured reviewer has not submitted or settled its verdict. Review the reviewer item first (accept as evidence, or terminate with your takeover reason), then decide the production delivery.')
    }
    const result = await state.sidecar.call('POST', '/api/review', { item_id: args.item_id,
      verdict: args.verdict, reason: args.verdict === 'terminate' ? 'manual-stopped' : undefined, review_note: args.reason || '' })
    state.recoveryReviewed = true
    await this.reconcile(state)
    return { ...result, worker_diagnostics: compactDiagnosticRecords(await this.diagnostics(state, args.item_id)) }
  }
}
