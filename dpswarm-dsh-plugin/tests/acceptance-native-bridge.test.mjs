import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { Sidecar } from '../lib/sidecar.js'
import { FixedTeamController } from '../lib/fixed-team.js'
import { modelToolView } from '../lib/tool-view.js'
import { hostModuleUrl, resolveHostRoot } from '../lib/host-modules.js'
const { isJsonValue } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-util-values/lib/index.js'))
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { HostModelRegistry } from '../lib/host-model-registry.js'
import { WriteScopeRegistry, installWriteScope } from '../lib/write-scope.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

// These are protocol/lifecycle tests: Python HTTP/EventStore and budget accounting
// are real; native model output is deterministic fixture data, with no provider call.
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const rows = value => Array.isArray(value) ? value : Object.values(value || {})
const fence = report => 'Deterministic offline protocol fixture; no model quality claim.\n```' + report.schema + '\n' + JSON.stringify(report) + '\n```'
const userMessage = (id, text, seq) => ({ seq, type: 'user/message', data: { id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] } })

async function fixture(t, { reviewerReport = 'valid', reviewerMode = 'model', mode = 'serial', consumeUpstream = true, capacityPoints, externalObservation = false, captureFailures = 0, independent = false, testerFinding = false } = {}) {
  const workspace = await mkdtemp(join(tmpdir(), 'dpswarm-acceptance-native-')), cwd = join(workspace, 'project')
  await mkdir(join(cwd, 'assets'), { recursive: true })
  await writeFile(join(cwd, 'assets/motion.js'), 'const fixtureMotion=1;')
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python', [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), workspace],
    { cwd: repo, env: { ...process.env }, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''; child.stderr.on('data', part => { stderr += part })
  const lines = createInterface({ input: child.stdout }), queue = [], waiters = []
  lines.on('line', line => { const value = JSON.parse(line); if (waiters.length) waiters.shift()(value); else queue.push(value) })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve))
  const h = { workspace, cwd, child, children: [], sessions: new Map(), calls: [], reviewerReport, implementationVersion: 1, outputs: [], nextChildId: 0, mode, consumeUpstream, captureFailures, captureAttempts: 0, testerFinding, reviewerPrompts: [] }
  t.after(async () => {
    try { await h.controller?.shutdown(); await h.budget?.shutdown?.() } finally {
      if (child.exitCode === null) {
        const exited = once(child, 'exit'); child.stdin.end('{"command":"stop"}\n')
        const timer = setTimeout(() => child.kill(), 5000)
        try { await exited } finally { clearTimeout(timer) }
      }
      lines.close()
    }
  })
  const { port } = await Promise.race([next(), once(child, 'exit').then(() => { throw new Error(stderr || 'Python sidecar exited before ready') })])
  const cfg = { workspace, sidecarUrl: `http://127.0.0.1:${port}`, autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    reviewerMode, reviewerProvider: 'fixture', reviewerModel: 'reviewer',
    workerTimeoutSeconds: 60, workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28,
    reworkBudgetMode: 'unlimited', teamModeOverrides: [{ sessionId: 'root', mode }],
    ...(independent ? { reviewerIndependence: 'independent' } : {}) }
  const original = 'Create an HTML delivery using the existing local motion script. Keep both the page and its script verifiable.'
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [userMessage('user-original', original, 0)], route: { provider: 'fixture', model: 'lead', reasoningEffort: 'max' },
    requestHeader() { return { config: this.route } } } }
  const journal = new MemoryAuditJournal()
  const sidecarFactory = config => {
    const sidecar = new Sidecar({ ...config, workspace, sidecarUrl: cfg.sidecarUrl }), call = sidecar.call.bind(sidecar)
    sidecar.call = async (method, path, body, ...rest) => {
      const result = await call(method, path, body, ...rest)
      h.calls.push({ method, path, body: structuredClone(body), result: structuredClone(result) })
      await h.onSidecarResult?.(method, path, body, result)
      return result
    }
    return sidecar
  }
  // Production shares one sidecar-backed audit store between the budget
  // runtime and the controller. Keep the in-memory journal authoritative for
  // existing assertions and mirror every committed write into a sidecar-backed
  // journal so status views (usage ledger) observe worker events like prod.
  const AuditJournalMod = await import('../lib/audit.js')
  const sidecarJournal = new AuditJournalMod.AuditJournal({ config: () => ({ ...cfg, sessionId: 'root' }) })
  let journalChain = Promise.resolve()
  const dualJournal = {
    read: (id) => journal.read(id),
    append: (id, type, data) => { const run = async () => { const record = await journal.append(id, type, data); await sidecarJournal.append(id, type, data); return record }; journalChain = journalChain.then(run, run); return journalChain },
    transaction: (id, work) => { const run = async () => { const outcome = await journal.transaction(id, work); for (const record of outcome.events || []) await sidecarJournal.append(id, record.type, record.data); return outcome }; journalChain = journalChain.then(run, run); return journalChain },
  }
  const budget = new WorkerBudgetRuntime({ config: () => cfg, journal: dualJournal,
    resolveSession: id => id === 'root' ? parent.session : h.sessions.get(id)?.session,
    listSessions: () => [parent.session, ...[...h.sessions.values()].map(agent => agent.session)] })
  const subagents = { async start(provider, request) {
    const id = `native-fixture-${h.nextChildId++}`, model = request.agentOptions.model
    const role = model === 'tester' ? 'tester' : model === 'reviewer' ? 'reviewer' : 'implementer'
    const session = { id, header: { id, parentSession: 'root', origin: 'subagent', delegationDepth: 1, cwd },
      events: [{ seq: 0, type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: request.prompt } }] }
    h.sessions.set(id, { id, session })
    const executeFixture = async () => {
    if (role === 'implementer' && h.deadFirstImplementer) {
      // Live session 88122af1 shape: the first implementer request dies at the
      // provider (zero-usage 400, terminal INVALID_REQUEST) before any tester
      // exists. The budget still freezes and settles its zero-usage call, and
      // dispose succeeds, so cleanup stays confirmable.
      h.deadFirstImplementer = false
      const allowance = await budget.ensure({ session }, request.signal)
      const ticket = await budget.admit(allowance, { provider: request.agentOptions.provider, model, messages: [], maxTokens: 100 })
      await budget.settle(ticket, { inputTokens: 0, outputTokens: 0 }, 'failed')
      session.events.push({ seq: session.events.length, type: 'turn/end', data: { reason: { kind: 'error',
        error: { code: 'INVALID_REQUEST', message: '400: {"code":"1210","message":"max_tokens参数非法：限制数值范围[1,131072]"}' } } }, time: Date.now() })
      return { output: [], stopReason: 'error' }
    }
    if (role === 'implementer' && mode !== 'serial') {
      const deadline = Date.now() + 10000
      while (!h.writeScope.forSession(id) || (mode === 'staged' && !h.calls.some(call => call.path === '/api/artifact/state'
        && call.body?.artifact_id === h.writeScope.forSession(id)?.subtask && call.body?.to === 'claimed'))) {
        if (Date.now() >= deadline) throw new Error('Native fixture claim was not published before managed work')
        request.signal?.throwIfAborted()
        await new Promise(resolve => setTimeout(resolve, 5))
      }
    }
    const allowance = await budget.ensure({ session }, request.signal)
    const ticket = await budget.admit(allowance, { provider: request.agentOptions.provider, model, messages: [], maxTokens: 100 })
    await budget.settle(ticket, { inputTokens: 20, outputTokens: 10 }, 'completed')
    let output
    if (role === 'implementer') {
      const claim = h.writeScope.forSession(id)
      const entry = mode === 'serial' ? 'index.html' : claim.subtask + '.html'
      const workerExec = { agent: { session }, signal: request.signal }
      if (mode === 'staged') {
        await h.controller.artifactState({ to: 'draft' }, workerExec)
        if (claim.subtask === 'downstream' && consumeUpstream) {
          const read = { ...workerExec, name: 'read', args: { file_path: 'upstream.html' } }
          const upstream = await h.managedHook(read, () => readFile(read.args.file_path, 'utf8'))
          assert.match(upstream, /fixture upstream/)
        }
      }
      const body = '<html><body>fixture ' + (claim?.subtask || 'version') + ' ' + h.implementationVersion + '</body>'
        + (mode === 'staged' && claim.subtask === 'downstream' ? '<iframe src="upstream.html"></iframe>' : '<script src="assets/motion.js"></script>') + '</html>'
      if (!h.skipImplementationWrite) {
      const write = { ...workerExec, name: 'write', args: { file_path: entry } }
      await h.managedHook(write, () => writeFile(join(cwd, entry), body))
      const callId = id + '-write'
      session.events.push({ type: 'tool/call', data: { name: 'write', callId, arguments: JSON.stringify({ file_path: entry }), turn: 1, step: 1 } },
        { type: 'tool/result', data: { turn: 1, step: 1, message: { role: 'user', source: { kind: 'tool', callId }, content: [{ type: 'tool-result', toolCallId: callId, isError: false, output: 'Fixture write saved.' }] } } })
      }
      if (externalObservation) {
        const path = join(workspace, 'native-diagnostics', 'verify.js')
        await mkdir(dirname(path), { recursive: true })
        await h.managedHook({ ...workerExec, name: 'write', args: { file_path: path } }, () => writeFile(path, '/* deterministic external verification helper */'))
        const outsideCallId = id + '-external-write'
        session.events.push({ type: 'tool/call', data: { name: 'write', callId: outsideCallId,
          arguments: JSON.stringify({ file_path: path }), turn: 1, step: 1 } },
          { type: 'tool/result', data: { turn: 1, step: 1, message: { role: 'user', source: { kind: 'tool', callId: outsideCallId },
            content: [{ type: 'tool-result', toolCallId: outsideCallId, isError: false, output: 'Fixture external helper saved.' }] } } })
        h.externalPath = path
      }
      if (mode === 'staged') await h.controller.artifactState({ to: 'ready', candidate_paths: [entry] }, workerExec)
      output = h.skipImplementationWrite ? 'Inspected the existing delivery without writing files.' : 'Saved ' + entry + '; local dependency paths remain explicit. Deterministic fixture write.'
    } else {
      const promptText = request.prompt[0].text
      if (role === 'reviewer') h.reviewerPrompts.push(promptText)
      const isConvergence = role === 'reviewer' && /Convergence review/.test(promptText)
      const state = h.controller.sessions.get('root'), a = state.acceptance, candidate = a?.candidate
      assert.ok(candidate?.candidate_id, 'A runtime candidate must exist before verifier dispatch')
      assert.ok(request.prompt[0].text.includes(candidate.candidate_id), 'Verifier prompt is bound to the current candidate')
      const snapshot = a.local_snapshot || candidate.snapshot
      assert.equal(await readFile(join(snapshot.view_path, 'assets/motion.js'), 'utf8'), 'const fixtureMotion=1;', 'Verifier reads the sealed dependency')
      // The negotiated v2 contract: a plan, completed fixture checks and
      // explicit finding dispositions (reports/2026-09-13 P1/P2a).
      const planChecks = a.contract.requirements.map(row => ({ id: `check-${row.id}`, requirement_ids: [row.id],
        method: 'static:fixture-read', required_for_claim: true, rationale: 'Deterministic fixture read of the sealed bytes.' }))
      const report = { schema: 'dpswarm-review-v2', candidate_id: candidate.candidate_id,
        manifest_digest: candidate.manifest_digest, requirement_revision: a.contract.requirement_revision,
        evidence_revision: a.evidence_revision, consumed_manifest_refs: snapshot.consumed_manifest_refs || [],
        verdict: 'pass',
        verification_plan: { revision: 1, coverage: 'Fixture static reads cover every enrolled requirement against the sealed candidate.', checks: planChecks },
        check_results: a.contract.requirements.map(row => ({ check_id: `check-${row.id}`, method: 'static:fixture-read',
          execution_status: 'completed', evidence_refs: ['Fixture read of immutable HTML and local JS bytes'], limitations: [],
          environment_ref: 'sealed-view', candidate_id: candidate.candidate_id, manifest_digest: candidate.manifest_digest,
          requirement_revision: a.contract.requirement_revision, verification_plan_revision: 1 })),
        requirements: a.contract.requirements.map(row => ({ id: row.id, result: 'met', evidence: ['Fixture read of immutable HTML and local JS bytes'],
          verification_plan_revision: 1, check_refs: [`check-${row.id}`] })),
        findings: rows(a.contract.findings).map(finding => ({ id: finding.id, requirement_ids: finding.requirement_ids,
          paths: finding.paths || finding.history?.at(-1)?.decision?.paths || [], observation: finding.observation,
          classification: finding.classification || finding.history?.at(-1)?.decision?.classification || 'unknown',
          state: 'verified-resolved', evidence: ['Fixture current snapshot check'], reason: 'The deterministic repaired fixture satisfies the retained requirement.',
          disposition: 'verified_resolved' })), unavailable_checks: [] }
      if (role === 'tester' && h.testerFinding) {
        report.findings = [{ id: 'F1', requirement_ids: ['user-task'], paths: ['index.html'], observation: 'Tester observed the wheel spokes render inverted at 90deg.',
          classification: 'defect', state: 'open', evidence: ['tester frame inspection'], reason: 'Found during executed checks.', disposition: 'fix_required' }]
        report.verdict = 'needs-rework'
      }
      if (isConvergence && h.testerFinding && report.findings.length) {
        // R18: the convergence verdict must rest on the tester finding.
        report.findings = report.findings.map(row => row.id === 'F1' ? { ...row, state: 'open', disposition: 'fix_required' } : row)
        report.verdict = 'needs-rework'
      }
      if (role === 'reviewer' && h.reviewerReport === 'open-finding') {
        report.verdict = 'blocked'
        report.findings = [{ id: 'F1', requirement_ids: ['user-task'], paths: ['index.html'], observation: 'Motion has not been observed for this fixture.',
          classification: 'unknown', state: 'open', evidence: ['Deterministic fixture has no motion observation.'], reason: 'Verification remains incomplete.',
          disposition: 'pending' }]
      }
      if (role === 'reviewer' && h.reviewerReport === 'missing') output = 'VERDICT: pass\nLegacy unstructured report intentionally has no required JSON.'
      else if (role === 'reviewer' && h.reviewerReport === 'old') { report.candidate_id = 'old-candidate'; report.manifest_digest = '0'.repeat(64); output = fence(report) }
      else output = fence(report)
    }
    h.outputs.push({ id, role, output })
    session.events.push({ type: 'turn/end', data: { reason: { kind: 'completed' } }, time: Date.now() })
    session.events.forEach((event, seq) => { event.seq = seq })
    return { output: [{ type: 'text', text: output }], stopReason: 'completed' }
    }
    const native = { id, provider, request, session, localAgent: { session },
      result: Promise.resolve().then(executeFixture), async dispose() {
        if (h.failDisposeRole === role) throw new Error('Deterministic native quiescence failure for ' + role)
      } }
    h.children.push(native)
    return native
  } }
  const modelRegistry = new HostModelRegistry(() => ({ async resolveCallConfig(route) { return route } }))
  const writeScope = new WriteScopeRegistry({ journal })
  h.writeScope = writeScope
  installWriteScope({ on: (_event, fn) => { h.managedHook = fn; return () => {} } }, writeScope)
  h.controller = new FixedTeamController({ config: () => cfg, budget, subagents, modelRegistry, writeScope, journal: dualJournal,
    sidecarFactory, resolveSession: id => h.sessions.get(id) })
  const capture = h.controller.acceptanceRuntime.capture.bind(h.controller.acceptanceRuntime)
  h.controller.acceptanceRuntime.capture = async (...args) => {
    h.captureAttempts++
    if (h.captureFailures > 0) { h.captureFailures--; throw Object.assign(new Error('CANDIDATE_PUBLISH_FAILED: deterministic one-shot capture failure'), { code: 'CANDIDATE_PUBLISH_FAILED' }) }
    return capture(...args)
  }
  h.requirement = new TeamRequirement({ config: () => cfg, journal })
  h.dispatcher = new TeamDispatcher({ controller: h.controller, requirement: h.requirement })
  h.parent = parent; h.cfg = cfg; h.budget = budget; h.journal = journal; h.sidecarFactory = sidecarFactory
  h.restart = async () => { child.stdin.write('{"command":"restart"}\n'); assert.equal((await next()).restarted, true) }
  h.exec = { agent: parent, signal: new AbortController().signal }
  h.run = () => h.dispatcher.run({ task: 'Implement the HTML entry only; preserve the existing JS dependency.', acceptance: 'Whole HTML delivery and local runtime dependency.',
    requirements: [{ id: 'local-script', description: 'The HTML retains and loads its local motion script.', mandatory: true }],
    candidate_paths: mode === 'serial' ? ['index.html'] : mode === 'parallel' ? ['part-a.html', 'part-b.html'] : ['upstream.html', 'downstream.html'], output_kind: 'files',
    ...(mode === 'parallel' ? { subtasks: [{ id: 'part-a', task: 'Create part-a.html with the local script.', write_scope: ['part-a.html'] },
      { id: 'part-b', task: 'Create part-b.html with the local script.', write_scope: ['part-b.html'] }] } : {}),
    ...(mode === 'staged' ? { staged: { phases: [{ id: 'first', task: 'Build upstream.' }, { id: 'second', task: 'Build downstream from upstream.' }],
      artifacts: [{ id: 'upstream', title: 'Upstream', task: 'Create upstream.html with the local script.', write_globs: ['upstream.html'], phase: 'first' },
        { id: 'downstream', title: 'Downstream', task: 'Read upstream.html through the managed read tool, then create downstream.html.', write_globs: ['downstream.html'], phase: 'second', deps: ['upstream'] }] } } : {}) }, h.exec)
  h.acceptance = () => h.controller.acceptanceStatus({}, h.exec)
  h.contract = async () => {
    const state = h.controller.sessions.get('root'), all = await state.sidecar.call('GET', '/api/acceptance')
    return rows(all.contracts).find(row => row.contract_id === state.acceptance.id)
  }
  h.accept = async (result, extra = {}) => {
    // Verifier work-item cleanup does not constitute production acceptance.
    for (const delivery of result.deliveries.filter(row => row.role !== 'implementer')) await h.dispatcher.review({ item_id: delivery.item_id, verdict: 'accept' }, h.exec)
    let accepted
    for (const delivery of result.deliveries.filter(row => row.role === 'implementer')) accepted = await h.dispatcher.review({ item_id: delivery.item_id, verdict: 'accept', ...extra }, h.exec)
    return accepted
  }
  if (capacityPoints !== undefined) {
    // The complete split team retains submitted workers until final acceptance.
    // Configure the test premise explicitly; production must never raise it silently.
    const control = sidecarFactory({ ...cfg, sessionId: 'root' })
    const receipt = await modelRegistry.resolve([{ role: 'lead', ...parent.session.route },
      { role: 'tester', provider: 'fixture', model: 'tester' }, { role: 'reviewer', provider: 'fixture', model: 'reviewer' }])
    await modelRegistry.publish(control, receipt)
    await control.call('POST', '/api/execution/root', { parent_session_id: 'root', delegation_depth: 0, provider: 'fixture', model: 'lead' })
    await control.call('POST', '/api/spec', { max_active_node_points: capacityPoints })
    assert.equal((await control.call('GET', '/api/status')).spec.max_active_node_points, capacityPoints)
  }
  return h
}

