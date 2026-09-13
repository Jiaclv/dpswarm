import assert from 'node:assert/strict'
import test from 'node:test'
import { registerHooks } from 'node:module'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

const host = resolveHostRoot(), lib = process.env.DPSWARM_TEST_LIB
  ? pathToFileURL(resolve(process.env.DPSWARM_TEST_LIB) + '/').href : new URL('../lib/', import.meta.url).href
const loopUrl = hostModuleUrl(host, 'dsh-agent-loop/lib/index.js')
// Expose the native lifecycle classes only in this test process, as the native
// budget pipeline test does. The host files and their methods remain unchanged.
const hooks = registerHooks({ load(url, context, nextLoad) {
  const loaded = nextLoad(url, context)
  return url === loopUrl ? { ...loaded, source: loaded.source + '\nexport { ReactLoopAgent, SystemPromptProjection, RuntimeContextProjection };\n' } : loaded
} })
const [{ ReactLoopAgent, SystemPromptProjection, RuntimeContextProjection }, { Session }, { Context },
  { default: LlmRuntime, LlmAdapter, createUserMessage }, { default: SystemPrompt }, { ToolRuntime },
  plugin, { AuditJournal }, { sessionEvents }] = await Promise.all([
  import(loopUrl), import(hostModuleUrl(host, 'dsh-session/lib/index.js')),
  import(hostModuleUrl(host, 'cordis/lib/index.js')), import(hostModuleUrl(host, 'dsh-llm/lib/index.js')),
  import(hostModuleUrl(host, 'dsh-system-prompt/lib/index.js')), import(hostModuleUrl(host, 'dsh-tools/lib/index.js')),
  import(new URL('index.js', lib)), import(new URL('audit.js', lib)), import(new URL('host-session-compat.js', lib)),
])
hooks.deregister()
const provider = 'cold-start-local-fixture', model = 'cold-start-text-model'
const message = text => createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] })
const capabilitySection = assembly => assembly.sections.find(row => row.name === 'dpswm:model-capabilities')?.text || ''

