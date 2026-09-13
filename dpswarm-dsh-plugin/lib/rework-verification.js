import { randomUUID } from 'node:crypto'
import { canonicalJson } from './candidate-snapshot.js'

const copy = value => JSON.parse(JSON.stringify(value))
const fail = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const sorted = rows => rows.map(copy).sort((a, b) => canonicalJson(a).localeCompare(canonicalJson(b)))
const values = value => Object.values(value || {})
const same = (a, b) => canonicalJson(a) === canonicalJson(b)

export function candidateBinding(acceptance) {
  const c = acceptance?.candidate
  return c ? { contract_id: acceptance.id, candidate_id: c.candidate_id || c.id,
    manifest_digest: c.manifest_digest, generation: c.generation,
    requirement_revision: c.requirement_revision,
    current_requirement_revision: acceptance.contract?.requirement_revision ?? c.requirement_revision, candidate_item_ids: [...c.candidate_item_ids].sort() } : null
}

function verificationInputs(acceptance) {
  const c = acceptance?.candidate, snapshot = c?.snapshot
  if (!snapshot || !['files', 'text'].includes(snapshot.kind) || !Array.isArray(snapshot.candidate_files)
    || !Array.isArray(snapshot.entry_paths) || !Array.isArray(snapshot.unknown_dependencies)
    || !Array.isArray(snapshot.consumed_manifest_refs) || typeof snapshot.dependency_complete !== 'boolean') return null
  const files = snapshot.candidate_files.map(({ path, operation, sha256, size }) => ({ path, operation, sha256, size }))
  if (!files.length || files.some(f => typeof f.path !== 'string' || !['file', 'deleted'].includes(f.operation)
    || !Number.isSafeInteger(f.size) || (f.operation === 'file' && !/^[a-f0-9]{64}$/.test(f.sha256 || '')))) return null
  return { contract_id: acceptance.id, requirement_revision: c.requirement_revision, kind: snapshot.kind,
    entry_paths: [...snapshot.entry_paths].sort(), candidate_files: sorted(files),
    unknown_dependencies: sorted(snapshot.unknown_dependencies), dependency_complete: snapshot.dependency_complete,
    // Reader identity is provenance, while the consumed artifact version and
    // paths are verification inputs. Never substitute these refs for new evidence.
    consumed_manifest_refs: sorted(snapshot.consumed_manifest_refs.map(({ worker_session_id, ...ref }) => ref)) }
}

/** Equality of sealed inputs is a scheduling fact, never an acceptance verdict. */
export function compareReworkCandidates(before, after) {
  const a = verificationInputs(before), b = verificationInputs(after)
  const changedPaths = a && b ? [...new Set([...a.candidate_files.map(f => f.path), ...b.candidate_files.map(f => f.path)])]
    .filter(path => !same(a.candidate_files.find(f => f.path === path) || null, b.candidate_files.find(f => f.path === path) || null)).sort() : []
  return { schema: 'dpswarm-rework-comparison-v1', result: !a || !b ? 'unknown' : same(a, b) ? 'unchanged' : 'changed',
    previous_candidate: candidateBinding(before), current_candidate: candidateBinding(after), changed_paths: changedPaths,
    dependency_complete: b?.dependency_complete ?? null, unknown_dependencies: b?.unknown_dependencies || [],
    comparison_scope: 'Sealed file/text bytes, entries, dependency facts, consumed artifact versions and requirement revision. Native write counts and candidate generation are not content changes.',
    note: 'Identical sealed inputs do not prove correctness or resolve findings. Historical evidence remains bound to its original candidate; current-candidate verification is still required for acceptance.' }
}

export function previousVerificationContext(acceptance) {
  const c = acceptance?.candidate, contract = acceptance?.contract
  return { candidate: candidateBinding(acceptance), findings: values(contract?.findings).map(f => ({ id: f.id, observation: f.observation,
      requirement_ids: copy(f.requirement_ids || []), first_candidate_id: f.first_candidate_id || null,
      latest_observed_decision: copy(f.history?.at(-1)?.decision || null) })),
    evidence: Object.entries(c?.roster_evidence || {}).map(([role, evidence_id]) => ({ role, evidence_id,
      item_id: contract?.evidence?.[evidence_id]?.identity?.item_id || null,
      worker_session_id: contract?.evidence?.[evidence_id]?.identity?.execution_binding?.execution_session_id
        || contract?.evidence?.[evidence_id]?.identity?.session_id || null })),
    note: 'References support inspection and focused verification. They are not transferred passes for the new candidate.' }
}

