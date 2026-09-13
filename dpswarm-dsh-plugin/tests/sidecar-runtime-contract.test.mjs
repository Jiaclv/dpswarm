import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import test from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const modulePath = process.env.DPSWARM_TEST_SIDECAR_LIB || join(repo, 'dpswarm-dsh-plugin/lib/sidecar.js')
const { Sidecar, FIXED_TEAM_RUNTIME_REQUIREMENTS: required } = await import(pathToFileURL(modulePath))
const { FixedTeamController } = await import(pathToFileURL(join(dirname(modulePath), 'fixed-team.js')))
const good = () => ({ bridge: { session_isolation: true, plugin_audit_v1: true,
  runtime: structuredClone(required), process: { pid: 1, start_id: 'first' } } })
const fixture = (health = good(), cfg = {}) => {
  const sidecar = new Sidecar({ sidecarUrl: 'http://127.0.0.1:8799', ...cfg })
  const calls = []
  sidecar.call = async (...args) => { calls.push(args); return health }
  sidecar._start = async () => assert.fail('compatibility preflight must not launch another process')
  return { sidecar, health, calls }
}

test('legacy audit flag alone is rejected by read-only fixed-team preflight', async () => {
  const { sidecar, calls } = fixture({ bridge: { session_isolation: true, plugin_audit_v1: true } })
  await assert.rejects(sidecar.requireRuntimeCapabilities(), error => {
    assert.equal(error.code, 'SIDECAR_RUNTIME_INCOMPATIBLE')
    assert.equal(error.details.error, error.code)
    assert.match(error.details.fingerprint, /^[a-f0-9]{64}$/)
    assert.ok(error.details.missing.includes('runtime.admission_cleanup'))
    assert.deepEqual(error.details.observed.audit_event_types, [])
    return true
  })
  assert.deepEqual(calls, [['GET', '/api/status', undefined, 2500]])
})

