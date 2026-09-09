/**
 * Rail-model characterization (0.8.2, slack added 0.9.2): limits are anomaly
 * rails, not tight plans. No output squeeze; closeout = safe park when one more
 * full step plus a report (plus a 1/3 estimate-uncertainty slack) no longer
 * fits; the Lead decides continuation via linked rework.
 * Numbers are the real ledger values from the 911 and 17:32 production runs.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime } from '../lib/budget-runtime.js'
import { CLOSEOUT_INSTRUCTION, CLOSEOUT_REPORT_FLOOR } from '../lib/worker-closeout.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

function makeSession(id, parent = null, events = []) {
  return { id, header: { id, ...(parent ? { parentSession: parent, origin: 'subagent', delegationDepth: 1, seedLength: 0 } : {}) }, events }
}
function fixture(config = {}) {
  const root = makeSession('lead'), a = makeSession('a', root.id)
  const sessions = new Map([root, a].map(session => [session.id, session]))
  const journal = new MemoryAuditJournal(), cfg = { workerBudgetMode: 'manual', workerTokenLimit: 200000, workerCallLimit: 12, ...config }
  const runtime = () => new WorkerBudgetRuntime({ config: () => cfg, resolveSession: id => sessions.get(id), listSessions: () => [...sessions.values()], journal })
  return { root, a, sessions, journal, cfg, runtime, agent: session => ({ session }) }
}
const signal = () => new AbortController().signal
const call = (extra = {}) => ({ provider: 'p', model: 'm', messages: [], maxTokens: 32768, ...extra })

test('rail model: the 17:32 tester numbers now park (0.9.2 slack closes the estimate-error death zone)', async () => {
  const h = fixture({ workerTokenLimit: 60000, workerCallLimit: 8 }), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  await r.settle(await r.admit(state, call()), { inputTokens: 2565, outputTokens: 223, cacheReadTokens: 9216 }, 'tool-calls')
  await r.settle(await r.admit(state, call()), { inputTokens: 304, outputTokens: 64, cacheReadTokens: 11776 }, 'tool-calls')
  // 35,852 remain. Bare arithmetic said one more 14,028 step plus a report fits
  // (35,852 >= 28,056 + 6,144) — but the 00:14 implementer died in exactly this
  // zone: the estimate understated the measured envelope, the park never engaged,
  // and the hard rail killed the turn with no report. The 1/3 slack parks it now.
  await r.prepareCloseout(state, { inputEstimate: 14028, finalInputEstimate: 14028 }, signal())
  assert.equal(state.closeout.mode, 'final_only')
  assert.equal(state.closeout.trigger, 'budget_rail')
  assert.equal(state.closeout.estimate_slack, 9352)
  // Still no squeeze of the parked delivery: full remaining room, bounded only by what remains.
  assert.equal(r.outputLimit(state, 14028, 32768), 21824)
})

test('rail model: the 911 tester numbers park with the budget_rail trigger (deliver what is saved, Lead decides)', async () => {
  const h = fixture(), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  for (const [inputTokens, outputTokens] of [[30000, 1000], [30000, 1000], [30406, 1516]]) {
    await r.settle(await r.admit(state, call()), { inputTokens, outputTokens }, 'stop')
  }
  // 106,078 remain of 200,000; a 51k step + 51k final + floor exceeds that.
  await r.prepareCloseout(state, { inputEstimate: 51000, finalInputEstimate: 51000 }, signal())
  assert.equal(state.closeout.mode, 'final_only')
  assert.equal(state.closeout.trigger, 'budget_rail')
  // Parked delivery keeps full remaining room (no squeeze): 106,078 - 51,000.
  assert.equal(r.outputLimit(state, 51000, 32768), 32768)
  assert.match(CLOSEOUT_INSTRUCTION, /Lead will decide|Lead decides/)
})

test('rail model: genuinely short budgets still park instead of overrunning (lower bound preserved)', async () => {
  const h = fixture({ workerTokenLimit: 600000, workerCallLimit: 36 }), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  for (let i = 0; i < 7; i++) await r.settle(await r.admit(state, call()), { inputTokens: 60000, outputTokens: 10000 }, 'stop')
  assert.equal(r.describe(state).remaining_tokens, 110000)
  await r.prepareCloseout(state, { inputEstimate: 70000, finalInputEstimate: 70000 }, signal())
  assert.equal(state.closeout.mode, 'final_only')
  assert.equal(state.closeout.trigger, 'budget_rail')
  await assert.rejects(r.admit(state, call()), { code: 'WORKER_CLOSEOUT_INSTRUCTION_MISSING' })
})

test('rail model: output is never squeezed below the request while room remains (no progressive halving)', async () => {
  const h = fixture({ workerTokenLimit: 100000, workerCallLimit: 6 }), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  await r.prepareCloseout(state, { inputEstimate: 10000, finalInputEstimate: 9000 }, signal())
  assert.equal(state.closeout, undefined)
  assert.equal(r.outputLimit(state, 10000, 90000), 90000, 'full request, no halving reserve')
})
