import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const source = readFileSync(process.env.DPSWARM_TEST_CLIENT_SOURCE || new URL('../lib/client.js', import.meta.url), 'utf8')
const host = resolveHostRoot()
const { Context, Service } = await import(hostModuleUrl(host, 'cordis/lib/index.js'))

const react = { Component: class {}, Fragment: 'fragment', createElement: () => null,
  useCallback: fn => fn, useEffect() {}, useId: () => 'id', useMemo: fn => fn(),
  useRef: () => ({ current: null }), useState: () => [undefined, () => {}] }

/** Evaluate the hand-written bundle exactly as the DSH module loader does. */
function clientModule(reactApi = react) {
  let plugin
  const sandbox = { URLSearchParams, setTimeout, clearTimeout, AbortController, location: { search: '', href: 'http://127.0.0.1/' },
    history: { state: null, replaceState() {} },
    window: { __ModuleLoader__: { load(module) {
      plugin = module.factory(id => {
        if (id === 'react') return reactApi
        throw new Error('Optional icons unavailable')
      })
    } } } }
  vm.runInNewContext(source, sandbox)
  return plugin
}

const remoteNamespaces = api => Object.fromEntries(
  Object.entries(api).map(([name, methods]) => [`remote.${name}`, methods]))

function stubHost({ connection, remote }) {
  const registrations = [], reads = []
  const ctx = { connection,
    locale: {},
    settingsScope: { bind: () => ({}), describe: () => ({}) },
    slots: { inject: (_name, callback) => callback(), register: spec => registrations.push(spec.name) },
    get: name => { reads.push(name); return remote?.[name] } }
  return { ctx, registrations, reads }
}

function boot(options) {
  const host_ = stubHost(options)
  const plugin = clientModule()
  plugin.apply(host_.ctx)
  assert.deepEqual(host_.registrations, ['settings.section', 'settings.plugin.item', 'conversation.input.left'])
  return { ...host_, plugin }
}

/** Stub host services every client entry needs, plus the given `remote.*`. */
function provideHost(ctx, { connection, remote = false }) {
  const registrations = []
  ctx.provide('connection', connection)
  ctx.provide('locale', {})
  ctx.provide('settingsScope', { bind: () => ({}), describe: () => ({}) })
  ctx.provide('slots', { inject: (_name, callback) => callback(),
    register: spec => registrations.push(spec.name) })
  if (remote) {
    // dsh-api-gateway installs `remote` and one service per namespace from a
    // child fiber of its owner context, never from the root.
    ctx.inject(['connection'], (scope) => {
      new Service(scope, 'remote')
      for (const name of ['settings', 'session']) {
        scope.plugin({ name: `remote.${name}`, apply: child => { new Service(child, `remote.${name}`) } })
      }
    })
  }
  return registrations
}

test('client starts on DSH typed Remote with no connection.api', () => {
  const noStartupRpc = () => { throw new Error('Startup must not read or write host settings') }
  const host_ = boot({ connection: { isLoopback: true },
    remote: remoteNamespaces({ settings: { mutate: noStartupRpc, describe: noStartupRpc },
      session: { modelCatalog: noStartupRpc } }) })
  // Startup must not capture services that can mount later or be replaced.
  assert.deepEqual(host_.reads, [])
  // Injecting a namespace name would park the entry on hosts that never mount
  // it, so the client reaches them through ctx.get() instead. (The bundle
  // evaluates in its own VM realm, so compare the names, not the array.)
  assert.deepEqual([...host_.plugin.inject], ['slots', 'locale', 'connection', 'settingsScope'])
})

test('client still starts on legacy connection.api hosts without Remote', () => {
  const host_ = boot({ connection: { isLoopback: true, api: { settings: {}, llm: {} } } })
  assert.deepEqual(host_.reads, [])
})

test('missing optional APIs and non-loopback hosts do not break host startup', () => {
  boot({ connection: { isLoopback: true } })
  boot({ connection: { isLoopback: false } })
})

test('client entry applies where the Remote namespaces live in child-fiber services', async () => {
  const ctx = new Context()
  const registrations = provideHost(ctx, { connection: { isLoopback: true }, remote: true })
  const plugin = clientModule()
  await new Promise(resolve => setImmediate(resolve))
  // Reading them as `ctx.remote.settings` without injecting that exact name
  // fails the whole entry with `cannot get property "remote.settings" without
  // inject`; ctx.get() reaches the same service without arming that gate.
  await ctx.plugin({ inject: plugin.inject, apply: plugin.apply })
  assert.deepEqual(registrations, ['settings.section', 'settings.plugin.item', 'conversation.input.left'])
})

test('client entry applies on a legacy host without any Remote service', async () => {
  const ctx = new Context()
  const registrations = provideHost(ctx, { connection: { isLoopback: true, api: { settings: {}, llm: {} } } })
  const plugin = clientModule()
  await ctx.plugin({ inject: plugin.inject, apply: plugin.apply })
  assert.deepEqual(registrations, ['settings.section', 'settings.plugin.item', 'conversation.input.left'])
})

