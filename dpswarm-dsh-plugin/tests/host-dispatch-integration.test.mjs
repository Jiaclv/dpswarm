import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, mkdir, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve, dirname } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const host = resolveHostRoot()
const lib = process.env.DPSWARM_TEST_LIB ? pathToFileURL(resolve(process.env.DPSWARM_TEST_LIB) + '/') : new URL('../lib/', import.meta.url)
const [{ Context }, { Storage }, jsonStorage, plugin, { Sidecar }, { TeamRequirement }, { AuditJournal }] = await Promise.all([
  import(hostModuleUrl(host, 'cordis/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-storage/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-storage-json/lib/index.js')),
  import(new URL('index.js', lib)), import(new URL('sidecar.js', lib)),
  import(new URL('team-required.js', lib)), import(new URL('audit.js', lib)),
])

// A real Cordis plugin fiber, real host JSON storage and real Python control plane.
// Only the native child/model boundary is deterministic; no external provider is used.
for (const hostStorage of ['json', 'missing', 'incompatible']) test(`enabled plugin dispatch through real Cordis (${hostStorage} storage)`, async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dpswarm-host-dispatch-'))
  const cwd = join(directory, 'project'), workspace = join(directory, 'runtime'), storageRoot = join(directory, 'storage')
  await mkdir(cwd); await mkdir(workspace)
  const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  const python = spawn(process.env.DPSWARM_TEST_PYTHON || 'python', [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), workspace],
    { cwd: repo, env: { ...process.env }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''; python.stderr.on('data', value => { stderr += value })
  const lines = createInterface({ input: python.stdout })
  const port = await Promise.race([once(lines, 'line').then(([line]) => JSON.parse(line).port), once(python, 'exit').then(() => { throw Error(stderr || 'Sidecar exited before ready') })])
  const ctx = new Context(), tools = new Map(), sessions = new Map(), children = []
  let disposed = 0, providerCalls = 0
  t.after(async () => {
    try { await ctx.fiber.dispose() } finally {
      if (python.exitCode === null) {
        const exited = once(python, 'exit'); python.stdin.end('{"command":"stop"}\n')
        const timer = setTimeout(() => python.kill(), 5000)
        try { await exited } finally { clearTimeout(timer) }
      }
      lines.close()
    }
  })
  const parent = { id: 'host-root', options: { provider: 'fixture', model: 'lead' }, session: {
    id: 'host-root', header: { id: 'host-root', cwd, delegationDepth: 0, origin: 'root' },
    events: [{ seq: 0, type: 'user/message', data: { id: 'host-user', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create fixture.html in this directory.' }] } }],
    requestHeader: () => ({ config: { provider: 'fixture', model: 'lead' } }),
  } }
  sessions.set(parent.id, parent.session)
  ctx.provide('tools', { register: tool => { tools.set(tool.name, tool); return () => tools.delete(tool.name) } })
  ctx.provide('settings', { installSection: (_owner, _ns, _schema, entry, hooks) => hooks.setSource(() => entry) })
  ctx.provide('systemPrompt', { section: () => () => {} })
  ctx.provide('llm', { resolveCallConfig: async route => route, prepareCall: () => { providerCalls++; throw Error('External model calls are forbidden in this fixture') } })
  ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()] })
  ctx.provide('subagents', { start: async (provider, request) => {
    const id = 'host-child-' + children.length
    const session = { id, header: { id, cwd, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 },
      events: [{ seq: 0, type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: request.prompt } },
        { seq: 1, type: 'turn/end', data: { reason: { kind: 'error', error: { code: 'FIXTURE_STOP', message: 'Intentional fixture terminal after successful dispatch' } } } }],
    }
    sessions.set(id, session)
    const child = { id, provider, localAgent: { session }, session, result: Promise.resolve({ stopReason: 'error', output: [] }), dispose: async () => { disposed++ } }
    children.push(child); return child
  } })
  // Host services are published from separate provider fibers, as in the daily profile.
  let incompatibleHub
  if (hostStorage === 'json') {
    await ctx.plugin(Storage)
    await ctx.plugin(jsonStorage, { root: storageRoot })
  } else if (hostStorage === 'incompatible') {
    incompatibleHub = { backend: {} }
    await ctx.plugin({ name: 'incompatible-storage-fixture', apply: scope => scope.provide('storage', incompatibleHub) })
  }
  const cfg = { workspace, sidecarUrl: `http://127.0.0.1:${port}`, autoStart: false, enabledSessions: [parent.id],
    implMode: 'lead', testProvider: 'fixture', testModel: 'tester', reviewerMode: 'lead', workerBudgetMode: 'unlimited' }
  await ctx.plugin({ name: plugin.name, inject: plugin.inject, apply: plugin.apply }, cfg)
  assert.ok(tools.has('dpswarm_run'), 'Actual plugin fiber must publish tools')
  const exec = { agent: parent, signal: new AbortController().signal }
  const args = { task: 'Create fixture.html.', candidate_paths: ['fixture.html'] }
  if (hostStorage === 'incompatible') {
    await assert.rejects(tools.get('dpswarm_run').execute(args, exec), { code: 'HOST_RUNTIME_INCOMPATIBLE' })
    assert.equal(children.length, 0)
    const requirement = ctx.get('dpswarmRequirement', false)
    assert.equal((await requirement.status(parent)).phase, 'blocked')
    assert.equal(await requirement.preExecute({ ...exec, name: 'ask_user_question' }, () => 'allowed'), 'allowed')
    assert.equal((await requirement.preExecute({ ...exec, name: 'write' }, () => { throw Error('No production bypass') })).kind, 'deny')
    await requirement.turnStopping({ ...exec, turn: 1 })
    // A repaired local registry is the recovery evidence; the sidecar was healthy throughout.
    incompatibleHub.backend = { names: () => [], get: () => null }
    assert.equal((await requirement.status(parent)).phase, 'required')
  }
  const result = await tools.get('dpswarm_run').execute(args, exec)
  assert.equal(children.length, 1, 'The enabled entry reaches native dispatch past contract and mailbox registration')
  assert.equal(disposed, 1)
  assert.equal(result.deliveries.length, 0)
  assert.equal(result.failed.length, 1)
  assert.equal(providerCalls, 0)
  const sidecar = new Sidecar({ ...cfg, sessionId: parent.id, sessionIsolation: true })
  const status = await sidecar.call('GET', '/api/status')
  assert.equal(status.snapshot.open_worker_slots_used, 0)
  const audit = await sidecar.call('GET', '/api/plugin-audit')
  assert.equal(audit.events.filter(e => e.type === 'dpswarm/team-required-started').length, 1)
  assert.equal(audit.events.findLast(e => e.type === 'dpswarm/team-required-finished').data.outcome, 'failed_takeover')
  if (hostStorage === 'json') {
    const mailboxBytes = await readFile(join(storageRoot, 'dpswarm_mailbox.json'), 'utf8')
    assert.match(mailboxBytes, /host-root/)
    assert.match(mailboxBytes, /implementer/)
  }
  assert.equal((await tools.get('dpswarm_status').execute({}, exec)).team_requirement.phase, 'finished')
  if (hostStorage === 'json') {
    assert.ok(status.bridge.runtime.audit_event_types.includes('dpswarm/team-required-continued'), 'The actual Python service advertises the new durable event')
    const oldRequirement = (await tools.get('dpswarm_status').execute({}, exec)).team_requirement
    const contractBefore = await sidecar.call('GET', '/api/acceptance')
    parent.session.events.push({ seq: 1, type: 'user/message', data: { id: 'host-followup', role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: 'Continue the same fixture delivery without changing the requirements.' }] } })
    const pending = (await tools.get('dpswarm_status').execute({}, exec)).team_requirement
    assert.equal(pending.phase, 'required')
    const args = { current_binding_id: pending.continuity.current_binding.binding_id,
      previous_binding_id: pending.continuity.previous_binding.binding_id, reason: 'The trusted human is continuing this existing delivery.' }
    const before = await sidecar.call('GET', '/api/plugin-audit')
    const continued = await tools.get('dpswarm_continue_task').execute(args, exec)
    assert.equal(continued.outcome, 'continued'); assert.equal(continued.phase, 'finished')
    assert.equal(continued.binding.binding_id, oldRequirement.binding.binding_id)
    assert.deepEqual(await sidecar.call('GET', '/api/acceptance'), contractBefore)
    const after = await sidecar.call('GET', '/api/plugin-audit')
    assert.deepEqual(after.events.slice(0, -1), before.events)
    assert.equal(after.events.at(-1).type, 'dpswarm/team-required-continued')
    assert.equal((await tools.get('dpswarm_continue_task').execute(args, exec)).outcome, 'already_continued')
    // Brand new JS journal/gate objects must reconstruct the persisted original
    // lifecycle from real authenticated Python HTTP responses, without warm maps.
    const coldJournal = new AuditJournal({ config: () => cfg, sidecarFactory: rootId => new Sidecar({ ...cfg, sessionId: rootId, sessionIsolation: true, auditJournalRequired: true }) })
    const cold = new TeamRequirement({ config: () => cfg, journal: coldJournal, inspectAdmission: async () => null })
    const replayed = await cold.beforeRun(parent)
    assert.equal(replayed.phase, 'finished'); assert.equal(replayed.finish.outcome, 'failed_takeover')
    assert.equal(replayed.binding.binding_id, oldRequirement.binding.binding_id)
    assert.equal(cold.sourceFor(parent, replayed).message_id, 'host-user')
    await assert.rejects(cold.beforeDispatch(parent), { code: 'TEAM_REQUIRED_ALREADY_FULFILLED' })
    assert.equal((await coldJournal.read(parent.id)).events.filter(e => e.type === 'dpswarm/team-required-continued').length, 1)
    assert.equal(children.length, 1); assert.equal(providerCalls, 0)
  }
})
