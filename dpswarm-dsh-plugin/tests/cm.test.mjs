import assert from 'node:assert/strict'
import test from 'node:test'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { CMRuntime, cmProfile, CM_MODEL } from '../lib/cm-runtime.js'
import { DPSwarmCM, selectCMRange } from '../lib/cm.js'
import { prepareChildRoute } from '../lib/lead-route.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
const host = resolveHostRoot()
const [{ Context }, { Session, KNOWN_SESSION_EVENT_TYPES }, { TokenMeter }, { createUserMessage }, { toolPairingBalancedAfter }] = await Promise.all(
  ['cordis', 'dsh-session', 'dsh-token-meter', 'dsh-llm', 'dsh-compaction'].map(p => import(hostModuleUrl(host, p + '/lib/index.js'))))
const usage = { inputTokens: 18000, outputTokens: 24, cacheReadTokens: 3000 }
function* chunks({ text = 'Goal: preserve fixed invariants. Evidence: original tests are unresolved.', finish = 'stop', tokens = usage, block } = {}) {
  yield { type: 'block-end', index: 0, block: block || { type: 'text', text } }
  if (tokens) yield { type: 'usage', usage: tokens }
  if (finish) yield { type: 'finish', reason: { kind: finish } }
}
function fixture({ on = true, response, id = 'root', parent, depth, count = 10, size = 2500, contextWindow = 25000, origin } = {}) {
  const cfg = { cmProvider: 'deepseek', cmEnabledSessions: on ? ['root'] : [], enabledSessions: [] }
  const ctx = new Context(), journal = new MemoryAuditJournal(), requests = [], metadata = []
  const windows = new Map([['lead/lead-model', contextWindow]])
  let reads = 0
  const originalRead = journal.read.bind(journal)
  journal.read = async id => { reads++; return originalRead(id) }
  const runtime = new CMRuntime(() => cfg, () => null, journal)
  ctx.provide('dpswarmCM', runtime)
  ctx.provide('sessions', { async flush() {} })
  ctx.provide('llm', { async resolveModelInfo(provider, model) { metadata.push({ provider, model }); return { provider, id: model, ...(windows.has(provider + '/' + model) && windows.get(provider + '/' + model) !== undefined ? { context: { contextWindow: windows.get(provider + '/' + model) } } : {}) } }, async *stream(options) { requests.push(options); yield* (response ? response(options) : chunks()) } })
  const meter = new TokenMeter(ctx), engine = new DPSwarmCM(ctx)
  const session = Session.create(id, undefined, { version: 0, id, createdAt: Date.now(),
    ...(origin ? { origin } : {}), ...(parent ? { parentSession: parent } : {}), ...(depth === undefined ? {} : { delegationDepth: depth }) })
  session.append('turn/start', { turn: 1 })
  for (let i = 0; i < count; i++) session.append('user/message', createUserMessage({
    content: [{ type: 'text', text: `record-${i} ` + `detail-${i} `.repeat(size) }], source: { kind: 'user' } }), { surfaceOp: 'append' })
  const agent = { id, session, options: { provider: 'lead', model: 'lead-model' } }
  const assembly = { sections: [], contexts: [], tools: [], variables: { provider: 'lead', model: 'lead-model' } }
  const capture = () => ctx.waterfall('system-prompt/assemble', assembly, { agent }, async () => assembly)
  const run = async signal => { await capture(); return engine.compactIfNeeded(agent, 'pressure', signal || new AbortController().signal) }
  return { cfg, ctx, runtime, journal, journalReads: () => reads, meter, engine, session, agent, requests, run, assembly, capture, windows, metadata }
}

test('CM off or short context makes no API call, journal write, or surface change', async () => {
  for (const options of [{ on: false }, { size: 10 }]) {
    const h = fixture(options), events = h.session.events.length, surface = h.session.deriveMessages()
    assert.equal(await h.run(), null); assert.equal(h.requests.length, 0)
    assert.equal(h.session.events.length, events); assert.deepEqual(h.session.deriveMessages(), surface)
    assert.equal((await h.journal.read('root')).events.length, 0)
    if (options.on === false) assert.equal(h.journalReads(), 1) // Only this assertion read; the disabled runtime made none.
  }
})

