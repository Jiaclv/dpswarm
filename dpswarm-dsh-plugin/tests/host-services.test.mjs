import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { getOptionalHostService, resolveHostSession, probeHostRuntime, combineRuntimeCompatibility } from '../lib/host-services.js'
import { KvMailboxStorage, TeamMailbox, MAILBOX_KV_UNIT } from '../lib/mailbox.js'
import { FixedTeamController } from '../lib/fixed-team.js'

const host = resolveHostRoot()
const [{ Context }, { Storage }] = await Promise.all(['cordis', 'dsh-storage'].map(name => import(hostModuleUrl(host, `${name}/lib/index.js`))))

async function fixture(t, { storage = true, sessions, unit: customUnit, backend: customBackend } = {}) {
  const root = new Context(), fibers = [], document = { tables: { messages: {} }, global: null }, opens = []
  const unit = customUnit || {
    async loadAll() { return structuredClone(document) },
    async putRecord(table, key, value) { document.tables[table][key] = structuredClone(value) },
    async deleteRecord(table, key) { delete document.tables[table][key] },
  }
  if (storage) {
    // The real service lives in a sibling plugin fiber, never on the root fixture.
    const provider = root.plugin({ name: 'fixture-storage', apply(ctx) {
      const hub = new Storage(ctx)
      ctx.effect(() => hub.backend.register('json', customBackend || { kv: { async open(descriptor) { opens.push(descriptor); return unit } } }))
    } })
    fibers.push(provider); await provider
  }
  if (sessions !== undefined) {
    const provider = root.plugin({ name: 'fixture-sessions', apply(ctx) { ctx.provide('sessions', sessions) } })
    fibers.push(provider); await provider
  }
  let ctx, runtime
  const plugin = root.plugin({ name: 'fixture-dpswarm-consumer', apply(value) {
    ctx = value
    value.inject([], child => { runtime = child })
  } })
  fibers.push(plugin); await plugin
  t.after(async () => { for (const fiber of fibers.reverse()) await fiber.dispose() })
  return { root, ctx, runtime, plugin, opens, document, storage: new KvMailboxStorage(ctx) }
}

const incompatible = (component, stage) => error => {
  assert.equal(error.code, 'HOST_RUNTIME_INCOMPATIBLE')
  assert.equal(error.details.component, component)
  assert.equal(error.details.stage, stage)
  assert.match(error.details.fingerprint, /^[a-f0-9]{64}$/)
  return true
}

test('real Cordis sibling service reproduces old direct-access failure in outer and nested contexts', async t => {
  const h = await fixture(t)
  for (const ctx of [h.ctx, h.runtime]) {
    assert.throws(() => ctx?.storage, /cannot get property "storage" without inject/)
    assert.ok(getOptionalHostService(ctx, 'storage'))
    assert.equal(new KvMailboxStorage(ctx).available(), true)
  }
  assert.equal(h.plugin.state, 2)
  assert.equal(probeHostRuntime(h.ctx).compatible, true)
  assert.equal(h.opens.length, 0, 'capability probing must not open or write KV')
})

test('real host registry persists mailbox registration and messages across adapter instances', async t => {
  const h = await fixture(t)
  const mailbox = new TeamMailbox({ storage: h.storage })
  await mailbox.register('root', { run_id: 'run-1', members: [{ id: 'implementer', role: 'implementer' }] })
  await mailbox.post('root', { message_id: 'fact-1', run_id: 'run-1', from: 'lead', to: 'implementer', kind: 'fact', content: 'approved API shape' })
  const cold = new TeamMailbox({ storage: new KvMailboxStorage(h.runtime) })
  assert.equal((await cold.pendingFor('root', 'implementer'))[0].message_id, 'fact-1')
  assert.equal(h.opens.length, 2)
  assert.deepEqual(h.opens[0], MAILBOX_KV_UNIT)
  assert.ok(Object.keys(h.document.tables.messages).length)
})

test('missing optional storage keeps consumer active and controller mailbox disabled', async t => {
  const h = await fixture(t, { storage: false })
  assert.equal(h.storage.available(), false)
  const controller = new FixedTeamController({ config: () => ({}), mailboxStorage: h.storage })
  assert.equal(await controller.openMailbox({}, {}, {}), null)
  const probe = probeHostRuntime(h.ctx)
  assert.equal(probe.compatible, true)
  assert.deepEqual(probe.storage, { available: false, composed: false })
  assert.equal(h.plugin.state, 2)
})

