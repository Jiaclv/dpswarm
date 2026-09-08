/** Isolated native session.selectModel -> fixed-team effective-route regression. */
import assert from 'node:assert/strict'
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmBudget', 'dpswarmCM', 'settings', 'sessions']

export function apply(ctx) {
  const output = process.env.DPSWARM_ROUTE_PROBE_OUTPUT
  const directory = process.env.DPSWARM_ROUTE_PROBE_DIR
  const python = process.env.DPSWARM_ROUTE_PROBE_PYTHON
  const source = process.env.DPSWARM_ROUTE_PROBE_SOURCE
  const caseName = process.env.DPSWARM_ROUTE_PROBE_CASE
  if (!output || !directory || !python || !source || !caseName || !process.env.DSH_HOME || !resolve(process.env.DSH_HOME).startsWith(resolve(directory))) throw new Error('Isolated route probe environment required')
  const cases = {
    flash_max: { provider: 'route-fixture-a', model: 'deepseek-v4-flash', effort: 'max' },
    glm_default: { provider: 'route-fixture-b', model: 'glm-5.3', effort: undefined },
  }
  const selected = cases[caseName]
  if (!selected) throw new Error('Unknown route probe case')
  const calls = [], checks = []
  let parent, handle, sidecarProcess, settingsBefore, cancelled = false
  const tool = (id, name, args = {}) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
  const route = session => {
    const config = session.requestHeader?.()?.config
    assert.ok(config, 'Native request/header config is required')
    return { provider: config.provider, model: config.model, ...(Object.hasOwn(config, 'reasoningEffort') ? { effort: config.reasoningEffort } : {}) }
  }
  const assertRoute = (actual, expected, label) => {
    assert.equal(actual.provider, expected.provider, label + ' provider')
    assert.equal(actual.model, expected.model, label + ' model')
    if (expected.effort === undefined) assert.ok(!Object.hasOwn(actual, 'effort'), label + ' must not inherit stale reasoning effort')
    else assert.equal(actual.effort, expected.effort, label + ' effort')
  }
  const returned = (options, id) => {
    const blocks = options.messages.flatMap(message => message.content || [])
    const result = blocks.find(block => block.type === 'tool-result' && block.toolCallId === id)
    assert.ok(result && !result.isError, 'Missing/successful tool result ' + id)
    return JSON.parse(result.content.filter(block => block.type === 'text').map(block => block.text).join(''))
  }
  class Fixture extends LlmAdapter {
    async resolveModel(provider, id) {
      if (id === 'glm-5.3') return { provider, id, name: id, defaultMaxTokens: 32768 }
      return { provider, id, name: id, defaultMaxTokens: 32768, reasoning: { efforts: [{ id: 'high', name: 'High' }, { id: 'max', name: 'Max' }] } }
    }
    async *stream(options) {
      const session = ctx.sessions.get(options.sessionId)
      const agent = ctx.agents.get(options.sessionId)
      const child = session.header.origin === 'subagent'
      const header = route(session)
      const row = { session_id: session.id, child, agent_options: agent?.options ? { ...agent.options } : null, options: { provider: options.provider, model: options.model, ...(options.reasoningEffort === undefined ? {} : { effort: options.reasoningEffort }) }, header }
      calls.push(row)
      let block
      if (child) {
        const budget = await ctx.dpswarmBudget.status({ session })
        row.role = budget.policy_binding?.label
        if (row.role === 'implementer') assertRoute(header, selected, 'implementer actual header')
        else if (row.role === 'tester') assertRoute(header, { provider: 'route-fixture-tester', model: 'glm-5.3-flash', effort: 'max' }, 'tester explicit header')
        else throw new Error('Unexpected child role')
        assert.deepEqual(row.options, header, 'Provider call must match native header exactly')
        block = { type: 'text', text: row.role + ' fixture delivery; no files, tests, or network work were performed.' }
      } else {
        assertRoute(header, selected, 'Lead actual header')
        assert.deepEqual(row.options, header, 'Lead provider call must match native header exactly')
        const number = calls.filter(call => !call.child).length
        if (number === 1) block = tool('models', 'dpswarm_models')
        else if (number === 2) {
          const models = returned(options, 'models')
          assertRoute({ provider: models.configured_profile.implementer.provider, model: models.configured_profile.implementer.model, ...(models.configured_profile.implementer.reasoning_effort ? { effort: models.configured_profile.implementer.reasoning_effort } : {}) }, selected, 'configured implementer route')
          assertRoute({ provider: models.configured_profile.tester.provider, model: models.configured_profile.tester.model, ...(models.configured_profile.tester.reasoning_effort ? { effort: models.configured_profile.tester.reasoning_effort } : {}) }, { provider: 'route-fixture-tester', model: 'glm-5.3-flash', effort: 'max' }, 'configured tester route')
          block = tool('run', 'dpswarm_run', { task: 'Native route probe only. Do not edit files, run tests, or use network.', acceptance: 'Return two no-op reports; Lead reviews both.' })
        } else if (number === 3) {
          const result = returned(options, 'run')
          assert.equal(result.failed.length, 0, JSON.stringify(result.failed))
          assert.equal(result.deliveries.length, 2)
          block = tool('review-impl', 'dpswarm_review', { item_id: result.deliveries.find(item => item.role === 'implementer').item_id, verdict: 'accept', reason: 'Native route probe delivery observed.' })
        } else if (number === 4) {
          const result = returned(options, 'run')
          assert.equal(returned(options, 'review-impl').ok, true)
          block = tool('review-test', 'dpswarm_review', { item_id: result.deliveries.find(item => item.role === 'tester').item_id, verdict: 'accept', reason: 'Native route probe delivery observed.' })
        } else if (number === 5) {
          assert.equal(returned(options, 'review-test').ok, true)
          block = { type: 'text', text: 'Native effective Lead route probe complete.' }
        } else throw new Error('Unexpected extra Lead request')
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 10, outputTokens: 20 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter(['route-fixture-a', 'route-fixture-b', 'route-fixture-tester', 'route-fixture-cm'], new Fixture())
  async function startSidecar(workspace) {
    sidecarProcess = spawn(python, ['-m', 'dpswarm.session_server', '--port', '0', '--workspace', workspace], { cwd: source, windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] })
    let transcript = ''
    sidecarProcess.stdout.on('data', data => { transcript += data.toString(); appendFileSync(join(directory, 'sidecar.stdout.log'), data) })
    sidecarProcess.stderr.on('data', data => appendFileSync(join(directory, 'sidecar.stderr.log'), data))
    const deadline = Date.now() + 20000
    while (Date.now() < deadline) {
      const match = /http:\/\/127\.0\.0\.1:(\d+)/.exec(transcript)
      if (match) return 'http://127.0.0.1:' + match[1]
      if (sidecarProcess.exitCode !== null) throw new Error('Own sidecar exited ' + sidecarProcess.exitCode)
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error('Own sidecar startup timeout')
  }
  async function waitForSelection() {
    const signal = join(directory, 'selection.json'), deadline = Date.now() + 30000
    while (Date.now() < deadline) {
      if (existsSync(signal)) return JSON.parse(readFileSync(signal, 'utf8'))
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error('Timed out waiting for isolated actual session.selectModel')
  }
  async function run() {
    let report
    try {
      const workspace = join(directory, 'sidecar-state'); mkdirSync(workspace, { recursive: true })
      settingsBefore = ctx.settings.get('dpswarm')
      const url = await startSidecar(workspace)
      const id = 'native-route-' + randomUUID()
      await ctx.settings.update('dpswarm', { sidecarUrl: url, workspace, pythonCmd: python, dpswarmDir: source, autoStart: false,
        workerBudgetMode: 'unlimited', workerBudgetSessionOverrides: [], workerTimeoutSeconds: 60,
        implMode: 'lead', implProvider: '', implModel: '', implEffort: '',
        testProvider: 'route-fixture-tester', testModel: 'glm-5.3-flash', testEffort: 'max', reviewerMode: 'lead',
        cmEnabledSessions: [id], cmProvider: 'route-fixture-cm', cmModel: 'glm-5.3-flash', cmEffort: 'max', enabledSessions: [id] })
      handle = await ctx.agents.create({ sessionId: id, meta: { cwd: process.cwd(), agentPreset: 'standard' },
        // Deliberately stale options: actual route arrives only through the native API selection.
        agentOptions: { provider: 'route-fixture-a', model: 'deepseek-v4-pro', reasoningEffort: 'high' },
        setup: async scope => { await ctx.agentPresets.mount(scope, 'standard') } })
      parent = handle.agent
      writeFileSync(join(directory, 'ready.json'), JSON.stringify({ session_id: id, startup_options: parent.options, case: caseName }, null, 2))
      const selectionReceipt = await waitForSelection()
      assert.deepEqual(selectionReceipt.selected, { provider: selected.provider, model: selected.model, ...(selected.effort === undefined ? {} : { reasoningEffort: selected.effort }) })
      assert.equal(parent.options.model, 'deepseek-v4-pro', 'Probe requires options to stay stale after API selection')
      assert.equal(parent.options.reasoningEffort, 'high')
      const cm = await ctx.dpswarmCM.profile(parent.session)
      assertRoute({ provider: cm.provider, model: cm.model, ...(cm.reasoningEffort ? { effort: cm.reasoningEffort } : {}) }, { provider: 'route-fixture-cm', model: 'glm-5.3-flash', effort: 'max' }, 'CM explicit profile')
      parent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Use the enabled fixed team for the no-op native route probe. Do not edit files or run tests. Review both worker reports.' }] }))
      await Promise.race([parent.whenIdle(), new Promise((_, reject) => setTimeout(() => reject(new Error('Lead did not finish')), 60000))])
      const children = calls.filter(row => row.child)
      assert.equal(calls.filter(row => !row.child).length, 5)
      assert.equal(children.length, 2)
      assert.deepEqual(children.map(row => row.role).sort(), ['implementer', 'tester'])
      assertRoute(route(parent.session), selected, 'folded Lead final header')
      checks.push('actual-session-selectModel-not-options', 'lead-and-implementer-native-header-follow-selection', 'tester-and-cm-explicit-glm-flash', selected.effort === undefined ? 'effort-removal-no-stale-high' : 'explicit-flash-max')
      report = { passed: true, external_model_calls: 0, case: caseName, selection: selected, startup_options: parent.options, lead_header: route(parent.session), cm_profile: cm, checks, calls,
        execution: 'Isolated native DSH host + actual local session.selectModel HTTP API + fixed team + real Python sidecar + local LlmAdapter only.' }
    } catch (error) {
      report = { passed: false, case: caseName, error: error.stack, checks, calls, parent_events: parent?.session.events || [] }
    } finally {
      try { await handle?.dispose() } catch (error) { report.cleanup_error = String(error); report.passed = false }
      try { if (settingsBefore) await ctx.settings.replace('dpswarm', settingsBefore) } catch (error) { report.settings_restore_error = String(error); report.passed = false }
      if (sidecarProcess && sidecarProcess.exitCode === null) sidecarProcess.kill()
      writeFileSync(output, JSON.stringify(report, null, 2))
    }
  }
  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, case: caseName, error: error.stack, calls, checks }, null, 2))) }, 400)
    return () => { cancelled = true; clearTimeout(timer); if (sidecarProcess && sidecarProcess.exitCode === null) sidecarProcess.kill() }
  })
}
