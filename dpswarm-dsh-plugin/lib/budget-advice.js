import { estimateRequestTokens, isWorkerSession, workerBudgetProfile } from './budget-runtime.js'

// Advisory measurements only. Never store prompts, choose budgets or mutate settings.
export function installBudgetAdvice(ctx, config) {
  const envelopes = new Map()
  const dispose = ctx.on('llm/stream', (options, next) => {
    const session = options.sessionId ? (ctx.get?.('sessions', false) || ctx.sessions)?.get(options.sessionId) : null
    if (session && !isWorkerSession(session) && !['compaction', 'session-title'].includes(options.purpose)) {
      if (!envelopes.has(session.id) && envelopes.size >= 256) envelopes.delete(envelopes.keys().next().value)
      envelopes.set(session.id, {
        source: 'latest Lead request envelope; proxy only, not a worker quote',
        estimation: 'ceil(serialized system/messages/tools characters / 3); tokenizer and provider overhead can differ',
        estimated_input_tokens: estimateRequestTokens(options).input,
        estimated_system_tools_tokens: estimateRequestTokens({ system: options.system, tools: options.tools }).input,
      })
    }
    return next()
  }, { prepend: true, global: true })
  return {
    status(agent) {
      const policy = workerBudgetProfile(config(), agent.session.id)
      return { mode: policy.mode, allocation_authority: policy.mode === 'auto' ? 'current Lead' : 'user settings',
        modifies_limits: false,
        token_unit: 'sum of input + output + cache read + cache write for every worker request and its CM; reasoning is included in output',
        envelope_reference: envelopes.get(agent.session.id) || null,
        reference_limit: 'Lead and worker prompts/tools/history differ. This proxy is not a minimum, a model tokenizer count, a cost prediction or an automatically selected grant.',
        planning: 'For Auto, choose per-role anomaly rails, not precise estimates: call counts are the primary scale (steps are predictable; token content is not). Every step re-sends the full history plus the system/tool envelope, so a write-then-verify worker spends several full envelopes after the file exists — size the rail for the write plus a few verification reads plus the report, not for the artifact alone. A worker that reaches its rail parks and reports saved files plus remaining work; you decide continuation. In manual/unlimited omit worker_budgets.',
      }
    },
    dispose() { if (typeof dispose === 'function') dispose(); envelopes.clear() },
  }
}
