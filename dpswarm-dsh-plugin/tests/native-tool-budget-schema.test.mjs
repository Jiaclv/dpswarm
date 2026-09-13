import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'node:http'
import { Sidecar } from '../lib/sidecar.js'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { validateJsonSchemaValue } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-tools/lib/index.js'))
const { apply } = await import('../lib/index.js')

function registeredTools() {
  const previous = process.env.DPSWARM_SKIP_SETTINGS
  process.env.DPSWARM_SKIP_SETTINGS = '1'
  const tools = new Map()
  const ctx = { get(name) { return this[name] }, on() { return () => {} }, provide(name, value) { this[name] = value },
    tools: { register(tool) { tools.set(tool.name, tool) } },
    subagents: { start() { throw new Error('No native workers permitted in schema test') } },
    systemPrompt: { section() {} }, inject(_deps, fn) { fn(ctx) }, effect() {} }
  try { apply(ctx, { autoStart: false }) }
  finally { if (previous === undefined) delete process.env.DPSWARM_SKIP_SETTINGS; else process.env.DPSWARM_SKIP_SETTINGS = previous }
  return tools
}

test('native registered run schema no longer exposes Lead-selected worker budgets', () => {
  const schema = registeredTools().get('dpswarm_run').parameters
  assert.ok(!('worker_budgets' in schema.properties), 'worker_budgets was removed together with Auto mode')
})

test('native registered review schema exposes explicit boolean takeover', () => {
  const schema = registeredTools().get('dpswarm_review').parameters
  assert.equal(schema.properties.takeover.type, 'boolean')
  assert.deepEqual(validateJsonSchemaValue(schema, { item_id: 'wi-test', verdict: 'accept', takeover: true, reason: 'Lead verified current artifact' }), [])
  assert.ok(validateJsonSchemaValue(schema, { item_id: 'wi-test', verdict: 'accept', takeover: 'yes' }).length > 0)
})

test('sidecar failure preserves admission cleanup evidence for controller settlement', async t => {
  const details = { ok: false, error: 'POINTS_EXCEEDED', message: 'Worker admission denied', partial: [],
    created: [{ item_id: 'wi-empty', subtask_index: 0 }], started: [],
    pending: [{ item_id: 'wi-empty', cleanup_status: 'terminated' }],
    cleanup: [{ item_id: 'wi-empty', status: 'terminated', reason: 'admission-failed' }], cleanup_complete: true }
  const server = createServer((_req, res) => { res.writeHead(409, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(details)) })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise(resolve => server.close(resolve)))
  const sidecar = new Sidecar({ sidecarUrl: `http://127.0.0.1:${server.address().port}`, sessionId: 'test', workspace: '/nonexistent-dpswarm-schema-test' })
  await assert.rejects(sidecar.call('POST', '/api/delegate', { subtasks: [] }), error => {
    assert.equal(error.code, 'POINTS_EXCEEDED')
    assert.deepEqual(error.details, details)
    return true
  })
})