test('native replacement changes subsequent request history and survives durable replay; original events and recent tail retained', async () => {
  const h = fixture(), original = h.session.deriveMessages(), before = h.meter.measure(h.session).surfaceTokens
  const oldEvents = [...h.session.events], result = await h.run()
  assert.ok(result); assert.equal(h.requests[0].model, CM_MODEL)
  assert.equal(h.requests[0].reasoningEffort, 'off'); assert.deepEqual(h.requests[0].tools, [])
  assert.equal(h.requests[0].purpose, 'compaction'); assert.equal(h.requests[0].sessionId, 'root')
  assert.equal(h.requests[0].maxTokens, 4096)
  const nextMessages = h.session.deriveMessages()
  assert.equal(nextMessages.length, 5); assert.deepEqual(nextMessages.slice(-4), original.slice(-4))
  assert.match(nextMessages[0].content.map(b => b.text || '').join(' '), /preserve fixed invariants/)
  assert.ok(h.meter.measure(h.session).surfaceTokens < before)
  assert.deepEqual(h.session.events.slice(0, oldEvents.length), oldEvents)
  h.session.append('turn/end', { turn: 1, reason: 'completed' })
  const restored = Session.fromRestore('root', JSON.parse(JSON.stringify(h.session.events)), h.session.header)
  assert.deepEqual(restored.deriveMessages(), nextMessages)
  const status = await new CMRuntime(() => h.cfg, () => null, h.journal).status({ session: restored })
  assert.ok(h.session.events.every(e => KNOWN_SESSION_EVENT_TYPES.has(e.type)))
  assert.equal(h.session.events.some(e => e.type.startsWith('dpswarm/')), false)
  assert.equal(status.adopted, 1); assert.equal(status.unknown_usage_calls, 0)
  assert.deepEqual(status.recent[0].usage, usage); assert.equal('cacheWriteTokens' in status.recent[0].usage, false)
  assert.equal(status.recent[0].compaction_id, result.compactionId)
})

for (const [name, response, reason] of [
  ['empty', { text: '' }, 'CM_EMPTY_SUMMARY'],
  ['truncated', { finish: 'max-tokens' }, 'CM_INCOMPLETE_RESPONSE'],
  ['no terminal chunk', { finish: null }, 'CM_INCOMPLETE_RESPONSE'],
  ['missing usage', { tokens: null }, 'CM_USAGE_UNKNOWN'],
  ['negative usage', { tokens: { inputTokens: -1, outputTokens: 3 } }, 'CM_USAGE_UNKNOWN'],
  ['tool output', { block: { type: 'tool-call', id: 'x', name: 'bash', arguments: '{}' } }, 'CM_NON_TEXT_RESPONSE'],
  ['provider failure', { finish: 'error' }, 'CM_INCOMPLETE_RESPONSE'],
]) test(`CM rejects ${name} without changing visible history; accounts for failed call`, async () => {
  const h = fixture({ response: () => chunks(response) }), prior = h.session.deriveMessages()
  await assert.rejects(h.run(), new RegExp(reason)); assert.deepEqual(h.session.deriveMessages(), prior)
  const end = (await h.journal.read('root')).events.at(-1)
  assert.equal(end.type, 'dpswarm/cm-end'); assert.equal(end.data.outcome, 'failed')
  assert.equal(end.data.error_code, reason); assert.equal(end.data.llm_stream_calls, 1)
  assert.equal(h.session.events.filter(e => e.type === 'compaction/summary').length, 0)
  if (response.tokens === null) assert.equal(end.data.usage, null)
})

test('expanding summary and concurrent history mutation are rejected by the real host transaction', async () => {
  const big = fixture({ response: () => chunks({ text: 'massive '.repeat(80000) }) }), prior = big.session.deriveMessages()
  await assert.rejects(big.run(), /not smaller/); assert.deepEqual(big.session.deriveMessages(), prior)
  let resolve
  const h = fixture({ response: async function* () { await new Promise(r => { resolve = r }); yield* chunks() } })
  const running = h.run(); await nextTurn()
  h.session.append('user/message', createUserMessage({ content: [{ type: 'text', text: 'new steering' }], source: { kind: 'user' } }), { surfaceOp: 'append' })
  const current = h.session.deriveMessages(); resolve()
  await assert.rejects(running, /surface changed/); assert.deepEqual(h.session.deriveMessages(), current)
})

