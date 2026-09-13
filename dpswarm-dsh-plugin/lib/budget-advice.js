import { isWorkerSession, workerBudgetProfile } from './budget-runtime.js'

// Advisory measurements only. Never store prompts, choose budgets or mutate settings.
// Auto (Lead-estimated per-role budgets) was removed: rails come from user
// settings, a worker that reaches its rail reports for a Lead decision, and
// continuation spends the user's rework allowance. No estimation surface remains.
export function installBudgetAdvice(ctx, config, { modelRegistry = null } = {}) {
  return {
    async status(agent) {
      const policy = workerBudgetProfile(config(), agent.session.id)
      // A cumulative grant above the route context window can never fit into a
      // single request; surface host-observed limits for the current Lead route
      // when metadata is available. Advisory only; never a budget decision.
      const route = agent?.session?.requestHeader?.()?.config
      let routeLimits = null
      if (modelRegistry && typeof route?.provider === 'string' && typeof route?.model === 'string'
        && route.provider.trim() && route.model.trim()) {
        const capability = await modelRegistry.resolveRouteCapability(route.provider, route.model).catch(() => null)
        if (capability && (capability.context_window || capability.default_max_tokens)) {
          routeLimits = { provider: route.provider, model: route.model,
            context_window: capability.context_window, default_max_tokens: capability.default_max_tokens,
            note: 'Host-observed for the current Lead route. One request output is clamped to default_max_tokens; a cumulative tokenLimit above context_window cannot fit in any single request, so split the work instead of granting one oversized rail. Advisory only; never a substitute for user settings.' }
        }
      }
      return { mode: policy.mode, allocation_authority: 'user settings',
        modifies_limits: false,
        token_unit: 'sum of input + output + cache read + cache write for every worker request and its CM; reasoning is included in output',
        route_limits: routeLimits,
        planning: 'Worker limits are user-set fixed rails, not per-task estimates: every initial worker gets the same cumulative tokenLimit and callLimit from settings, and you never choose, announce or rescale them. A rail is an anomaly bound, not a plan for the work. A worker that reaches its rail parks: it stops tool use and reports what is complete, which files were provably saved, and what remains — you then decide: continue via dpswarm_rework (its allowance comes from the user\'s rework settings, never a grant you invent), accept the partial delivery where the requirements allow, or terminate. Full acceptance still requires every necessary requirement and related finding to pass for the sealed candidate.',
      }
    },
    dispose() {},
  }
}
