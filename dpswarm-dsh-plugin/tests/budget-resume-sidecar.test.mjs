import assert from 'node:assert/strict'
import test from 'node:test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { Sidecar } from '../lib/sidecar.js'
import { AuditJournal } from '../lib/audit.js'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const signal = () => new AbortController().signal

test('real Python Sidecar and AuditJournal persist recovery claims, enforce CAS, and revoke unbound grants', async t => {
  const workspace = await mkdtemp(join(tmpdir(), 'dpswarm-budget-resume-'))
  const processChild = spawn(process.env.DPSWARM_TEST_PYTHON || 'python',
    [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), workspace],
    { cwd: repo, env: { ...process.env }, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''
  processChild.stderr.on('data', part => { stderr += part })
  const lines = createInterface({ input: processChild.stdout })
  t.after(async () => {
    if (processChild.exitCode === null) {
      const exited = once(processChild, 'exit')
      processChild.stdin.end('{"command":"stop"}\n')
      const timer = setTimeout(() => processChild.kill(), 5000)
      try { await exited } finally { clearTimeout(timer) }
    }
    lines.close()
  })
  const ready = await Promise.race([once(lines, 'line').then(([line]) => JSON.parse(line)),
    once(processChild, 'exit').then(() => { throw new Error(stderr || 'Python sidecar exited before ready') })])
  const root = { id: 'resume-audit-root', header: { id: 'resume-audit-root' }, events: [] }
  const sessions = new Map([[root.id, root]]), parent = { session: root }
  const cfg = { workspace, sidecarUrl: `http://127.0.0.1:${ready.port}`, autoStart: false,
    workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 4 }
  const sidecarFactory = rootId => new Sidecar({ ...cfg, sessionId: rootId, sessionIsolation: true,
    auditJournalRequired: true, runtimeCapabilitiesRequired: true })
  const journal = new AuditJournal({ config: () => cfg, sidecarFactory })
  const rivalJournal = new AuditJournal({ config: () => cfg, sidecarFactory })
  const runtime = journal => new WorkerBudgetRuntime({ config: () => cfg, journal,
    resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()] })
  const r = runtime(journal), rival = runtime(rivalJournal)
  const child = (id, prompt) => {
    const session = { id, header: { id, parentSession: root.id, origin: 'subagent', delegationDepth: 1, seedLength: 0 },
      events: [{ type: 'user/message', seq: 0, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: prompt }] } }] }
    sessions.set(id, session)
    return session
  }
  const original = await r.beginTeamRun(parent, { roles: ['implementer', 'tester', 'reviewer'] })
  await r.finishTeamRun(parent, original)
  await journal.append(root.id, 'dpswarm/verification-recovery', { version: 1, root_session_id: root.id,
    owner_session_id: root.id, checkpoint_id: 'checkpoint-fixture', run_id: 'controller-run', budget_run_id: original.run_id,
    phase: 'ready', stage: 'capture', candidate_id: null, roles: ['tester', 'reviewer'], source_items: [] })
  const request = { runId: original.run_id, roles: ['tester', 'reviewer'], expectedProfile: original.profile }
  const race = await Promise.allSettled([r.resumeTeamRun(parent, request), rival.resumeTeamRun(parent, request)])
  assert.equal(race.filter(result => result.status === 'fulfilled').length, 1)
  assert.equal(race.find(result => result.status === 'rejected').reason.code, 'WORKER_BUDGET_RESUME_ALREADY_CLAIMED')
  const winnerIndex = race.findIndex(result => result.status === 'fulfilled'), winner = winnerIndex === 0 ? r : rival
  const handle = race[winnerIndex].value
  const tester = await winner.issueTeamWorker(parent, handle, { label: 'tester', task: 'Verify saved output.' })
  const reviewer = await winner.issueTeamWorker(parent, handle, { label: 'reviewer', task: 'Review candidate evidence.' })
  const testSession = child('tester-worker', tester.prompt), state = await winner.ensure({ session: testSession }, signal())
  await winner.settle(await winner.admit(state, { provider: 'fixture', model: 'offline', messages: [], maxTokens: 100 }),
    { inputTokens: 75, outputTokens: 25 }, 'stop')
  await winner.finishTeamRun(parent, handle)
  await assert.rejects(winner.ensure({ session: child('never-started-reviewer', reviewer.prompt) }, signal()), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
  const restarted = once(lines, 'line')
  processChild.stdin.write('{"command":"restart"}\n')
  assert.deepEqual(JSON.parse((await restarted)[0]), { restarted: true })
  const coldJournal = new AuditJournal({ config: () => cfg, sidecarFactory }), cold = runtime(coldJournal)
  const restored = await cold.ensure({ session: testSession }, signal())
  assert.equal(cold.describe(restored).remaining_tokens, 900)
  assert.equal(cold.describe(restored).remaining_calls, 3)
  await assert.rejects(cold.resumeTeamRun(parent, request), { code: 'WORKER_BUDGET_RESUME_ALREADY_CLAIMED' })
  assert.deepEqual(await cold.teamRunRecoveryStatus(parent, { runId: original.run_id }),
    { claimed: true, resume_id: handle.resume_id, ended: true, issued_roles: ['tester', 'reviewer'] })
  const saved = await coldJournal.read(root.id)
  for (const type of ['dpswarm/worker-budget-team-run-resumed', 'dpswarm/worker-budget-team-run-resume-ended', 'dpswarm/verification-recovery']) {
    assert.equal(saved.events.filter(event => event.type === type).length, 1)
  }
  assert.equal(saved.events.filter(event => event.type === 'dpswarm/worker-budget-team-run').length, 1)
  assert.equal(saved.events.filter(event => event.type === 'dpswarm/worker-budget-allocation').length, 2)
})