test('CM works without a team, includes direct children, excludes unrelated tasks and grandchildren', async () => {
  for (const options of [{ id: 'root' }, { id: 'child', parent: 'root', depth: 1 },
    { id: 'other' }, { id: 'grandchild', parent: 'child', depth: 2 }]) {
    const h = fixture(options), eligible = ['root', 'child'].includes(options.id)
    assert.deepEqual(h.cfg.enabledSessions, [])
    assert.equal(!!await h.run(), eligible); assert.equal(h.requests.length, eligible ? 1 : 0)
    if (eligible) {
      const events = (await h.journal.read('root')).events
      const start = events.find(e => e.type === 'dpswarm/cm-start')
      assert.equal(start.data.root_session_id, 'root')
      assert.equal(start.data.owner_session_id, options.id)
      assert.equal(start.data.agent_session_id, options.id)
      assert.equal(h.session.events.some(e => e.type.startsWith('dpswarm/')), false)
    }
  }
})

test('closing CM cancels adoption; a late provider is charged once and cannot commit later', async () => {
  let resolve
  const h = fixture({ response: async function* () { await new Promise(r => { resolve = r }); yield* chunks() } })
  const prior = h.session.deriveMessages(), running = h.run()
  await nextTurn(); h.cfg.cmEnabledSessions = []; await h.runtime.settingsChanged()
  await assert.rejects(running, /CM_DISABLED/); assert.deepEqual(h.session.deriveMessages(), prior)
  assert.equal((await h.runtime.status(h.agent)).unknown_usage_calls, 1)
  h.cfg.cmEnabledSessions = ['root']; assert.equal(await h.run(), null) // still physically pending
  resolve(); await nextTurn()
  const status = (await h.runtime.status(h.agent))
  assert.equal(status.attempts, 1); assert.equal(status.adopted, 0); assert.equal(status.active_calls, 0)
  assert.equal(status.unknown_usage_calls, 0); assert.deepEqual(status.recent[0].usage, usage)
  const restored = await new CMRuntime(() => h.cfg, () => null, h.journal).status(h.agent)
  assert.equal(restored.attempts, 1); assert.equal(restored.adopted, 0)
  assert.equal(restored.unknown_usage_calls, 0); assert.deepEqual(restored.recent[0].usage, usage)
  assert.deepEqual(h.session.deriveMessages(), prior)
})

test('hard timeout returns promptly even if the provider ignores cancellation; late output never enters context', async () => {
  let resolve
  const h = fixture({ response: async function* () { await new Promise(r => { resolve = r }); yield* chunks() } })
  // Exercise timeout without spending 120 seconds; production policy remains immutable.
  h.runtime.frozen.set('root', { profile: { ...cmProfile(h.cfg), timeoutSeconds: .01 }, disabled: false })
  const prior = h.session.deriveMessages()
  await assert.rejects(h.run(), /CM_TIMEOUT/); assert.deepEqual(h.session.deriveMessages(), prior)
  resolve(); await nextTurn(); assert.deepEqual(h.session.deriveMessages(), prior)
})

test('per-agent per-turn cap is durable across reload; a new turn has its own cap', async () => {
  const h = fixture({ response: () => chunks({ finish: null }) })
  for (let i = 0; i < 12; i++) await assert.rejects(h.run(), /CM_INCOMPLETE/)
  assert.equal(await h.run(), null); assert.equal(h.requests.length, 12)
  h.session.append('turn/end', { turn: 1, reason: 'completed' })
  const restored = Session.fromRestore('root', JSON.parse(JSON.stringify(h.session.events)), h.session.header)
  const newRuntime = new CMRuntime(() => h.cfg, () => null, h.journal), profile = cmProfile(h.cfg)
  assert.equal(await newRuntime.begin({ session: restored }, profile, {}, new AbortController().signal), null)
  restored.append('turn/start', { turn: 2 })
  const ticket = await newRuntime.begin({ session: restored }, profile, {}, new AbortController().signal)
  assert.ok(ticket); await newRuntime.finish(ticket, null, new Error('test'), 0)
  assert.equal((await newRuntime.status({ session: restored })).unknown_usage_calls, 0) // never-dispatched attempt is not an unknown model charge
})

