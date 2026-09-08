import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { FixedTeamController, fixedProfile } from '../lib/fixed-team.js'
import { CMRuntime } from '../lib/cm-runtime.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { installBudget } from '../lib/budget.js'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const [{ Context }, { Session }] = await Promise.all(['cordis', 'dsh-session'].map(p => import(hostModuleUrl(host, `${p}/lib/index.js`))))

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'dpswarm-fixed-')), cwd = join(directory, 'project')
  mkdirSync(cwd)
  const cfg = { sidecarUrl: 'http://127.0.0.1:8791', workspace: directory, autoStart: false,
    enabledSessions: ['parent'], subagentProvider: 'spawn', implProvider: 'glm', implModel: 'glm-5.3-flash',
    testProvider: 'glm', testModel: 'glm-5.3-flash', workerTimeoutSeconds: 600 }
  const parent = { id: 'parent', session: { id: 'parent', header: { cwd }, route: { provider: 'gpt', model: 'sol' }, requestHeader() { return { config: this.route } } }, options: { provider: 'startup', model: 'deepseek-v4-pro', reasoningEffort: 'high' } }
  const controls = new Map(), calls = [], children = []
  const sidecarFactory = snapshot => {
    const state = controls.get(snapshot.sessionId) || { items: {}, sealed: false, audit: { root_session_id: snapshot.sessionId, revision: 0, events: [], head_hash: '0'.repeat(64) } }
    controls.set(snapshot.sessionId, state)
    return { cfg: snapshot, async ensure() {}, async call(method, path, body) {
      calls.push({ method, path, body, snapshot })
      if (path === '/api/plugin-audit') {
        if (method === 'POST') { assert.equal(body.expected_revision, state.audit.revision); state.audit.revision++; state.audit.events.push(...body.events) }
        return structuredClone(state.audit)
      }
      if (path === '/api/status') return { bridge: { session_isolation: true }, snapshot: {
        work_items: state.items, open_worker_slots_used: Object.values(state.items).filter(i => !['accepted','terminated'].includes(i.acceptance)).length,
        seal_phase: state.sealed ? { root: 'cutoff' } : {} } }
      if (path === '/api/delegate') {
        const id = 'item-' + Object.keys(state.items).length
        state.items[id] = { acceptance: 'active' }
        return { items: [{ item_id: id, node_id: id, session_id: 'reservation-' + id, context_epoch: 0, attempt: 1, kind: 'derive' }] }
      }
      if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
      if (path === '/api/submit') state.items[body.item_id].acceptance = 'submitted'
      if (path === '/api/execution/fail') {
        state.items[body.item_id].acceptance = 'terminated'
        state.sealed = body.physical_cleanup_confirmed === false
      }
      if (path === '/api/review') {
        assert.ok(state.items[body.item_id]);state.items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated'
      }
      return { ok: true }
    } }
  }
  const subagents = { async start(provider, request) {
    let resolve
    const child = { id: 'real-child-' + children.length, disposed: 0,
      result: new Promise(r => { resolve = r }), async dispose() { this.disposed++ } }
    children.push({ child, resolve, request, provider })
    return child
  } }
  const controller = new FixedTeamController({ config: () => cfg, subagents, sidecarFactory })
  const signal = new AbortController(), exec = { agent: parent, signal: signal.signal }
  const finish = (i, text = 'fixture delivery') => children[i].resolve({ output: [{ type: 'text', text }], stopReason: 'completed' })
  return { controller, cfg, parent, exec, signal, children, finish, calls, controls, directory }
}

test('off rejects before sidecar, lease or child; model cannot choose activation or routes', async t => {
  const h=fixture(t);h.cfg.enabledSessions=[]
  assert.equal((await h.controller.status(h.parent)).state,'off')
  await assert.rejects(h.controller.run({task:'work'},h.exec),/DPSWARM_DISABLED/)
  assert.equal(h.calls.length,0);assert.equal(h.children.length,0)
  h.cfg.enabledSessions=['parent']
  await assert.rejects(h.controller.run({task:'work',model:'other'},h.exec),/FIXED_MODE_ONLY/)
  await assert.rejects(h.controller.run({task:'work',enabled:true},h.exec),/FIXED_MODE_ONLY/)
})

