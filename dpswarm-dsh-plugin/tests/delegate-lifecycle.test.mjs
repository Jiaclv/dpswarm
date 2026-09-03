import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'

const host = process.env.DSH_HOST_ROOT
  || 'C:/Users/93711/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai'
const available = existsSync(join(host, 'dsh-tools/lib/index.js'))
let apply
if (available) {
  // Model a running DSH host, whose shared modules are already initialized.
  // No host provider, session, sidecar, or model is started by these tests.
  await import(pathToFileURL(join(host, 'cosmokit/lib/index.js')).href)
  await import(pathToFileURL(join(host, 'dsh-tools/lib/index.js')).href)
  process.env.DPSWARM_SKIP_SETTINGS = '1'
  ;({ apply } = await import('../lib/index.js'))
}

function harness(t, kind = 'derive') {
  const tools = new Map(), requests = [], children = [], events = []
  const oldFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = oldFetch })
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool) } },
    systemPrompt: { section() {} }, inject(_deps, callback) { callback(ctx) }, effect() {},
    subagents: { async start(provider, options) {
      let resolve, reject
      const child = { id: 'actual-session-' + children.length, localAgent: undefined,
        result: new Promise((a, b) => { resolve = a; reject = b }), disposed: 0,
        async dispose() { this.disposed++; events.push('dispose:' + this.id) } }
      children.push({ child, resolve, reject, provider, options })
      events.push('start:' + child.id)
      return child
    } },
  }
  globalThis.fetch = async (url, options) => {
    const path = new URL(url).pathname
    requests.push({ path, body: options.body ? JSON.parse(options.body) : null })
    events.push(path)
    const item = { item_id: 'cp-item', node_id: 'cp-node', kind, level: 'B',
      context_epoch: 0, session_id: 'cp-session' }
    if (kind === 'split') Object.assign(item, { assistant_node_id: 'assistant-node', channel_id: 'peer-channel' })
    return new Response(JSON.stringify(path === '/api/delegate' ? { items: [item] } : { ok: true }), { status: 200 })
  }
  apply(ctx, { sidecarUrl: 'http://offline.invalid', autoStart: false,
    dpswarmDir: join(tmpdir(), 'nonexistent-dpswarm-test-' + randomUUID()),
    pythonCmd: 'python', subagentProvider: 'spawn' })
  const signal = new AbortController()
  const parent = { id: 'parent-session', session: { id: 'parent-session' },
    options: { provider: 'parent-provider', model: 'parent-model' } }
  const execute = () => tools.get('dpswarm_delegate').execute({ kind,
    subtasks: [{ title: 'fixture', prompt: 'fixture only', provider: 'selected-provider', model: 'selected-model' }] },
  { agent: parent, signal: signal.signal })
  const results = () => requests.filter(value => value.path === '/api/submit')
  return { execute, results, children, requests, events, signal, parent }
}

test('delegate never submits while child result is pending; submits real output after disposal', { skip: !available }, async t => {
  const h = harness(t)
  const running = h.execute()
  await nextTurn()
  assert.equal(h.children.length, 1)
  assert.equal(h.results().length, 0)
  assert.equal(h.children[0].options.parent, h.parent)
  assert.deepEqual(h.children[0].options.agentOptions, { provider: 'selected-provider', model: 'selected-model' })
  h.children[0].resolve({ output: [{ type: 'text', text: 'ACTUAL DELIVERY' }], stopReason: 'completed' })
  const result = await running
  assert.equal(h.children[0].child.disposed, 1)
  assert.equal(h.results().length, 1)
  assert.equal(h.results()[0].body.output, 'ACTUAL DELIVERY')
  assert.equal(h.results()[0].body.stop_reason, 'completed')
  assert.ok(h.events.indexOf('dispose:actual-session-0') < h.events.indexOf('/api/submit'))
  assert.equal(result.deliveries[0].execution_session_id, 'actual-session-0')
})

test('model refusal is a failure without submit', { skip: !available }, async t => {
  const h = harness(t), running = h.execute()
  await nextTurn()
  h.children[0].resolve({ output: [{ type: 'text', text: 'partial' }], stopReason: 'refusal' })
  const result = await running
  assert.deepEqual(result.deliveries, [])
  assert.equal(result.failed[0].code, 'SUBAGENT_NOT_COMPLETED')
  assert.equal(h.results().length, 0)
  assert.equal(h.children[0].child.disposed, 1)
})

test('rejected result and cancelled pending child never submit', { skip: !available }, async t => {
  const h = harness(t), running = h.execute()
  await nextTurn()
  h.children[0].reject(new Error('child transport failed'))
  const result = await running
  assert.equal(result.failed.length, 1)
  assert.equal(h.results().length, 0)
  assert.equal(h.children[0].child.disposed, 1)
})

test('cancelled pending child is disposed without submit', { skip: !available }, async t => {
  const h = harness(t), running = h.execute()
  await nextTurn()
  h.signal.abort()
  const result = await running
  assert.equal(result.failed[0].code, 'SUBAGENT_ABORTED')
  assert.equal(h.results().length, 0)
  assert.equal(h.children[0].child.disposed, 1)
  h.children[0].resolve({ output: [], stopReason: 'aborted' })
})

test('split waits for assistant before peer delivery and primary execution', { skip: !available }, async t => {
  const h = harness(t, 'split'), running = h.execute()
  await nextTurn()
  assert.equal(h.children.length, 1)
  assert.equal(h.requests.filter(value => value.path === '/api/peer').length, 0)
  h.children[0].resolve({ output: [{ type: 'text', text: 'ACTUAL ASSISTANT' }], stopReason: 'completed' })
  await nextTurn()
  assert.equal(h.children.length, 2)
  assert.equal(h.children[0].child.disposed, 1)
  assert.equal(h.requests.find(value => value.path === '/api/peer').body.body, 'ACTUAL ASSISTANT')
  assert.match(h.children[1].options.prompt[0].text, /ACTUAL ASSISTANT/)
  assert.equal(h.results().length, 0)
  h.children[1].resolve({ output: [{ type: 'text', text: 'ACTUAL PRIMARY' }], stopReason: 'completed' })
  const result = await running
  assert.equal(result.deliveries[0].output, 'ACTUAL PRIMARY')
  assert.equal(h.results().length, 1)
})

test('failed split assistant cannot start primary or emit peer/submit', { skip: !available }, async t => {
  const h = harness(t, 'split'), running = h.execute()
  await nextTurn()
  h.children[0].resolve({ output: [], stopReason: 'error' })
  const result = await running
  assert.equal(result.failed.length, 1)
  assert.equal(h.children.length, 1)
  assert.equal(h.requests.filter(value => ['/api/peer', '/api/submit'].includes(value.path)).length, 0)
  assert.equal(h.children[0].child.disposed, 1)
})