test('native strict serial bridge seals dependencies, records bound tester/reviewer reports and accepts atomically', { timeout: 90000 }, async t => {
  const h = await fixture(t), result = await h.run()
  assert.deepEqual(result.failed, [])
  assert.deepEqual(result.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  const before = await h.acceptance(), candidate = before.candidate
  assert.equal(before.available, true)
  assert.deepEqual(candidate.snapshot.entry_paths, ['index.html'])
  assert.deepEqual(candidate.snapshot.changed_paths, ['index.html'])
  assert.deepEqual(candidate.snapshot.candidate_files.map(file => file.path), ['assets/motion.js', 'index.html'])
  assert.equal(candidate.snapshot.dependency_complete, true)
  assert.ok(before.review?.review_id, JSON.stringify(before.report_error ?? before))
  await h.accept(result)
  const contract = await h.contract()
  assert.ok(contract.accepted.some(row => row.candidate_id === candidate.candidate_id))
  assert.equal(h.children.length, 3, 'Only configured mock-native roles ran')
  assert.ok(h.calls.some(call => call.path === '/api/acceptance' && call.body?.action === 'review'))
})

for (const mode of ['missing', 'old']) test(`native strict bridge retains and refuses ${mode} reviewer report`, { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: mode }), result = await h.run()
  assert.equal(result.deliveries.length, 3)
  const original = h.outputs.find(row => row.role === 'reviewer').output
  await assert.rejects(h.accept(result), error => /ACCEPTANCE|REVIEW|CANDIDATE|EVIDENCE/.test(error.code || error.message))
  const contract = await h.contract()
  assert.deepEqual(contract.accepted, [])
  assert.ok(rows(contract.evidence).some(row => row.identity?.report_hash === createHash('sha256').update(original).digest('hex')), 'Invalid raw report remains in authoritative evidence')
})