test('fixed roles are sequential and route/config changes cannot alter an in-flight run', async t => {
  const h=fixture(t), running=h.controller.run({task:'repair',acceptance:'retain behavior'},h.exec)
  await nextTurn();assert.equal(h.children.length,1)
  assert.equal(h.children[0].request.agentOptions.model,'glm-5.3-flash')
  h.cfg.testModel='future-model';h.cfg.sidecarUrl='http://127.0.0.1:9999'
  h.finish(0,'implementation candidate')
  await nextTurn();assert.equal(h.children.length,2)
  assert.equal(h.children[1].request.agentOptions.model,'glm-5.3-flash')
  assert.match(h.children[1].request.prompt[0].text,/implementation candidate/)
  h.finish(1,'test report')
  const result=await running
  assert.deepEqual(result.deliveries.map(d=>d.role),['implementer','tester'])
  assert.ok(h.calls.every(c=>c.snapshot.sidecarUrl==='http://127.0.0.1:8791'))
  await assert.rejects(h.controller.run({task:'again'},h.exec),/RUN_PENDING/)
  const lease=h.controller.sessions.get('parent').lease.path;assert.ok(existsSync(lease))
  h.cfg.enabledSessions=[]
  for(const d of result.deliveries) await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
  assert.equal(existsSync(lease),false)
  assert.ok(h.calls.every(c=>c.snapshot.sidecarUrl==='http://127.0.0.1:8791'))
})

test('closing a session switch aborts pending work and does not start the tester', async t => {
  const h=fixture(t), running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.cfg.enabledSessions=[];h.controller.settingsChanged()
  const result=await running
  assert.equal(result.stopped,true);assert.equal(h.children.length,1);assert.equal(h.children[0].child.disposed,1)
  assert.equal(h.controller.sessions.get('parent').lease,null)
  h.finish(0)
})

test('host cancellation disposes a pending child and settles the role', async t => {
  const h=fixture(t), running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.signal.abort()
  const result=await running
  assert.equal(result.stopped,true);assert.equal(h.children.length,1)
  assert.ok(h.calls.some(c=>c.path==='/api/execution/fail'))
  h.finish(0)
})

test('failed cleanup holds workspace and blocks a second enabled session', async t => {
  const h=fixture(t), running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.children[0].child.dispose=async()=>{throw new Error('still running')}
  h.children[0].resolve({output:[],stopReason:'error'})
  const result=await running
  assert.equal(result.failed.length,1);assert.equal(h.children.length,1)
  assert.ok(h.controller.sessions.get('parent').lease)
  h.cfg.enabledSessions.push('second')
  const second={agent:{...h.parent,id:'second',session:{...h.parent.session,id:'second'}},signal:new AbortController().signal}
  await assert.rejects(h.controller.run({task:'another'},second),/WORKSPACE_BUSY/)
})

test('different sessions have independent activation and control scope', async t => {
  const h=fixture(t)
  const second={...h.parent,id:'second',session:{...h.parent.session,id:'second'}}
  assert.equal((await h.controller.status(second)).enabled,false)
  h.cfg.enabledSessions.push('second')
  const a=h.controller.session(h.parent), b=h.controller.session(second)
  assert.notEqual(a.sidecar.cfg.sessionId,b.sidecar.cfg.sessionId)
})

test('nested calls, missing routes, and invalid timeouts fail before child creation', async t => {
  const h=fixture(t)
  h.parent.session.header.delegationDepth=1
  await assert.rejects(h.controller.run({task:'repair'},h.exec),/NESTED_DELEGATION_UNSUPPORTED/)
  h.parent.session.header.delegationDepth=0;h.cfg.implMode='model';h.cfg.implProvider=''
  await assert.rejects(h.controller.run({task:'repair'},h.exec),/FIXED_ROUTE_REQUIRED/)
  assert.equal(h.children.length,0)
  assert.throws(()=>fixedProfile({...h.cfg,implProvider:'glm',workerTimeoutSeconds:0}),/INVALID_WORKER_TIMEOUT/)
})

test('first release does not permit rejection-driven automatic rerouting', async t => {
  const h=fixture(t)
  await assert.rejects(h.controller.review({item_id:'fake',verdict:'reject'},h.exec),/FIXED_REVIEW_ONLY/)
  assert.equal(h.calls.length,0)
})

test('plugin disposal waits for the active child cleanup and blocks new runs', async t => {
  const h=fixture(t), running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();await h.controller.shutdown()
  assert.equal(h.children[0].child.disposed,1)
  assert.equal((await running).stopped,true)
  assert.equal(h.controller.sessions.get('parent').lease,null)
  await assert.rejects(h.controller.run({task:'again'},h.exec),/PLUGIN_DISPOSED/)
  h.finish(0)
})


