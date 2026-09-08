/** Load only as an extra plugin in an isolated DSH profile, with provider rows disabled.
 * Exercises the installed DPswarm package, real preset mounts, real AgentLoop requests,
 * native session persistence, and a deterministic local LlmAdapter. No external API calls.
 */
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const [{ LlmAdapter, createUserMessage }, { Session }] = await Promise.all([
  import(hostModuleUrl(host, 'dsh-llm/lib/index.js')), import(hostModuleUrl(host, 'dsh-session/lib/index.js'))])
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmCM', 'tokenMeter', 'settings', 'sessions']
export function apply(ctx) {
  if (!process.env.DPSWARM_CM_PROBE_OUTPUT) throw new Error('An isolated probe output path is required')
  const requests = []
  class FixtureAdapter extends LlmAdapter {
    async listModels(provider) { return Promise.all(['deepseek-v4-flash','glm-5.3-flash','gpt-5.6-sol','fixture-summary-custom'].map(model => this.resolveModel(provider, model))) }
    async resolveModel(provider, model) { return { provider, id: model, name: model, reasoning: { efforts: [{ id: 'off', name: 'Off' }, { id: 'max', name: 'Max' }] } } }
    async *stream(options) {
      assert.equal(options.provider, 'dpswarm-cm-fixture')
      requests.push({ sessionId: options.sessionId, purpose: options.purpose || 'main',
        model: options.model, messages: options.messages, reasoning: options.reasoningEffort })
      const text = options.purpose === 'compaction' ? 'Goal: probe-invariant-K42; original source remains available. Tests remain unverified.' : 'Fixture response; no tools executed.'
      yield { type: 'block-end', index: 0, block: { type: 'text', text } }
      yield { type: 'usage', usage: { inputTokens: 15000, cacheReadTokens: 100, outputTokens: 30 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter(['dpswarm-cm-fixture'], new FixtureAdapter())
  ctx.effect(() => {
    let cancelled = false
    // Yield until all installation rows, including the isolated CM engine, finish loading.
    const timer = setTimeout(() => { void run().catch(error => {
      writeFileSync(process.env.DPSWARM_CM_PROBE_OUTPUT, JSON.stringify({ passed: false, error: error.stack }, null, 2))
    }) }, 1200)
    async function run() {
      const previous = ctx.settings.get('dpswarm'), handles = []
      const results = [], rootId = 'dpswarm-cm-probe-' + randomUUID()
      try {
        await ctx.settings.update('dpswarm', { cmProvider: 'dpswarm-cm-fixture', cmModel: 'deepseek-v4-flash', cmEffort: 'off', cmEnabledSessions: [rootId, rootId + '-minimal', rootId + '-custom'], enabledSessions: [] })
        for (const [label, id, parentId, preset] of [
          ['root', rootId, null, 'standard'], ['child', rootId + '-child', rootId, null],
          ['off', rootId + '-off', null, 'standard'], ['minimal', rootId + '-minimal', null, 'minimal'],
          ['custom', rootId + '-custom', null, 'standard'],
        ]) {
          if (cancelled) throw new Error('probe disposed')
          if (label === 'custom') await ctx.settings.update('dpswarm', { cmModel: 'fixture-summary-custom', cmEffort: 'max' })
          const seed = Session.create('seed')
          seed.append('turn/start', { turn: 1 })
          for (let i = 0; i < 10; i++) seed.append('user/message', createUserMessage({ source: { kind: 'user' },
            content: [{ type: 'text', text: `history-record-${i} ` + 'probe-invariant-K42 '.repeat(1400) }] }), { surfaceOp: 'append' })
          seed.append('turn/end', { turn: 1, reason: 'completed' })
          const handle = await ctx.agents.create({ sessionId: id, seed: seed.events,
            meta: { cwd: process.cwd(), ...(parentId ? { parentSession: parentId, delegationDepth: 1, origin: 'subagent' } : {}), ...(preset ? { agentPreset: preset } : {}) },
            agentOptions: { provider: 'dpswarm-cm-fixture', model: 'fixture-main' },
            setup: async agentCtx => { if (parentId) ctx.agentPresets.composeFrom(agentCtx, handles[0].agent.ctx); else await ctx.agentPresets.mount(agentCtx, preset) } })
          handles.push(handle)
          const { agent } = handle, before = agent.session.deriveMessages()
          assert.equal(ctx.dpswarmCM.status(agent).attached, true, 'CM engine loaded from installed bundle')
          const native = ctx.agentPresets.serviceFor(agent, 'compaction')
          if (preset === 'standard') assert.equal(native.constructor.name, 'BasicCompactionEngine')
          agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Return the fixture response.' }] }))
          await agent.whenIdle()
          const own = requests.filter(r => r.sessionId === id), cm = own.filter(r => r.purpose === 'compaction'), main = own.find(r => r.purpose === 'main')
          assert.ok(main, 'real AgentLoop dispatched a main request')
          assert.equal(cm.length, label === 'off' ? 0 : 1, JSON.stringify({label,status:ctx.dpswarmCM.status(agent),types:agent.session.events.map(e=>e.type)}))
          const flat = JSON.stringify(main.messages)
          assert.equal(flat.includes('history-record-0'), label === 'off')
          assert.equal(flat.includes('history-record-9'), true)
          assert.ok(agent.session.events.some(e => e.type === 'user/message' && JSON.stringify(e.data).includes('history-record-0')))
          if (cm.length) { assert.equal(cm[0].model, label === 'custom' ? 'fixture-summary-custom' : 'deepseek-v4-flash'); assert.equal(cm[0].reasoning, label === 'custom' ? 'max' : 'off') }
          await ctx.sessions.flush(agent.session)
          const restored = Session.fromRestore(id, JSON.parse(JSON.stringify(agent.session.events)), agent.session.header)
          assert.deepEqual(restored.deriveMessages(), agent.session.deriveMessages())
          results.push({ label, cm_calls: cm.length, cm_model: cm[0]?.model, cm_effort: cm[0]?.reasoning, main_message_count: main.messages.length,
            original_message_count: before.length, adopted: agent.session.events.filter(e => e.type === 'dpswarm/cm-end' && e.data.outcome === 'adopted').length,
            preset: ctx.agentPresets.composedPreset(agent.ctx), native_compaction: native?.constructor.name || null })
        }
        writeFileSync(process.env.DPSWARM_CM_PROBE_OUTPUT, JSON.stringify({ passed: true, external_model_calls: 0,
          execution: 'installed package + actual DSH AgentLoop + real presets + deterministic local adapter', results }, null, 2))
      } finally {
        for (const handle of handles.reverse()) await handle.dispose()
        await ctx.settings.replace('dpswarm', previous)
      }
    }
    return () => { cancelled = true; clearTimeout(timer) }
  }, 'isolated CM host probe')
}
