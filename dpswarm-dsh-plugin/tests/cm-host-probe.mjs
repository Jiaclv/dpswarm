/** Extra plugin for the isolated CM pressure launcher. It uses real DSH session,
 * prompt assembly and AgentLoop paths, but registers a deterministic local adapter. */
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const [{ LlmAdapter, createUserMessage }, { Session }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-session/lib/index.js')),
])
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmCM', 'settings', 'sessions']
const FIXTURE_PROVIDER = 'dpswarm-cm-fixture'
const WINDOWS = Object.freeze({ 'fixture-main-wide': 1_000_000, 'fixture-main-narrow': 70_000, 'deepseek-v4-flash': 1_000_000 })
const userMessage = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
function seedWithHistory(tokenTarget) {
  const seed = Session.create('cm-window-seed'); seed.append('turn/start', { turn: 1 })
  // DSH's deterministic surface meter estimates ordinary text at roughly four
  // characters per token.  Keep ten 5K-token turns: after keeping four, one
  // proactive summary leaves room below the narrow 60K-window threshold.
  const charsPerMessage = Math.ceil((tokenTarget * 4) / 10)
  for (let index = 0; index < 10; index++) seed.append('user/message', userMessage(`pressure-history-${index} ` + 'x'.repeat(charsPerMessage)), { surfaceOp: 'append' })
  seed.append('turn/end', { turn: 1, reason: 'completed' }); return seed.events
}
function nativeService(ctx, agent) { return ctx.agentPresets.serviceFor(agent, 'compaction')?.constructor?.name || null }
export function apply(ctx) {
  const output = process.env.DPSWARM_CM_PROBE_OUTPUT, sidecarUrl = process.env.DPSWARM_CM_PROBE_SIDECAR_URL, source = process.env.DPSWARM_CM_PROBE_SOURCE
  if (!output || !sidecarUrl || !source) throw new Error('CM probe requires isolated output, sidecar and source paths')
  const requests = []
  class FixtureAdapter extends LlmAdapter {
    async listModels(provider) { return Promise.all([...Object.keys(WINDOWS), 'fixture-main-unknown'].map(id => this.resolveModel(provider, id))) }
    async resolveModel(provider, id) {
      const contextWindow = WINDOWS[id]
      return { provider, id, name: id, ...(contextWindow === undefined ? {} : { context: { contextWindow } }), reasoning: { efforts: [{ id: 'off', name: 'Off' }, { id: 'max', name: 'Max' }] } }
    }
    async *stream(options) {
      assert.equal(options.provider, FIXTURE_PROVIDER)
      requests.push({ session_id: options.sessionId, purpose: options.purpose || 'main', model: options.model, system_present: typeof options.system === 'string' && options.system.length > 0, tool_count: Array.isArray(options.tools) ? options.tools.length : null, message_count: Array.isArray(options.messages) ? options.messages.length : null, reasoning_effort: options.reasoningEffort ?? null })
      const text = options.purpose === 'compaction' ? 'Goal: preserve pressure-history invariants. References: probe-K42. Unresolved: no provider test.' : 'Fixture main response; no tools were called.'
      yield { type: 'block-end', index: 0, block: { type: 'text', text } }; yield { type: 'usage', usage: { inputTokens: 16000, cacheReadTokens: 100, outputTokens: 30 } }; yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([FIXTURE_PROVIDER], new FixtureAdapter())
  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, error: error.stack || String(error) }, null, 2))) }, 900)
    async function run() {
      const previous = ctx.settings.get('dpswarm'), handles = [], root = `cm-pressure-root-${randomUUID()}`
      const low = `${root}-low`, near = `${root}-near`, off = `${root}-off`, unknown = `${root}-unknown`, child = `${root}-child`, results = []
      try {
        await ctx.settings.update('dpswarm', { sidecarUrl, workspace: process.env.DPSWARM_CM_PROBE_SIDECAR_STATE, dpswarmDir: source, pythonCmd: process.env.DPSWARM_CM_PROBE_PYTHON || '', autoStart: false, cmProvider: FIXTURE_PROVIDER, cmModel: 'deepseek-v4-flash', cmEffort: 'off', cmEnabledSessions: [low, near, root, unknown], enabledSessions: [] })
        async function create({ label, id, model, parent = null }) {
          const handle = await ctx.agents.create({ sessionId: id, seed: seedWithHistory(50_000), meta: { cwd: process.cwd(), agentPreset: 'standard', ...(parent ? { parentSession: parent, delegationDepth: 1, origin: 'subagent' } : {}) }, agentOptions: { provider: FIXTURE_PROVIDER, model, reasoningEffort: 'off' }, setup: async agentCtx => { if (parent) ctx.agentPresets.composeFrom(agentCtx, handles[0].agent.ctx); else await ctx.agentPresets.mount(agentCtx, 'standard') } })
          handles.push(handle); const { agent } = handle, beforeEvents = agent.session.events.length
          agent.followup(userMessage(`Run ${label} through the local fixture.`)); await agent.whenIdle()
          let profileError = null
          try { await ctx.dpswarmCM.profile(agent.session) } catch (error) { profileError = { code: error?.code || null, message: error?.message || String(error) } }
          const status = await ctx.dpswarmCM.status(agent), own = requests.filter(row => row.session_id === id), cm = own.filter(row => row.purpose === 'compaction'), main = own.filter(row => row.purpose === 'main')
          assert.equal(main.length, 1, `${label}: a real main AgentLoop request is required`); assert.equal(main[0].system_present, true, `${label}: pressure is measured after system assembly`); assert.notEqual(main[0].tool_count, null, `${label}: tools are observed at request time`)
          assert.ok(agent.session.events.length > beforeEvents); assert.ok(agent.session.events.some(event => event.type === 'request/context')); assert.ok(agent.session.events.some(event => event.type === 'user/message' && JSON.stringify(event.data).includes('pressure-history-0')))
          await ctx.sessions.flush(agent.session); const restored = Session.fromRestore(id, JSON.parse(JSON.stringify(agent.session.events)), agent.session.header); assert.deepEqual(restored.deriveMessages(), agent.session.deriveMessages())
          return { label, id, model, cm_calls: cm.length, main_calls: main.length, main_request: main[0], native_compaction_service: nativeService(ctx, agent), pressure: status.last_pressure_check, status_summary: { enabled: status.enabled, requested: status.requested, attached: status.attached, configuration_error: status.configuration_error, profile: status.profile, profile_error: profileError }, attempts: status.attempts, adopted: status.adopted, request_context: agent.session.requestContext(), native_event_count: agent.session.events.length }
        }
        const lowResult = await create({ label: 'low_50k_at_1m', id: low, model: 'fixture-main-wide' }); assert.equal(lowResult.cm_calls, 0); assert.equal(lowResult.pressure?.reason, 'below_threshold', JSON.stringify(lowResult)); assert.equal(lowResult.pressure?.context_window, 1_000_000); results.push(lowResult)
        const nearResult = await create({ label: 'near_50k_at_70k', id: near, model: 'fixture-main-narrow' }); assert.equal(nearResult.cm_calls, 1); assert.equal(nearResult.attempts, 1); assert.equal(nearResult.adopted, 1); assert.equal(nearResult.pressure?.decision, 'eligible'); assert.equal(nearResult.pressure?.context_window, 70_000); assert.equal(nearResult.pressure?.threshold_tokens, 56_000); assert.equal(nearResult.pressure?.pressure_route?.model, 'fixture-main-narrow'); results.push(nearResult)
        const rootResult = await create({ label: 'root_wide', id: root, model: 'fixture-main-wide' }); const childResult = await create({ label: 'child_narrow', id: child, model: 'fixture-main-narrow', parent: root }); assert.equal(rootResult.cm_calls, 0); assert.equal(rootResult.pressure?.context_window, 1_000_000); assert.equal(childResult.cm_calls, 1); assert.equal(childResult.pressure?.context_window, 70_000); results.push(rootResult, childResult)
        const offResult = await create({ label: 'cm_off', id: off, model: 'fixture-main-narrow' }); assert.equal(offResult.cm_calls, 0); assert.equal(offResult.attempts, 0); results.push(offResult)
        const unknownResult = await create({ label: 'unknown_window', id: unknown, model: 'fixture-main-unknown' }); assert.equal(unknownResult.cm_calls, 0); assert.equal(unknownResult.pressure?.reason, 'unknown_model_context'); assert.equal(unknownResult.native_compaction_service, 'BasicCompactionEngine'); results.push(unknownResult)
        writeFileSync(output, JSON.stringify({ passed: true, external_model_calls: 0, fixture_context: 'isolated DSH AgentLoop with deterministic local adapter; external provider plugins disabled', cases: results, request_observation: 'Each main request records final system/tools presence; CM pressure records are created before that request.' }, null, 2))
      } finally { for (const handle of handles.reverse()) await handle.dispose(); await ctx.settings.replace('dpswarm', previous) }
    }
    return () => clearTimeout(timer)
  }, 'isolated CM window pressure probe')
}