test('team freezes CM provider until review finishes; off is immediate and re-enable waits for next run', async () => {
  const h = fixture(), snapshot = await h.runtime.beginRun(h.agent)
  h.cfg.cmProvider = 'new-provider'
  assert.equal((await h.runtime.profile(h.session)).provider, 'deepseek')
  assert.equal(snapshot.profile.provider, 'deepseek')
  h.cfg.cmEnabledSessions = []; await h.runtime.settingsChanged()
  h.cfg.cmEnabledSessions = ['root']; assert.equal((await h.runtime.profile(h.session)), null)
  await h.runtime.finishRun('root'); assert.equal((await h.runtime.profile(h.session)).provider, 'new-provider')
  h.cfg.cmEnabledSessions = []; await h.runtime.beginRun(h.agent)
  h.cfg.cmEnabledSessions = ['root']; assert.equal((await h.runtime.profile(h.session)), null)
})

test('surface selection preserves tool pairs at the retention boundary and accepts nonmonotonic seqs', async () => {
  const h = fixture({ count: 5 }), s = h.session
  s.append('step/start', { turn: 1, step: 1 })
  const assistant = s.append('assistant/message', { turn: 1, step: 1,
    message: { role: 'assistant', content: [{ type: 'tool-call', id: 'call-1', name: 'bash', arguments: '{}' }] } }, { surfaceOp: 'append' })
  s.append('tool/result', { turn: 1, step: 1,
    message: { role: 'tool', callId: 'call-1', content: [{ type: 'text', text: 'observed result' }] } }, { surfaceOp: 'append' })
  s.append('step/end', { turn: 1, step: 1 })
  for (let i=0; i<3; i++) s.append('user/message', createUserMessage({content:[{type:'text',text:'recent'}],source:{kind:'user'}}), {surfaceOp:'append'})
  assert.equal(toolPairingBalancedAfter(s, assistant.seq), false)
  const selected = selectCMRange(s, 4)
  assert.ok(selected[1] < assistant.seq)
  await h.run()
  assert.ok(s.surface.nodes[0] > s.surface.nodes[1])
  assert.deepEqual(s.deriveMessages().slice(-5).map(m => m.role), ['assistant', 'tool', 'user', 'user', 'user'])
})

test('pre-step middleware calls the next request after replacing history, or falls back on failure', async () => {
  for (const fails of [false, true]) {
    const h = fixture({ response: () => chunks(fails ? { tokens: null } : {}) })
    const signal = new AbortController().signal
    let modelVisible
    await h.capture()
    const result = await h.ctx.waterfall('agent/pre-step', { agent: h.agent, signal }, async () => {
      modelVisible = h.session.deriveMessages(); return { kind: 'enter' }
    })
    assert.equal(result.kind, 'enter'); assert.equal(modelVisible.length, fails ? 10 : 5)
  }
})


test('CM team snapshot survives host restart and review releases the persisted freeze', async () => {
  const h = fixture()
  await h.runtime.beginRun(h.agent); h.cfg.cmProvider = 'provider-next-run'
  h.session.append('turn/end', { turn: 1, reason: 'completed' })
  const restored = Session.fromRestore('root', JSON.parse(JSON.stringify(h.session.events)), h.session.header)
  const resumed = new CMRuntime(() => h.cfg, id => id === 'root' ? restored : null, h.journal)
  const child = Session.create('child', undefined, { version: 0, id: 'child', createdAt: Date.now(), parentSession: 'root', delegationDepth: 1 })
  assert.equal((await resumed.profile(child)).provider, 'deepseek')
  h.cfg.cmEnabledSessions = []; await resumed.settingsChanged()
  h.cfg.cmEnabledSessions = ['root']
  const again = new CMRuntime(() => h.cfg, () => null, h.journal)
  assert.equal(await again.profile(restored), null)
  await again.finishRun('root', restored)
  assert.equal((await new CMRuntime(() => h.cfg, () => null, h.journal).profile(restored)).provider, 'provider-next-run')
})


