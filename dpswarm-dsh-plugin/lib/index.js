import { Sidecar } from './sidecar.js'
import { readEvidence } from './evidence-reader.js'
import { modelToolView, acceptancePage } from './tool-view.js'
import { usageLedger, leadUsageMessages } from './usage-ledger.js'
import { leadGuide } from './role-guidance.js'
import { installBudgetAdvice } from './budget-advice.js'
import { HostModelRegistry } from './host-model-registry.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'
import { runtimePaths } from './paths.js'
import { FixedTeamController, fixedProfile } from './fixed-team.js'
import { WriteScopeRegistry, installWriteScope } from './write-scope.js'
import { KvMailboxStorage } from './mailbox.js'
import { resolveHostSession, probeHostRuntime, combineRuntimeCompatibility } from './host-services.js'
import { compactBudgetStatus } from './worker-diagnostics.js'
import { CMRuntime } from './cm-runtime.js'
import { installBudget } from './budget.js'
import { AuditJournal } from './audit.js'
import { installTeamRequirement } from './team-required.js'
import { TeamDispatcher } from './team-dispatch.js'
import { effectiveLeadRoute, installChildRoutes } from './lead-route.js'
export { requireRootCaller } from './delegation.js'

const host = resolveHostRoot()
const [toolsMod, settingsMod, zMod] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-tools/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-settings/lib/index.js')),
  import(hostModuleUrl(host, 'schemastery/lib/index.cjs')),
])
const { defineTool } = toolsMod
const z = zMod.default ?? zMod
export const name = 'dpswarm-dsh-plugin'
export const inject = ['tools', 'subagents', 'settings', 'systemPrompt', 'llm']
export const DPSWARM_NS = 'dpswarm'

export const defaults = Object.freeze({ sidecarUrl: 'http://127.0.0.1:8791', autoStart: true,
  dpswarmDir: '', workspace: '', pythonCmd: '', subagentProvider: 'spawn', enabledSessions: [],
  implMode: '', implProvider: '', implModel: '', implEffort: '',
  testProvider: '', testModel: 'glm-5.3-flash', testEffort: '',
  reviewerMode: 'lead', reviewerProvider: '', reviewerModel: '', reviewerEffort: '',
  workerBudgetMode: 'unlimited', workerTokenLimit: 1200000, workerCallLimit: 50, workerBudgetSessionOverrides: [],
  reviewerIndependence: 'guided',
  reworkBudgetMode: 'fixed', reworkTokenLimit: 600000, reworkCallLimit: 28,
  teamModeOverrides: [],
  workerTimeoutSeconds: 600, cmEnabledSessions: [], cmProvider: 'deepseek', cmModel: 'deepseek-v4-flash', cmEffort: 'off' })

