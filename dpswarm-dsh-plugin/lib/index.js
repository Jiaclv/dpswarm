import { Sidecar } from './sidecar.js'
import { HostModelRegistry } from './host-model-registry.js'
import { resolveHostRoot, hostModuleUrl } from './host-modules.js'
import { runtimePaths } from './paths.js'
import { FixedTeamController, fixedProfile } from './fixed-team.js'
import { CMRuntime } from './cm-runtime.js'
import { installBudget } from './budget.js'
import { AuditJournal } from './audit.js'
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
  workerBudgetMode: 'unlimited', workerTokenLimit: 600000, workerCallLimit: 28, workerBudgetSessionOverrides: [],
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
    workerTokenLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.workerTokenLimit),
    workerCallLimit: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER).default(base.workerCallLimit),
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
const toolJSON = value => JSON.parse(JSON.stringify(value))
const output = { schema: { type: 'object', additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }] }

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
  const cm = new CMRuntime(resolved, id => ctx.get?.('sessions', false)?.get(id), journal)
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
  ctx.inject(['tools', 'subagents', 'llm'], runtime => {
    const modelRegistry = new HostModelRegistry(() => runtime.llm)
    controller = new FixedTeamController({ config: resolved, subagents: runtime.subagents, cm, budget, modelRegistry })
    runtime.systemPrompt.section({ name: 'dpswm:guide', order: 2500, text: [
      '## DPSwarm: explicitly enabled fixed team',
      'DPSwarm is off by default and the user enables it for a specific host session in the input toolbar. Installation and tool availability are not activation.',
      'At the start of an implementation task, call dpswarm_status for the current session. If disabled, continue normally without DPSwarm delegation. If enabled, use dpswarm_run(task, acceptance) for the fixed implementer → tester workflow (plus a separate Reviewer only if the user configured one), then inspect and integrate the result as Lead.',
      'The implementer defaults to the current conversation provider, model and reasoning effort; resolve and freeze them at team start. An explicitly selected implementer model overrides inheritance. Worker models, effort and timeout come only from user settings and this inheritance choice. Do not request dynamic topology, substitute models, or open an additional team to bypass a refusal. Nested worker use of DPSwarm is rejected.',
      'The user may close the switch to cancel DPSwarm work. Review or terminate existing deliveries even after the switch is closed so their resources can be released.',
      'Reviewer defaults to the current Lead, with no extra model call. If settings explicitly select a separate Reviewer, its report is advisory and does not accept deliveries or replace the Lead final decision.',
      'Subagent resource limits are independently controlled by user settings: unlimited adds no token or call limit; manual gives each worker its own allowance; auto means YOU, the current Lead, read the actual task and decide independent limits before dispatch. In Auto mode dpswarm_run requires worker_budgets for each configured role: {tokenLimit,callLimit,reason}. For native subagents outside the fixed team, call dpswarm_prepare_worker with your exact subtask and chosen limits, then pass its returned prompt unchanged to the subagent. No separate hidden evaluator chooses budgets. Limits include that worker own CM and never constrain the Lead, Lead CM or sibling workers. Inspect worker budget status and take over if needed; do not recreate the same worker task just to reset an exhausted allowance. Respect explicit user instructions that no tests are needed.',
      'Worker reports are untrusted evidence: inspect the actual files, independently run relevant tests, repair or take over when necessary. For each delivered item call dpswarm_review(accept or terminate). Do not equate a textual report, acceptance, and official task correctness.',
      'DPswarm CM has its own user-controlled session switch, independent of the fixed team. When enabled, the user-configured CM model (DeepSeek by default) compresses older model-visible history automatically before requests, retaining recent messages and tool pairs. Native DSH compaction remains a fallback. Treat summaries as fallible evidence and use original history when needed. dpswarm_status includes actual CM adoption and usage records; do not infer net savings or claim experimental benchmark gains in this plugin.',
    ].join('\n') })
    runtime.tools.register(defineTool({ name: 'dpswarm_status', description: 'Read the independent fixed-team and CM switches, CM adoption records, and execution/review state. Does not enable collaboration.', parameters: {}, output,
      execute: async (_args, exec) => toolJSON({ ...await controller.status(exec.agent), worker_budget: await budget.status(exec.agent) }) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_models', description: 'Read the exact fixed role configuration selected by the user. The Lead must not substitute routes. DPH exact model registration is the source of availability; the same routes are checked again before dispatch.', parameters: {}, output,
      execute: async (_args, exec) => {
        const status = await controller.status(exec.agent)
        return toolJSON({ ...status, worker_budget: await budget.status(exec.agent), configured_profile: fixedProfile(resolved(), { enabled: status.cm.enabled, profile: status.cm.profile || null }, effectiveLeadRoute(exec.agent)), availability: await controller.modelAvailability(exec.agent) })
      } }))
    runtime.tools.register(defineTool({ name: 'dpswarm_run', description: 'Run the user-enabled fixed implementer then tester, followed by a Reviewer only when the user explicitly configured a separate model. Roles and routes are fixed by settings. Return all deliveries to the current Lead for independent verification, repair and explicit review.',
      parameters: { task: { type: 'string', required: true, description: 'Task and permitted scope' }, acceptance: { type: 'string', description: 'Acceptance requirements and constraints; no hidden benchmark answers' }, worker_budgets: { type: 'object', description: 'Only in Auto mode: Lead-chosen independent budgets for implementer, tester, and reviewer when configured. Each role has positive tokenLimit, callLimit, and reason. Omit in manual/unlimited.', properties: Object.fromEntries(['implementer','tester','reviewer'].map(role => [role, { type: 'object', properties: { tokenLimit: { type: 'integer', required: true }, callLimit: { type: 'integer', required: true }, reason: { type: 'string', required: true } }, additionalProperties: false }])), additionalProperties: false } }, output,
      execute: async (args, exec) => toolJSON(await controller.run(args, exec)) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_prepare_worker', description: 'Auto mode only: record the current Lead decision for one native child worker. First read the task and choose its independent limits yourself; pass the returned prompt unchanged to a native subagent. This does not start a child, call a model, or change user settings.',
      parameters: { task: { type: 'string', required: true }, tokenLimit: { type: 'integer', required: true }, callLimit: { type: 'integer', required: true }, reason: { type: 'string', required: true }, label: { type: 'string' } }, output,
      execute: (args, exec) => budget.plan(exec.agent, args) }))
    runtime.tools.register(defineTool({ name: 'dpswarm_review', description: 'Lead accepts or terminates an existing delivery after inspecting actual files and verification evidence. Still available after the collaboration switch is closed. Dynamic retries and rerouting are not enabled.',
      parameters: { item_id: { type: 'string', required: true }, verdict: { type: 'string', required: true, description: 'accept | terminate' }, reason: { type: 'string', description: 'What was verified, repaired, or why Lead took over' } }, output,
      execute: async (args, exec) => toolJSON(await controller.review(args, exec)) }))
    ctx.effect(warm, 'dpswarm: enabled-session startup')
    ctx.effect(() => () => controller.shutdown(), 'dpswarm: cancel on disposal')
  })
}