test('native bridge rejects workspace dependency drift after successful snapshot review', { timeout: 90000 }, async t => {
  const h = await fixture(t), result = await h.run(), before = await h.acceptance()
  await writeFile(join(h.cwd, 'assets/motion.js'), 'const fixtureMotion=2;')
  await assert.rejects(h.accept(result), { code: 'CANDIDATE_STALE' })
  const contract = await h.contract()
  assert.deepEqual(contract.accepted, [])
  assert.equal(before.candidate.manifest_digest, (await h.acceptance()).candidate.manifest_digest)
})

test('trusted user amendment remains in the original lineage and supports native rework', { timeout: 90000 }, async t => {
  const h = await fixture(t), first = await h.run(), initial = await h.contract()
  h.parent.session.events.push(userMessage('user-amendment', 'Keep this same HTML delivery, but label it as the static version. Retain the existing local script.', 1))
  const amendment = await h.dispatcher.amend({ requirements: [{ id: 'static-label', description: 'Label this delivery as the static version.', mandatory: true }], reason: 'Apply the actual new user message to this same delivery.' }, h.exec)
  assert.equal(amendment.contract_revision, initial.requirement_revision + 1)
  h.implementationVersion = 2
  const second = await h.dispatcher.rework({ item_id: first.deliveries[0].item_id, feedback: 'Apply the new static version label; preserve the local script.' }, h.exec)
  assert.deepEqual(second.failed, [])
  const current = await h.contract()
  assert.equal(current.task_lineage, initial.task_lineage)
  assert.equal(current.requirement_revision, initial.requirement_revision + 1)
  assert.notEqual(current.current_candidate_id, initial.current_candidate_id)
  assert.ok(h.children.at(-1).request.prompt[0].text.includes('user-amendment'))
  await h.accept(second)
})

test('report-only native continuation preserves candidate bytes and registers a new report identity', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: 'missing' }), first = await h.run(), before = await h.acceptance()
  const reviewer = first.deliveries.find(row => row.role === 'reviewer'), previousOutput = h.outputs.find(row => row.role === 'reviewer').output
  h.reviewerReport = 'valid'
  const repaired = await h.dispatcher.repairReport({ item_id: reviewer.item_id, feedback: 'Return the required current-candidate JSON block; do not edit production files.' }, h.exec)
  const after = await h.acceptance(), contract = await h.contract()
  assert.equal(after.candidate.candidate_id, before.candidate.candidate_id)
  assert.equal(after.candidate.manifest_digest, before.candidate.manifest_digest)
  assert.equal(h.children.length, 4, 'One report-only mock-native continuation; no repeated implementation/tester')
  assert.ok(after.review?.review_id, JSON.stringify(after.report_error ?? after))
  assert.ok(rows(contract.evidence).some(row => row.identity?.report_hash === createHash('sha256').update(previousOutput).digest('hex')))
  const currentReviewer = repaired.deliveries?.find(row => row.role === 'reviewer') || repaired.deliveries?.at(-1) || repaired.delivery || repaired
  assert.ok(currentReviewer.item_id && currentReviewer.item_id !== reviewer.item_id, 'New bound review identity preserves the original report package')
  const result = { deliveries: [...first.deliveries.filter(row => row.role !== 'reviewer'), { ...currentReviewer, role: 'reviewer' }] }
  await h.accept(result)
})


test('cold native controller and restarted Python recover the same candidate and accept its sealed evidence', { timeout: 90000 }, async t => {
  const h = await fixture(t), first = await h.run(), before = await h.acceptance()
  await h.restart()
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  const recovered = await cold.acceptanceStatus({}, h.exec)
  assert.equal(recovered.candidate.candidate_id, before.candidate.candidate_id)
  assert.equal(recovered.candidate.manifest_digest, before.candidate.manifest_digest)
  for (const delivery of first.deliveries.filter(row => row.role !== 'implementer')) await cold.review({ item_id: delivery.item_id, verdict: 'accept' }, h.exec)
  await cold.review({ item_id: first.deliveries[0].item_id, verdict: 'accept' }, h.exec)
  const contract = await h.contract()
  assert.ok(contract.accepted.some(row => row.candidate_id === before.candidate.candidate_id))
})


test('Lead verification without a model Reviewer requires a complete report bound to root execution and candidate', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerMode: 'lead' }), result = await h.run()
  assert.deepEqual(result.failed, [])
  assert.deepEqual(result.deliveries.map(row => row.role), ['implementer', 'tester'])
  await assert.rejects(h.accept(result), { code: 'ACCEPTANCE_REVIEW_REQUIRED' })
  const current = await h.acceptance(), candidate = current.candidate
  // Construct the final report exclusively from the public read-only tool result.
  const discovered = current.review_format
  assert.ok(discovered.json_schema.required.includes('evidence_revision'))
  const filled = structuredClone(discovered.template)
  filled.verdict = 'pass'
  for (const row of filled.requirements) {
    row.result = 'met'; row.evidence = ['Offline Lead fixture checked the exact sealed HTML and local dependency.']
    delete row.reason
  }
  // v2: fill the plan-bound check scaffold honestly before submitting.
  for (const row of filled.check_results || []) {
    row.execution_status = 'completed'
    row.method = 'static:fixture-read'
    row.evidence_refs = ['Offline Lead fixture checked the exact sealed HTML and local dependency.']
  }
  for (const row of filled.findings || []) if (row.disposition === 'pending') row.disposition = 'verified_resolved'
  const report = fence(filled)
  await h.accept(result, { report })
  const contract = await h.contract()
  assert.ok(contract.accepted.some(row => row.candidate_id === candidate.candidate_id))
  assert.equal(h.children.length, 2, 'No independent reviewer was silently added')
})


test('strict parallel native bridge verifies the integrated two-entry candidate through the common acceptance gate', { timeout: 90000 }, async t => {
  const h = await fixture(t, { mode: 'parallel', capacityPoints: 16 }), result = await h.run()
  assert.deepEqual(result.failed, [])
  assert.equal(result.deliveries.filter(row => row.role === 'implementer').length, 2)
  const before = await h.acceptance()
  assert.deepEqual(before.candidate.snapshot.entry_paths, ['part-a.html', 'part-b.html'])
  assert.deepEqual(before.candidate.snapshot.candidate_files.map(file => file.path), ['assets/motion.js', 'part-a.html', 'part-b.html'])
  assert.equal(before.candidate.snapshot.dependency_complete, true)
  assert.ok(before.review?.review_id, JSON.stringify(before.report_error ?? before))
  await h.accept(result)
  assert.equal((await h.contract()).accepted.length, 2)
})

