import assert from 'node:assert/strict'
import test from 'node:test'
import { effectiveLeadRoute, installChildRoutes, prepareChildRoute } from '../lib/lead-route.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const { Session } = await import(hostModuleUrl(resolveHostRoot(), 'dsh-session/lib/index.js'))

const parent = config => ({ options: { provider: 'old', model: 'deepseek-v4-pro', reasoningEffort: 'high' },
  session: { requestHeader: () => config === undefined ? undefined : ({ config }) } })

test('route is copied and frozen from the effective request; startup defaults never leak', () => {
  const config = { provider: 'glmcp', model: 'glm-5.3-flash' }
  const selected = effectiveLeadRoute(parent(config))
  assert.deepEqual(selected, config)
  assert.equal(Object.hasOwn(selected, 'reasoningEffort'), false)
  assert.ok(Object.isFrozen(selected))
  config.model = 'later-model'
  assert.equal(selected.model, 'glm-5.3-flash')
})

for (const config of [undefined, {}, {provider:'p'}, {provider:'',model:'m'}, {provider:'p',model:' '},
  {provider:'p',model:'m',reasoningEffort:null}, {provider:'p',model:'m',reasoningEffort:''},
  {provider:'p',model:'m',reasoningEffort:42}]) {
  test('invalid or missing effective route fails closed: '+JSON.stringify(config), () => {
    assert.throws(() => effectiveLeadRoute(parent(config)), /ROOT_MODEL_REQUIRED|INVALID_ROOT_EFFORT/)
  })
}

test('missing native header accessor cannot fall back to configured startup model', () => {
  assert.throws(() => effectiveLeadRoute({ options:{provider:'old',model:'old'},session:{} }), /ROOT_MODEL_REQUIRED/)
})

test('native folded request headers preserve actual effort and apply its removal', () => {
  const session=Session.create('route-parent',undefined,{version:0,id:'route-parent',createdAt:1})
  const agent={session,options:{provider:'old',model:'deepseek-v4-pro',reasoningEffort:'high'}}
  assert.throws(() => effectiveLeadRoute(agent),/ROOT_MODEL_REQUIRED/)
  session.append('request/header',{header:{config:{provider:'deepseek-official',model:'deepseek-v4-flash',reasoningEffort:'max'}}})
  assert.deepEqual(effectiveLeadRoute(agent),{provider:'deepseek-official',model:'deepseek-v4-flash',reasoningEffort:'max'})
  session.append('request/header',{header:{config:{provider:'glmcp',model:'glm-5.3-flash'}}})
  assert.deepEqual(effectiveLeadRoute(agent),{provider:'glmcp',model:'glm-5.3-flash'})
})

function requestFixture(route = { provider: 'chosen', model: 'worker', reasoningEffort: 'max' }) {
  let request
  const journal = new MemoryAuditJournal()
  installChildRoutes({ on(name, hook, options) {
    assert.equal(name, 'agent/request')
    assert.deepEqual(options, { prepend: true, global: true })
    request = hook
    return () => {}
  } }, { journal })
  const lead = { session: { id: 'root' } }
  const ticket = prepareChildRoute(lead, route, { journal, label: 'dpswarm:DPswarm implementer' })
  const session = Session.create('child', undefined, { version: 0, id: 'child', createdAt: 1,
    origin: 'subagent', parentSession: 'root', delegationDepth: 1 })
  session.append('subagent/descriptor', { label: 'dpswarm:DPswarm implementer' })
  return { request, ticket, journal, agent: { id: 'child', session, options: { ...ticket.agentOptions, subagentDepth: 1 } } }
}

test('first child request waits for CP publication binding before native resolution', async () => {
  const { request, ticket, agent } = requestFixture()
  let nativeCalls = 0
  const result = request({ agent, signal: new AbortController().signal }, async () => {
    nativeCalls++
    return { provider: 'startup', model: 'pro', reasoningEffort: 'high', maxTokens: 4096, temperature: 0.5 }
  })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(nativeCalls, 0)
  await ticket.bind('child')
  assert.deepEqual(await result, { provider: 'chosen', model: 'worker', reasoningEffort: 'max', maxTokens: 4096, temperature: 0.5 })
  assert.equal(nativeCalls, 1)
  await assert.rejects(ticket.bind('sibling'), /CHILD_ROUTE_BINDING_INVALID/)
  ticket.close()
})

test('bound route removes a stale native effort when the real Lead omitted effort', async () => {
  const { request, ticket, agent } = requestFixture({ provider: 'glmcp', model: 'glm-5.3-flash' })
  await ticket.bind('child')
  assert.deepEqual(await request({ agent }, async () => ({ provider: 'old', model: 'old', reasoningEffort: 'high', maxTokens: 3000 })),
    { provider: 'glmcp', model: 'glm-5.3-flash', maxTokens: 3000 })
  ticket.close()
})

