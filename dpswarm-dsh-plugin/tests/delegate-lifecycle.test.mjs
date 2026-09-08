import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'

import { Sidecar } from '../lib/sidecar.js'
import { delegateOnce } from '../lib/delegation.js'
const available = true

function harness(t, kind = 'derive', savedSettings = null) {
  const tools = new Map(), requests = [], children = [], events = []
  const oldFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = oldFetch })
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool) } },
    settings: { register() { return { get: () => savedSettings, watch() {} } } },
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
  const audit = { root_session_id: 'parent-session', revision: 0, events: [], head_hash: '0'.repeat(64) }
  globalThis.fetch = async (url, options) => {
    const path = new URL(url).pathname
    requests.push({ path, origin: new URL(url).origin, body: options.body ? JSON.parse(options.body) : null })
    events.push(path)
    const item = { item_id: 'cp-item', node_id: 'cp-node', kind, level: 'B',
      context_epoch: 0, session_id: 'cp-session', attempt: 1 }
    if (kind === 'split') Object.assign(item, { assistant_node_id: 'assistant-node', channel_id: 'peer-channel',
      assistant_fence: { context_epoch: 0, session_id: 'assistant-reservation', attempt: 1 } })
    const body = options.body ? JSON.parse(options.body) : {}
    if (path === '/api/plugin-audit' && options.method === 'POST') { assert.equal(body.expected_revision, audit.revision); audit.revision++; audit.events.push(...body.events) }
    const response = path === '/api/plugin-audit' ? audit : path === '/api/delegate' ? { items: [item] }
      : path === '/api/execution/bind' ? { ok: true, context_epoch: body.context_epoch, session_id: body.execution_session_id }
        : { ok: true, outcome: path === '/api/execution/fail' ? 'terminated' : undefined }
    return new Response(JSON.stringify(response), { status: 200 })
  }
  const config = { sidecarUrl: 'http://127.0.0.1:18791', autoStart: false,
    dpswarmDir: join(tmpdir(), 'nonexistent-dpswarm-test-' + randomUUID()),
    pythonCmd: 'python', subagentProvider: 'spawn' }

  const signal = new AbortController()
  const parent = { id: 'parent-session', session: { id: 'parent-session', header: {}, requestHeader: () => ({ config: { provider: 'parent-provider', model: 'parent-model' } }) },
    options: { provider: 'startup-provider', model: 'deepseek-v4-pro', reasoningEffort: 'high' } }
  const execute = () => delegateOnce({ kind,
    subtasks: [{ title: 'fixture', prompt: 'fixture only', provider: 'selected-provider', model: 'selected-model' }] },
  { agent: parent, signal: signal.signal }, new Sidecar({ ...config, ...savedSettings }), ctx.subagents)
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
  assert.deepEqual(Object.fromEntries(Object.entries(h.children[0].options.agentOptions)), { provider: 'selected-provider', model: 'selected-model' })
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

test('published identity is the submit fence and failures explicitly settle resources', { skip: !available }, async t => {
  const h = harness(t), running = h.execute()
  await nextTurn()
  const bind = h.requests.find(r => r.path === '/api/execution/bind').body
  assert.equal(bind.execution_session_id, h.children[0].child.id)
  assert.equal(bind.parent_session_id, h.parent.session.id)
  assert.equal(bind.reservation_session_id, 'cp-session')
  h.children[0].resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  await running
  assert.equal(h.results()[0].body.session_id, h.children[0].child.id)
  assert.equal(h.results()[0].body.token_usage.cost_usd, null)
})

test('saved settings source applies to the next call and stays fixed during a running child', { skip: !available }, async t => {
  delete process.env.DPSWARM_SKIP_SETTINGS
  t.after(() => { process.env.DPSWARM_SKIP_SETTINGS = '1' })
  const saved = { sidecarUrl: 'http://127.0.0.1:18792', subagentProvider: 'fork' }
  const h = harness(t, 'derive', saved), running = h.execute()
  await nextTurn()
  assert.equal(h.children[0].provider, 'fork')
  saved.sidecarUrl = 'http://127.0.0.1:18793'
  h.children[0].resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  await running
  assert.ok(h.requests.every(request => request.origin === 'http://127.0.0.1:18792'))
  const second = h.execute()
  await nextTurn()
  h.children[1].resolve({ output: [{ type: 'text', text: 'next' }], stopReason: 'completed' })
  await second
  assert.equal(h.requests.at(-1).origin, 'http://127.0.0.1:18793')
})

test('nested caller is rejected before admission and starting a child', { skip: !available }, async t => {
  const h = harness(t)
  h.parent.session.header.delegationDepth = 1
  await assert.rejects(h.execute(), /NESTED_DELEGATION_UNSUPPORTED/)
  assert.equal(h.requests.length, 0)
  assert.equal(h.children.length, 0)
})

test('disposal failure preserves initial failure and reports failed physical cleanup', { skip: !available }, async t => {
  const h = harness(t), running = h.execute()
  await nextTurn()
  h.children[0].child.dispose = async () => { throw new Error('dispose also failed') }
  h.children[0].resolve({ output: [], stopReason: 'refusal' })
  const result = await running
  assert.equal(result.failed[0].code, 'SUBAGENT_NOT_COMPLETED')
  assert.equal(result.failed[0].details.disposalError, 'dispose also failed')
  const failure = h.requests.find(r => r.path === '/api/execution/fail')
  assert.equal(failure.body.physical_cleanup_confirmed, false)
  assert.equal(result.failed[0].control_settlement.outcome, 'terminated')
  assert.equal(h.results().length, 0)
})