for (const consumeUpstream of [true, false]) test('strict staged native bridge ' + (consumeUpstream ? 'accepts a recorded ready-manifest read' : 'refuses missing declared upstream consumption'), { timeout: 90000 }, async t => {
  const h = await fixture(t, { mode: 'staged', consumeUpstream, capacityPoints: 16 }), result = await h.run()
  assert.deepEqual(result.failed, [])
  const before = await h.acceptance(), snapshot = before.candidate.snapshot
  assert.deepEqual(snapshot.entry_paths, ['downstream.html', 'upstream.html'])
  assert.ok(before.review?.review_id, JSON.stringify(before.report_error ?? before))
  if (consumeUpstream) {
    const consumed = snapshot.consumed_manifest_refs.find(ref => ref.artifact_id === 'upstream')
    assert.ok(consumed)
    assert.deepEqual(consumed.paths, ['upstream.html'])
    assert.equal(snapshot.dependency_complete, true)
    assert.deepEqual(before.review.record.consumed_manifest_refs, snapshot.consumed_manifest_refs)
    await h.accept(result)
    assert.equal((await h.contract()).accepted.length, 2)
  } else {
    assert.equal(snapshot.dependency_complete, false)
    assert.ok(snapshot.unknown_dependencies.some(row => row.reason === 'declared-upstream-consumption-unverified'))
    await assert.rejects(h.accept(result), error => /DEPENDENCY|CANDIDATE/.test(error.code || error.message))
    assert.deepEqual((await h.contract()).accepted, [])
  }
})


test('strict parallel default capacity refuses the full team before any native model call', { timeout: 90000 }, async t => {
  const h = await fixture(t, { mode: 'parallel' })
  await assert.rejects(h.run(), { code: 'FIXED_TEAM_CAPACITY_REQUIRED' })
  assert.equal(h.children.length, 0)
  assert.equal(h.outputs.length, 0)
  const state = h.controller.sessions.get('root')
  const status = await state.sidecar.call('GET', '/api/status')
  assert.equal(status.spec.max_active_node_points, 8, 'The published capacity remains unchanged')
  assert.equal(status.snapshot.open_worker_slots_used, 0, 'No worker item was admitted')
  assert.equal(h.calls.filter(call => call.path === '/api/spec' && call.method === 'POST').length, 0)
})


test('report-only capacity refusal keeps the old evidence and permits retry from the unchanged source remainder', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: 'missing' }), first = await h.run()
  const reviewer = first.deliveries.find(row => row.role === 'reviewer')
  const state = h.controller.sessions.get('root'), source = state.reviewers.get(reviewer.item_id)
  const sourceBudget = await h.budget.ensure({ session: h.sessions.get(source.worker_session_id).session }, h.exec.signal)
  const sourceBefore = h.budget.describe(sourceBudget), before = await h.contract()
  const allocations = () => h.journal.state('root').events.filter(event => event.type === 'dpswarm/worker-budget-allocation'
    && event.data.budget_origin === 'source_remaining_report_repair').map(event => event.data)
  // Close the old control-plane item without replacing its acceptance evidence.
  // Two remaining items then fill the explicitly published two-slot limit.
  await h.dispatcher.review({ item_id: reviewer.item_id, verdict: 'terminate' }, h.exec)
  assert.equal((await state.sidecar.call('GET', '/api/status')).snapshot.open_worker_slots_used, 2)
  // The real admission service rejects the replacement before a native child starts.
  await state.sidecar.call('POST', '/api/spec', { max_open_work_items: 2 })
  await assert.rejects(h.dispatcher.repairReport({ item_id: reviewer.item_id, feedback: 'Supply the current bound report JSON.' }, h.exec),
    { code: 'SLOT_EXCEEDED' })
  assert.equal(h.children.length, 3, 'Rejected admission makes no new native model call')
  const failed = await h.contract(), revoked = allocations()[0]
  assert.ok(revoked)
  assert.deepEqual(failed.candidates[failed.current_candidate_id].roster_evidence,
    before.candidates[before.current_candidate_id].roster_evidence, 'The original reviewer evidence remains current')
  assert.equal(Object.keys(failed.evidence).length, Object.keys(before.evidence).length, 'Unbound refusal adds no fabricated missing report')
  assert.deepEqual(h.budget.describe(sourceBudget), sourceBefore, 'The source allowance is unchanged')
  assert.ok(h.journal.state('root').events.some(event => event.type === 'dpswarm/worker-budget-rework-revoked'
    && event.data.allocation_id === revoked.allocation_id))
  await state.sidecar.call('POST', '/api/spec', { max_open_work_items: 4 })
  h.reviewerReport = 'valid'
  const retry = await h.dispatcher.repairReport({ item_id: reviewer.item_id, feedback: 'Retry the same report after the explicit capacity update.' }, h.exec)
  const grants = allocations()
  assert.equal(grants.length, 2)
  assert.notEqual(grants[0].allocation_id, grants[1].allocation_id)
  assert.deepEqual(grants[1].source_remaining, grants[0].source_remaining, 'Retry reuses the authenticated remainder, with no budget reset')
  assert.deepEqual(grants[1].source_remaining, { tokens: sourceBefore.remaining_tokens, calls: sourceBefore.remaining_calls })
  assert.equal(h.children.length, 4)
  assert.ok((await h.acceptance()).review?.review_id)
  const current = retry.deliveries.find(row => row.role === 'reviewer') || retry.deliveries.at(-1)
  await h.accept({ deliveries: [...first.deliveries.filter(row => row.role !== 'reviewer'), { ...current, role: 'reviewer' }] })
})


test('nine observed malformed Lead takeover patterns fail before reviewer identity or review state changes', { timeout: 90000 }, async t => {
  const h = await fixture(t), result = await h.run(), view = await h.acceptance()
  const before = await h.contract(), item_id = result.deliveries.find(row => row.role === 'implementer').item_id
  const base = () => ({ ...structuredClone(view.review_format.template), verdict: 'pass' })
  const cases = [
    () => ({ ...base(), reviewer: 'lead', requirement_coverage: [], findings_resolution: [], limitations: [] }),
    () => ({ ...base(), coverage: [], limitations: [] }),
    () => { const r = base(); delete r.requirements; return r },
    () => ({ ...base(), requirements: [{ id: 'user-task', status: 'pass' }] }),
    () => ({ ...base(), requirements: view.requirements.map(row => ({ id: row.id, result: 'pass' })) }),
    () => ({ ...base(), requirements: [{ id: 'local-script', result: 'pass' }] }),
    () => ({ ...base(), requirements: [{ id: 'user-task', result: 'pass' }] }),
    () => ({ ...base(), requirements: [{ id: 'user-task', result: 'satisfied' }] }),
    () => ({ ...base(), requirements: [{ requirement_id: 'user-task', result: 'satisfied', notes: 'fixture' }], limitations: [] }),
  ]
  for (const [index, make] of cases.entries()) {
    const writes = h.calls.filter(call => call.path === '/api/acceptance' && call.body?.action === 'takeover').length
    await assert.rejects(h.dispatcher.review({ item_id, verdict: 'accept', takeover: true, reason: 'Explicit offline takeover fixture', report: fence(make()) }, h.exec), error => {
      assert.equal(error.code, 'REVIEW_REPORT_INVALID', 'case ' + (index + 1))
      assert.equal(error.details.authority_changed, false)
      assert.ok(error.details.issues.some(issue => issue.path && issue.code && issue.expected))
      assert.match(error.message, /"authority_changed":false/)
      assert.match(error.message, /"schema_version":"dpswarm-review-v1"/)
      return true
    })
    assert.equal(h.calls.filter(call => call.path === '/api/acceptance' && call.body?.action === 'takeover').length, writes)
    const after = await h.contract()
    for (const key of ['reviewer_id', 'revision', 'review_revision', 'current_review_id']) assert.equal(after[key], before[key], key)
    assert.deepEqual(after.takeovers, before.takeovers)
    assert.deepEqual(after.accepted, [])
  }
})

