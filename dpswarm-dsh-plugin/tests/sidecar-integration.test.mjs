import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdirSync, mkdtempSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const host = process.env.DSH_HOST_ROOT || 'C:/Users/93711/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'
const available = existsSync(join(host, 'dsh-tools/lib/index.js'))
let apply
if (available) {
  await import(pathToFileURL(join(host, 'cosmokit/lib/index.js')).href)
  await import(pathToFileURL(join(host, 'dsh-tools/lib/index.js')).href)
  process.env.DPSWARM_SKIP_SETTINGS = '1'
  ;({ apply } = await import('../lib/index.js'))
}

test('real loopback sidecar: root ownership, published identity, accept, failure settlement and restart',
  { skip: !available, timeout: 30000 }, async t => {
  const output = join(repo, 'test-artifacts/engineering-20260905/dsh/loopback')
  mkdirSync(output, { recursive: true })
  const directory = mkdtempSync(join(output, 'run-'))
  const child = spawn(process.env.DPSWARM_TEST_PYTHON || 'python',
    [join(repo, 'dpswarm-dsh-plugin/tests/sidecar-harness.py'), directory],
    { cwd: repo, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] })
  let stderr = ''
  child.stderr.on('data', value => { stderr += value.toString() })
  const lines = createInterface({ input: child.stdout })
  const queue = [], waiters = []
  lines.on('line', line => {
    const value = JSON.parse(line)
    if (waiters.length) waiters.shift()(value)
    else queue.push(value)
  })
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve))
  t.after(async () => {
    if (child.exitCode === null) {
      const exited = once(child, 'exit')
      child.stdin.end(JSON.stringify({ command: 'stop' }) + '\n')
      await exited
    }
    lines.close()
  })
  const first = await Promise.race([next(), once(child, 'exit').then(() => { throw new Error(stderr) })])
  const base = `http://127.0.0.1:${first.port}`
  const registered = new Map()
  let starts = 0, disposed = 0, mode = 'success'
  const ctx = {
    tools: { register(tool) { registered.set(tool.name, tool) } },
    systemPrompt: { section() {} }, inject(_deps, callback) { callback(ctx) }, effect() {},
    subagents: { async start(_provider, request) {
      const id = 'host-published-run-' + (++starts)
      return { id, localAgent: undefined,
        result: Promise.resolve({ output: [{ type: 'text', text: mode === 'success' ? 'verified fixture delivery' : 'partial' }],
          stopReason: mode === 'success' ? 'completed' : 'error' }),
        async dispose() { disposed++; if (mode === 'disposal-error') throw new Error('fixture disposal fault') } }
    } },
  }
  apply(ctx, { sidecarUrl: base, autoStart: false, dpswarmDir: directory,
    pythonCmd: 'python', subagentProvider: 'spawn' })
  const parent = { id: 'real-root-session', session: { id: 'real-root-session', header: {} }, options: { provider: 'mock', model: 'b-kimi' } }
  const exec = { agent: parent, signal: new AbortController().signal }
  const request = { kind: 'derive', subtasks: [{ title: 'offline test', prompt: 'no model calls', provider: 'mock', model: 'b-kimi' }] }
  const tool = registered.get('dpswarm_delegate')
  const result = await tool.execute(request, exec)
  assert.equal(result.failed.length, 0)
  assert.equal(disposed, 1)
  const delivery = result.deliveries[0]
  assert.equal(delivery.execution_session_id, 'host-published-run-1')
  assert.deepEqual(delivery.token_usage, { input_tokens: null, output_tokens: null,
    cache_read_tokens: null, cache_write_tokens: null, cost_usd: null })
  let status = await (await fetch(base + '/api/status')).json()
  const item = status.snapshot.work_items[delivery.item_id]
  assert.equal(item.submission_session_id, 'host-published-run-1')
  const submittedId = item.submission_id
  const review = await registered.get('dpswarm_review').execute({ item_id: delivery.item_id, verdict: 'accept' }, exec)
  assert.equal(review.outcome, 'accepted')
  status = await (await fetch(base + '/api/status')).json()
  assert.equal(status.snapshot.open_worker_slots_used, 0)
  assert.equal(status.snapshot.active_points, 1)
  child.stdin.write(JSON.stringify({ command: 'restart' }) + '\n')
  assert.equal((await next()).restarted, true)
  status = await (await fetch(base + '/api/status')).json()
  assert.equal(status.snapshot.work_items[delivery.item_id].submission_id, submittedId)
  assert.equal(status.snapshot.work_items[delivery.item_id].acceptance, 'accepted')
  const foreign = { agent: { ...parent, id: 'other-root', session: { id: 'other-root', header: {} } } }
  await assert.rejects(tool.execute(request, foreign), error => error.code === 'ROOT_EXECUTION_CONFLICT')
  assert.equal(starts, 1)
  await assert.rejects(tool.execute(request, { agent: { ...parent, session: { id: parent.id, header: { delegationDepth: 1 } } } }), /NESTED_DELEGATION_UNSUPPORTED/)
  assert.equal(starts, 1)
  mode = 'disposal-error'
  const failed = await tool.execute(request, exec)
  assert.equal(failed.failed.length, 1)
  assert.equal(failed.failed[0].code, 'SUBAGENT_NOT_COMPLETED')
  assert.equal(failed.failed[0].details.disposalError, 'fixture disposal fault')
  assert.equal(failed.failed[0].control_settlement.outcome, 'terminated')
  assert.equal(failed.failed[0].control_settlement.failure.physical_cleanup_confirmed, false)
  status = await (await fetch(base + '/api/status')).json()
  assert.equal(status.snapshot.open_worker_slots_used, 0)
  assert.equal(status.snapshot.active_points, 1)
  assert.equal(status.snapshot.work_items[failed.failed[0].item_id].acceptance, 'terminated')
  assert.equal(status.snapshot.seal_phase.root, 'cutoff')
  child.stdin.write(JSON.stringify({ command: 'restart' }) + '\n')
  assert.equal((await next()).restarted, true)
  await assert.rejects(tool.execute(request, exec), error => error.code === 'EXECUTION_CLEANUP_UNCONFIRMED')
  assert.equal(starts, 2)
})