test('full plugin assembles a headerless native session, reaches the local provider, then renders observed capabilities', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'dpswarm-cold-start-capabilities-'))
  const ctx = new Context(), journal = new MemoryAuditJournal(), calls = []
  // Audit persistence is unrelated to prompt assembly. Keep every incidental
  // read in memory, and fail any attempted sidecar write instead of contacting
  // a daily host or constructing a team/Python control plane for this test.
  t.mock.method(AuditJournal.prototype, 'read', rootId => journal.read(rootId))
  t.mock.method(AuditJournal.prototype, 'transaction', () => { throw new Error('Unexpected audit write in cold-start fixture') })
  t.after(async () => { await ctx.fiber.dispose(); await rm(directory, { recursive: true, force: true }) })
  new SystemPrompt(ctx, { includeHarnessIdentity: false, includeRuntimeContext: true })
  new ToolRuntime(ctx)
  const llm = new LlmRuntime(ctx)
  class LocalAdapter extends LlmAdapter {
    async resolveModel(routeProvider, id) {
      assert.equal(routeProvider, provider)
      assert.equal(id, model)
      return { provider: routeProvider, id, name: id, inputModalities: ['text'], context: { contextWindow: 1000000 } }
    }
    async *stream(request) {
      calls.push(request)
      yield { type: 'block-end', index: 0, block: { type: 'text', text: 'Local cold-start fixture response.' } }
      yield { type: 'usage', usage: { inputTokens: 100, outputTokens: 10 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  llm.registerAdapter([provider], new LocalAdapter())
  const session = Session.create('cold-start-capabilities-root', undefined, {
    version: 3, id: 'cold-start-capabilities-root', createdAt: 1, isSeeded: false, cwd: directory,
  })
  const agent = Object.assign(Object.create(ReactLoopAgent.prototype), {
    id: session.id, session, options: { provider, model, maxTokens: 1024 },
    phase: { kind: 'running', turn: 1, step: 1, abort: new AbortController() },
    requestHeaderLogged: false, requestSurfaceGeneration: session.surface.replaceGeneration,
    frozenMessages: new WeakSet(), assistantStreamRevision: 0, assistantAttemptCounter: 0,
  })
  ctx.provide('sessions', { get: id => id === session.id ? session : undefined, list: () => [session] })
  ctx.provide('agents', { get: id => id === session.id ? agent : undefined, requireInitiator: () => agent })
  ctx.provide('agentLoop', { config: { maxParallelToolCalls: 1 } })
  ctx.provide('settings', { installSection: (_owner, _namespace, _schema, entry, settingsHooks) => settingsHooks.setSource(() => entry) })
  ctx.provide('subagents', { start: () => { throw new Error('No worker may start in a cold-start prompt test') } })
  let queued = []
  Object.assign(agent, {
    loopCtx: ctx,
    dispatch: { waterfall: (name, payload, next) => ctx.waterfall(name, { ...payload, agent }, next), emit: () => {} },
    inbox: { claim: () => { const current = queued; queued = []; return current } },
    runtimeContext: new RuntimeContextProjection(ctx, session), systemPrompt: new SystemPromptProjection(session),
  })
  await ctx.plugin({ name: plugin.name, inject: plugin.inject, apply: plugin.apply }, {
    workspace: directory, autoStart: false, enabledSessions: [], cmEnabledSessions: [],
    implMode: 'lead', testProvider: provider, testModel: model, reviewerMode: 'lead', workerBudgetMode: 'unlimited',
  })
  assert.ok(ctx.tools.get('dpswarm_models'), 'the complete plugin must actually register its tools and prompt sections')
  assert.equal(session.requestHeader(), undefined)
  const assembly = await ctx.systemPrompt.assemble({ agent, signal: agent.phase.abort.signal })
  assert.ok(Array.isArray(assembly.sections), 'assembly shape: ' + JSON.stringify(assembly))
  assert.ok(assembly.sections.some(row => row.name === 'dpswm:guide'), 'do not pass by omitting the plugin')
  assert.equal(capabilitySection(assembly), '', 'a creation-time route is not an observed request route')
  assert.equal(calls.length, 0)
  queued = [message('Run the deterministic local cold-start fixture.')]
  const first = await agent.preStep('next-step', { turn: 1, step: 1 })
  assert.equal(first.kind, 'enter')
  assert.equal(session.requestHeader(), undefined, 'native prompt assembly precedes the first request header')
  await agent.step(first)
  assert.equal(calls.length, 1, 'first native request must reach the local adapter')
  assert.equal(calls[0].provider, provider)
  assert.equal(calls[0].model, model)
  assert.equal(session.requestHeader().config.provider, provider)
  assert.equal(session.requestHeader().config.model, model)
  assert.ok(sessionEvents(session).some(row => row.type === 'assistant/message' && row.data.usage?.inputTokens === 100))

  // Populate the actual plugin-owned registry through its registered discovery
  // tool. Capability metadata comes from the real LLM service/local adapter.
  const discovery = await ctx.tools.get('dpswarm_models').execute({}, { agent, signal: agent.phase.abort.signal })
  assert.match(JSON.stringify(discovery), /"image_input":"unsupported"/)
  const warm = await ctx.systemPrompt.assemble({ agent, signal: agent.phase.abort.signal })
  assert.match(capabilitySection(warm), /Host-observed capability for the exact current route/)
  assert.match(capabilitySection(warm), /"image_input":"unsupported"/)
  assert.match(capabilitySection(warm), new RegExp(model))
  agent.phase.step = 2
  agent.runtimeContext = new RuntimeContextProjection(ctx, session)
  const second = await agent.preStep('next-step', { turn: 1, step: 2 })
  assert.equal(second.kind, 'enter')
  await agent.step(second)
  assert.equal(calls.length, 2)
  assert.match(JSON.stringify(calls[1].messages), /Host-observed capability for the exact current route/)
  assert.equal(calls[1].provider, provider)
  assert.equal(calls[1].model, model, 'optional capability guidance never substitutes the selected model')
})