test('a user-created fork is an independent task, not an implicitly enabled subagent', async () => {
  const h = fixture({ id: 'fork', parent: 'root', depth: 0 })
  assert.equal(await h.run(), null)
  h.cfg.cmEnabledSessions = ['fork']
  assert.ok(await h.run())
})


test('forked history never re-attributes inherited CM charges to the new child', async () => {
  const h = fixture(); await h.run()
  h.session.append('turn/end', { turn: 1, reason: 'completed' })
  const child = Session.create('child', h.session.events, { version: 0, id: 'child', createdAt: Date.now(), parentSession: 'root', delegationDepth: 1 })
  assert.equal((await h.runtime.status({ session: child })).recent.filter(r => r.session_id === 'child').length, 0)
  assert.equal((await new CMRuntime(() => h.cfg, () => null, h.journal).status({ session: child })).attempts, 0)
})


test('an independent user fork does not inherit an unfinished parent team freeze', async () => {
  const h = fixture(); await h.runtime.beginRun(h.agent)
  h.session.append('turn/end', { turn: 1, reason: 'completed' })
  const fork = Session.create('fork', h.session.events, { version: 0, id: 'fork', createdAt: Date.now(), parentSession: 'root' })
  h.cfg.cmEnabledSessions = ['fork']; h.cfg.cmProvider = 'fork-provider'
  const runtime = new CMRuntime(() => h.cfg, () => null, h.journal)
  assert.equal((await runtime.profile(fork)).provider, 'fork-provider'); assert.equal(runtime.frozen.has('fork'), false)
  assert.equal((await runtime.beginRun({ session: fork })).profile.provider, 'fork-provider')
})


test('configured CM model and effort reach the actual request and audit; blank effort uses adapter default', async () => {
  for (const effort of ['max', '']) {
    const h = fixture()
    Object.assign(h.cfg, { cmProvider: 'custom-provider', cmModel: 'custom-model', cmEffort: effort })
    const result = await h.run()
    assert.ok(result)
    assert.equal(h.requests[0].provider, 'custom-provider')
    assert.equal(h.requests[0].model, 'custom-model')
    if (effort) assert.equal(h.requests[0].reasoningEffort, effort)
    else assert.equal(Object.hasOwn(h.requests[0], 'reasoningEffort'), false)
    const status = (await h.runtime.status(h.agent))
    assert.equal(status.model, 'custom-model')
    assert.equal(status.recent[0].profile.model, 'custom-model')
  }
})

test('CM route changes stay frozen across restart, then become effective after team review', async () => {
  const h = fixture()
  Object.assign(h.cfg, { cmModel: 'old-model', cmEffort: 'off' })
  const initial = await h.runtime.beginRun(h.agent)
  Object.assign(h.cfg, { cmProvider: 'next-provider', cmModel: 'next-model', cmEffort: 'max' })
  await h.runtime.settingsChanged()
  const session = Session.fromRestore('root', JSON.parse(JSON.stringify(h.session.events)), h.session.header)
  const resumed = new CMRuntime(() => h.cfg, () => null, h.journal)
  assert.equal((await resumed.status({ session })).model, 'old-model')
  assert.equal((await resumed.profile(session)).id, initial.profile.id)
  await resumed.finishRun('root', session)
  assert.equal((await resumed.status({ session })).model, 'next-model')
  assert.equal((await resumed.profile(session)).reasoningEffort, 'max')
  assert.notEqual((await resumed.profile(session)).id, initial.profile.id)
})

test('legacy settings retain DeepSeek defaults; invalid CM routes cannot dispatch', async () => {
  assert.equal(cmProfile({ cmProvider: 'deepseek' }).model, CM_MODEL)
  assert.equal(cmProfile({ cmProvider: 'deepseek' }).reasoningEffort, 'off')
  for (const invalid of [{ cmModel: ' ' }, { cmModel: 3 }, { cmEffort: 3 }]) {
    const h = fixture(), before = h.session.deriveMessages()
    Object.assign(h.cfg, invalid)
    await assert.rejects(h.run(), /CM_(MODEL_REQUIRED|EFFORT_INVALID)/)
    assert.equal(h.requests.length, 0)
    assert.deepEqual(h.session.deriveMessages(), before)
  }
})