test('team invokes and persists the actual CM snapshot until both deliveries are reviewed', async t => {
  const h = fixture(t)
  h.cfg.cmProvider = 'deepseek'; h.cfg.cmEnabledSessions = ['parent']
  h.parent.session.events = []
  h.parent.session.append = (type, data) => h.parent.session.events.push({ seq: h.parent.session.events.length, type, data })
  const journal = new MemoryAuditJournal()
  const cm = new CMRuntime(() => h.cfg, () => null, journal); h.controller.cm = cm
  const running = h.controller.run({ task: 'repair' }, h.exec)
  await nextTurn(); h.cfg.cmProvider = 'future-provider'
  assert.equal((await cm.profile(h.parent.session)).provider, 'deepseek')
  h.finish(0); await nextTurn(); h.finish(1)
  const result = await running
  assert.equal(result.profile.cm.enabled, true)
  assert.equal(result.profile.cm.profile.provider, 'deepseek')
  assert.equal(cm.frozen.has('parent'), true)
  for (const delivery of result.deliveries) await h.controller.review({ item_id: delivery.item_id, verdict: 'accept' }, h.exec)
  assert.equal(cm.frozen.has('parent'), false)
  assert.equal((await journal.read('parent')).events.at(-1).data.active, false)
  assert.equal(h.parent.session.events.length, 0, 'CM must not add unsupported native events')
})


test('Reviewer defaults to Lead and does not start a third model, even if an old route is present', async t => {
  const h=fixture(t)
  Object.assign(h.cfg,{reviewerProvider:'stored-provider',reviewerModel:'stored-model'})
  const running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.finish(0);await nextTurn();h.finish(1)
  const result=await running
  assert.deepEqual(result.profile.reviewer,{mode:'lead'})
  assert.equal(h.children.length,2)
  for(const d of result.deliveries)await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
})

test('an explicitly selected Reviewer receives both reports, uses its frozen route, and cannot auto-accept', async t => {
  const h=fixture(t)
  Object.assign(h.cfg,{reviewerMode:'model',reviewerProvider:'review-provider',reviewerModel:'review-model',reviewerEffort:'max'})
  const running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.cfg.reviewerMode='lead';h.cfg.reviewerModel='future-model'
  h.finish(0,'production delivery');await nextTurn();h.finish(1,'validation evidence');await nextTurn()
  assert.equal(h.children.length,3)
  const request=h.children[2].request
  assert.deepEqual(Object.fromEntries(Object.entries(request.agentOptions)),{provider:'review-provider',model:'review-model',reasoningEffort:'max'})
  assert.match(request.prompt[0].text,/production delivery/);assert.match(request.prompt[0].text,/validation evidence/)
  assert.match(request.prompt[0].text,/do not edit files or accept deliveries/)
  h.finish(2,'advisory review')
  const result=await running
  assert.deepEqual(result.deliveries.map(d=>d.role),['implementer','tester','reviewer'])
  assert.equal(h.calls.filter(c=>c.path==='/api/review').length,0)
  const lease=h.controller.sessions.get('parent').lease.path
  for(const d of result.deliveries.slice(0,2))await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
  assert.equal(existsSync(lease),true)
  await h.controller.review({item_id:result.deliveries[2].item_id,verdict:'accept'},h.exec)
  assert.equal(existsSync(lease),false)
})

test('invalid or incomplete independent Reviewer configuration is refused before child execution', async t => {
  const h=fixture(t)
  assert.throws(()=>fixedProfile({...h.cfg,reviewerMode:'automatic'}),/INVALID_REVIEWER_MODE/)
  h.cfg.reviewerMode='model'
  await assert.rejects(h.controller.run({task:'repair'},h.exec),/FIXED_ROUTE_REQUIRED/)
  assert.equal(h.children.length,0)
})