test('public takeover template retains findings; stale and omitted evidence cannot change authority or accept', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: 'open-finding' }), result = await h.run(), view = await h.acceptance()
  const item_id = result.deliveries.find(row => row.role === 'implementer').item_id
  const before = await h.contract(), original = view.review_format.template
  // v2: placeholders are rejected at the syntax gate; fill only the transport
  // scaffold and keep the unknown/pending semantics for this takeover test.
  for (const row of original.check_results || []) { row.execution_status = 'not_run'; row.method = 'static:fixture-read' }
  assert.equal(original.findings[0].id, 'F1')
  assert.equal(original.findings[0].observation, view.findings[0].observation)
  assert.equal(original.findings[0].state, 'open')
  for (const variant of ['old-candidate', 'old-evidence', 'missing-finding']) {
    const report = structuredClone(original)
    if (variant === 'old-candidate') report.candidate_id = 'old-candidate'
    if (variant === 'old-evidence') report.evidence_revision--
    if (variant === 'missing-finding') report.findings = []
    await assert.rejects(h.dispatcher.review({ item_id, verdict: 'accept', takeover: true, reason: 'Offline takeover fixture', report: fence(report) }, h.exec), error => {
      assert.equal(error.details.authority_changed, false)
      assert.ok(error.details.issues.length)
      return true
    })
    const after = await h.contract()
    assert.equal(after.reviewer_id, before.reviewer_id)
    assert.equal(after.revision, before.revision)
    assert.deepEqual(after.accepted, [])
  }
  // The unknown template is valid transport, but Python must refuse production acceptance.
  await assert.rejects(h.dispatcher.review({ item_id, verdict: 'accept', takeover: true, reason: 'Offline takeover retains unknown finding', report: fence(original) }, h.exec), error => {
    assert.equal(error.code, 'REVIEW_NOT_PASS')
    assert.equal(error.details.authority_changed, true)
    assert.equal(error.details.reviewer_id, null)
    assert.equal(error.details.review_record_changed, true)
    assert.match(error.message, /"authority_changed":true/)
    return true
  })
  const afterTakeover = await h.contract()
  assert.equal(afterTakeover.reviewer_id, null)
  assert.equal(afterTakeover.takeovers.length, before.takeovers.length + 1)
  assert.deepEqual(afterTakeover.accepted, [])
  // A later syntax failure must describe this attempt, not reuse the earlier authority change.
  const invalid = { ...original, verdict: 'guessed' }
  await assert.rejects(h.dispatcher.review({ item_id, verdict: 'accept', report: fence(invalid) }, h.exec), error => {
    assert.equal(error.details.authority_changed, false)
    assert.equal(error.details.reviewer_id_before, null)
    return true
  })
  // Agent supplies explicit current evidence; runtime still owns the final decision.
  const resolved = structuredClone((await h.acceptance()).review_format.template)
  resolved.verdict = 'pass'
  for (const row of resolved.requirements) { row.result = 'met'; row.evidence = ['Offline fixture verified immutable candidate bytes.'] }
  for (const row of resolved.check_results) { row.execution_status = 'completed'; row.method = 'static:fixture-read'; row.evidence_refs = ['Offline fixture verified immutable candidate bytes.'] }
  for (const row of resolved.findings) { row.state = 'not-a-defect'; row.evidence = ['Offline fixture supplied the previously missing observation.']; row.reason = 'The current fixture check supports this requirement.'
    if (row.disposition === 'pending') row.disposition = 'not_a_defect' }
  await h.dispatcher.review({ item_id, verdict: 'accept', report: fence(resolved) }, h.exec)
  assert.ok((await h.contract()).accepted.some(row => row.candidate_id === view.candidate.candidate_id))
})


const captureResumeArgs = first => ({ checkpoint_id: first.verification_recovery.checkpoint_id,
  reason: 'The saved implementation is unchanged; retry the runtime capture and its original unissued verification roles.' })
const bytesHash = bytes => createHash('sha256').update(bytes).digest('hex')
const budgetEvents = h => h.journal.state('root').events.filter(event => event.type.startsWith('dpswarm/worker-budget-'))
const resumeClaims = h => budgetEvents(h).filter(event => event.type === 'dpswarm/worker-budget-team-run-resumed')

test('native external diagnostic write does not prevent candidate capture or independent verification', { timeout: 90000 }, async t => {
  const h = await fixture(t, { externalObservation: true }), result = await h.run()
  assert.deepEqual(result.failed, [])
  assert.deepEqual(result.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  const view = await h.acceptance(), snapshot = view.candidate.snapshot
  assert.deepEqual(snapshot.changed_paths, ['index.html'])
  assert.deepEqual(snapshot.candidate_files.map(file => file.path), ['assets/motion.js', 'index.html'])
  assert.equal(snapshot.dependency_complete, true)
  assert.equal(view.capture_observations.excluded_count, 1)
  assert.equal(view.capture_observations.exclusions[0].path, h.externalPath)
  const diagnostics = await h.controller.diagnostics(h.controller.sessions.get('root'))
  const implementation = diagnostics.find(row => row.role === 'implementer')
  assert.ok(implementation.diagnostic.closeout.candidates.some(row => row.path === h.externalPath && row.source === 'native-successful-tool'))
  assert.equal(await readFile(h.externalPath, 'utf8'), '/* deterministic external verification helper */')
  await h.accept(result)
  assert.equal((await h.contract()).accepted.length, 1)
})

test('capture recovery resumes only original unissued verifiers and accepts the same implementation', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1, externalObservation: true }), first = await h.run()
  assert.deepEqual(first.deliveries.map(row => row.role), ['implementer'])
  assert.equal(first.execution_error.code, 'CANDIDATE_PUBLISH_FAILED')
  assert.equal(first.verification_recovery.phase, 'ready')
  assert.deepEqual(first.verification_recovery.pending_roles, ['tester', 'reviewer'])
  const original = first.deliveries[0], state = h.controller.sessions.get('root')
  const before = await state.sidecar.call('GET', '/api/status')
  assert.equal(before.snapshot.work_items[original.item_id].acceptance, 'submitted')
  assert.equal((await h.requirement.beforeRun(h.parent)).phase, 'finished', 'Safely completed worker unlocks recovery without a terminate verdict')
  assert.equal(resumeClaims(h).length, 0)
  assert.equal(h.calls.filter(call => call.path === '/api/review').length, 0)
  const hash = bytesHash(await readFile(join(h.cwd, 'index.html')))
  const source = await h.budget.diagnosticsForSession(original.execution_session_id || original.worker_session_id)
  const allocationsBefore = budgetEvents(h).filter(event => event.type === 'dpswarm/worker-budget-allocation')
  assert.equal(allocationsBefore.length, 1)
  const originalRun = allocationsBefore[0].data.run_id
  const resumed = await h.dispatcher.resume(captureResumeArgs(first), h.exec)
  assert.deepEqual(resumed.failed, [])
  assert.deepEqual(resumed.deliveries.map(row => row.role), ['tester', 'reviewer'])
  assert.deepEqual(resumed.reused_implementer_items, [original.item_id])
  assert.equal(resumed.recovery.phase, 'completed')
  assert.equal(bytesHash(await readFile(join(h.cwd, 'index.html'))), hash)
  assert.equal(h.children.length, 3)
  assert.equal(h.outputs.filter(row => row.role === 'implementer').length, 1)
  assert.deepEqual(await h.budget.diagnosticsForSession(source.worker_session_id), source, 'Resuming verifiers does not debit or reset implementer allowance')
  const grants = budgetEvents(h).filter(event => event.type === 'dpswarm/worker-budget-allocation').map(event => event.data)
  assert.deepEqual(grants.map(row => row.label), ['implementer', 'tester', 'reviewer'])
  assert.ok(grants.every(row => row.run_id === originalRun))
  assert.ok(grants.slice(1).every(row => row.resume_id === resumeClaims(h)[0].data.resume_id
    && row.profile.tokenLimit === 600000 && row.profile.callLimit === 28))
  assert.equal(resumeClaims(h).length, 1)
  assert.equal(budgetEvents(h).filter(event => event.type === 'dpswarm/worker-budget-team-run').length, 1)
  const current = await h.acceptance()
  assert.deepEqual(current.candidate.candidate_item_ids, [original.item_id])
  assert.ok(current.review?.review_id)
  await h.accept({ deliveries: [original, ...resumed.deliveries] })
  const accepted = await h.contract()
  assert.equal(accepted.accepted.length, 1)
  assert.equal(accepted.accepted[0].candidate_id, current.candidate.candidate_id)
})

test('a repeated capture failure keeps the checkpoint retryable without claiming verifier quota', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 2 }), first = await h.run(), args = captureResumeArgs(first)
  const budgetBefore = structuredClone(budgetEvents(h)), hash = bytesHash(await readFile(join(h.cwd, 'index.html')))
  await assert.rejects(h.dispatcher.resume(args, h.exec), { code: 'CANDIDATE_PUBLISH_FAILED' })
  assert.equal(h.children.length, 1)
  assert.equal(h.outputs.length, 1)
  assert.equal(resumeClaims(h).length, 0)
  assert.deepEqual(budgetEvents(h), budgetBefore)
  const status = await h.controller.status(h.parent)
  assert.equal(status.verification_recovery.phase, 'ready')
  assert.equal(status.verification_recovery.checkpoint_id, args.checkpoint_id)
  const resumed = await h.dispatcher.resume(args, h.exec)
  assert.deepEqual(resumed.deliveries.map(row => row.role), ['tester', 'reviewer'])
  assert.equal(h.captureAttempts, 3)
  assert.equal(h.outputs.filter(row => row.role === 'implementer').length, 1)
  assert.equal(bytesHash(await readFile(join(h.cwd, 'index.html'))), hash)
  await h.accept({ deliveries: [...first.deliveries, ...resumed.deliveries] })
})

test('terminated implementation cannot resume and refusal makes no control or budget write', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), original = first.deliveries[0]
  await h.dispatcher.review({ item_id: original.item_id, verdict: 'terminate', reason: 'Explicit test-only abandonment.' }, h.exec)
  const beforeBudget = structuredClone(budgetEvents(h)), beforeContract = await h.contract()
  const posts = h.calls.filter(call => call.method === 'POST').length, beforeChildren = h.children.length
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'RESUME_SOURCE_NOT_SUBMITTED' })
  assert.deepEqual(budgetEvents(h), beforeBudget)
  assert.deepEqual(await h.contract(), beforeContract)
  assert.equal(h.calls.filter(call => call.method === 'POST').length, posts)
  assert.equal(h.children.length, beforeChildren)
  assert.equal(h.captureAttempts, 1)
})