function configSchema(base) {
  return z.object({ sidecarUrl: z.string().default(base.sidecarUrl), autoStart: z.boolean().default(base.autoStart),
    dpswarmDir: z.string().default(base.dpswarmDir), workspace: z.string().default(base.workspace),
    pythonCmd: z.string().default(base.pythonCmd), subagentProvider: z.string().default(base.subagentProvider),
    enabledSessions: z.array(z.string()).default(base.enabledSessions),
    implMode: z.string().default(base.implMode),
    implProvider: z.string().default(base.implProvider), implModel: z.string().default(base.implModel), implEffort: z.string().default(base.implEffort),
    testProvider: z.string().default(base.testProvider), testModel: z.string().default(base.testModel), testEffort: z.string().default(base.testEffort),
    reviewerMode: z.string().default(base.reviewerMode), reviewerProvider: z.string().default(base.reviewerProvider),
    reviewerModel: z.string().default(base.reviewerModel), reviewerEffort: z.string().default(base.reviewerEffort),
    workerBudgetMode: z.union(['unlimited', 'manual', 'auto']).default(base.workerBudgetMode),
    reviewerIndependence: z.union(['guided', 'independent']).default(base.reviewerIndependence),
    workerTokenLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.workerTokenLimit),
    workerCallLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.workerCallLimit),
    reworkBudgetMode: z.union(['unlimited', 'fixed']).default(base.reworkBudgetMode),
    reworkTokenLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.reworkTokenLimit),
    reworkCallLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.reworkCallLimit),
    teamModeOverrides: z.array(z.object({
      sessionId: z.string().required(), mode: z.union(['serial', 'parallel', 'staged']).required(),
    })).default(base.teamModeOverrides),
    workerBudgetSessionOverrides: z.array(z.object({
      sessionId: z.string().required(), mode: z.union(['unlimited', 'manual', 'auto']).required(),
      tokenLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER),
      callLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER),
    })).default(base.workerBudgetSessionOverrides),
    workerTimeoutSeconds: z.number().default(base.workerTimeoutSeconds),
    cmEnabledSessions: z.array(z.string()).default(base.cmEnabledSessions), cmProvider: z.string().default(base.cmProvider),
    cmModel: z.string().default(base.cmModel), cmEffort: z.string().default(base.cmEffort) })
}
export const Config = configSchema(defaults)
const requirementListSchema = { type: 'array', description: 'Derived acceptance requirements faithful to the trusted user request; unique id (user-task is reserved), description, optional mandatory (defaults true). A Lead plan cannot create user restrictions.', items: { type: 'object', properties: { id: { type: 'string', required: true }, description: { type: 'string', required: true }, mandatory: { type: 'boolean' } }, additionalProperties: false } }
const candidatePathsSchema = { type: 'array', items: { type: 'string' }, description: 'Explicit delivery entry paths inside the authorized workspace. Runtime seals these and bounded local dependencies; this is separate from changed paths and write scopes.' }
const toolJSON = value => modelToolView(value)
const output = { schema: { type: 'object', additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] }

