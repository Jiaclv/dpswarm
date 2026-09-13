const terminal = new Set(['accepted', 'terminated', 'escalated', 'aborted-finalize'])
const resolved = new Set(['verified-resolved', 'not-a-defect', 'not-applicable'])

/** Read-only projection. Execution, submitted evidence and acceptance stay distinct. */
export function completionStatus({ snapshot, contract, lease = null, leaseKnown = true, busy = false } = {}) {
  if (!snapshot) return { available: false, reason: 'control_state_unavailable', attention_required: true }
  const items = Object.entries(snapshot.work_items || {})
    .filter(([, item]) => ['derive', 'fission'].includes(item.kind))
  const pending = items.filter(([, item]) => !terminal.has(item.acceptance))
    .map(([id, item]) => ({ item_id: item.item_id || id, state: item.acceptance || 'active' }))
  const candidate = contract?.candidates?.[contract.current_candidate_id]
  const candidateIds = candidate?.candidate_item_ids || []
  const review = contract?.reviews?.[contract.current_review_id]
  const currentReview = Boolean(candidate && review && !contract.needs_revalidation
    && candidate.requirement_revision === contract.requirement_revision
    && review.review_id === contract.current_review_id && review.candidate_id === candidate.candidate_id
    && review.manifest_digest === candidate.manifest_digest && review.requirement_revision === contract.requirement_revision
    && review.evidence_revision === contract.evidence_revision && review.reviewer_id === contract.reviewer_id
    && review.record?.verdict === 'pass')
  const receipts = contract?.accepted || []
  const previouslyAccepted = receipts.some(record => record.candidate_id === candidate?.candidate_id)
  const accepted = currentReview && candidateIds.length > 0 && candidateIds.every(id =>
    snapshot.work_items?.[id]?.acceptance === 'accepted'
    && receipts.some(record => record.item_id === id && record.candidate_id === candidate.candidate_id
      && record.review_id === review.review_id))
  const findings = Object.values(contract?.findings || {}).map(finding => {
    const decision = finding.history?.at(-1)?.decision || finding
    return { id: finding.id || decision.id, classification: decision.classification || 'unknown', state: decision.state || 'open' }
  }).filter(finding => !resolved.has(finding.state))
  // P3b: acceptance and cleanup are distinct facts. An accepted candidate
  // with pending auxiliary items, a held lease or open slots is still in
  // cleanup; only a fully drained state is finished.
  const cleanupPending = pending.length > 0 || Boolean(lease) || (snapshot.open_worker_slots_used ?? 0) > 0
  const finalization = accepted ? (cleanupPending ? 'accepted+cleanup_pending' : 'accepted+finished') : 'not_accepted'
  return { available: true, schema: 'dpswarm-completion-v1', execution: busy ? 'running' : 'not_running',
    finalization, cleanup_pending: accepted ? cleanupPending : null,
    candidate: candidate ? { candidate_id: candidate.candidate_id, manifest_digest: candidate.manifest_digest,
      saved_snapshot: true, workspace_currentness: 'not_rechecked', item_ids: candidateIds } : null,
    acceptance: contract ? { status: accepted ? 'accepted' : previouslyAccepted ? 'needs_revalidation' : 'not_accepted', contract_id: contract.contract_id,
      revision: contract.revision, current_review_id: contract.current_review_id || null } : { status: 'legacy_or_unavailable' },
    unresolved_findings: findings.slice(0, 32), unresolved_findings_count: findings.length,
    pending_items: pending.slice(0, 16), pending_items_count: pending.length,
    open_worker_slots: snapshot.open_worker_slots_used ?? null, workspace_lease_held: leaseKnown ? Boolean(lease) : null, workspace_lease_owner: lease?.session_id || null,
    attention_required: !leaseKnown || busy || Boolean(lease) || pending.length > 0 || (snapshot.open_worker_slots_used ?? 0) > 0 || (Boolean(contract) && !accepted),
    next: 'Use dpswarm_acceptance for the current contract, review_format and findings; dpswarm_report pages final reports or recovery progress. Decide whether evidence supports acceptance, report repair, necessary rework, explicit termination or a partial/blocked handoff. State saved, reported and accepted separately. Pending items and a held lease remain recovery work; presenting a file or ending a turn does not accept it.' }
}