for (const changed of ['task', 'testModel', 'reviewerEffort', 'workerTimeoutSeconds', 'lease', 'missing-lease']) test('verification resume refuses changed ' + changed + ' before issuing any verifier', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), state = h.controller.sessions.get('root')
  const beforeBudget = structuredClone(budgetEvents(h)), beforeContract = await h.contract()
  let restore = async () => {}
  if (changed === 'task') h.parent.session.events.push(userMessage('new-task', 'This is a different user request: produce a different artifact.', 1))
  if (changed === 'testModel') h.cfg.testModel = 'different-tester'
  if (changed === 'reviewerEffort') h.cfg.reviewerEffort = 'low'
  if (changed === 'workerTimeoutSeconds') h.cfg.workerTimeoutSeconds = 61
  if (changed === 'lease') {
    const path = state.lease.path, contents = await readFile(path, 'utf8'), lease = JSON.parse(contents)
    await writeFile(path, JSON.stringify({ ...lease, session_id: 'different-owner' }))
    restore = () => writeFile(path, contents)
  }
  if (changed === 'missing-lease') {
    const originalLease = state.lease
    state.lease = null
    restore = async () => { state.lease = originalLease }
  }
  try {
    await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), error => {
      const expected = changed === 'task' ? ['RESUME_TASK_NOT_SETTLED', 'RESUME_TASK_MISMATCH']
        : ['testModel', 'reviewerEffort', 'workerTimeoutSeconds'].includes(changed) ? ['REWORK_CONFIGURATION_CHANGED'] : ['RESUME_WORKSPACE_UNCONFIRMED', 'WORKSPACE_BUSY']
      assert.ok(expected.includes(error.code), error.code + ': ' + error.message)
      return true
    })
    assert.equal(h.children.length, 1)
    assert.equal(h.captureAttempts, 1)
    assert.deepEqual(budgetEvents(h), beforeBudget)
    assert.deepEqual(await h.contract(), beforeContract)
    assert.equal(resumeClaims(h).length, 0)
  } finally { await restore() }
})

test('concurrent and completed duplicate resume cannot dispatch or debit verifiers twice', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), args = captureResumeArgs(first)
  const running = h.dispatcher.resume(args, h.exec)
  await assert.rejects(h.dispatcher.resume(args, h.exec), { code: 'RUN_PENDING' })
  const resumed = await running, beforeBudget = structuredClone(budgetEvents(h))
  await assert.rejects(h.dispatcher.resume(args, h.exec), { code: 'RESUME_CHECKPOINT_UNAVAILABLE' })
  assert.deepEqual(budgetEvents(h), beforeBudget)
  assert.equal(h.children.length, 3)
  assert.equal(resumeClaims(h).length, 1)
  assert.deepEqual(h.outputs.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  await h.accept({ deliveries: [...first.deliveries, ...resumed.deliveries] })
})


test('resume budget close failure remains blocked and keeps the original workspace lease', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), state = h.controller.sessions.get('root')
  const leasePath = state.lease.path, finish = h.budget.finishTeamRun.bind(h.budget)
  let injected = false
  h.budget.finishTeamRun = async (parent, handle) => {
    if (handle.resume_id && !injected) {
      injected = true
      throw Object.assign(new Error('Deterministic resumed budget close failure'), { code: 'FIXTURE_RESUME_BUDGET_CLOSE_FAILED' })
    }
    return finish(parent, handle)
  }
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'FIXTURE_RESUME_BUDGET_CLOSE_FAILED' })
  assert.equal(injected, true)
  assert.equal(h.children.length, 3, 'Both verifiers completed before close acknowledgement failed')
  assert.equal(state.cleanup.budget_error.code, 'FIXTURE_RESUME_BUDGET_CLOSE_FAILED')
  assert.equal(state.lease.path, leasePath)
  assert.equal(JSON.parse(await readFile(leasePath, 'utf8')).session_id, 'root')
  const status = await h.controller.status(h.parent)
  assert.equal(status.verification_recovery.phase, 'blocked')
  assert.equal(status.cleanup.workspace_lease_held, true)
  assert.doesNotMatch(status.verification_recovery.next, /dpswarm_resume\(/)
  const records = (await state.journal.read('root')).events.filter(event => event.type === 'dpswarm/verification-recovery')
  assert.equal(records.at(-1).data.phase, 'blocked', 'No completed checkpoint is published without durable budget close')
  const before = structuredClone(budgetEvents(h))
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'RESUME_CHECKPOINT_UNAVAILABLE' })
  assert.deepEqual(budgetEvents(h), before)
  assert.equal(h.children.length, 3)
})

for (const blockedWriteFails of [false, true]) test('claimed resume whose checkpoint append fails is projected blocked and cannot issue another claim or worker' + (blockedWriteFails ? ' while blocked-state writes also fail' : ''), { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), state = h.controller.sessions.get('root')
  const append = state.journal.append.bind(state.journal)
  let injected = false
  state.journal.append = async (rootId, type, data, ...rest) => {
    if (type === 'dpswarm/verification-recovery' && ((data.phase === 'resuming' && !injected)
      || (blockedWriteFails && injected && data.phase === 'blocked'))) {
      injected = true
      throw Object.assign(new Error('Deterministic checkpoint write failure after durable budget claim'), { code: 'FIXTURE_RESUME_CHECKPOINT_WRITE_FAILED' })
    }
    return append(rootId, type, data, ...rest)
  }
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'FIXTURE_RESUME_CHECKPOINT_WRITE_FAILED' })
  assert.equal(injected, true)
  assert.equal(resumeClaims(h).length, 1, 'The original-role budget authority was already claimed exactly once')
  assert.equal(h.children.length, 1, 'No verifier started before the failed resuming checkpoint')
  if (blockedWriteFails) {
    const raw = (await state.journal.read('root')).events.filter(event => event.type === 'dpswarm/verification-recovery').at(-1).data
    assert.equal(raw.phase, 'ready', 'The raw capture checkpoint stayed ready because its later writes were unavailable')
  }
  const status = await h.controller.status(h.parent)
  assert.equal(status.verification_recovery.phase, 'blocked')
  assert.doesNotMatch(status.verification_recovery.next, /dpswarm_resume\(/)
  const before = structuredClone(budgetEvents(h)), attempts = h.captureAttempts
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'RESUME_CHECKPOINT_UNAVAILABLE' })
  assert.deepEqual(budgetEvents(h), before)
  assert.equal(h.children.length, 1)
  assert.equal(h.captureAttempts, attempts)
})


test('resume cancellation after control admission creates no child and revokes its unbound original-role grant', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), state = h.controller.sessions.get('root')
  let injected = false
  h.onSidecarResult = async (method, path) => {
    if (method === 'POST' && path === '/api/delegate' && !injected) {
      injected = true
      state.abort.abort(Object.assign(new Error('Cancel immediately before native child creation'), { code: 'FIXTURE_RESUME_ABORT' }))
    }
  }
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), error => {
    assert.ok(['FIXTURE_RESUME_ABORT', 'SUBAGENT_ABORTED'].includes(error.code), error.code + ': ' + error.message)
    return true
  })
  assert.equal(injected, true)
  assert.equal(h.children.length, 1)
  assert.equal(h.outputs.length, 1)
  const events = budgetEvents(h), resumed = events.filter(event => event.type === 'dpswarm/worker-budget-allocation' && event.data.resume_id)
  assert.equal(resumed.length, 1)
  assert.equal(resumed[0].data.label, 'tester')
  assert.ok(!events.some(event => event.type === 'dpswarm/worker-budget-allocation-bound' && event.data.allocation_id === resumed[0].data.allocation_id))
  assert.ok(events.some(event => event.type === 'dpswarm/worker-budget-team-run-resume-ended'
    && event.data.resume_id === resumed[0].data.resume_id && event.data.unbound_allocations === 'revoked'))
  const status = await h.controller.status(h.parent)
  assert.equal(status.verification_recovery.phase, 'blocked')
  assert.equal(status.snapshot.work_items[first.deliveries[0].item_id].acceptance, 'submitted')
  assert.equal(resumeClaims(h).length, 1)
})

test('unconfirmed tester cleanup blocks recovery, preserves lease and never starts the reviewer', { timeout: 90000 }, async t => {
  const h = await fixture(t, { captureFailures: 1 }), first = await h.run(), state = h.controller.sessions.get('root')
  const leasePath = state.lease.path
  h.failDisposeRole = 'tester'
  await assert.rejects(h.dispatcher.resume(captureResumeArgs(first), h.exec), { code: 'RESUME_VERIFICATION_UNSETTLED' })
  assert.deepEqual(h.outputs.map(row => row.role), ['implementer', 'tester'])
  assert.equal(h.children.length, 2)
  assert.ok(!h.outputs.some(row => row.role === 'reviewer'))
  const status = await h.controller.status(h.parent)
  assert.equal(status.verification_recovery.phase, 'blocked')
  assert.equal(status.snapshot.seal_phase.root, 'cutoff')
  assert.equal(state.lease.path, leasePath)
  assert.equal(JSON.parse(await readFile(leasePath, 'utf8')).session_id, 'root')
  const diagnostics = await h.controller.diagnostics(state)
  const tester = diagnostics.find(row => row.role === 'tester')
  assert.equal(tester.diagnostic.cleanup.physical_cleanup_confirmed, false)
  assert.equal((await h.contract()).accepted.length, 0)
})


