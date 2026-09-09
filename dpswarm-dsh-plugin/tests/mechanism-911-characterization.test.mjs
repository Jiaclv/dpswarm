/**
 * Characterization tests pinning 0.7.7 behavior observed in the first real-model
 * run (session 911, 2026-09-09: fixed team on deepseek-v4.1-flash, Auto budgets).
 * Each test states whether the 0.7.8 patches keep it (invariant) or intentionally
 * flip it (behavior change), so the patch phase has an explicit red/green matrix.
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { WorkerBudgetRuntime, workerBudgetProfile, estimateRequestTokens } from '../lib/budget-runtime.js'
import { CLOSEOUT_INSTRUCTION } from '../lib/worker-closeout.js'
import { compactWorkerEntry } from '../lib/worker-diagnostics.js'
import { installBudget } from '../lib/budget.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const host = resolveHostRoot()
const [{ Context }, { Session }] = await Promise.all(['cordis', 'dsh-session'].map(p => import(hostModuleUrl(host, `${p}/lib/index.js`))))

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
const call = (extra = {}) => ({ provider: 'p', model: 'm', messages: [], maxTokens: 2048, ...extra })

test('911 closeout: tester entered final-only with 53% budget and 9 calls left, then delivered in 21.5k (0.8.2: trigger renamed budget_rail)', async () => {
  const h = fixture(), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  // 911 tester role: 3 settled calls, 93,922 observed tokens of a 200,000 grant.
  for (const [inputTokens, outputTokens] of [[30000, 1000], [30000, 1000], [30406, 1516]]) {
    await r.settle(await r.admit(state, call()), { inputTokens, outputTokens }, 'stop')
  }
  assert.equal(r.describe(state).remaining_tokens, 106078)
  assert.equal(r.describe(state).remaining_calls, 9)
  await r.prepareCloseout(state, { inputEstimate: 51000, finalInputEstimate: 51000 }, signal())
  assert.equal(state.closeout.mode, 'final_only')
  assert.equal(state.closeout.trigger, 'budget_rail')
  assert.equal(state.closeout.calls_at_closeout, 3)
  // The real 911 tester still delivered in its single remaining call (21,516
  // tokens), leaving 84,562 granted tokens unused — early closeout shrank
  // verification depth, and the head-occlusion defect escaped to the Lead.
  const final = await r.admit(state, call({ system: CLOSEOUT_INSTRUCTION, tools: [] }))
  await r.settle(final, { inputTokens: 19000, outputTokens: 2516 }, 'stop')
  assert.equal(r.describe(state).remaining_tokens, 84562)
  assert.equal(r.describe(state).remaining_calls, 8)
  // Rail closeout: a marker-less call is refused for the missing instruction;
  // one tool-attempt step plus the report call may still be admitted past it.
  await assert.rejects(r.admit(state, call()), { code: 'WORKER_CLOSEOUT_INSTRUCTION_MISSING' })
  await r.settle(await r.admit(state, call({ system: CLOSEOUT_INSTRUCTION })), { inputTokens: 500, outputTokens: 100 }, 'stop')
  await assert.rejects(r.admit(state, call({ system: CLOSEOUT_INSTRUCTION })), { code: 'WORKER_CLOSEOUT_ALREADY_SENT' })
})

test('911 closeout re-evaluated: with meter-accurate estimates the same budget does NOT close out (Phase 2b gate evidence: the reserve rule stays)', async () => {
  const h = fixture(), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  // Same 911 tester ledger: 93,922 of 200,000 committed over 3 calls (~31k each).
  for (const [inputTokens, outputTokens] of [[30000, 1000], [30000, 1000], [30406, 1516]]) {
    await r.settle(await r.admit(state, call()), { inputTokens, outputTokens }, 'stop')
  }
  // The 0.7.7 trigger came from inflated estimates (~51k vs ~31k observed):
  // 106,078 < 51,000*2 + 4,096 by 18 tokens. With honest estimates the reserve
  // rule (one more exploration step + final report must both fit) is satisfied
  // twice over: 106,078 >= 31,000*2 + 4,096.
  await r.prepareCloseout(state, { inputEstimate: 31000, finalInputEstimate: 31000 }, signal())
  assert.equal(state.closeout, undefined)
  // And the rule still protects genuinely short budgets (existing suite covers
  // this: 110,000 remaining vs 70,000 inputs must trigger final-only).
})

test('17:32 tester: under the rail model it neither parks early nor gets squeezed (the 0.8.1 floor trigger is superseded)', async () => {
  const h = fixture({ workerTokenLimit: 60000, workerCallLimit: 8 }), r = h.runtime(), state = await r.ensure(h.agent(h.a), signal())
  // Real ledger: two settled calls, 24,148 committed of 60,000 (35,852 remain).
  await r.settle(await r.admit(state, call()), { inputTokens: 2565, outputTokens: 223, cacheReadTokens: 9216 }, 'tool-calls')
  await r.settle(await r.admit(state, call()), { inputTokens: 304, outputTokens: 64, cacheReadTokens: 11776 }, 'tool-calls')
  // One more full step (≈14,028 in) plus a report still fits; no park, and the
  // next call keeps its full output bound — the 5,710-token squeeze death of
  // the real 17:32 run cannot happen anymore.
  await r.prepareCloseout(state, { inputEstimate: 14028, finalInputEstimate: 14028 }, signal())
  assert.equal(state.closeout, undefined)
  assert.equal(r.outputLimit(state, 14028, 32768), 21824)
})

test('911 estimator: chars/3 understates Chinese-heavy input by at least 2x (invariant: documents why; estimateRequestTokens itself is unchanged)', () => {
  const chars = 30000, chinese = '良'.repeat(chars)
  const estimate = estimateRequestTokens({ system: '', messages: [{ role: 'user', content: [{ type: 'text', text: chinese }] }], tools: [] }).input
  assert.ok(estimate <= Math.ceil((chars + 200) / 3), 'chars/3 mechanics')
  assert.ok(estimate * 2 < chars, 'real tokenizers price CJK near 1 token/char; chars/3 is a 2-3x underestimate')
})

function hostFixture(config = {}) {
  const ctx = new Context()
  const root = Session.create('lead', undefined, { version: 0, id: 'lead', createdAt: 1 })
  const child = Session.create('child', undefined, { version: 0, id: 'child', createdAt: 2, parentSession: root.id, origin: 'subagent', delegationDepth: 1 })
  const sessions = new Map([root, child].map(s => [s.id, s]))
  const agents = new Map([...sessions.values()].map(s => [s.id, { session: s, options: { provider: 'worker-provider', model: 'model' } }]))
  const assembly = { sections: [], contexts: [], variables: {}, tools: [] }
  ctx.provide('systemPrompt', { assemble: async context => {
    const current = structuredClone(assembly)
    return ctx.waterfall('system-prompt/assemble', current, context, async () => current)
  } })
  ctx.provide('sessions', { get: id => sessions.get(id), list: () => [...sessions.values()], flush: async () => {} })
  ctx.provide('agents', { get: id => agents.get(id) })
  const measures = []
  ctx.provide('tokenMeter', { measure: (...args) => { measures.push(args); return { totalTokens: 424242 } } })
  ctx.provide('llm', { resolveCallConfig: async c => ({ ...c, maxTokens: c.maxTokens ?? 32768 }) })
  const cfg = { workerBudgetMode: 'manual', workerTokenLimit: 1000000, workerCallLimit: 50, ...config }
  const service = installBudget(ctx, () => cfg, { journal: new MemoryAuditJournal() })
  return { ctx, child, agents, service, measures }
}

test('911 estimator: pre-step closeout forecast prefers the native token meter (0.7.8 Phase 2a)', async () => {
  const h = hostFixture(), agent = h.agents.get('child')
  await h.ctx.get('systemPrompt').assemble({ agent })
  await h.ctx.waterfall('agent/pre-step', { agent, messages: [] }, async () => ({ kind: 'enter' }))
  assert.equal(h.measures.length, 2, 'prepareStep now measures input and final-input envelopes with the native meter')
  assert.equal(h.service.runtime.states.get('child').stepBudget.input_estimate, 424242,
    'the forecast uses the metered envelope instead of the chars/3 serialization')
  const original = Object.freeze({ provider: 'worker-provider', model: 'model', maxTokens: 5000 })
  await h.ctx.waterfall('agent/request', { agent }, async () => original)
  assert.equal(h.measures.length, 3, 'agent/request keeps its existing meter measurement')
  h.service.shutdown()
})

async function completedFixedImplementer(h, decisions) {
  const r = h.runtime()
  const handle = await r.beginTeamRun(h.agent(h.root), { roles: ['implementer'], decisions, expectedProfile: workerBudgetProfile(h.cfg, h.root.id) })
  const grant = await r.issueTeamWorker(h.agent(h.root), handle, { task: 'Implement the requested file.', label: 'implementer' })
  h.a.events.push({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: grant.prompt }] } })
  const state = await r.ensure(h.agent(h.a), signal())
  await r.settle(await r.admit(state, call({ maxTokens: 100 })), { inputTokens: 20, outputTokens: 10 }, 'completed')
  h.a.events.push({ type: 'turn/end', data: { reason: { kind: 'completed' } }, time: Date.now() })
  return r
}

test('911 rework: issueRework is hardcoded unlimited under every initial-worker mode (pins 0.7.7; 0.7.8 adds an opt-in fixed mode, default stays unlimited)', async t => {
  for (const mode of ['manual', 'auto', 'unlimited']) await t.test(mode, async () => {
    const h = fixture({ workerBudgetMode: mode })
    const decisions = mode === 'auto' ? { implementer: { tokenLimit: 80000, callLimit: 40, reason: 'bounded implementer' } } : undefined
    const r = await completedFixedImplementer(h, decisions)
    // 911 evidence: one geometry rework burned 943,467 tokens / 10 calls under
    // this unlimited grant — more than the capped first-pass implementer (590,284).
    const rework = await r.issueRework(h.agent(h.root), { workerSessionId: 'a', task: 'Fix the reviewed defects only.' })
    assert.deepEqual(rework.profile, { mode: 'unlimited' })
    const allocation = (await h.journal.read('lead')).events.find(e => e.type === 'dpswarm/worker-budget-allocation' && e.data.authority === 'fixed-team-rework').data
    assert.equal(allocation.budget_origin, 'unlimited_rework')
    assert.equal(allocation.decided_by, 'user_authorized_unlimited_rework')
    assert.equal(allocation.tokenLimit, undefined)
  })
})

test('911 reporting: Lead-facing view truncates a 5,422-char reviewer report to 600 chars with no in-session read path (0.7.8 adds dpswarm_report)', () => {
  const report = '审'.repeat(5422) // 911 reviewer report: 5,422 chars, geometry findings past the cut
  const entry = compactWorkerEntry({ item_id: 'wi-reviewer', title: 'DPswarm reviewer', kind: 'derive', role: 'reviewer',
    level: 'B', stop_reason: 'completed', execution_session_id: 'w3', output: report })
  assert.equal(entry.output_original_chars, 5422)
  assert.equal(entry.output_truncated, true)
  assert.equal(entry.output.length, 600 + '… [truncated; full value in plugin audit]'.length)
  assert.equal(entry.detail_source, 'Full report: dpswarm_report(item_id, offset, limit) pages the unabridged text; authenticated /api/plugin-audit remains the audit of record. Inspect original task and files before deciding',
    '0.7.8 points the Lead to the in-session paged read path')
})
