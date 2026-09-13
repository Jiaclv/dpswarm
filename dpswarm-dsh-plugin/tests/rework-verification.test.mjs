import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { compareReworkCandidates, candidateBinding, deferReworkVerification, readReworkVerification, claimReworkVerification, reworkVerificationView } from '../lib/rework-verification.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
const digest = text => createHash('sha256').update(text).digest('hex')
const file = (path, text) => ({ path, operation: 'file', sha256: digest(text), size: Buffer.byteLength(text) })
function candidate({ text = 'original', kind = 'files', generation = 0, requirement_revision = 1 } = {}) {
  const path = kind === 'text' ? '__text__/delivery.txt' : 'entry.html'
  return { id: 'contract', candidate: { candidate_id: 'candidate-' + generation, generation, requirement_revision,
    manifest_digest: digest('bound-' + generation), candidate_item_ids: ['item-' + generation],
    snapshot: { kind, entry_paths: [path], changed_paths: [path], candidate_files: [file(path, text)],
      dependency_complete: true, unknown_dependencies: [], consumed_manifest_refs: [] } } }
}
test('same sealed bytes defer across new bindings and observed write/restore, but never transfer acceptance', () => {
  const before = candidate(), after = candidate({ generation: 1 })
  after.candidate.snapshot.changed_paths = []
  const comparison = compareReworkCandidates(before, after)
  assert.equal(comparison.result, 'unchanged')
  assert.notDeepEqual(comparison.previous_candidate, comparison.current_candidate)
  assert.match(comparison.note, /do not prove correctness/)
  assert.deepEqual(comparison.changed_paths, [])
})
test('file content, entry/dependency content and evidence-only files remain real input changes', () => {
  const before = candidate(), after = candidate({ generation: 1, text: 'changed' })
  assert.deepEqual(compareReworkCandidates(before, after).changed_paths, ['entry.html'])
  const evidence = candidate({ generation: 1 }); evidence.candidate.snapshot.candidate_files.push(file('evidence.json', '{}'))
  assert.deepEqual(compareReworkCandidates(before, evidence).changed_paths, ['evidence.json'])
  const dependency = candidate({ generation: 1 }); dependency.candidate.snapshot.consumed_manifest_refs.push({ artifact_id: 'upstream', manifest_digest: digest('other') })
  assert.equal(compareReworkCandidates(before, dependency).result, 'changed')
  assert.equal(compareReworkCandidates(before, candidate({ requirement_revision: 2 })).result, 'changed')
  assert.equal(compareReworkCandidates(null, before).result, 'unknown')
})
test('text equality is byte equality and unresolved dependencies stay explicit when unchanged', () => {
  assert.equal(compareReworkCandidates(candidate({ kind: 'text' }), candidate({ kind: 'text', generation: 1 })).result, 'unchanged')
  assert.equal(compareReworkCandidates(candidate({ kind: 'text' }), candidate({ kind: 'text', text: 'different' })).result, 'changed')
  const before = candidate(); before.candidate.snapshot.unknown_dependencies = [{ path: 'entry.html', reference: 'dynamic', reason: 'unsupported' }]
  before.candidate.snapshot.dependency_complete = false
  const after = structuredClone(before); after.candidate.generation++
  const compared = compareReworkCandidates(before, after)
  assert.equal(compared.result, 'unchanged'); assert.equal(compared.dependency_complete, false)
  assert.equal(compared.unknown_dependencies.length, 1)
  after.candidate.snapshot.unknown_dependencies[0].reason = 'new observation'
  assert.equal(compareReworkCandidates(before, after).result, 'changed')
})
test('deferred claims survive cold reads, serialize concurrent callers, and never revive another candidate/root', async () => {
  const journal = new MemoryAuditJournal(), parent = { session: { id: 'root' } }, state = { journal, acceptance: candidate({ generation: 1 }) }
  const decision = await deferReworkVerification(state, parent, { source_item_id: 'item-1', source_worker_session_id: 'worker-1', run_id: 'run-1',
    candidate_binding: candidateBinding(state.acceptance), budget_profile: { mode: 'fixed', tokenLimit: 100, callLimit: 2 }, roles: ['tester', 'reviewer'] })
  assert.equal((await readReworkVerification({ ...state }, parent)).status, 'ready')
  const claims = await Promise.allSettled([1, 2].map(n => claimReworkVerification(state, parent, decision, { reason: 'independent check', runId: 'verify-' + n })))
  assert.equal(claims.filter(r => r.status === 'fulfilled').length, 1)
  const current = await readReworkVerification(state, parent)
  assert.equal(current.status, 'claimed'); assert.equal(reworkVerificationView(current).unissued_roles, null); assert.deepEqual(reworkVerificationView(current).dispatchable_roles, [])
  state.acceptance = candidate({ generation: 2 })
  assert.equal((await readReworkVerification(state, parent)).status, 'stale')
  assert.equal(await readReworkVerification(state, { session: { id: 'other-root' } }), null)
})

test('a requirement amendment makes the old deferred candidate stale even if its candidate ID is retained', async () => {
  const journal = new MemoryAuditJournal(), parent = { session: { id: 'root' } }, state = { journal, acceptance: candidate() }
  await deferReworkVerification(state, parent, { source_item_id: 'item-0', run_id: 'run', candidate_binding: candidateBinding(state.acceptance) })
  state.acceptance.contract = { requirement_revision: 2 }
  assert.equal((await readReworkVerification(state, parent)).status, 'stale')
})