const ownEvents = (journal, rootId) => journal.events.filter(e => e.type === 'dpswarm/worker-rework'
  && e.data.root_session_id === rootId && e.data.owner_session_id === rootId)
function latestDecision(journal, rootId, itemId) {
  const events = ownEvents(journal, rootId)
  const paused = events.findLast(e => e.data.phase === 'verification-deferred'
    && (!itemId || e.data.source_item_id === itemId))
  if (!paused) return null
  const claim = events.find(e => e.data.phase === 'verification-claimed' && e.data.decision_id === paused.data.decision_id)
  const finished = events.findLast(e => ['verification-finished', 'verification-failed'].includes(e.data.phase)
    && e.data.decision_id === paused.data.decision_id)
  return { ...copy(paused.data), status: finished?.data.phase === 'verification-failed' ? 'blocked' : finished?.data.phase === 'verification-finished' ? 'finished' : claim ? 'claimed' : 'ready',
    failure_codes: finished?.data.failure_codes || (finished?.data.error_code ? [finished.data.error_code] : []),
    claim_id: claim?.data.claim_id || null, claimed_run_id: claim?.data.verification_run_id || null }
}

export async function deferReworkVerification(state, parent, data) {
  const rootId = parent.session.id
  const decision = { version: 1, root_session_id: rootId, owner_session_id: rootId,
    phase: 'verification-deferred', decision_id: randomUUID(), at: Date.now(), ...copy(data) }
  await state.journal.append(rootId, 'dpswarm/worker-rework', decision)
  return { ...decision, status: 'ready', claim_id: null, claimed_run_id: null }
}

export async function readReworkVerification(state, parent, itemId) {
  const result = latestDecision(await state.journal.read(parent.session.id), parent.session.id, itemId)
  if (result && !same(result.candidate_binding, candidateBinding(state.acceptance))) result.status = 'stale'
  return result
}

export function reworkVerificationView(decision) {
  if (!decision) return null
  return { status: decision.status, decision_id: decision.decision_id, item_id: decision.source_item_id,
    candidate: decision.candidate_binding, comparison: decision.comparison, prior_verification: decision.prior_verification,
    unissued_roles: decision.status === 'ready' ? decision.roles : null,
    dispatchable_roles: decision.status === 'ready' ? decision.roles : [], failure_codes: decision.failure_codes || [], worker_budget_policy: decision.budget_profile,
    next: decision.status === 'ready'
      ? 'Choose further implementation with dpswarm_rework, direct verification of this candidate with dpswarm_verify_rework({item_id, reason}), or describe the remaining gap. No downstream allowance has been issued. No finding or acceptance gate was cleared.'
      : 'This decision is not available for another dispatch. Read current candidate evidence and worker reports; issued verification cannot be restarted through this decision.' }
}

export async function claimReworkVerification(state, parent, decision, { reason, runId }) {
  const rootId = parent.session.id, claimId = randomUUID()
  await state.journal.transaction(rootId, journal => {
    const latest = latestDecision(journal, rootId, decision.source_item_id)
    if (!latest || latest.decision_id !== decision.decision_id || latest.status !== 'ready'
      || !same(latest.candidate_binding, candidateBinding(state.acceptance))) {
      throw fail('REWORK_VERIFICATION_ALREADY_CLAIMED', 'This exact deferred verification has already been claimed or its candidate changed. No allowance was issued by this request.')
    }
    return { events: [{ type: 'dpswarm/worker-rework', data: { version: 1, root_session_id: rootId, owner_session_id: rootId,
      phase: 'verification-claimed', decision_id: decision.decision_id, source_item_id: decision.source_item_id,
      run_id: decision.run_id, verification_run_id: runId, claim_id: claimId, reason, at: Date.now() } }] }
  })
  return claimId
}
