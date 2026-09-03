import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { runSubagentToCompletion } from '../lib/subagent-run.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((a, b) => { resolve = a; reject = b })
  return { promise, resolve, reject }
}

function fixture() {
  const pending = deferred()
  const signal = new AbortController()
  const handle = { id: 'real-child-session', localAgent: undefined, result: pending.promise,
    disposals: 0, async dispose() { this.disposals++ } }
  const calls = []
  const subagents = { async start(provider, request) { calls.push({ provider, request }); return handle } }
  const request = { parent: { id: 'parent-session' }, signal: signal.signal,
    agentOptions: { provider: 'chosen-provider', model: 'chosen-model' } }
  return { pending, signal, handle, calls, subagents, request }
}

test('a published handle is not a completed result; wait for result and dispose', async () => {
  const f = fixture()
  let returned = false
  const promise = runSubagentToCompletion(f.subagents, 'spawn', f.request).then(value => {
    returned = true
    return value
  })
  await nextTurn()
  assert.equal(returned, false)
  assert.equal(f.handle.disposals, 0)
  f.pending.resolve({ output: [{ type: 'text', text: 'Actual final' }, { type: 'text', text: 'message' }], stopReason: 'completed' })
  assert.deepEqual(await promise, { text: 'Actual final\nmessage', stopReason: 'completed', sessionId: 'real-child-session' })
  assert.equal(f.handle.disposals, 1)
  assert.equal(f.calls[0].provider, 'spawn')
  assert.equal(f.calls[0].request, f.request)
})

test('wait for quiescent disposal before returning evidence', async () => {
  const f = fixture(), disposed = deferred()
  f.handle.dispose = async () => { f.handle.disposals++; await disposed.promise }
  f.pending.resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  let returned = false
  const promise = runSubagentToCompletion(f.subagents, 'spawn', f.request).then(() => { returned = true })
  await nextTurn()
  assert.equal(f.handle.disposals, 1)
  assert.equal(returned, false)
  disposed.resolve()
  await promise
  assert.equal(returned, true)
})

for (const stopReason of ['error', 'aborted', 'refusal', 'max-tokens', 'unknown-future-stop']) {
  test(`non-completed terminal reason ${stopReason} is not a delivery`, async () => {
    const f = fixture()
    f.pending.resolve({ output: [{ type: 'text', text: 'partial answer' }], stopReason })
    await assert.rejects(runSubagentToCompletion(f.subagents, 'spawn', f.request), error => {
      assert.equal(error.code, 'SUBAGENT_NOT_COMPLETED')
      assert.equal(error.details.stopReason, stopReason)
      assert.equal(error.details.output, 'partial answer')
      return true
    })
    assert.equal(f.handle.disposals, 1)
  })
}

test('infrastructure rejection disposes the published handle and rejects', async () => {
  const f = fixture()
  const promise = runSubagentToCompletion(f.subagents, 'spawn', f.request)
  f.pending.reject(new Error('infrastructure fault'))
  await assert.rejects(promise, /infrastructure fault/)
  assert.equal(f.handle.disposals, 1)
})

test('disposal failure cannot return an otherwise successful result', async () => {
  const f = fixture()
  f.pending.resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  f.handle.dispose = async () => { throw new Error('still running') }
  await assert.rejects(runSubagentToCompletion(f.subagents, 'spawn', f.request), { code: 'SUBAGENT_DISPOSAL_FAILED' })
})

test('cancellation disposes a pending child and never fabricates completed', async () => {
  const f = fixture()
  const promise = runSubagentToCompletion(f.subagents, 'spawn', f.request)
  await nextTurn()
  f.signal.abort()
  await assert.rejects(promise, { code: 'SUBAGENT_ABORTED' })
  assert.equal(f.handle.disposals, 1)
  f.pending.resolve({ output: [], stopReason: 'aborted' })
})

test('an already cancelled request never starts a child', async () => {
  const f = fixture()
  f.signal.abort()
  await assert.rejects(runSubagentToCompletion(f.subagents, 'spawn', f.request), { code: 'SUBAGENT_ABORTED' })
  assert.equal(f.calls.length, 0)
})

for (const value of [undefined, {}, { output: [], stopReason: 'completed' },
  { output: [{ type: 'text', text: 'not terminal' }] }]) {
  test(`missing terminal fields or empty output are rejected: ${JSON.stringify(value)}`, async () => {
    const f = fixture()
    f.pending.resolve(value)
    await assert.rejects(runSubagentToCompletion(f.subagents, 'spawn', f.request), error =>
      ['SUBAGENT_INVALID_RESULT', 'SUBAGENT_EMPTY_DELIVERY'].includes(error.code))
    assert.equal(f.handle.disposals, 1)
  })
}

test('publication callback receives exact trusted child handle before settling', async () => {
  const f = fixture(), bound = []
  f.pending.resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  const result = await runSubagentToCompletion(f.subagents, 'spawn', f.request, {
    onPublished: async run => { bound.push(run); assert.equal(f.handle.disposals, 0) },
  })
  assert.equal(bound[0], f.handle)
  assert.equal(result.sessionId, f.handle.id)
})

test('publication binding failure still disposes child and rejects delivery', async () => {
  const f = fixture()
  f.pending.resolve({ output: [{ type: 'text', text: 'done' }], stopReason: 'completed' })
  await assert.rejects(runSubagentToCompletion(f.subagents, 'spawn', f.request, {
    onPublished: async () => { throw new Error('binding rejected') },
  }), /binding rejected/)
  assert.equal(f.handle.disposals, 1)
})