/** Render the original settings component far enough to exercise its catalog
 * transport. Hook state is retained across renders; no source instrumentation
 * or external model/settings calls are used. */
function catalogClient(ctx) {
  let cursor = 0
  const hooks = [], components = new Map()
  const reactApi = { ...react,
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useRef: value => { const i = cursor++; return hooks[i] ??= { current: value } },
    useState: value => {
      const i = cursor++
      if (!(i in hooks)) hooks[i] = typeof value === 'function' ? value() : value
      return [hooks[i], next => { hooks[i] = typeof next === 'function' ? next(hooks[i]) : next }]
    },
  }
  const snapshot = { status: 'ready', mode: 'host', writable: true, value: {} }
  ctx.provide('locale', {})
  ctx.provide('settingsScope', { bind: () => ({ getSnapshot: () => snapshot }), describe: () => ({}) })
  ctx.provide('slots', { inject: (_name, callback) => callback(),
    register: (spec, component) => components.set(spec.name, component) })
  const plugin = clientModule(reactApi)
  const fiber = ctx.plugin({ inject: plugin.inject, apply: plugin.apply })
  const catalog = () => {
    cursor = 0
    const wrapped = components.get('settings.section')({})
    const settings = wrapped.props.children[0]
    const tree = settings.type(settings.props)
    const find = node => {
      if (!node || typeof node !== 'object') return
      if (node.props?.role === 'test') return node.props.catalog
      for (const child of (Array.isArray(node) ? node : node.props?.children ?? [])) {
        const found = find(child)
        if (found) return found
      }
    }
    return find(tree)
  }
  return { fiber, catalog }
}

function provideRemote(ctx, model = 'configured-tester') {
  return ctx.inject(['connection'], scope => {
    new Service(scope, 'remote')
    scope.plugin({ name: 'fixture-session', apply: child => {
      const service = new Service(child, 'remote.session')
      service.modelCatalog = async (...args) => {
        assert.equal(args.length, 0)
        return { ok: true, value: { groups: [{ id: 'configured-provider', models: [{ id: model }] }], failures: [] } }
      }
    } })
  })
}

const settled = () => new Promise(resolve => setImmediate(resolve))

test('real Cordis property gate differs from public get; catalog reads a ready namespace', async t => {
  const ctx = new Context()
  t.after(() => ctx.fiber.dispose())
  ctx.provide('connection', { isLoopback: true })
  await provideRemote(ctx)
  await settled()
  await ctx.plugin({ name: 'uninjected-probe', apply: scope => {
    assert.throws(() => scope.remote, /without inject/)
    assert.equal(typeof scope.get('remote.session').modelCatalog, 'function')
  } })
  const client = catalogClient(ctx)
  await client.fiber
  await client.catalog().load()
  assert.equal(client.catalog().status, 'ready')
  assert.equal(client.catalog().groups[0].models[0].id, 'configured-tester')
})

test('catalog recovers when typed Remote mounts after an empty legacy llm shell', async t => {
  const ctx = new Context()
  t.after(() => ctx.fiber.dispose())
  ctx.provide('connection', { isLoopback: true, api: { llm: {} } })
  const client = catalogClient(ctx)
  await client.fiber
  await client.catalog().load()
  assert.equal(client.catalog().status, 'error')
  await provideRemote(ctx)
  await settled()
  await client.catalog().load()
  assert.equal(client.catalog().status, 'ready')
  assert.equal(client.catalog().groups[0].models[0].id, 'configured-tester')
})

test('catalog follows Remote removal and remount instead of retaining a disposed service', async t => {
  const ctx = new Context()
  t.after(() => ctx.fiber.dispose())
  ctx.provide('connection', { isLoopback: true })
  const client = catalogClient(ctx)
  await client.fiber
  const remote = provideRemote(ctx, 'first-tester')
  await remote
  await settled()
  await client.catalog().load()
  assert.equal(client.catalog().status, 'ready')
  await remote.dispose()
  await settled()
  await client.catalog().load()
  assert.equal(client.catalog().status, 'error')
  await provideRemote(ctx, 'replacement-tester')
  await settled()
  await client.catalog().load()
  assert.equal(client.catalog().status, 'ready')
  assert.equal(client.catalog().groups[0].models[0].id, 'replacement-tester')
})

test('catalog reads the current legacy API after it is installed late', async t => {
  const ctx = new Context()
  t.after(() => ctx.fiber.dispose())
  const connection = { isLoopback: true }
  ctx.provide('connection', connection)
  const client = catalogClient(ctx)
  await client.fiber
  connection.api = { llm: { models: async (_request, signal) => {
    assert.ok(signal instanceof AbortSignal)
    return { result: { ok: true, value: { groups: [{ id: 'legacy', models: [{ id: 'legacy-tester' }] }], failures: [] } } }
  } } }
  await client.catalog().load()
  assert.equal(client.catalog().status, 'ready')
  assert.equal(client.catalog().groups[0].models[0].id, 'legacy-tester')
})