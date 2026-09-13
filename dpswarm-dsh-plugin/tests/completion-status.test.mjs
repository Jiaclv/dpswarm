import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FixedTeamController } from '../lib/fixed-team.js'
import { completionStatus } from '../lib/completion-status.js'
import { TeamRequirement } from '../lib/team-required.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { createUserMessage } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-llm/lib/index.js'))

const state = () => ({ snapshot: { open_worker_slots_used: 2, work_items: {
  root: { kind: 'root', acceptance: 'active' }, impl: { kind: 'derive', acceptance: 'submitted' },
  test: { kind: 'derive', acceptance: 'submitted' }, review: { kind: 'derive', acceptance: 'terminated' } } },
  lease: { run_id: 'run' }, contract: { contract_id: 'contract', revision: 5, current_candidate_id: 'candidate',
    candidates: { candidate: { candidate_id: 'candidate', candidate_item_ids: ['impl'], requirement_revision: 1, manifest_digest: 'a'.repeat(64) } },
    accepted: [], requirement_revision: 1, evidence_revision: 2, reviewer_id: null, reviews: {}, current_review_id: null, findings: { F1: { id: 'F1', history: [{ decision: { id: 'F1', classification: 'unknown', state: 'open' } }] } } } })

test('settled native workers and a saved file do not imply acceptance or released capacity', () => {
  const value = completionStatus(state())
  assert.equal(value.execution, 'not_running')
  assert.equal(value.candidate.saved_snapshot, true)
  assert.equal(value.acceptance.status, 'not_accepted')
  assert.deepEqual(value.pending_items.map(x => x.item_id), ['impl', 'test'])
  assert.equal(value.workspace_lease_held, true)
  assert.deepEqual(value.unresolved_findings, [{ id: 'F1', classification: 'unknown', state: 'open' }])
  assert.equal(value.attention_required, true)
})

test('acceptance needs the current candidate receipt and resource closure remains separate', () => {
  const input = state()
  input.snapshot.work_items.impl.acceptance = 'accepted'
  input.contract.accepted.push({ item_id: 'impl', candidate_id: 'old-candidate' })
  assert.equal(completionStatus(input).acceptance.status, 'not_accepted')
  input.contract.current_review_id = 'review-1'
  input.contract.reviews['review-1'] = { review_id: 'review-1', candidate_id: 'candidate', manifest_digest: 'a'.repeat(64),
    requirement_revision: 1, evidence_revision: 2, reviewer_id: null, record: { verdict: 'pass' } }
  input.contract.accepted.push({ item_id: 'impl', candidate_id: 'candidate', review_id: 'review-1' })
  assert.equal(completionStatus(input).acceptance.status, 'accepted')
  assert.equal(completionStatus(input).attention_required, true, 'tester and lease remain pending')
  input.snapshot.work_items.test.acceptance = 'accepted'
  input.snapshot.open_worker_slots_used = 0
  input.contract.findings.F1.history.push({ decision: { classification: 'unknown', state: 'not-a-defect' } })
  input.lease = null
  assert.equal(completionStatus(input).attention_required, false)
  assert.deepEqual(completionStatus(input).unresolved_findings, [])
})