export function apply(ctx, config) {
  const entry = { ...defaults, ...config }
  let source = () => entry
  let controller
  const resolved = () => ({ ...entry, ...source() })
  const journal = new AuditJournal({ config: resolved })
  const disposeChildRoutes = installChildRoutes(ctx, { journal })
  ctx.effect(() => disposeChildRoutes, 'dpswarm: effective child request routes')
  const budget = installBudget(ctx, resolved, { journal })
  ctx.provide('dpswarmBudget', budget)
  ctx.effect(() => () => budget.shutdown(), 'dpswarm: dispose worker budget hooks')
  const cm = new CMRuntime(resolved, id => resolveHostSession(ctx, id), journal)
  ctx.provide('dpswarmCM', cm)
  ctx.effect(() => () => cm.shutdown(), 'dpswarm: cancel CM on disposal')
  const warm = () => {
    void cm.settingsChanged().catch(error => ctx.logger?.warn?.('dpswarm: CM settings audit update failed: ' + error.message))
    controller?.settingsChanged()
    const cfg = resolved()
    if (!cfg.enabledSessions?.length || !cfg.autoStart) return
    const sidecar = new Sidecar({ ...cfg, ...runtimePaths(cfg), sessionIsolation: true })
    void sidecar.ensure().catch(error => ctx.logger?.warn?.('dpswarm: CM settings audit update failed: ' + error.message))
  }
  const hooks = { setSource: value => { source = value }, onChange: warm }
  if (process.env.DPSWARM_SKIP_SETTINGS !== '1') {
    // Registration failure must be visible; a default-off entry cannot authorize runs.
    if (typeof settingsMod.installSettingsSection === 'function') {
      settingsMod.installSettingsSection(ctx, DPSWARM_NS, configSchema(entry), entry, hooks)
    } else {
      ctx.inject(['settings'], settingsCtx => settingsCtx.settings.installSection(ctx, DPSWARM_NS, configSchema(entry), entry, hooks))
    }
  }
  if (process.env.DPSWARM_SKIP_TOOLS === '1') return
  const requirement = installTeamRequirement(ctx, { config: resolved, journal,
    readCompletion: (parent, binding) => controller?.completionStatus(parent, binding),
    infrastructureStateDirectory: () => runtimePaths(resolved()).workspace + '/runtime-blocks',
    checkRuntime: async parent => {
      const hostRuntime = probeHostRuntime(ctx)
      const cfg = { ...resolved(), ...runtimePaths(resolved()), sessionId: parent.session.id, sessionIsolation: true }
      return combineRuntimeCompatibility(hostRuntime, await new Sidecar(cfg).requireRuntimeCapabilities())
    } })
  ctx.provide('dpswarmRequirement', requirement)
  const writeScope = new WriteScopeRegistry({ journal })
  ctx.provide('dpswarmWriteScope', writeScope)
  ctx.effect(() => installWriteScope(ctx, writeScope), 'dpswarm: enforce parallel worker write scopes')
  ctx.inject(['tools', 'subagents', 'llm'], runtime => {
    const modelRegistry = new HostModelRegistry(() => runtime.llm)
    const mailboxStorage = new KvMailboxStorage(ctx)
    controller = new FixedTeamController({ config: resolved, subagents: runtime.subagents, cm, budget, modelRegistry, writeScope, mailboxStorage, journal,
      resolveSession: id => resolveHostSession(ctx, id) })
    const dispatcher = new TeamDispatcher({ controller, requirement })
    const advice = installBudgetAdvice(runtime, resolved, { modelRegistry })
    ctx.effect(() => () => advice.dispose(), 'dpswarm: dispose advisory envelope reference')
    runtime.systemPrompt.section({ name: 'dpswm:guide', order: 2500,
      text: context => leadGuide(resolved(), context.agent) })
    runtime.systemPrompt.section({ name: 'dpswm:model-capabilities', order: 2501,
      text: context => modelRegistry.capabilityGuideForAgent(context.agent) })
    runtime.tools.register(defineTool({ name: 'dpswarm_status', description: 'Read the independent fixed-team and CM switches, CM adoption records, and execution/review state. Does not enable collaboration.', parameters: {}, output,
      execute: async (_args, exec) => toolJSON({ ...await controller.status(exec.agent), worker_budget: compactBudgetStatus(await budget.status(exec.agent)), budget_planning: await advice.status(exec.agent), team_requirement: await requirement.status(exec.agent) }) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_models', description: 'Read the exact fixed role configuration selected by the user. The Lead must not substitute routes. DPH exact model registration is the source of availability; the same routes are checked again before dispatch.', parameters: {}, output,
      execute: async (_args, exec) => {
        const status = await controller.status(exec.agent)
        return toolJSON({ ...status, worker_budget: compactBudgetStatus(await budget.status(exec.agent)), budget_planning: await advice.status(exec.agent), team_requirement: await requirement.status(exec.agent), configured_profile: fixedProfile(resolved(), { enabled: status.cm.enabled, profile: status.cm.profile || null }, effectiveLeadRoute(exec.agent)), availability: await controller.modelAvailability(exec.agent) })
      } }))
    runtime.tools.register(defineTool({ name: 'dpswarm_run', description: 'Run the user-enabled fixed implementer then tester, followed by a Reviewer only when the user explicitly configured a separate model. Roles and routes are fixed by settings. The user picks the allowed split form per task in the compass popover (see dpswarm_status team_mode): serial refuses any split, parallel allows subtasks, staged allows the staged board. Return deliveries to the Lead for scoped review, implementer rework when necessary, and explicit acceptance.',
      parameters: { task: { type: 'string', required: true, description: 'Lead implementation plan and edit scope; the runtime separately binds the original trusted user request' }, acceptance: { type: 'string', description: 'Full delivery acceptance scope, including related known findings beyond this round edit scope; no hidden benchmark answers' }, requirements: requirementListSchema, candidate_paths: candidatePathsSchema, output_kind: { type: 'string', enum: ['files', 'text'], description: 'Delivery kind, defaults files. Text applies only to actual text deliverables and cannot substitute for a file candidate.' }, subtasks: { type: 'array', description: 'Parallel implementer split: 1-3 disjoint subtasks, each with a unique id, a task, and a nonempty write_scope glob list (write/edit calls outside a worker scope are rejected). Requires user team mode parallel (see dpswarm_status team_mode; a cross-form call is refused with USER_TEAM_MODE_*). Omit for the sequential team.', items: { type: 'object', properties: { id: { type: 'string', required: true }, task: { type: 'string', required: true }, write_scope: { type: 'array', required: true }, acceptance: { type: 'string' } }, additionalProperties: false } }, staged: { type: 'object', description: 'Staged artifact board (requires user team mode staged; mutually exclusive with subtasks): 1-4 phases plus 1-8 artifacts, each artifact with a unique id, title, task, a nonempty write_globs glob list, a valid phase, and optional deps on same-or-earlier-phase artifacts (the graph must be acyclic). A worker that needs a not-ready artifact suspends and is woken when it turns ready. Omit for the sequential team.', properties: { phases: { type: 'array', required: true, items: { type: 'object', properties: { id: { type: 'string', required: true }, task: { type: 'string', required: true } }, additionalProperties: false } }, artifacts: { type: 'array', required: true, items: { type: 'object', properties: { id: { type: 'string', required: true }, title: { type: 'string', required: true }, task: { type: 'string', required: true }, write_globs: { type: 'array', required: true }, phase: { type: 'string', required: true }, deps: { type: 'array' } }, additionalProperties: false } } }, additionalProperties: false } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.run(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_continue_task', description: 'Explicitly associate the latest trusted user message with the same existing task, without changing its contract, running workers, budgets or acceptance. Use only after judging that the user is continuing that task; read team_requirement.continuity for current and previous binding IDs. New requirements use amend_task; unrelated work needs its own run.',
      parameters: { current_binding_id: { type: 'string', required: true }, previous_binding_id: { type: 'string', required: true }, reason: { type: 'string', required: true } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.continueTask(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_verify_rework', description: 'After rework deferred verification because candidate inputs did not change, or when the original verifiers never started at all (for example the first implementer died before any tester existed), explicitly choose to run the never-issued tester/reviewer checks. The paused path continues the rework allowance; the never-issued path resumes the still-unissued original allowances on the frozen routes. No implementer rerun, extra grant or acceptance. Requires the current item, unchanged candidate and original task/configuration.',
      parameters: { item_id: { type: 'string', required: true }, reason: { type: 'string', required: true } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.verifyRework(args, exec)) }))
    runtime.inject(['fs'], filesystem => {
      runtime.tools.register(defineTool({ name: 'dpswarm_read_evidence', description: 'Read JSON evidence through the host filesystem permissions, decoding UTF-8 or BOM-marked UTF-16. Returns bounded values and JSON-pointer pages plus a byte digest. Use for measurement readback instead of guessing encoding or treating file existence as successful verification. Available to workers and Lead; never modifies files or grants acceptance.',
        parameters: { file_path: { type: 'string', required: true }, pointer: { type: 'string', description: 'RFC 6901 JSON pointer, empty for root. Escape / as ~1 and ~0 in keys.' }, offset: { type: 'integer' }, limit: { type: 'integer', description: '1–40 entries, default 20.' }, expected_sha256: { type: 'string', description: 'Digest from the previous page, prevents mixing changed files.' }, encoding: { type: 'string', enum: ['auto','utf-8','utf-16le','utf-16be'] } }, output, isConcurrencySafe: () => true,
        execute: async (args, exec) => toolJSON(await readEvidence(filesystem, args, exec)) }))
    })
    runtime.tools.register(defineTool({ name: 'dpswarm_resume', description: 'Resume an explicit candidate-capture failure checkpoint after examining the saved implementation. Reuses the submitted implementation and only the original never-issued tester/reviewer allowances; no implementer rerun, budget increase, acceptance or reopening terminated items. Read verification_recovery from dpswarm_status. Configuration, task, source and lease must still match.',
      parameters: { checkpoint_id: { type: 'string', required: true }, reason: { type: 'string', required: true, description: 'Why the existing implementation should proceed to verification; name remaining uncertainty.' } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.resume(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_rework', description: 'Return concrete necessary corrections to the current task fixed implementer, using its original model. The rework allowance comes from the rework budget settings (fixed 600,000 tokens / 28 calls by default; saved settings take precedence) and never touches initial worker limits; all actual usage stays recorded. Only the latest eligible implementer item can be reworked; failure or a missing delivery package is not acceptance.',
      parameters: { item_id: { type: 'string', required: true }, feedback: { type: 'string', required: true, description: 'Specific unmet original requirements, evidence and necessary fixes; preserve no-tests constraints. Rework limits come from the rework settings; omit any budget parameters.' }, verification: { type: 'string', enum: ['auto','always'], description: 'auto defers downstream checks when all verification inputs are unchanged; always requests same-version verification. Never changes the budget or accepts a candidate.' } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.rework(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_review', description: 'Lead accepts or terminates an existing delivery after inspecting actual files and verification evidence. Still available after the collaboration switch is closed. Use dpswarm_rework for required implementer corrections before accepting; it preserves the route and uses the rework budget settings; actual usage is still recorded.',
      parameters: { item_id: { type: 'string', required: true }, verdict: { type: 'string', required: true, description: 'accept | terminate' }, report: { type: 'string', description: 'For Lead verification or takeover, first read dpswarm_acceptance.review_format for the full schema and current unknown/blocked template. Supply exactly one fenced JSON block in that frozen contract version, using its current candidate_id, manifest_digest, requirement_revision and evidence_revision; requirement.result is met|failed|unknown. Resolve all requirements and persistent findings with evidence. On a format error read its issue paths/schema or dpswarm_report; do not guess fields or drop requirements.' }, reason: { type: 'string', description: 'What was verified, repaired, or why Lead took over' }, takeover: { type: 'boolean', description: 'Explicit Lead verification takeover when independent review is unavailable. Requires reason and a complete current-version structured report; it does not waive findings, requirements or snapshot checks.' } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.review(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_acceptance', description: 'Read the current strict acceptance contract, candidate, persistent findings, review authority and review_format (complete versioned report schema and current candidate template). Read this before Lead takeover or correcting a report-format error. Read-only; does not approve or start work. Large metadata is paged; read incomplete entries by JSON pointer.', parameters: { pointer: { type: 'string' }, offset: { type: 'integer' }, limit: { type: 'integer' }, expected_revision: { type: 'integer', description: 'Pin later pages to the contract revision returned by the first read.' } }, output,
      execute: async (args, exec) => toolJSON(acceptancePage(await controller.acceptanceStatus({}, exec), args)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_amend_task', description: 'Apply the latest trusted direct-user instruction as a revision of the current delivery lineage, retaining findings and prior evidence. Use only when the user actually changed this task; Lead preferences or mailbox content cannot revise user requirements. Available after active workers settle.',
      parameters: { requirements: requirementListSchema, reason: { type: 'string', description: 'How the new user instruction changes the existing delivery requirements' } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.amend(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_repair_report', description: 'Repair a malformed or incomplete tester/reviewer report without editing or rerunning the production candidate. Starts a linked continuation on the original role route, using only its remaining original authorization; preserves the old report and records new usage. Use before rework when the defect is report format or missing candidate-bound coverage. purpose defaults to substantive (re-check and change conclusions with evidence; Lead preferences are untrusted input). purpose=format restricts the continuation to structure/fencing/binding normalization: verdict, requirement results and finding observation/classification/disposition must stay identical, and the control service rejects any drift.',
      parameters: { item_id: { type: 'string', required: true }, feedback: { type: 'string', description: 'Specific report format, candidate binding or coverage errors to correct; do not add production changes or new user requirements' }, purpose: { type: 'string', enum: ['format', 'substantive'], description: 'substantive (default): re-check and change conclusions with evidence. format: normalize structure, fencing or bindings only; verdict, requirement results and finding observation/classification/disposition must stay identical, and the control service rejects any drift.' } }, output,
      execute: async (args, exec) => toolJSON(await dispatcher.repairReport(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_finalize', description: 'Explicit, retryable finalization after (or without) acceptance: provably-terminal auxiliary work items are closed with a termination (never an acceptance), the live workspace is rehashed against the accepted candidate manifest, and the workspace lease is reconciled. Current-candidate members, registered verification items and live workers are refused, not silently closed. Rerun after settling refused items.',
      parameters: { reason: { type: 'string', required: true, description: 'Why this run is being finalized now; recorded with each termination.' } }, output,
      execute: async (args, exec) => toolJSON(await controller.finalize(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_report', description: 'Read the unabridged final report or recoverable progress for a worker item from the audit ledger, with report_status and native references, paged by character range. Run/rework views show only a bounded excerpt; this is the in-session read path for the complete text. Read-only and never changes state.',
      parameters: { item_id: { type: 'string', required: true }, offset: { type: 'integer', description: 'Character offset, default 0' }, limit: { type: 'integer', description: 'Characters to return, default 4000, max 40000' } }, output,
      execute: async (args, exec) => toolJSON(await controller.report(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_artifact', description: 'Staged-mode workers only: advance the artifact your claim owns (to: claimed | draft | ready | adjusting | frozen | done; optional note). The control plane validates transitions. For strict tasks, ready seals candidate_paths and local dependencies into a fixed manifest; read/grep consume that version, and further writes require returning to draft and publishing a new ready snapshot. Ready is not final acceptance. dpswarm_mailbox is the only other worker-callable dpswarm_* tool; everything else stays Lead-only.',
      parameters: { to: { type: 'string', required: true }, note: { type: 'string' }, candidate_paths: candidatePathsSchema }, output,
      execute: async (args, exec) => toolJSON(await controller.artifactState(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_mailbox', description: 'Bounded persistent Lead↔worker mailbox for the current fixed-team run. Post kind fact (a quiet context update), clarify (a question that wakes the worker with its next continuation) or block (a blocker notice) to a registered member, or read pending mail. Every kind other than fact|clarify|block is rejected: contract changes, permission expansion and delivery approval never travel here — use dpswarm_run / dpswarm_rework / dpswarm_review. Workers may call it for their own mail (they address only the lead). Pending mail survives restart; per-member queue is capped (64) and messages are capped (64 KiB).',
      parameters: { action: { type: 'string', required: true, description: 'post | read' }, to: { type: 'string', description: 'Target member: a worker subtask/artifact id, a role name (implementer | tester | reviewer), or lead (workers only)' }, kind: { type: 'string', description: 'fact | clarify | block' }, content: { type: 'string', description: 'Message text (nonempty)' }, refs: { type: 'array', items: { type: 'string' }, description: 'Up to 16 short references (artifact ids, item ids, paths)' }, message_id: { type: 'string', description: 'Caller-chosen unique id; reposting the same id deduplicates instead of queueing twice' } }, output,
      execute: async (args, exec) => toolJSON(await controller.mailbox(args, exec)) }))
    ctx.effect(warm, 'dpswarm: enabled-session startup')
    ctx.effect(() => () => controller.shutdown(), 'dpswarm: cancel on disposal')
  })
}