test('missing session registry and unknown session IDs return null without direct-property fallback', async t => {
  const missing = await fixture(t, { storage: false })
  assert.throws(() => missing.ctx.sessions, /without inject/)
  assert.equal(resolveHostSession(missing.ctx, 'unknown'), null)
  const session = { id: 'known' }
  const available = await fixture(t, { storage: false, sessions: new Map([['known', session]]) })
  assert.throws(() => available.ctx.sessions, /without inject/)
  assert.equal(resolveHostSession(available.ctx, 'unknown'), null)
  assert.equal(resolveHostSession(available.ctx, 'known').id, 'known')
})

test('managed lookup failures and invalid registry shapes use typed host incompatibility', async t => {
  const h = await fixture(t, { storage: false, sessions: {} })
  assert.throws(() => probeHostRuntime(h.ctx), incompatible('sessions', 'registry-shape'))
  const cause = new Error('lookup failed with unrelated text')
  assert.throws(() => probeHostRuntime({ get() { throw cause } }), error => {
    incompatible('storage', 'service-lookup')(error)
    assert.equal(error.cause, cause)
    return true
  })
  assert.throws(() => probeHostRuntime({}), incompatible('storage', 'service-lookup'))
  assert.throws(() => probeHostRuntime({ get: name => name === 'storage' ? {} : null }), incompatible('storage', 'backend-shape'))
})

test('generic optional services preserve lookup failures outside the host probe coverage', () => {
  const lookupError = Object.assign(new Error('token meter lookup failed'), { code: 'TOKEN_METER_LOOKUP_FAILED' })
  const ctx = { get(name) { if (name === 'tokenMeter') throw lookupError; return null } }
  assert.equal(probeHostRuntime(ctx).compatible, true)
  assert.throws(() => getOptionalHostService(ctx, 'tokenMeter'), error => error === lookupError)
  assert.throws(() => getOptionalHostService({}, 'tools'), error => {
    assert.ok(error instanceof TypeError)
    assert.equal(error.code, undefined)
    return true
  })
})

test('probe checks registered KV shape without opening it', async t => {
  const h = await fixture(t, { backend: { kv: { open: 'incompatible' } } })
  assert.throws(() => probeHostRuntime(h.ctx), incompatible('storage', 'kv-shape'))
  assert.equal(h.opens.length, 0)
  await assert.rejects(h.storage.loadAll(), incompatible('storage', 'kv-shape'))
})

test('actual KV IO failures stay original failures and cannot be reported as compatibility success', async t => {
  const io = Object.assign(new Error('disk unavailable'), { code: 'EIO' })
  const h = await fixture(t, { backend: { kv: { async open() { throw io } } } })
  assert.equal(probeHostRuntime(h.ctx).compatible, true)
  await assert.rejects(h.storage.loadAll(), error => error === io)
  const write = await fixture(t, { unit: { async loadAll() { return { tables: { messages: {} }, global: null } }, async putRecord() { throw io }, async deleteRecord() {} } })
  await assert.rejects(new TeamMailbox({ storage: write.storage }).register('root', { run_id: 'run-1', members: [] }), error => error === io)
})

test('healthy shape probe does not reclassify runtime session lookup or opened-unit failures', async t => {
  const lookupError = Object.assign(new Error('session store temporarily unavailable'), { code: 'SESSION_LOOKUP_FAILED' })
  const sessions = await fixture(t, { storage: false, sessions: { get() { throw lookupError } } })
  assert.equal(probeHostRuntime(sessions.ctx).compatible, true)
  assert.throws(() => resolveHostSession(sessions.ctx, 'known'), error => error === lookupError)

  const malformedUnit = await fixture(t, { unit: { async loadAll() {} } })
  assert.equal(probeHostRuntime(malformedUnit.ctx).compatible, true)
  await assert.rejects(malformedUnit.storage.loadAll(), error => {
    assert.equal(error.code, 'DPSWARM_MAILBOX_STORAGE_UNAVAILABLE')
    assert.notEqual(error.code, 'HOST_RUNTIME_INCOMPATIBLE')
    return true
  })
})

test('combined health fingerprints bind both host capability and sidecar observations', async t => {
  const h = await fixture(t), noStorage = await fixture(t, { storage: false })
  const sidecar = { compatible: true, fingerprint: 'sidecar-a', runtime: { revision: 1 } }
  const combined = combineRuntimeCompatibility(probeHostRuntime(h.ctx), sidecar)
  assert.equal(combined.compatible, true)
  assert.equal(combined.host_runtime.compatible, true)
  assert.equal(combined.sidecar_fingerprint, 'sidecar-a')
  assert.deepEqual(combined.runtime, sidecar.runtime)
  assert.notEqual(combined.fingerprint, combineRuntimeCompatibility(probeHostRuntime(noStorage.ctx), sidecar).fingerprint)
  assert.notEqual(combined.fingerprint, combineRuntimeCompatibility(probeHostRuntime(h.ctx), { ...sidecar, fingerprint: 'sidecar-b' }).fingerprint)
})
