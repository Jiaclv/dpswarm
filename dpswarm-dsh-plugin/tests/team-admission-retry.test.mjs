import assert from 'node:assert/strict'
import test from 'node:test'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { workerDiagnostics } from '../lib/worker-diagnostics.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { createUserMessage } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-llm/lib/index.js'))

const budget = (rootId, sessionId) => ({ async diagnosticsForSession(id) {
  assert.equal(id, sessionId)
  return { root_session_id: rootId, worker_session_id: sessionId, frozen: true, mode: 'fixed',
    calls: 0, unknown_usage_calls: 0, active_calls: 0, committed_tokens: 0, observed_tokens_lower_bound: 0,
    audit_warning: null }
} })
const nativeSession = (rootId, id) => ({ id, header: { parentSession: rootId, origin: 'subagent', delegationDepth: 1 }, events: [{
  seq: 1, type: 'turn/end', data: { reason: { kind: 'error', error: { code: 'UNKNOWN', message: 'WORKER_TOKEN_RESERVATION_DENIED: insufficient reservation' } } },
}] })
async function deniedDiagnostic(rootId, sessionId, role) {
  return workerDiagnostics({ session: nativeSession(rootId, sessionId), sessionId, rootId, role,
    cleanup: { physical_cleanup_confirmed: true }, budget: budget(rootId, sessionId) })
}
function fixture(run) {
  const rootId = 'root', journal = new MemoryAuditJournal(), cfg = { enabledSessions: [rootId] }
  const session = { id: rootId, header: { delegationDepth: 0 }, events: [{ type: 'user/message', data: createUserMessage({
    source: { kind: 'user' }, content: [{ type: 'text', text: 'Create the requested artifact.' }],
  }) }] }
  const agent = { id: rootId, session }, requirement = new TeamRequirement({ config: () => cfg, journal })
  return { rootId, journal, agent, requirement, dispatcher: new TeamDispatcher({ requirement, controller: { run } }), exec: { agent } }
}

test('only complete durable zero-call reservation denials release the same binding for a new attempt', async () => {
  let attempt = 0
  const h = fixture(async (args, exec, { taskBinding, onChildStarted }) => {
    attempt++
    const runId = `run-${attempt}`, roles = ['implementer', 'tester', 'reviewer']
    for (const role of roles) {
      const sessionId = `${runId}-${role}`
      await onChildStarted({ run_id: runId, execution_session_id: sessionId, role })
      if (attempt === 1) {
        const diagnostic = await deniedDiagnostic(h.rootId, sessionId, role)
        await h.journal.append(h.rootId, 'dpswarm/worker-diagnostic', { root_session_id: h.rootId, owner_session_id: sessionId,
          worker_session_id: sessionId, run_id: runId, diagnostic })
      }
    }
    if (attempt === 2) return { mode: 'fixed-team-v1', deliveries: [{ role: 'implementer' }], failed: [], stopped: false }
    return { mode: 'fixed-team-v1', deliveries: [], stopped: false, failed: roles.map(role => {
      const sessionId = `${runId}-${role}`
      return { execution_session_id: sessionId, diagnostic: null, control_settlement: { ok: true },
        details: { sessionId, physicalCleanupConfirmed: true } }
    }) }
  })
  // Populate each result diagnostic from the real collector after all children were published.
  const original = h.dispatcher.controller.run
  h.dispatcher.controller.run = async (...args) => {
    const result = await original(...args)
    if (attempt === 1) for (const item of result.failed) item.diagnostic = await deniedDiagnostic(h.rootId, item.execution_session_id, item.execution_session_id.split('-').at(-1))
    return result
  }
  const retryResult = await h.dispatcher.run({}, h.exec)
  assert.equal(retryResult.retry_allowed, true); assert.match(retryResult.next, /call dpswarm_run again/)
  const first = await h.requirement.beforeRun(h.agent)
  assert.equal(first.phase, 'required'); assert.equal(first.retry.outcome, 'retryable_admission_failure')
  assert.equal(first.starts.length, 0); assert.equal(first.allStarts.length, 3)
  assert.match((await h.requirement.status(h.agent)).next, /call dpswarm_run again/)
  await assert.rejects(h.requirement.finishRun(first.binding, { outcome: 'failed_takeover', run_id: 'run-1' }), { code: 'TEAM_REQUIRED_FINISH_INVALID' })
  await h.dispatcher.run({}, h.exec)
  const settled = await h.requirement.beforeRun(h.agent)
  assert.equal(settled.phase, 'finished'); assert.equal(settled.finish.run_id, 'run-2')
})

test('missing or nonzero durable usage leaves a published admission failed closed', async () => {
  const h = fixture(async (args, exec, { onChildStarted }) => {
    await onChildStarted({ run_id: 'run-1', execution_session_id: 'child-1', role: 'implementer' })
    const diagnostic = await deniedDiagnostic(h.rootId, 'child-1', 'implementer')
    diagnostic.budget.calls = 1
    return { mode: 'fixed-team-v1', deliveries: [], stopped: false, failed: [{ execution_session_id: 'child-1', diagnostic,
      control_settlement: { ok: true }, details: { sessionId: 'child-1', physicalCleanupConfirmed: true } }] }
  })
  await h.dispatcher.run({}, h.exec)
  const state = await h.requirement.beforeRun(h.agent)
  assert.equal(state.phase, 'finished'); assert.equal(state.finish.outcome, 'failed_takeover')
  await assert.rejects(h.dispatcher.run({}, h.exec), { code: 'TEAM_REQUIRED_ALREADY_FULFILLED' })
})

test('closed attempts reject late child publication and terminal callbacks remain idempotent', async () => {
  const h = fixture(async () => { throw new Error('unused') }), binding = await h.requirement.beforeRun(h.agent)
  for (const runId of ['run-a', 'run-b']) {
    const sessionId = `${runId}-child`
    await h.requirement.markStarted(binding, { run_id: runId, execution_session_id: sessionId, role: 'implementer' })
    const diagnostic = await deniedDiagnostic(h.rootId, sessionId, 'implementer')
    await h.journal.append(h.rootId, 'dpswarm/worker-diagnostic', { root_session_id: h.rootId, owner_session_id: sessionId,
      worker_session_id: sessionId, run_id: runId, diagnostic })
    await h.requirement.finishAdmissionFailure(binding, { run_id: runId, execution_session_ids: [sessionId], no_delivery: true })
    await h.requirement.finishAdmissionFailure(binding, { run_id: runId, execution_session_ids: [sessionId], no_delivery: true })
  }
  await assert.rejects(h.requirement.markStarted(binding, { run_id: 'run-a', execution_session_id: 'late-a', role: 'implementer' }), { code: 'TEAM_REQUIRED_ATTEMPT_CLOSED' })
  await h.requirement.markStarted(binding, { run_id: 'run-c', execution_session_id: 'run-c-child', role: 'implementer' })
  await h.requirement.finishRun(binding, { outcome: 'failed_takeover', run_id: 'run-c' })
  await h.requirement.finishRun(binding, { outcome: 'failed_takeover', run_id: 'run-c' })
  assert.equal((await h.requirement.beforeRun(h.agent)).phase, 'finished')
})