async function settled(readCompletion) {
  const root = { id: 'root', header: { delegationDepth: 0 }, events: [{ type: 'user/message', data:
    createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Create a file.' }] }) }] }
  const agent = { id: 'root', session: root, notices: [], steer(message) { this.notices.push(message) } }
  const requirement = new TeamRequirement({ config: () => ({ enabledSessions: ['root'] }), journal: new MemoryAuditJournal(), readCompletion })
  const binding = await requirement.beforeRun(agent)
  await requirement.markStarted(binding.binding, { run_id: 'run', execution_session_id: 'worker', role: 'implementer' })
  await requirement.finishRun(binding.binding, { outcome: 'failed_takeover', run_id: 'run' })
  return { requirement, agent }
}

test('failed takeover receives one factual closeout notice without blocking a partial final', async () => {
  let reads = 0
  const { requirement, agent } = await settled(async () => { reads++; return completionStatus(state()) })
  await requirement.turnStopping({ agent, turn: 1 })
  assert.equal(agent.notices.length, 1)
  assert.match(agent.notices[0].content[0].text, /not_accepted/)
  assert.match(agent.notices[0].content[0].text, /partial\/blocked/)
  await requirement.turnStopping({ agent, turn: 1 })
  assert.equal(reads, 1)
  assert.equal(agent.notices.length, 1)
  await requirement.turnStopping({ agent, turn: 2 })
  assert.equal(agent.notices.length, 2, 'later user continuation rechecks current facts')
})

test('unavailable closeout state is disclosed and cannot create another TEAM_REQUIRED loop', async () => {
  const { requirement, agent } = await settled(async () => { throw Object.assign(new Error('read failed'), { code: 'AUDIT_DOWN' }) })
  await requirement.turnStopping({ agent, turn: 1 })
  assert.match(agent.notices[0].content[0].text, /AUDIT_DOWN/)
  await requirement.turnStopping({ agent, turn: 1 })
  assert.equal(agent.notices.length, 1)
})


test('old acceptance receipts become revalidation work after amendment or evidence changes', () => {
  const input = state()
  input.snapshot.work_items.impl.acceptance = 'accepted'
  input.contract.accepted = [{ item_id: 'impl', candidate_id: 'candidate', review_id: 'review-1' }]
  for (const mutation of [
    contract => { contract.requirement_revision = 2 },
    contract => { contract.needs_revalidation = true },
    contract => { contract.evidence_revision = 3 },
    contract => { contract.reviewer_id = 'different-verifier' },
  ]) {
    const next = structuredClone(input)
    next.contract.current_review_id = 'review-1'
    next.contract.reviews['review-1'] = { review_id: 'review-1', candidate_id: 'candidate', manifest_digest: 'a'.repeat(64),
      requirement_revision: 1, evidence_revision: 2, reviewer_id: null, record: { verdict: 'pass' } }
    mutation(next.contract)
    const value = completionStatus(next)
    assert.equal(value.acceptance.status, 'needs_revalidation')
    assert.equal(value.attention_required, true)
  }
})

test('a cold controller observes an existing live-owner lease without adopting or changing it', async () => {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-completion-')), cwd = join(workspace, 'project')
  mkdirSync(cwd); mkdirSync(join(workspace, 'workspace-leases'))
  const journal = new MemoryAuditJournal()
  const controller = new FixedTeamController({ config: () => ({ workspace, autoStart: false }),
    sidecarFactory: () => ({ async ensure() {}, async call(method, path) {
      if (path === '/api/plugin-audit') return journal.read('root')
      return { snapshot: { work_items: {}, open_worker_slots_used: 0 } }
    } }) })
  const parent = { id: 'root', session: { id: 'root', header: { cwd, delegationDepth: 0 } } }
  const path = controller.leasePath(workspace, cwd)
  const lease = JSON.stringify({ pid: process.pid, session_id: 'root', run_id: 'original-run' })
  writeFileSync(path, lease)
  assert.equal(controller.session(parent).lease, undefined)
  const result = await controller.completionStatus(parent)
  assert.equal(result.workspace_lease_held, true)
  assert.equal(result.workspace_lease_owner, 'root')
  assert.equal(result.attention_required, true)
  assert.equal(controller.session(parent).lease, undefined)
  assert.equal(readFileSync(path, 'utf8'), lease)
})

test('finalization distinguishes accepted+cleanup_pending from accepted+finished (P3b)', () => {
  const base = state()
  base.snapshot.work_items.impl.acceptance = 'accepted'
  base.contract.current_review_id = 'review-1'
  base.contract.reviews['review-1'] = { review_id: 'review-1', candidate_id: 'candidate', manifest_digest: 'a'.repeat(64),
    requirement_revision: 1, evidence_revision: 2, reviewer_id: null, record: { verdict: 'pass' } }
  base.contract.accepted.push({ item_id: 'impl', candidate_id: 'candidate', review_id: 'review-1' })
  // Accepted, but the tester item is still submitted and the lease is held.
  const pending = completionStatus(base)
  assert.equal(pending.acceptance.status, 'accepted')
  assert.equal(pending.finalization, 'accepted+cleanup_pending')
  assert.equal(pending.cleanup_pending, true)
  // Drain everything: tester terminated, no lease, no open slots.
  const finished = JSON.parse(JSON.stringify(base))
  finished.snapshot.work_items.test.acceptance = 'terminated'
  finished.snapshot.open_worker_slots_used = 0
  finished.lease = null
  const done = completionStatus(finished)
  assert.equal(done.finalization, 'accepted+finished')
  assert.equal(done.cleanup_pending, false)
  assert.equal(done.attention_required, false)
  // Never accepted: finalization stays not_accepted regardless of cleanup.
  const unaccepted = completionStatus(state())
  assert.equal(unaccepted.finalization, 'not_accepted')
})