for (const mutation of ['sibling', 'foreign-root', 'descendant', 'mismatched-session']) {
  test(`a copied genuine capability cannot route ${mutation}`, async () => {
    const { request, ticket, agent } = requestFixture()
    await ticket.bind('child')
    const fake = { ...agent, session: { id: 'child', header: { ...agent.session.header } } }
    if (mutation === 'sibling') { fake.id = 'sibling'; fake.session.id = 'sibling'; fake.session.header.id = 'sibling' }
    if (mutation === 'foreign-root') fake.session.header.parentSession = 'foreign-root'
    if (mutation === 'descendant') fake.session.header.delegationDepth = 2
    if (mutation === 'mismatched-session') fake.session.id = 'other'
    let called = false
    await assert.rejects(request({ agent: fake }, async () => { called = true; return {} }), /CHILD_ROUTE_BINDING_INVALID/)
    assert.equal(called, false)
    ticket.close()
  })
}

test('failed publication and aborted waiting child do not resolve or call the provider', async () => {
  for (const failure of ['publication', 'abort']) {
    const { request, ticket, agent } = requestFixture()
    const abort = new AbortController()
    let called = false
    const pending = request({ agent, signal: abort.signal }, async () => { called = true; return {} })
    if (failure === 'publication') ticket.close()
    else abort.abort(new Error('fixture cancelled'))
    await assert.rejects(pending, failure === 'publication' ? /CHILD_ROUTE_NOT_BOUND/ : /fixture cancelled/)
    assert.equal(called, false)
    ticket.close()
  }
})

test('ordinary roots and native children do not inherit a DP fixed-role request override', async () => {
  const { request, ticket, agent } = requestFixture()
  const native = Object.freeze({ provider: 'ordinary', model: 'native', reasoningEffort: 'low' })
  assert.equal(await request({ agent: { ...agent, session: { ...agent.session, header: agent.session.header, events: [] }, options: {} } }, async () => native), native)
  assert.equal(await request({ agent: { id: 'root', session: { id: 'root', header: {} } } }, async () => native), native)
  ticket.close()
})

test('a completed grant cannot be replayed into another provider request', async () => {
  const { request, ticket, agent } = requestFixture()
  await ticket.bind('child')
  ticket.close()
  await assert.rejects(request({ agent }, async () => assert.fail('closed grant dispatch')), /CHILD_ROUTE_BINDING_INVALID/)
})

for (const withEffort of [true, false]) {
  test('cold fixed role restores only its durable route: effort '+withEffort, async () => {
    const route = { provider: 'chosen', model: 'worker', ...(withEffort ? { reasoningEffort: 'max' } : {}) }
    const { request, ticket, agent, journal } = requestFixture(route)
    await ticket.bind('child')
    const cold = { ...agent, options: { provider: 'old', model: 'old', reasoningEffort: 'high' } }
    agent.session.append('request/header', { header: { config: route } })
    ticket.close()
    assert.deepEqual(await request({ agent: cold }, async () => ({ provider: 'old', model: 'old', reasoningEffort: 'high' })), route)
    const saved = await journal.read('root')
    assert.equal(saved.events.filter(e => e.type === 'dpswarm/route-bound').length, 1)
  })
}

for (const mismatch of ['missing', 'owner', 'root', 'parent', 'label', 'duplicate', 'header']) {
  test('cold role rejects '+mismatch+' binding before native/provider call', async () => {
    const { request, ticket, agent, journal } = requestFixture()
    if (mismatch !== 'missing') await ticket.bind('child')
    const record = (await journal.read('root')).events.find(e => e.type === 'dpswarm/route-bound')
    if (mismatch === 'header') agent.session.append('request/header', { header: { config: { provider: 'chosen', model: 'worker' } } })
    if (record && mismatch !== 'header') {
      const savedRead = journal.read.bind(journal)
      journal.read = async id => {
        const value = await savedRead(id), item = value.events.find(e => e.type === 'dpswarm/route-bound')
        if (mismatch === 'duplicate') value.events.push(structuredClone(item))
        else item.data[mismatch === 'owner' ? 'owner_session_id' : mismatch === 'root' ? 'root_session_id' : mismatch === 'parent' ? 'parent_session_id' : 'label'] = 'wrong'
        return value
      }
    }
    ticket.close()
    await assert.rejects(request({ agent: { ...agent, options: {} } }, async () => assert.fail('invalid cold dispatch')),
      /CHILD_ROUTE_BINDING_REQUIRED|CHILD_ROUTE_BINDING_INVALID|CHILD_ROUTE_HEADER_MISMATCH/)
  })
}
