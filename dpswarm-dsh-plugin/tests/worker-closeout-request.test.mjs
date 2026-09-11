import assert from 'node:assert/strict'
import test from 'node:test'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const lib = name => process.env.DPSWARM_TEST_LIB
  ? pathToFileURL(resolve(process.env.DPSWARM_TEST_LIB, name)).href : new URL('../lib/' + name, import.meta.url).href
const { WorkerBudgetRuntime } = await import(lib('budget-runtime.js'))
const { CLOSEOUT_INSTRUCTION, CLOSEOUT_MARKER } = await import(lib('worker-closeout.js'))

async function fixture() {
  const root = { id: 'lead', header: { id: 'lead' }, events: [] }
  const child = { id: 'worker', header: { id: 'worker', parentSession: root.id, origin: 'subagent', delegationDepth: 1 }, events: [] }
  const sessions = new Map([root, child].map(s => [s.id, s]))
  const journal = new MemoryAuditJournal()
  const create = () => new WorkerBudgetRuntime({ config: () => ({ workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28 }),
    resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  const runtime = create(), agent = { session: child }, state = await runtime.ensure(agent)
  // Reproduce the real 19:52 worker's seven settled calls, including cached input.
  for (let i = 0; i < 7; i++) {
    const ticket = await runtime.admit(state, { provider: 'fixture', model: 'local', messages: [], maxTokens: 1024 })
    await runtime.settle(ticket, { inputTokens: i === 6 ? 25738 : 1000, cacheReadTokens: 52000, outputTokens: 428 }, 'stop')
  }
  assert.equal(runtime.describe(state).committed_tokens, 398734)
  await runtime.prepareCloseout(state, { inputEstimate: 85282, finalInputEstimate: 76513 })
  assert.equal(state.closeout.mode, 'final_only')
  return { runtime, state, create, agent, journal }
}
const message = (role, text) => Object.freeze({ role, content: Object.freeze([{ type: 'text', text }]) })
const request = (extra = {}) => Object.freeze({ provider: 'fixture', model: 'local', maxTokens: 113003,
  messages: Object.freeze([message('system', CLOSEOUT_INSTRUCTION), message('user', 'Report the saved candidate.')]), ...extra })

test('DSH 0.1.5 frozen system-role request admits a closeout without legacy system or extra allocation', async () => {
  const { runtime, state } = await fixture(), options = request()
  assert.equal(options.system, undefined)
  const ticket = await runtime.admit(state, options)
  assert.ok(ticket)
  assert.equal(state.profile.tokenLimit, 600000)
  assert.equal(runtime.describe(state).calls, 8)
  assert.equal(options.system, undefined, 'must not modify or duplicate the frozen request prompt')
})

test('closeout authorization follows only the last nonempty system message', async t => {
  const cases = [
    ['legacy one-shot', { messages: [], system: CLOSEOUT_INSTRUCTION }, true],
    ['system string content', { messages: [{ role: 'system', content: CLOSEOUT_INSTRUCTION }] }, true],
    ['empty trailing projection', { messages: [message('system', CLOSEOUT_INSTRUCTION), message('system', ' \n ')] }, true],
    ['ordinary user after system', { messages: [message('system', CLOSEOUT_INSTRUCTION), message('user', 'continue')] }, true],
    ['user-only marker', { messages: [message('user', CLOSEOUT_INSTRUCTION)] }, false],
    ['tool-only marker', { messages: [message('tool', CLOSEOUT_INSTRUCTION)] }, false],
    ['assistant-only marker', { messages: [message('assistant', CLOSEOUT_INSTRUCTION)] }, false],
    ['superseded marker', { messages: [message('system', CLOSEOUT_INSTRUCTION), message('system', 'Current system has no closeout instruction.')] }, false],
    ['legacy marker superseded', { system: CLOSEOUT_INSTRUCTION, messages: [message('system', 'New prompt.')] }, false],
    ['current marker supersedes legacy', { system: 'Old prompt.', messages: [message('system', CLOSEOUT_INSTRUCTION)] }, true],
    ['non-text system part', { messages: [{ role: 'system', content: [{ type: 'image', text: CLOSEOUT_MARKER }] }] }, false],
    ['missing instruction', { messages: [] }, false],
  ]
  for (const [name, extra, allowed] of cases) await t.test(name, async () => {
    const { runtime, state } = await fixture()
    if (allowed) assert.ok(await runtime.admit(state, request(extra)))
    else {
      await assert.rejects(runtime.admit(state, request(extra)), { code: 'WORKER_CLOSEOUT_INSTRUCTION_MISSING' })
      assert.equal(runtime.describe(state).calls, 7, 'rejection must not create an admitted model call')
    }
  })
})

test('native closeout after cold restore retains two-call maximum and final-only compaction gate', async () => {
  const { create, agent } = await fixture(), runtime = create(), state = await runtime.ensure(agent)
  await assert.rejects(runtime.admit(state, request({ purpose: 'compaction' })), { code: 'WORKER_CLOSEOUT_CM_DEFERRED' })
  for (let i = 0; i < 2; i++) await runtime.settle(await runtime.admit(state, request()), { inputTokens: 100, outputTokens: 100 }, 'stop')
  await assert.rejects(runtime.admit(state, request()), { code: 'WORKER_CLOSEOUT_ALREADY_SENT' })
})

test('correct system instruction cannot bypass the remaining token limit', async () => {
  const { runtime, state } = await fixture()
  await assert.rejects(runtime.admit(state, request({ maxTokens: 201270 })), { code: 'WORKER_TOKEN_RESERVATION_DENIED' })
  assert.equal(runtime.describe(state).calls, 7)
})