test('two runtime instances atomically contend for the last durable per-agent turn slot; sibling allowance remains separate', async () => {
  const h = fixture(), profile = cmProfile(h.cfg), signal = new AbortController().signal
  for (let i = 0; i < 11; i++) {
    const ticket = await h.runtime.begin(h.agent, profile, {}, signal)
    assert.ok(ticket)
    await h.runtime.finish(ticket, null, new Error('not dispatched'), 0)
  }
  const runtimes = [0, 1].map(() => new CMRuntime(() => h.cfg, () => null, h.journal))
  const decisions = await Promise.all(runtimes.map(runtime => runtime.begin(h.agent, profile, {}, signal)))
  assert.equal(decisions.filter(Boolean).length, 1)
  for (let i = 0; i < decisions.length; i++) if (decisions[i]) await runtimes[i].finish(decisions[i], null, new Error('not dispatched'), 0)
  const records = (await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/cm-start')
  assert.equal(records.length, 12)
  assert.equal(await h.runtime.begin(h.agent, profile, {}, signal), null)
  const sibling = Session.create('sibling', undefined, { version: 0, id: 'sibling', createdAt: Date.now(), parentSession: 'root', delegationDepth: 1 })
  sibling.append('turn/start', { turn: 1 })
  const siblingTicket = await runtimes[0].begin({ session: sibling }, profile, {}, signal)
  assert.ok(siblingTicket)
  await runtimes[0].finish(siblingTicket, null, new Error('not dispatched'), 0)
  assert.equal((await h.journal.read('root')).events.filter(e => e.type === 'dpswarm/cm-start' && e.data.owner_session_id === 'sibling').length, 1)
  assert.equal(h.requests.length, 0)
})


test('same history crosses a small actual window but not a large window, independent of 12k', async () => {
  const large = fixture({ contextWindow: 1000000 }), small = fixture({ contextWindow: 25000 })
  assert.ok(large.meter.measure(large.session).surfaceTokens > 12000)
  assert.equal(await large.run(), null); assert.equal(large.requests.length, 0)
  assert.equal((await large.runtime.status(large.agent)).last_pressure_check.reason, 'below_threshold')
  assert.ok(await small.run()); assert.equal(small.requests.length, 1)
  const record = (await small.runtime.status(small.agent)).recent[0]
  assert.deepEqual(record.pressure_route, { provider: 'lead', model: 'lead-model' })
  assert.equal(record.context_window, 25000); assert.equal(record.threshold_tokens, 20000)
  assert.equal(record.threshold_ratio, 0.8); assert.equal(record.profile.version, 'dpswarm-cm-window-v2')
  assert.equal(Object.hasOwn(record.profile, 'thresholdTokens'), false)
  assert.equal(record.request_baseline.kind, 'estimated')
  assert.ok(record.compactable_tokens_estimate >= 4096)
})

test('pressure threshold is inclusive at exactly eighty percent', async () => {
  for (const delta of [0, 1]) {
    const h = fixture()
    const total = h.meter.measure(h.session).totalTokens
    h.windows.set('lead/lead-model', Math.ceil((total + delta) / 0.8))
    assert.equal(!!await h.run(), delta === 0)
    assert.equal(h.requests.length, delta === 0 ? 1 : 0)
  }
})

test('current assembly route supersedes a previous request and a changed model resolves a fresh capacity', async () => {
  const h = fixture({ contextWindow: 1000000 })
  h.session.append('request/header', { header: { config: { provider: 'old', model: 'old-small' } }, reason: 'initial' })
  h.windows.set('old/old-small', 1000)
  assert.equal(await h.run(), null)
  assert.deepEqual(h.metadata.at(-1), { provider: 'lead', model: 'lead-model' })
  h.assembly.variables = { provider: 'next', model: 'small' }; h.windows.set('next/small', 25000)
  assert.ok(await h.run()); assert.deepEqual(h.metadata.at(-1), { provider: 'next', model: 'small' })
  assert.deepEqual((await h.runtime.status(h.agent)).recent[0].pressure_route, { provider: 'next', model: 'small' })
})

test('missing or invalid model capacity skips DP CM and preserves native continuation', async () => {
  for (const window of [undefined, 0, -1, 0.5, Infinity]) {
    const h = fixture(); h.windows.set('lead/lead-model', window)
    await h.capture(); let continued = false
    await h.ctx.waterfall('agent/pre-step', { agent: h.agent, messages: [], signal: new AbortController().signal }, async () => { continued = true; return { kind: 'enter' } })
    assert.equal(continued, true); assert.equal(h.requests.length, 0)
    assert.equal((await h.runtime.status(h.agent)).last_pressure_check.reason, 'unknown_model_context')
  }
})

test('missing current assembly cannot fall back to a stale route header', async () => {
  const h = fixture()
  assert.equal(await h.engine.compactIfNeeded(h.agent, 'pressure', new AbortController().signal), null)
  assert.equal(h.requests.length, 0); assert.equal(h.metadata.length, 0)
  assert.equal((await h.runtime.status(h.agent)).last_pressure_check.reason, 'missing_current_assembly')
})

test('current system and tool schemas contribute to pressure before a request header exists', async () => {
  const h = fixture({ contextWindow: 100000 })
  assert.equal(h.session.requestHeader(), undefined)
  h.assembly.sections = [{ name: 'system', text: 's'.repeat(120000) }]
  h.assembly.tools = [{ name: 'tool', description: 'd'.repeat(80000), parameters: {} }]
  const surface = h.meter.measure(h.session).surfaceTokens
  assert.ok(surface < 80000)
  assert.ok(await h.run())
  const record = (await h.runtime.status(h.agent)).recent[0]
  assert.ok(record.request_tokens_before_estimate > surface + 49000)
  assert.equal(h.session.requestHeader(), undefined, 'Pressure measurement does not forge a native header')
})

test('pending claimed messages count in this step without being appended or counted twice', async () => {
  const h = fixture({ contextWindow: 100000 }), message = createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'p'.repeat(120000) }] })
  await h.capture()
  const visible = h.session.deriveMessages()[0], originalEvents = h.session.events.length
  let continued = false
  await h.ctx.waterfall('agent/pre-step', { agent: h.agent, messages: [visible, message], signal: new AbortController().signal }, async () => { continued = true; return { kind: 'enter', messages: [visible, message] } })
  assert.equal(continued, true); assert.equal(h.requests.length, 1)
  const record = (await h.runtime.status(h.agent)).recent[0]
  assert.equal(record.pending_tokens_estimate, h.meter.estimateMessage(message))
  assert.equal(h.session.events.slice(originalEvents).filter(event => event.type === 'user/message' && event.data?.id === message.id).length, 0)
})