test('cancelling an independent Reviewer disposes it, preserves earlier evidence and never substitutes a model', async t => {
  const h=fixture(t)
  Object.assign(h.cfg,{reviewerMode:'model',reviewerProvider:'review-provider',reviewerModel:'review-model'})
  const running=h.controller.run({task:'repair'},h.exec)
  await nextTurn();h.finish(0);await nextTurn();h.finish(1);await nextTurn()
  h.cfg.enabledSessions=[];h.controller.settingsChanged()
  const result=await running
  assert.equal(h.children.length,3);assert.equal(h.children[2].child.disposed,1)
  assert.deepEqual(result.deliveries.map(d=>d.role),['implementer','tester'])
  assert.equal(result.failed[0].role,'reviewer')
  for(const d of result.deliveries)await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
  assert.equal(h.controller.sessions.get('parent').lease,null)
  h.finish(2)
})


test('default implementer inherits the actual conversation and freezes its route and effort', async t => {
  const h=fixture(t)
  // The old model text without an explicit Provider is not a configured route.
  h.cfg.implProvider='';h.cfg.implModel='glm-5.3-flash';h.cfg.implEffort='stale-effort'
  h.parent.session.route={provider:'current-provider',model:'current-dialogue-model',reasoningEffort:'max'}
  const running=h.controller.run({task:'repair'},h.exec)
  await nextTurn()
  assert.deepEqual(Object.fromEntries(Object.entries(h.children[0].request.agentOptions)),{provider:'current-provider',model:'current-dialogue-model',reasoningEffort:'max'})
  h.parent.session.route.model='next-dialogue-model';h.parent.session.route.reasoningEffort='low'
  h.cfg.implMode='model';h.cfg.implProvider='new-custom';h.cfg.implModel='next-custom-model'
  h.finish(0);await nextTurn();h.finish(1)
  const result=await running
  assert.deepEqual(result.profile.implementer,{mode:'lead',provider:'current-provider',model:'current-dialogue-model',reasoning_effort:'max'})
  for(const d of result.deliveries)await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
  h.cfg.implMode='lead'
  const again=h.controller.run({task:'next run'},h.exec)
  await nextTurn();assert.equal(h.children[2].request.agentOptions.model,'next-dialogue-model');assert.equal(h.children[2].request.agentOptions.reasoningEffort,'low')
  h.finish(2);await nextTurn();h.finish(3)
  for(const d of (await again).deliveries)await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
})

test('inherited implementer ignores a stored custom route and resolves separately for each conversation', () => {
  const cfg={implMode:'lead',implProvider:'old-provider',implModel:'old-model',implEffort:'old-effort',testProvider:'test',testModel:'test-model'}
  const a=fixedProfile(cfg,undefined,{provider:'a',model:'one',reasoningEffort:'off'})
  const b=fixedProfile(cfg,undefined,{provider:'b',model:'two'})
  assert.deepEqual(a.implementer,{mode:'lead',provider:'a',model:'one',reasoning_effort:'off'})
  assert.deepEqual(b.implementer,{mode:'lead',provider:'b',model:'two',reasoning_effort:undefined})
  assert.notEqual(a.id,b.id)
  assert.throws(()=>fixedProfile(cfg),/ROOT_MODEL_REQUIRED/)
  assert.throws(()=>fixedProfile({...cfg,implMode:'auto-router'}),/INVALID_IMPLEMENTER_MODE/)
})

test('old explicitly configured implementer routes are preserved when upgrading', () => {
  const cfg={implProvider:'saved-provider',implModel:'saved-model',implEffort:'high',testProvider:'test',testModel:'test-model'}
  assert.deepEqual(fixedProfile(cfg).implementer,{mode:'model',provider:'saved-provider',model:'saved-model',reasoning_effort:'high'})
  assert.deepEqual(fixedProfile({...cfg,implMode:'model'},undefined,{provider:'lead',model:'other'}).implementer,fixedProfile(cfg).implementer)
})