test('unchanged native rework pauses downstream grants; explicit verification binds the same candidate exactly once', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: 'open-finding' }), initial = await h.run()
  assert.deepEqual(initial.failed, [])
  const before = await h.acceptance(), originalUsage = await h.budget.diagnosticsForSession(initial.deliveries[1].execution_session_id)
  const paused = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Resolve the retained motion observation.' }, h.exec)
  assert.deepEqual(paused.deliveries.map(row => row.role), ['implementer'])
  assert.deepEqual(paused.failed, [])
  assert.equal(h.children.length, 4)
  assert.equal(paused.candidate_comparison.result, 'unchanged')
  assert.equal(paused.deferred_verification.status, 'ready')
  assert.deepEqual(paused.deferred_verification.unissued_roles, ['tester', 'reviewer'])
  assert.notEqual(paused.acceptance.candidate.candidate_id, before.candidate.candidate_id)
  assert.deepEqual(paused.acceptance.candidate.roster_evidence, {})
  assert.ok(paused.deferred_verification.prior_verification.findings.some(f => f.id === 'F1' && f.latest_observed_decision?.state === 'open'), JSON.stringify(paused.deferred_verification.prior_verification))
  const allocated = () => h.journal.read('root').then(j => j.events.filter(e => e.type === 'dpswarm/worker-budget-allocation' && e.data.authority === 'fixed-team-rework'))
  assert.equal((await allocated()).length, 1, 'Only the implementation rework allowance was issued')
  await assert.rejects(h.dispatcher.review({ item_id: paused.deliveries[0].item_id, verdict: 'accept' }, h.exec))
  assert.deepEqual((await h.contract()).accepted, [])
  const status = await h.controller.status(h.parent)
  assert.equal(status.deferred_verification.status, 'ready')
  const cold = new FixedTeamController({ config: () => h.cfg, sidecarFactory: h.sidecarFactory })
  assert.equal((await cold.acceptanceStatus({}, h.exec)).deferred_verification.status, 'ready')
  const args = { item_id: paused.deliveries[0].item_id, reason: 'Independently resolve the retained finding on these unchanged sealed bytes.' }
  h.reviewerReport = 'valid'
  const verified = await h.dispatcher.verifyRework(args, h.exec)
  assert.deepEqual(verified.failed, [])
  assert.deepEqual(verified.deliveries.map(row => row.role), ['tester', 'reviewer'])
  assert.equal(h.children.length, 6)
  assert.equal(verified.acceptance.candidate.candidate_id, paused.acceptance.candidate.candidate_id)
  assert.equal(verified.deferred_verification.status, 'finished')
  assert.ok(verified.acceptance.review?.review_id, JSON.stringify(verified.acceptance.report_error || verified.acceptance.review))
  assert.equal((await allocated()).length, 3)
  const routes = h.children.slice(4).map(child => child.request.agentOptions.model)
  assert.deepEqual(routes, ['tester', 'reviewer'])
  assert.deepEqual(await h.budget.diagnosticsForSession(initial.deliveries[1].execution_session_id), originalUsage)
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  assert.equal((await allocated()).length, 3)
  await h.accept({ deliveries: [...paused.deliveries, ...verified.deliveries] })
  assert.ok((await h.contract()).accepted.some(row => row.candidate_id === paused.acceptance.candidate.candidate_id))
})

test('a dead first implementer run result stays lossless at the execute boundary (live session e3589816)', { timeout: 90000 }, async t => {
  const h = await fixture(t)
  h.deadFirstImplementer = true
  const initial = await h.dispatcher.run({ task: 'Implement the HTML entry only; preserve the existing JS dependency.',
    acceptance: 'Whole HTML delivery and local runtime dependency.',
    requirements: [{ id: 'local-script', description: 'The HTML retains and loads its local motion script.', mandatory: true }],
    candidate_paths: ['index.html'], output_kind: 'files' }, h.exec)
  // The host tool executor validates the execute return value (what modelToolView
  // produces), not the raw controller object; both boundaries must hold.
  assert.equal(initial.deliveries.length, 0)
  assert.ok(isJsonValue(initial) === true, 'controller-level result must be lossless')
  assert.ok(isJsonValue(modelToolView(initial)) === true, 'execute-level result must be lossless')
})

test('never-issued verification roles are dispatched for a candidate whose verifiers never started (session 88122af1 deadlock)', { timeout: 90000 }, async t => {
  const h = await fixture(t)
  h.deadFirstImplementer = true
  const initial = await h.run()
  assert.equal(initial.deliveries.length, 0)
  assert.equal(initial.failed.filter(row => row.role === 'implementer').length, 1)
  assert.equal(h.children.length, 1, 'the dead first implementer never let a tester or reviewer start')
  const dead = initial.failed.find(row => row.role === 'implementer')
  const repaired = await h.dispatcher.rework({ item_id: dead.item_id, feedback: 'Produce the delivery within the original constraints.' }, h.exec)
  assert.deepEqual(repaired.deliveries.map(row => row.role), ['implementer'])
  assert.ok(repaired.failed.some(row => row.role === 'tester' && row.code === 'REVERIFY_SOURCE_UNAVAILABLE'))
  assert.ok(repaired.failed.some(row => row.role === 'reviewer' && row.code === 'REVERIFY_SOURCE_UNAVAILABLE'))
  // The structural deadlock: acceptance permanently needs tester evidence that
  // no current lever can ever issue for this candidate.
  await assert.rejects(h.dispatcher.review({ item_id: repaired.deliveries[0].item_id, verdict: 'accept' }, h.exec))
  const status = await h.controller.status(h.parent)
  assert.equal(status.deferred_verification?.status ?? 'none', 'ready', 'the never-issued verification is offered for explicit dispatch')
  const args = { item_id: repaired.deliveries[0].item_id, reason: 'Issue the never-started original tester and reviewer for the current candidate.' }
  const verified = await h.dispatcher.verifyRework(args, h.exec)
  assert.deepEqual(verified.failed, [])
  assert.deepEqual(verified.deliveries.map(row => row.role), ['tester', 'reviewer'])
  assert.equal(verified.deferred_verification.status, 'finished')
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  await h.accept({ deliveries: [...repaired.deliveries, ...verified.deliveries] })
  assert.ok((await h.contract()).accepted.some(row => row.candidate_id === repaired.acceptance.candidate.candidate_id))
})

test('a run that issued its verifiers never offers the never-issued dispatch', { timeout: 90000 }, async t => {
  const h = await fixture(t), initial = await h.run()
  assert.deepEqual(initial.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  h.implementationVersion = 2
  const changed = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Produce version 2.' }, h.exec)
  // The rework re-verified through the issued lineages; no deferred decision exists.
  assert.deepEqual(changed.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  assert.equal(changed.deferred_verification, null)
  const status = await h.controller.status(h.parent)
  assert.equal(status.deferred_verification, null, 'issued verifier lineages keep their own chains')
  await assert.rejects(h.dispatcher.verifyRework({ item_id: changed.deliveries[0].item_id, reason: 'Try the never-issued path.' }, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  const budgetEvents = (await h.journal.read('root')).events
  assert.equal(budgetEvents.filter(e => e.type === 'dpswarm/worker-budget-team-run-resumed').length, 0, 'no team-run resume was claimed')
})

test('native real content changes and explicit always retain normal downstream dispatch', { timeout: 90000 }, async t => {
  const h = await fixture(t), initial = await h.run()
  h.implementationVersion = 2
  const changed = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Produce version 2.' }, h.exec)
  assert.deepEqual(changed.failed, [])
  assert.equal(changed.candidate_comparison.result, 'changed')
  assert.deepEqual(changed.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
  assert.equal(changed.deferred_verification, null)
  const explicit = await h.dispatcher.rework({ item_id: changed.deliveries[0].item_id, feedback: 'Reassess the same candidate and independently verify it.', verification: 'always' }, h.exec)
  assert.equal(explicit.candidate_comparison.result, 'unchanged')
  assert.deepEqual(explicit.deliveries.map(row => row.role), ['implementer', 'tester', 'reviewer'])
})

test('paused verification rejects budget changes, stale candidates and concurrent repeat grants', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerMode: 'lead' }), initial = await h.run()
  h.cfg.reworkBudgetMode = 'fixed'; h.cfg.reworkTokenLimit = 600000; h.cfg.reworkCallLimit = 28
  const paused = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Inspect the same delivery.' }, h.exec)
  const args = { item_id: paused.deliveries[0].item_id, reason: 'Run the pending independent tester.' }
  assert.deepEqual(paused.deferred_verification.unissued_roles, ['tester'])
  h.cfg.reworkTokenLimit++
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_BUDGET_CHANGED/)
  assert.equal(h.children.length, 3)
  h.cfg.reworkTokenLimit--
  const results = await Promise.allSettled([h.dispatcher.verifyRework(args, h.exec), h.dispatcher.verifyRework(args, h.exec)])
  assert.equal(results.filter(r => r.status === 'fulfilled').length, 1, JSON.stringify(results.map(r => r.reason?.message || r.status)))
  const verified = results.find(r => r.status === 'fulfilled').value
  assert.deepEqual(verified.deliveries.map(row => row.role), ['tester'])
  assert.equal(h.children.length, 4)
  const current = await h.dispatcher.rework({ item_id: paused.deliveries[0].item_id, feedback: 'Inspect again without imposing a verdict.' }, h.exec)
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  assert.equal(current.deferred_verification.status, 'ready')
})


test('deferred verification rejects a mismatched issued role allowance without starting that worker or reopening the claim', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerMode: 'lead' }), initial = await h.run()
  const paused = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Inspect the same delivery.' }, h.exec)
  const original = h.budget.issueRework.bind(h.budget)
  h.budget.issueRework = async (...args) => ({ ...await original(...args), role: 'implementer' })
  const args = { item_id: paused.deliveries[0].item_id, reason: 'Verify this current candidate.' }
  const result = await h.dispatcher.verifyRework(args, h.exec)
  assert.deepEqual(result.deliveries, [])
  assert.equal(result.failed[0].code, 'REWORK_VERIFICATION_ALLOCATION_INVALID')
  assert.equal(h.children.length, 3)
  assert.equal(result.deferred_verification.status, 'finished')
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  const events = (await h.journal.read('root')).events
  assert.equal(events.filter(e => e.type === 'dpswarm/worker-budget-rework-revoked').length, 1)
  assert.deepEqual((await h.contract()).accepted, [])
})