test('new runtime context contributes once; already-retained snapshot is not duplicated', async () => {
  for (const retained of [false, true]) {
    const h = fixture({ contextWindow: 100000 }), context = 'c'.repeat(120000)
    h.assembly.contexts = [{ name: 'context', text: context }]
    const rendered = 'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n' + context
    if (retained) h.session.append('user/message', createUserMessage({ source: { kind: 'plugin', plugin: '@deepseek-ai/dsh-system-prompt' }, content: [{ type: 'text', text: rendered }] }), { surfaceOp: 'append' })
    assert.ok(await h.run())
    const check = (await h.runtime.status(h.agent)).last_pressure_check
    assert.equal(check.pending_tokens_estimate, retained ? 0 : h.meter.estimateMessage({ role: 'user', content: [{ type: 'text', text: rendered }] }))
  }
})

test('tiny old span is not summarized even when a large retained tail creates window pressure', async () => {
  const h = fixture({ count: 2, size: 100, contextWindow: 20000 })
  for (let i = 0; i < 4; i++) h.session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'large-tail'.repeat(10000) }] }), { surfaceOp: 'append' })
  const before = h.session.deriveMessages()
  assert.equal(await h.run(), null); assert.equal(h.requests.length, 0)
  const check = (await h.runtime.status(h.agent)).last_pressure_check
  assert.equal(check.reason, 'compactable_span_too_small'); assert.ok(check.compactable_tokens_estimate < 628)
  assert.deepEqual(h.session.deriveMessages(), before)
})