// Use the actual Cordis hooks and immutable native Session journals for the
// policy integration tests; the provider itself is deterministic and local.
function budgetFixture(t, mode) {
  const h = fixture(t), ctx = new Context(), native = new Map(), agents = new Map(), providerCalls = []
  h.parent.session = Session.create('parent', undefined, { version: 0, id: 'parent', createdAt: 1, cwd: h.parent.session.header.cwd })
  h.parent.session.append('request/header', { header: { config: { provider: 'gpt', model: 'sol' } } })
  native.set('parent', h.parent.session); agents.set('parent', h.parent)
  Object.assign(h.cfg, { workerBudgetMode: mode, workerTokenLimit: 10000, workerCallLimit: 4,
    reviewerMode: 'model', reviewerProvider: 'review', reviewerModel: 'review-model' })
  ctx.provide('sessions', { get: id => native.get(id), list: () => [...native.values()], flush: async () => {} })
  ctx.provide('agents', { get: id => agents.get(id) })
  const llm = { stream: options => ctx.waterfall('llm/stream', options, () => (async function* () {
    providerCalls.push(options)
    yield { type: 'usage', usage: { inputTokens: 20, outputTokens: 10 } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })()) }
  ctx.provide('llm', llm)
  const journal = new MemoryAuditJournal()
  const budget = installBudget(ctx, () => h.cfg, { journal }); h.controller.budget = budget
  const start = h.controller.subagents.start
  const launch = async (id, prompt) => {
    const session = Session.create(id, undefined, { version: 0, id, createdAt: 2, parentSession: 'parent', origin: 'subagent', delegationDepth: 1 })
    const agent = { id, session, options: { provider: 'fixture', model: 'worker' } }
    native.set(id, session); agents.set(id, agent)
    const messages = [{ role: 'user', source: { kind: 'user' }, content: prompt }]
    await ctx.waterfall('agent/pre-step', { agent, messages }, async () => ({ kind: 'enter', messages }))
    session.append('user/message', messages[0], { surfaceOp: 'append' })
    for await (const chunk of llm.stream(Object.freeze({ sessionId: id, provider: 'fixture', model: 'worker', messages, maxTokens: 100 }))) { /* consume */ }
    return agent
  }
  h.controller.subagents.start = async (provider, request) => {
    await launch('real-child-' + h.children.length, request.prompt)
    return start(provider, request)
  }
  t.after(() => budget.shutdown())
  const decisions = { implementer: { tokenLimit: 11000, callLimit: 5, reason: 'implementation scope' },
    tester: { tokenLimit: 8000, callLimit: 3, reason: 'focused validation' }, reviewer: { tokenLimit: 6000, callLimit: 2, reason: 'read only review' } }
  return { ...h, budget, journal, native, agents, providerCalls, launch, decisions }
}

for (const scenario of [
  { name: 'Auto to manual', before: 'auto', next: { workerBudgetMode: 'manual', workerTokenLimit: 7777, workerCallLimit: 7 } },
  { name: 'Auto to unlimited', before: 'auto', next: { workerBudgetMode: 'unlimited' } },
  { name: 'manual value edits', before: 'manual', next: { workerTokenLimit: 7777, workerCallLimit: 7 } },
  { name: 'manual to Auto', before: 'manual', next: { workerBudgetMode: 'auto' } },
  { name: 'unlimited to manual', before: 'unlimited', next: { workerBudgetMode: 'manual', workerTokenLimit: 7777, workerCallLimit: 7 } },
]) test(`frozen team policy survives ${scenario.name}; same-root native worker uses current policy`, async t => {
  const h = budgetFixture(t, scenario.before)
  const running = h.controller.run({ task: 'repair', ...(scenario.before === 'auto' ? { worker_budgets: h.decisions } : {}) }, h.exec)
  await nextTurn(); assert.equal(h.children.length, 1)
  Object.assign(h.cfg, scenario.next)
  // An unrelated native child must never inherit this pipeline snapshot.
  const current = h.cfg.workerBudgetMode
  if (current === 'auto') {
    await assert.rejects(h.launch('ordinary-missing', [{ type: 'text', text: 'unrelated task' }]), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
    const plan = await h.budget.plan(h.parent, { task: 'unrelated task', tokenLimit: 3333, callLimit: 2, reason: 'its own Lead decision' })
    await h.launch('ordinary', [{ type: 'text', text: plan.prompt }])
  } else await h.launch('ordinary', [{ type: 'text', text: 'unrelated task' }])
  const ordinary = await h.budget.status(h.agents.get('ordinary'))
  assert.equal(ordinary.mode, current); assert.equal(ordinary.policy_binding, null)
  if (current === 'manual') assert.equal(ordinary.tokenLimit, 7777)
  if (current === 'auto') assert.equal(ordinary.tokenLimit, 3333)
  h.finish(0); await nextTurn(); assert.equal(h.children.length, 2)
  h.finish(1); await nextTurn(); assert.equal(h.children.length, 3)
  h.finish(2); const result = await running
  const roles = ['implementer', 'tester', 'reviewer']
  for (let i = 0; i < roles.length; i++) {
    const state = await h.budget.status(h.agents.get('real-child-' + i))
    assert.equal(state.mode, scenario.before)
    assert.equal(state.policy_binding.label, roles[i]); assert.equal(state.calls, 1)
    if (scenario.before === 'auto') {
      assert.equal(state.tokenLimit, h.decisions[roles[i]].tokenLimit)
      assert.equal(state.callLimit, h.decisions[roles[i]].callLimit)
      assert.equal(state.decision.reason, h.decisions[roles[i]].reason)
    } else if (scenario.before === 'manual') {
      assert.equal(state.tokenLimit, 10000); assert.equal(state.callLimit, 4)
    } else { assert.equal(state.remaining_calls, null); assert.equal(state.remaining_tokens, null) }
  }
  assert.equal(result.worker_budget_policy.mode, scenario.before)
  assert.equal((await h.journal.read('parent')).events.filter(e => e.type.endsWith('team-run-ended')).length, 1)
  for (const d of result.deliveries) await h.controller.review({ item_id: d.item_id, verdict: 'accept' }, h.exec)
})

test('failed native dispatch revokes unused team grants and clears controller lifecycle', async t => {
  const h = budgetFixture(t, 'auto')
  h.controller.subagents.start = async () => { throw new Error('fixture dispatch failure') }
  const result = await h.controller.run({ task: 'repair', worker_budgets: h.decisions }, h.exec)
  assert.ok(result.failed.length >= 1)
  const allocation = (await h.journal.read('parent')).events.find(e => e.type.endsWith('allocation'))?.data
  assert.ok(allocation)
  assert.equal(h.controller.sessions.get('parent').budgetRun, null)
  assert.equal((await h.journal.read('parent')).events.filter(e => e.type.endsWith('team-run-ended')).length, 1)
  await assert.rejects(h.launch('late-replay', [{ type: 'text', text: allocation.prompt }]), { code: 'WORKER_BUDGET_DECISION_REQUIRED' })
})


test('manual user limits cannot be silently skipped when the budget runtime is missing', async t => {
  const h = fixture(t)
  Object.assign(h.cfg, { workerBudgetMode: 'manual', workerTokenLimit: 10000, workerCallLimit: 4 })
  await assert.rejects(h.controller.run({ task: 'repair' }, h.exec), { code: 'WORKER_BUDGET_UNAVAILABLE' })
  assert.equal(h.children.length, 0)
})


test('explicit no-tests task constrains implementer and tester without rewriting the user task', async t => {
  const h = fixture(t), task = '创建一个 HTML 动画，你不需要任何测试'
  const running = h.controller.run({ task }, h.exec)
  await nextTurn(); h.finish(0); await nextTurn(); h.finish(1)
  const result = await running
  for (const child of h.children) {
    const text = child.request.prompt[0].text
    assert.ok(text.includes(`Task:\n${task}`))
    assert.match(text, /take precedence over default role guidance/)
    assert.match(text, /do not run tests or add tests/)
    assert.match(text, /permitted read-only inspection/)
  }
  for (const d of result.deliveries) await h.controller.review({ item_id: d.item_id, verdict: 'accept' }, h.exec)
})

test('inherited route fails before child work without an actual Lead request header', async t => {
  const h=fixture(t);h.cfg.implProvider='';h.cfg.implMode='lead';delete h.parent.session.requestHeader
  await assert.rejects(h.controller.run({task:'repair'},h.exec), {code:'ROOT_MODEL_REQUIRED'})
  assert.equal(h.children.length,0)
  assert.equal(h.calls.length,0)
})

test('worker without selected effort never inherits startup high; root metadata follows actual Lead', async t => {
  const h=fixture(t);h.cfg.implProvider='';h.cfg.implMode='lead'
  h.parent.session.route={provider:'glmcp',model:'glm-5.3-flash'}
  const running=h.controller.run({task:'repair'},h.exec)
  await nextTurn()
  assert.deepEqual(Object.fromEntries(Object.entries(h.children[0].request.agentOptions)),{provider:'glmcp',model:'glm-5.3-flash'})
  h.finish(0);await nextTurn();h.finish(1)
  const result=await running
  for(const d of result.deliveries)await h.controller.review({item_id:d.item_id,verdict:'accept'},h.exec)
  const registrations=h.calls.filter(c=>c.path==='/api/execution/root')
  assert.ok(registrations.length>=4)
  assert.ok(registrations.every(c=>c.body.provider==='glmcp' && c.body.model==='glm-5.3-flash'))
})