test('native no-write rework reuses sealed entry declarations when a historical task binding lacks candidate_paths', { timeout: 90000 }, async t => {
  const h = await fixture(t), initial = await h.run(), before = await h.acceptance()
  // Current new runs require declared paths. This models only an older persisted
  // binding lacking them; the real authoritative snapshot retains its identity.
  h.controller.sessions.get('root').fixedTask.candidate_paths = []
  assert.deepEqual(before.candidate.snapshot.entry_paths, ['index.html'])
  assert.equal(before.candidate.snapshot.binding.root_session_id, 'root')
  assert.equal(before.candidate.snapshot.binding.contract_id, before.contract_id)
  h.skipImplementationWrite = true
  const paused = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Inspect the current delivery and report any remaining evidence gap.' }, h.exec)
  assert.deepEqual(paused.failed, [])
  assert.deepEqual(paused.deliveries.map(row => row.role), ['implementer'])
  assert.deepEqual(paused.acceptance.candidate.snapshot.changed_paths, [])
  assert.deepEqual(paused.acceptance.candidate.snapshot.entry_paths, ['index.html'])
  assert.equal(paused.candidate_comparison.result, 'unchanged')
  assert.equal(paused.deferred_verification.status, 'ready')
  assert.equal(h.children.length, 4)
})

test('deferred tester budget cleanup uncertainty blocks reviewer dispatch and preserves the consumed decision and lease', { timeout: 90000 }, async t => {
  const h = await fixture(t), initial = await h.run()
  const paused = await h.dispatcher.rework({ item_id: initial.deliveries[0].item_id, feedback: 'Inspect the same delivery.' }, h.exec)
  h.budget.revokeRework = async () => { throw Object.assign(new Error('deterministic budget cleanup uncertainty'), { code: 'FIXTURE_REVOKE_UNCONFIRMED' }) }
  const args = { item_id: paused.deliveries[0].item_id, reason: 'Run the pending verification.' }
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REVERIFY_CLEANUP_UNCONFIRMED/)
  assert.equal(h.children.length, 5, 'Only the tester is started; reviewer remains unissued')
  const status = await h.controller.status(h.parent)
  assert.equal(status.deferred_verification.status, 'blocked')
  assert.deepEqual(status.deferred_verification.failure_codes, ['REVERIFY_CLEANUP_UNCONFIRMED'])
  assert.equal(status.cleanup.workspace_lease_held, true)
  await assert.rejects(h.dispatcher.verifyRework(args, h.exec), /REWORK_VERIFICATION_UNAVAILABLE/)
  assert.deepEqual((await h.contract()).accepted, [])
})

test('acceptance views expose a compact blocking-and-actions decision summary (P3a)', { timeout: 90000 }, async t => {
  const h = await fixture(t), result = await h.run()
  const mid = await h.acceptance()
  // The run already registered the final reviewer pass: the summary must be
  // ready, with an explicit accept action and no blocking facts.
  assert.equal(typeof mid.decision_summary, 'object')
  assert.equal(mid.decision_summary.ready_to_accept, true)
  assert.deepEqual(mid.decision_summary.blocking.mandatory_unknown, [])
  assert.ok(mid.decision_summary.legal_actions.some(row => row.tool === 'dpswarm_review' && row.form === 'accept' && row.ready === true))
  await h.accept(result)
  const done = await h.acceptance()
  assert.equal(done.decision_summary.ready_to_accept, true)
  assert.deepEqual(Object.values(done.decision_summary.blocking).every(list => !list.length), true)
})

test('an open v2 finding blocks the summary until an explicit disposition exists', { timeout: 90000 }, async t => {
  const h = await fixture(t, { reviewerReport: 'open-finding' }), result = await h.run()
  const view = await h.acceptance()
  assert.equal(view.decision_summary.ready_to_accept, false)
  assert.deepEqual(view.decision_summary.blocking.undisposed_findings, ['F1'])
  assert.ok(view.decision_summary.legal_actions.some(row => row.tool === 'dpswarm_rework'))
})

test('finalize closes only provably-terminal auxiliary items and reports workspace consistency (P3b)', { timeout: 90000 }, async t => {
  const h = await fixture(t)
  const result = await h.run()
  await h.accept(result)
  // The accepted candidate files are still on disk untouched.
  const fin = await h.controller.finalize({ reason: 'Delivered and accepted; drain auxiliary state.' }, h.exec)
  assert.equal(fin.mode, 'dpswarm-finalize-v1')
  assert.equal(fin.workspace_consistency.result, 'consistent')
  assert.equal(fin.workspace_consistency.mismatches.length, 0)
  assert.ok(fin.workspace_consistency.checked_at)
  // Drift the workspace and recheck: reported, never rolled back.
  const { writeFile: fsWriteFile } = await import('node:fs/promises')
  await fsWriteFile(join(h.cwd, 'index.html'), '<html>drifted</html>')
  const drifted = await h.controller.finalize({ reason: 'Recheck after an external edit.' }, h.exec)
  assert.equal(drifted.workspace_consistency.result, 'drifted')
  assert.ok(drifted.workspace_consistency.mismatches.some(m => m.path === 'index.html'))
}).catch(error => { throw error })

test('status exposes a root usage ledger split by activity and role (P4)', { timeout: 90000 }, async t => {
  const h = await fixture(t)
  const result = await h.run()
  await h.accept(result)
  const status = await h.controller.status(h.parent)
  const ledger = status.usage_ledger
  assert.equal(ledger.schema, 'dpswarm-usage-ledger-v1')
  const activities = ledger.by_activity
  assert.ok(activities.implementation.calls >= 1, 'implementer settled calls are classified')
  assert.ok(activities.verification.calls >= 2, 'tester and reviewer calls are verification')
  assert.ok(activities.implementation.input_tokens + activities.implementation.output_tokens > 0)
  assert.ok(ledger.by_role.implementer.calls >= 1)
  assert.ok(ledger.totals.observed_tokens > 0)
  assert.ok(ledger.notes.some(n => /never double-counted/.test(n)))
})

test('independent mode runs tester and a blind first pass concurrently, then converges (P5)', { timeout: 90000 }, async t => {
  const h = await fixture(t, { independent: true }), result = await h.run()
  assert.deepEqual(result.failed, [])
  // implementer + (tester + first pass) + convergence = 4 children
  assert.equal(h.children.length, 4)
  // The first pass saw no tester verdict.
  assert.match(h.reviewerPrompts[0], /Independent first pass/)
  assert.ok(!/Tester report/.test(h.reviewerPrompts[0]))
  // The convergence round quotes the tester report.
  assert.match(h.reviewerPrompts[1], /Convergence review/)
  assert.match(h.reviewerPrompts[1], /Tester report/)
  // The registered reviewer evidence is the convergence execution, not the first pass.
  const state = h.controller.sessions.get('root')
  const contract = await h.contract()
  const reviewerEvidence = contract.evidence[state.acceptance.candidate.roster_evidence.reviewer]
  assert.equal(reviewerEvidence.identity.session_id, h.children[3].id, 'final reviewer evidence is the convergence session')
  assert.notEqual(reviewerEvidence.identity.session_id, h.children[2].id, 'the blind first pass never registers as reviewer evidence')
  await h.accept(result)
  assert.ok((await h.contract()).accepted.length >= 1)
})

test('R18: a first-pass pass cannot survive a later tester finding without convergence re-judgement', { timeout: 90000 }, async t => {
  const h = await fixture(t, { independent: true, testerFinding: true }), result = await h.run()
  // The tester found a defect; the convergence round must re-judge it rather
  // than echo the first pass, and acceptance stays blocked until it is fixed.
  assert.match(h.reviewerPrompts[1], /Tester observed the wheel spokes render inverted/)
  const state = h.controller.sessions.get('root')
  const contract = await h.contract()
  const reviewerEvidence = contract.evidence[state.acceptance.candidate.roster_evidence.reviewer]
  assert.equal(reviewerEvidence.identity.session_id, h.children[3].id)
  const record = reviewerEvidence.record
  assert.equal(record.verdict, 'needs-rework')
  assert.ok(record.findings.some(row => row.id === 'F1' && row.disposition === 'fix_required'))
  await assert.rejects(h.accept(result))
  assert.deepEqual((await h.contract()).accepted, [])
})