test('CM model capacity and worker cumulative allowance do not determine conversation pressure', async () => {
  const h = fixture({ contextWindow: 1000000 })
  Object.assign(h.cfg, { workerBudgetMode: 'manual', workerTokenLimit: 600000, workerCallLimit: 28 })
  h.windows.set('deepseek/' + CM_MODEL, 1000)
  assert.equal(await h.run(), null)
  h.cfg.workerTokenLimit = 1; h.cfg.workerCallLimit = 1
  assert.equal(await h.run(), null); assert.equal(h.requests.length, 0)
  assert.ok(h.metadata.every(call => call.provider === 'lead' && call.model === 'lead-model'))
})

test('old frozen fixed-threshold profile is skipped rather than silently reinterpreted', async () => {
  const h = fixture(), legacy = { ...cmProfile(h.cfg), version: 'dpswarm-cm-v1', thresholdTokens: 12000 }
  delete legacy.thresholdRatio
  h.runtime.frozen.set('root', { profile: legacy, disabled: false })
  assert.equal(await h.run(), null); assert.equal(h.requests.length, 0)
  const status = await h.runtime.status(h.agent)
  assert.equal(status.profile.version, 'dpswarm-cm-v1'); assert.equal(status.last_pressure_check.reason, 'legacy_profile_requires_new_run')
})


test('a bound fixed child uses its own model window instead of the root or assembly default', async () => {
  const h = fixture({ id: 'child', parent: 'root', depth: 1, origin: 'subagent', contextWindow: 1000000 })
  const label = 'dpswarm:DPswarm implementer'
  h.session.append('subagent/descriptor', { label })
  const parent = { session: { id: 'root', requestHeader: () => ({ config: { provider: 'worker', model: 'small', reasoningEffort: 'max' } }) } }
  const route = prepareChildRoute(parent, { provider: 'worker', model: 'small', reasoningEffort: 'max' }, { journal: h.journal, label })
  h.agent.options = route.agentOptions
  await route.bind('child')
  h.windows.set('worker/small', 25000)
  assert.ok(await h.run())
  assert.deepEqual(h.metadata, [{ provider: 'worker', model: 'small' }])
  const record = (await h.runtime.status(h.agent)).recent[0]
  assert.equal(record.agent_session_id, 'child'); assert.equal(record.root_session_id, 'root')
  assert.equal(record.route_source, 'frozen_child_route'); assert.equal(record.context_window, 25000)
  assert.deepEqual(record.pressure_route, { provider: 'worker', model: 'small' })
  assert.equal(h.requests[0].model, CM_MODEL, 'Summarizer model selection remains unchanged')
  route.close()
})


test('same route and content preserve a real usage-pressure bound; changed route or header cannot reuse it', async t => {
  for (const change of ['none', 'route', 'system', 'tools']) await t.test(change, async () => {
    const h = fixture({ contextWindow: 100000 }), prior = { config: { provider: 'lead', model: 'lead-model', reasoningEffort: 'max', maxTokens: 8192 } }
    h.session.append('step/start', { turn: 1, step: 1 })
    h.session.append('request/header', { header: prior, reason: 'initial' })
    h.session.append('assistant/message', { turn: 1, step: 1,
      message: { role: 'assistant', content: [{ type: 'text', text: 'observed result' }] },
      usage: { inputTokens: 90000, cacheReadTokens: 10000, outputTokens: 5 },
    }, { surfaceOp: 'append' })
    h.session.append('step/end', { turn: 1, step: 1 })
    assert.equal(h.meter.measure(h.session).baseline.kind, 'usage')
    assert.ok(h.meter.measure(h.session).surfaceTokens < 80000)
    if (change === 'route') { h.assembly.variables = { provider: 'new', model: 'new-model' }; h.windows.set('new/new-model', 100000) }
    if (change === 'system') h.assembly.sections = [{ name: 'changed', text: 'new system' }]
    if (change === 'tools') h.assembly.tools = [{ name: 'new-tool', parameters: {} }]
    assert.equal(!!await h.run(), change === 'none')
    const check = (await h.runtime.status(h.agent)).last_pressure_check
    assert.equal(check.request_baseline.kind, change === 'none' ? 'usage' : 'estimated')
    assert.equal(check.request_pressure_basis, change === 'none' ? 'same_route_content_prior_usage_upper_bound' : 'current_assembly_header')
    assert.equal(Object.hasOwn(check, 'same_route_content_prior_usage_tokens'), change === 'none')
  })
})