test('runtime compatibility is opt-in for ensure and unrelated old connections stay compatible', async () => {
  const old = { bridge: { session_isolation: true, plugin_audit_v1: true } }
  await fixture(old, { sessionIsolation: true, auditJournalRequired: true }).sidecar.ensure()
  await assert.rejects(fixture(old, { runtimeCapabilitiesRequired: true }).sidecar.ensure(),
    { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' })
})

for (const [name, mutate] of [
  ['missing required audit event', h => h.bridge.runtime.audit_event_types.pop()],
  ['missing cleanup implementation', h => delete h.bridge.runtime.admission_cleanup],
  ['future unsupported revision', h => h.bridge.runtime.revision = 2],
  ['version string instead of revision', h => h.bridge.runtime.revision = '1'],
  ['package version in place of capability schema', h => h.bridge.runtime.schema = '0.99.0'],
  ['audit types with the wrong shape', h => h.bridge.runtime.audit_event_types = required.audit_event_types.join(',')],
  ['missing session isolation', h => delete h.bridge.session_isolation],
]) {
  test(`bounded protocol rejects ${name}`, async () => {
    const h = good(); mutate(h)
    await assert.rejects(fixture(h).sidecar.requireRuntimeCapabilities(), { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' })
  })
}

for (const type of ['dpswarm/verification-recovery', 'dpswarm/worker-budget-team-run-resumed', 'dpswarm/worker-budget-team-run-resume-ended']) {
  test(`preflight rejects a service missing recovery vocabulary ${type}`, async () => {
    const health = good()
    health.bridge.runtime.audit_event_types = health.bridge.runtime.audit_event_types.filter(value => value !== type)
    const h = fixture(health)
    await assert.rejects(h.sidecar.requireRuntimeCapabilities(), error => {
      assert.equal(error.code, 'SIDECAR_RUNTIME_INCOMPATIBLE')
      assert.ok(error.details.missing.includes(`audit_event_type:${type}`))
      return true
    })
    assert.deepEqual(h.calls, [['GET', '/api/status', undefined, 2500]])
  })
}

test('fingerprint ignores identity, order and unrelated events but reflects required capability recovery', async () => {
  const a = await fixture().sidecar.requireRuntimeCapabilities()
  const changed = good()
  changed.bridge.process = { pid: 999, start_id: 'new-process' }
  changed.bridge.runtime.audit_event_types.reverse()
  changed.bridge.runtime.audit_event_types.push('dpswarm/future-additive-event')
  changed.bridge.runtime.package_version = '123.456.789'
  const b = await fixture(changed).sidecar.requireRuntimeCapabilities()
  assert.equal(a.compatible, true)
  assert.equal(a.fingerprint, b.fingerprint)
  changed.bridge.runtime.audit_event_types = []
  let first
  await assert.rejects(fixture(changed).sidecar.requireRuntimeCapabilities(), error => {
    first = error.details.fingerprint
    assert.notEqual(a.fingerprint, first)
    return true
  })
  changed.bridge.process.pid++
  await assert.rejects(fixture(changed).sidecar.requireRuntimeCapabilities(), error => {
    assert.equal(error.details.fingerprint, first)
    return true
  })
})

async function liveServer(t, pythonRoot, workspace) {
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python',
    [join(repo, 'dpswarm-dsh-plugin/tests/session-sidecar-harness.py'), workspace],
    { cwd: repo, env: { ...process.env, DPSWARM_TEST_PYTHON_ROOT: pythonRoot },
      windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''
  child.stderr.on('data', part => { stderr += part })
  const lines = createInterface({ input: child.stdout })
  t.after(async () => {
    if (child.exitCode === null) {
      const exited = once(child, 'exit')
      child.stdin.end('{"command":"stop"}\n')
      const timer = setTimeout(() => child.kill(), 5000)
      try { await exited } finally { clearTimeout(timer) }
    }
    lines.close()
  })
  const first = await Promise.race([
    once(lines, 'line').then(([line]) => JSON.parse(line)),
    once(child, 'exit').then(() => { throw new Error(stderr || 'Python sidecar exited before ready') }),
  ])
  const sidecar = new Sidecar({ sidecarUrl: `http://127.0.0.1:${first.port}`,
    workspace, sessionId: 'runtime-preflight-unstarted', autoStart: false,
    sessionIsolation: true, auditJournalRequired: true, runtimeCapabilitiesRequired: true })
  sidecar._start = async () => assert.fail('a listening legacy server must never launch another process')
  return { sidecar, child }
}

function outputDirectory(prefix) {
  const root = join(repo, 'test-artifacts/runtime-protocol')
  mkdirSync(root, { recursive: true })
  return mkdtempSync(join(root, prefix))
}

test('real matching Python advertises loaded capabilities without creating a business session', { timeout: 20000 }, async t => {
  const directory = outputDirectory('matching-')
  const pythonRoot = process.env.DPSWARM_TEST_PYTHON_ROOT || join(repo, 'dpswarm-plugin')
  const { sidecar, child } = await liveServer(t, pythonRoot, directory)
  const before = readFileSync(join(directory, 'events.jsonl'), 'utf8')
  const check = await sidecar.requireRuntimeCapabilities()
  assert.equal(check.compatible, true)
  const health = await sidecar.call('GET', '/api/status')
  assert.equal(health.bridge.process.pid, child.pid)
  assert.match(health.bridge.process.start_id, /^[a-f0-9]{32}$/)
  await sidecar.ensure()
  assert.equal(health.state, 'not_started')
  assert.equal(existsSync(join(directory, 'sessions')), false)
  assert.equal(readFileSync(join(directory, 'events.jsonl'), 'utf8'), before)
})

const legacyRoot = process.env.DPSWARM_TEST_LEGACY_PYTHON_ROOT
  || join(repo, '.tmp/runtime-protocol-fix-20260911/baseline/python')
test('real archived legacy Python still fails before native starts after its files are upgraded on disk',
  { timeout: 20000, skip: !existsSync(join(legacyRoot, 'dpswarm/session_server.py')) }, async t => {
    const directory = outputDirectory('legacy-')
    const pythonRoot = join(directory, 'python')
    cpSync(join(legacyRoot, 'dpswarm'), join(pythonRoot, 'dpswarm'),
      { recursive: true, filter: path => !path.includes('__pycache__') })
    const workspace = join(directory, 'state')
    const { sidecar } = await liveServer(t, pythonRoot, workspace)
    const before = readFileSync(join(workspace, 'events.jsonl'), 'utf8')
    const health = await sidecar.call('GET', '/api/status')
    assert.equal(health.bridge.plugin_audit_v1, true)
    assert.equal(health.bridge.runtime, undefined)
    const matchingRoot = process.env.DPSWARM_TEST_PYTHON_ROOT || join(repo, 'dpswarm-plugin')
    for (const file of ['session_server.py', 'server.py', 'plugin_audit.py']) {
      cpSync(join(matchingRoot, 'dpswarm', file), join(pythonRoot, 'dpswarm', file))
    }
    const project = join(directory, 'project'); mkdirSync(project)
    const cfg = { ...sidecar.cfg, enabledSessions: ['runtime-preflight-unstarted'],
      implProvider: 'mock', implModel: 'b-kimi', testProvider: 'mock', testModel: 'b-kimi',
      subagentProvider: 'spawn', workerTimeoutSeconds: 60,
      workerBudgetMode: 'manual', workerTokenLimit: 1000, workerCallLimit: 2 }
    const parent = { id: cfg.sessionId, session: { id: cfg.sessionId, header: { cwd: project },
      requestHeader: () => ({ config: { provider: 'mock', model: 'b-kimi' } }) },
      options: { provider: 'mock', model: 'b-kimi' } }
    const counts = { lease: 0, budget: 0, cm: 0, registry: 0, native: 0 }
    const writes = []
    const failIfCalled = key => async () => { counts[key]++; assert.fail(`${key} started before runtime preflight`) }
    const controller = new FixedTeamController({ config: () => cfg,
      budget: { beginTeamRun: failIfCalled('budget'), issueTeamWorker: failIfCalled('budget') },
      cm: { beginRun: failIfCalled('cm'), finishRun: failIfCalled('cm'), status: async () => ({ enabled: false }) },
      modelRegistry: { resolve: failIfCalled('registry') },
      subagents: { start: failIfCalled('native') },
      sidecarFactory: snapshot => {
        const client = new Sidecar(snapshot), call = client.call.bind(client)
        client.call = async (method, ...args) => {
          if (method !== 'GET') writes.push({ method, path: args[0] })
          return call(method, ...args)
        }
        return client
      } })
    controller.acquire = () => { counts.lease++; assert.fail('workspace lease acquired before runtime preflight') }
    const exec = { agent: parent, signal: new AbortController().signal }
    await assert.rejects(controller.run({ task: 'Offline compatibility preflight fixture' }, exec),
      { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' })
    const state = controller.sessions.get(cfg.sessionId)
    assert.equal(state.busy, false)
    assert.equal(Boolean(state.lease), false)
    const status = await controller.status(parent)
    assert.equal(status.state, 'blocked')
    assert.equal(status.runtime_compatibility.code, 'SIDECAR_RUNTIME_INCOMPATIBLE')
    await assert.rejects(sidecar.ensure(), { code: 'SIDECAR_RUNTIME_INCOMPATIBLE' })
    assert.deepEqual(counts, { lease: 0, budget: 0, cm: 0, registry: 0, native: 0 })
    assert.deepEqual(writes, [])
    assert.equal(existsSync(join(workspace, 'workspace-leases')), false)
    assert.equal(existsSync(join(workspace, 'sessions')), false)
    assert.equal(readFileSync(join(workspace, 'events.jsonl'), 'utf8'), before)
  })