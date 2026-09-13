import { sessionEvents } from './host-session-compat.js'

/** P4 root-level usage ledger. A read-only aggregation of the authenticated
 *  budget ledger, CM records and native Lead messages. Never a new budget:
 *  reserved amounts, unknown calls and attribution limits stay explicit. */
const zero = () => ({ calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
  cache_write_tokens: 0, reasoning_tokens: 0, unknown_usage_calls: 0, reserved_tokens: 0 })
const addUsage = (bucket, usage) => {
  bucket.calls++
  if (!usage) { bucket.unknown_usage_calls++; return }
  for (const key of ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens']) {
    const value = usage[key]
    if (Number.isSafeInteger(value) && value >= 0) {
      const target = { inputTokens: 'input_tokens', outputTokens: 'output_tokens',
        cacheReadTokens: 'cache_read_tokens', cacheWriteTokens: 'cache_write_tokens',
        reasoningTokens: 'reasoning_tokens' }[key]
      bucket[target] += value
    }
  }
}

const ACTIVITY = {
  implementation: 'implementation', verification: 'verification',
  product_rework: 'product_rework', report_repair: 'report_repair',
  cm: 'cm', lead_control: 'lead_control',
}

function activityFor(binding, purpose) {
  if (purpose === 'compaction') return ACTIVITY.cm
  if (!binding) return null
  if (binding.authority === 'fixed-team-run') return binding.label === 'implementer' ? ACTIVITY.implementation : ACTIVITY.verification
  if (binding.authority === 'fixed-team-rework') return binding.budget_origin === 'source_remaining_report_repair' ? ACTIVITY.report_repair : ACTIVITY.product_rework
  return null
}

/**
 * @param {Array} journalEvents root-scoped audit events (dpswarm/worker-budget-*, dpswarm/cm-*)
 * @param {Array} leadMessages native Lead assistant messages with usage fields
 */
export function usageLedger(journalEvents = [], leadMessages = []) {
  const byActivity = Object.fromEntries(Object.values(ACTIVITY).map(key => [key, zero()]))
  const byRole = { lead: zero(), implementer: zero(), tester: zero(), reviewer: zero() }
  // worker_session_id -> {activity, role} from allocation/bound events; falls
  // back to the frozen worker's policy binding for restored sessions.
  const bindings = new Map()
  const events = journalEvents.filter(e => e?.data && typeof e.data === 'object')
  for (const event of events) {
    const d = event.data
    if (event.type === 'dpswarm/worker-budget-allocation') {
      bindings.set(d.allocation_id, { authority: d.authority, label: d.label, budget_origin: d.budget_origin || null })
    } else if (event.type === 'dpswarm/worker-budget-allocation-bound') {
      const prior = bindings.get(d.allocation_id) || {}
      bindings.set(d.worker_session_id, { ...prior, authority: prior.authority, label: prior.label,
        budget_origin: prior.budget_origin })
    } else if (event.type === 'dpswarm/worker-budget-frozen') {
      if (d.policy_binding && !bindings.has(d.worker_session_id)) {
        bindings.set(d.worker_session_id, { authority: d.policy_binding.authority, label: d.policy_binding.label,
          budget_origin: d.policy_binding.source_worker_session_id ? 'source_remaining_report_repair' : null })
      }
    }
  }
  // Settled calls are appended once per call_id by the ledger; re-reads of the
  // same journal therefore aggregate each call exactly once.
  const settledCalls = new Map()
  for (const event of events) {
    if (event.type === 'dpswarm/worker-budget-settled') settledCalls.set(event.data.call_id, event.data)
  }
  const purposes = new Map()
  for (const event of events) if (event.type === 'dpswarm/worker-budget-admitted') purposes.set(event.data.call_id, event.data.purpose)
  for (const call of settledCalls.values()) {
    const binding = bindings.get(call.worker_session_id)
    const activity = activityFor(binding, purposes.get(call.call_id)) || ACTIVITY.verification
    addUsage(byActivity[activity], call.usage)
    if (binding?.label && byRole[binding.label]) addUsage(byRole[binding.label], call.usage)
    else if (byRole.tester === undefined) { /* unreachable */ }
  }
  // Reserved-but-unknown calls (admitted, unsettled or incomplete usage).
  // Compaction calls settle in the same worker ledger (purpose=compaction);
  // they are classified into the cm activity here and never counted twice —
  // each settled call_id is aggregated exactly once above.
  const admitted = new Map()
  for (const event of events) {
    if (event.type === 'dpswarm/worker-budget-admitted') admitted.set(event.data.call_id, event.data)
  }
  let reservedTotal = 0, unknownCalls = 0
  for (const [callId, call] of admitted) {
    const settled = settledCalls.get(callId)
    if (settled?.usage_complete) continue
    const binding = bindings.get(call.worker_session_id)
    const activity = call.purpose === 'compaction' ? ACTIVITY.cm : activityFor(binding, call.purpose) || ACTIVITY.verification
    byActivity[activity].unknown_usage_calls++
    reservedTotal += call.reserved_tokens || 0
    unknownCalls++
  }
  // Lead: native assistant message usage; per-message usage appears once in
  // the session journal, so this is already deduplicated. Mixed activities
  // (scheduling, verification, acceptance) are not split per message.
  for (const message of leadMessages) {
    const usage = message?.usage
    if (usage && typeof usage === 'object') addUsage(byRole.lead, usage)
  }
  byActivity.lead_control = { ...byRole.lead }
  const observed = Object.values(byActivity).reduce((sum, bucket) =>
    sum + bucket.input_tokens + bucket.output_tokens + bucket.cache_read_tokens + bucket.cache_write_tokens, 0)
  const times = events.map(e => e.data?.at || e.data?.finished_at || e.data?.started_at || e.data?.frozen_at).filter(Number.isFinite)
  return { schema: 'dpswarm-usage-ledger-v1',
    window: { started_at: times.length ? Math.min(...times) : null, ended_at: times.length ? Math.max(...times) : null },
    by_activity: byActivity, by_role: byRole,
    totals: { observed_tokens: observed, reserved_tokens_unknown: reservedTotal, unknown_usage_calls: unknownCalls },
    notes: [
      'Token unit: input + output + cache read + cache write per call; reasoning is reported separately inside output.',
      'CM requests are charged to the cm activity and their worker role from the same settled ledger rows — never double-counted.',
      'Lead totals aggregate native session message usage; scheduling, verification and acceptance inside one message are not split (mixed).',
      'Unknown or interrupted calls keep their reservation as a lower bound; nothing is zero-filled.',
      'Reserved amounts are authorizations, not consumption.',
    ] }
}

export function leadUsageMessages(session) {
  if (!session) return []
  return sessionEvents(session).filter(e => e.type === 'assistant/message' && e.data?.usage).map(e => e.data)
}
