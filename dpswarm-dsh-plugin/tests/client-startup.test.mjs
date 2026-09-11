import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const source = readFileSync(new URL('../lib/client.js', import.meta.url), 'utf8')
const host = resolveHostRoot()
const { Context, Service } = await import(hostModuleUrl(host, 'cordis/lib/index.js'))

const react = { Component: class {}, Fragment: 'fragment', createElement: () => null,
  useCallback: fn => fn, useEffect() {}, useId: () => 'id', useMemo: fn => fn(),
  useRef: () => ({ current: null }), useState: () => [undefined, () => {}] }

/** Evaluate the hand-written bundle exactly as the DSH module loader does. */
function clientModule() {
  let plugin
  const sandbox = { URLSearchParams, setTimeout, location: { search: '', href: 'http://127.0.0.1/' },
    history: { state: null, replaceState() {} },
    window: { __ModuleLoader__: { load(module) {
      plugin = module.factory(id => {
        if (id === 'react') return react
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
  // Namespace services are mounted by a host that has them, so a load-time
  // read only reports which generation this host is; it must not call them.
  assert.deepEqual(host_.reads, ['remote.settings', 'remote.session'])
  // Injecting a namespace name would park the entry on hosts that never mount
  // it, so the client reaches them through ctx.get() instead. (The bundle
  // evaluates in its own VM realm, so compare the names, not the array.)
  assert.deepEqual([...host_.plugin.inject], ['slots', 'locale', 'connection', 'settingsScope'])
})

test('client still starts on legacy connection.api hosts without Remote', () => {
  const host_ = boot({ connection: { isLoopback: true, api: { settings: {}, llm: {} } } })
  assert.deepEqual(host_.reads, ['remote.settings', 'remote.session'])
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
