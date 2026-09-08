/** Two-process native cold-recovery probe for durable fixed-role routes. */
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { AuditJournal } from '../lib/audit.js'
import { Sidecar } from '../lib/sidecar.js'

const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
const { KNOWN_SESSION_EVENT_TYPES } = await import(hostModuleUrl(host, 'dsh-session/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'settings', 'sessions', 'dpswarmBudget']

const sha = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function deadline(promise, ms, label) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timeout`)), ms) })]) }
  finally { clearTimeout(timer) }
}

export function apply(ctx) {
  const directory = process.env.DPSWARM_ROUTE_COLD_DIR
  const phase = Number(process.env.DPSWARM_ROUTE_COLD_PHASE)
  const sidecarUrl = process.env.DPSWARM_ROUTE_COLD_SIDECAR_URL
  const python = process.env.DPSWARM_ROUTE_COLD_PYTHON
  const source = process.env.DPSWARM_ROUTE_COLD_SOURCE
  if (!directory || !sidecarUrl || !python || !source || ![1, 2].includes(phase) || !process.env.DSH_HOME
    || !resolve(process.env.DSH_HOME).startsWith(resolve(directory))) throw new Error('Unique isolated cold-route probe environment required')
  const rootIdPath = join(directory, 'phase1-checkpoint.json')
  const output = join(directory, `phase${phase}-result.json`)
  const prior = phase === 2 ? JSON.parse(readFileSync(rootIdPath, 'utf8')) : null
  const ids = prior?.ids || { root: `route-cold-root-${randomUUID()}` }
  const caseName = process.env.DPSWARM_ROUTE_COLD_CASE || 'flash_max'
  const selectedCases = {
    flash_max: { provider: 'route-cold-lead', model: 'deepseek-v4-flash', reasoningEffort: 'max' },
    glm_default: { provider: 'route-cold-glm', model: 'glm-5.3' },
  }
  const selected = selectedCases[caseName]
  if (!selected) throw new Error('Unknown cold route probe case')
  const tester = { provider: 'route-cold-tester', model: 'glm-5.3-flash', reasoningEffort: 'max' }
  const calls = [], checks = [], handles = []
  let lead, result, settingsBefore
  const descriptorFor = session => session.events.find(event => event.type === 'subagent/descriptor')?.data
  const routeFor = session => {
    const config = session.requestHeader?.()?.config
    assert.ok(config, `Missing native request header for ${session.id}`)
    return { provider: config.provider, model: config.model, ...(config.reasoningEffort === undefined ? {} : { reasoningEffort: config.reasoningEffort }) }
  }
  const assertRoute = (actual, expected, label) => assert.deepEqual(actual, expected, label)
  const nativeKnown = session => {
    const unknown = [...new Set(session.events.map(event => event.type).filter(type => !KNOWN_SESSION_EVENT_TYPES.has(type)))]
    assert.deepEqual(unknown, [], `Unknown native events in ${session.id}`)
    assert.ok(!session.events.some(event => event.type.startsWith('dpswarm/')), 'Audit records must stay out of the native session journal')
    return { session_id: session.id, event_count: session.events.length, unknown, plugin_native_events: 0 }
  }
  const tool = (id, name, args = {}) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
  const returned = (options, id) => {
    const block = options.messages.flatMap(message => message.content || []).find(item => item.type === 'tool-result' && item.toolCallId === id)
    assert.ok(block && !block.isError, `Expected successful tool result ${id}: ${JSON.stringify(block)}`)
    return JSON.parse(block.content.filter(item => item.type === 'text').map(item => item.text).join(''))
  }
  class Fixture extends LlmAdapter {
    async resolveModel(provider, id) {
      return { provider, id, name: id, defaultMaxTokens: 32768,
        reasoning: { efforts: [{ id: 'high', name: 'High' }, { id: 'max', name: 'Max' }] } }
    }
    async *stream(options) {
      const session = ctx.sessions.get(options.sessionId)
      const header = routeFor(session)
      const row = { phase, session_id: session.id, header, options: { provider: options.provider, model: options.model, ...(options.reasoningEffort === undefined ? {} : { reasoningEffort: options.reasoningEffort }) } }
      calls.push(row)
      assert.deepEqual(row.options, header, 'Provider request must equal final native header')
      let block
      if (session.id === ids.root) {
        assertRoute(header, selected, 'Lead must use actual session.selectModel route')
        const turn = calls.filter(call => call.session_id === ids.root).length
        if (turn === 1) block = tool('models', 'dpswarm_models')
        else if (turn === 2) block = tool('run', 'dpswarm_run', { task: 'Cold route probe only. Return two no-op reports. Do not edit files, run tests, or use network.', acceptance: 'Lead reviews both reports.' })
        else if (turn === 3) {
          result = returned(options, 'run')
          assert.equal(result.mode, 'fixed-team-v1')
          assert.equal(result.failed.length, 0, JSON.stringify(result.failed))
          block = tool('review-implementer', 'dpswarm_review', { item_id: result.deliveries.find(item => item.role === 'implementer').item_id, verdict: 'accept', reason: 'Probe only.' })
        } else if (turn === 4) block = tool('review-tester', 'dpswarm_review', { item_id: result.deliveries.find(item => item.role === 'tester').item_id, verdict: 'accept', reason: 'Probe only.' })
        else if (turn === 5) block = { type: 'text', text: 'Cold route phase one complete.' }
        else throw new Error(`Unexpected Lead call ${turn}`)
      } else {
        const descriptor = descriptorFor(session)
        row.label = descriptor?.label
        if (session.id === prior?.implementer_id || descriptor?.label === 'dpswarm:DPswarm implementer') assertRoute(header, selected, 'Implementer route')
        else if (session.id === prior?.tester_id || descriptor?.label === 'dpswarm:DPswarm tester') assertRoute(header, tester, 'Tester route')
        else throw new Error(`Unexpected fixture child ${session.id}: ${JSON.stringify(descriptor)}`)
        block = { type: 'text', text: 'Native fixed-role probe report. No files, tests, or network work were performed.' }
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 10, outputTokens: 20 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter(['route-cold-lead', 'route-cold-glm', 'route-cold-tester'], new Fixture())
  const setupRoot = async scope => { await ctx.agentPresets.mount(scope, 'standard') }
  const setupChild = parent => async scope => { ctx.agentPresets.composeFrom(scope, parent.ctx) }
  const staleOptions = { provider: 'route-cold-lead', model: 'deepseek-v4-pro', reasoningEffort: 'high' }
  async function createRoot() {
    const handle = await ctx.agents.create({ sessionId: ids.root, meta: { cwd: process.cwd(), agentPreset: 'standard' }, agentOptions: staleOptions, setup: setupRoot })
    handles.push(handle); return handle.agent
  }
  async function resume(id, parent = null) {
    const handle = await ctx.agents.resume({ resumeSessionId: id, agentOptions: staleOptions, setup: parent ? setupChild(parent) : setupRoot })
    handles.push(handle); return handle.agent
  }
  async function prompt(agent, text) {
    agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] }))
    await deadline(agent.whenIdle(), 30000, `Prompt ${agent.session.id}`)
  }
  async function waitSelection() {
    const file = join(directory, 'selection.json'), until = Date.now() + 30000
    while (Date.now() < until) { if (existsSync(file)) return JSON.parse(readFileSync(file, 'utf8')); await wait(100) }
    throw new Error('Timed out waiting for actual native session.selectModel')
  }
  async function configure() {
    await ctx.settings.update('dpswarm', { autoStart: false, sidecarUrl, workspace: join(directory, 'sidecar-state'), pythonCmd: python, dpswarmDir: source,
      enabledSessions: [ids.root], workerBudgetMode: 'unlimited', workerBudgetSessionOverrides: [], workerTimeoutSeconds: 60,
      implMode: 'lead', implProvider: '', implModel: '', implEffort: '', testProvider: tester.provider, testModel: tester.model, testEffort: tester.reasoningEffort,
      reviewerMode: 'lead', cmEnabledSessions: [] })
  }
  function journal(rootId) {
    const sidecar = new Sidecar({ sidecarUrl, workspace: join(directory, 'sidecar-state'), sessionId: rootId, sessionIsolation: true, autoStart: false, auditJournalRequired: true })
    return { sidecar, audit: new AuditJournal({ sidecarFactory: () => sidecar }) }
  }
  async function phaseOne() {
    settingsBefore = ctx.settings.get('dpswarm'); await configure(); lead = await createRoot()
    writeFileSync(join(directory, 'ready.json'), JSON.stringify({ session_id: ids.root, startup_options: lead.options }, null, 2))
    const receipt = await waitSelection()
    assert.deepEqual(receipt.selected, selected)
    assert.equal(lead.options.model, 'deepseek-v4-pro', 'Probe needs creation options to remain stale')
    await prompt(lead, 'Use the enabled fixed team for this local route probe. Return two no-op reports, do not edit files or run tests, then review both.')
    assert.equal(result.deliveries.length, 2)
    const implementer = result.deliveries.find(item => item.role === 'implementer')
    const test = result.deliveries.find(item => item.role === 'tester')
    assert.ok(implementer?.execution_session_id && test?.execution_session_id)
    // Completed fixed-role sessions are intentionally evicted from the live
    // host registry. Their only valid continuity check is the next-process
    // native agents.resume path, never an in-memory Session object.
    const { audit } = journal(ids.root), snapshot = await audit.read(ids.root)
    const bindings = snapshot.events.filter(event => event.type === 'dpswarm/route-bound')
    assert.equal(bindings.length, 2, JSON.stringify(bindings))
    const bindingFor = id => bindings.find(event => event.data.child_session_id === id)
    assertRoute(bindingFor(implementer.execution_session_id)?.data.route, selected, 'Durable implementer binding')
    assertRoute(bindingFor(test.execution_session_id)?.data.route, tester, 'Durable tester binding')
    await ctx.sessions.flush(lead.session)
    const checkpoint = { ready_for_hard_restart: true, phase: 1, case: caseName, pid: process.pid, ids, implementer_id: implementer.execution_session_id, tester_id: test.execution_session_id,
      selected, tester, audit: { revision: snapshot.revision, head_hash: snapshot.head_hash, bindings: bindings.map(event => ({ seq: event.seq, type: event.type, data: event.data })) },
      native_known: [nativeKnown(lead.session)], completed_child_sessions: [implementer.execution_session_id, test.execution_session_id], calls, checks: ['route-bound-fsync-before-first-role-request'], dsh_home: process.env.DSH_HOME, external_model_calls: 0 }
    writeFileSync(rootIdPath, JSON.stringify(checkpoint, null, 2))
    await new Promise(() => {})
  }
  async function phaseTwo() {
    assert.notEqual(process.pid, prior.pid); assert.equal(resolve(process.env.DSH_HOME), resolve(prior.dsh_home)); await configure()
    lead = await resume(ids.root)
    const { audit } = journal(ids.root)
    const before = await audit.read(ids.root)
    const testerBinding = before.events.find(event => event.type === 'dpswarm/route-bound' && event.data.child_session_id === prior.tester_id)
    assert.ok(testerBinding, 'Tester durable binding missing before injected duplicate test')
    const duplicate = await audit.append(ids.root, 'dpswarm/route-bound', testerBinding.data)
    assert.equal(duplicate.journal.revision, before.revision + 1)
    checks.push('test-only-cas-duplicate-binding-appended-after-cold-restart')
    const implementer = await resume(prior.implementer_id, lead)
    await prompt(implementer, 'After cold restart, produce the no-op report using only the durable fixed route.')
    assert.equal(calls.filter(call => call.session_id === prior.implementer_id).length, 1)
    assertRoute(routeFor(implementer.session), selected, 'Cold-restored implementer header')
    checks.push('cold-bound-implementer-restored-from-durable-route')
    const testerAgent = await resume(prior.tester_id, lead)
    await prompt(testerAgent, 'This must be rejected before any provider call because its durable route record is ambiguous.')
    assert.equal(calls.filter(call => call.session_id === prior.tester_id).length, 0, 'Ambiguous route must reject before provider')
    const invalidEnd = testerAgent.session.events.filter(event => event.type === 'turn/end').at(-1)
    assert.match(JSON.stringify(invalidEnd), /CHILD_ROUTE_BINDING_INVALID/)
    checks.push('cold-duplicate-binding-rejected-before-provider')
    const missingHandle = await ctx.agents.create({ sessionId: `route-cold-missing-${randomUUID()}`,
      meta: { cwd: process.cwd(), parentSession: lead.session.id, delegationDepth: 1, origin: 'subagent' }, agentOptions: staleOptions, setup: setupChild(lead) })
    handles.push(missingHandle)
    const missing = missingHandle.agent
    const legitimate = descriptorFor(implementer.session)
    assert.ok(legitimate?.label === 'dpswarm:DPswarm implementer')
    missing.session.append('subagent/descriptor', JSON.parse(JSON.stringify(legitimate)))
    await ctx.sessions.flush(missing.session)
    await prompt(missing, 'This synthetic native child is deliberately missing a durable route binding and must fail closed.')
    assert.equal(calls.filter(call => call.session_id === missing.session.id).length, 0, 'Missing route must reject before provider')
    const missingEnd = missing.session.events.filter(event => event.type === 'turn/end').at(-1)
    assert.match(JSON.stringify(missingEnd), /CHILD_ROUTE_BINDING_REQUIRED/)
    checks.push('cold-missing-binding-rejected-before-provider')
    const sessions = [lead.session, implementer.session, testerAgent.session, missing.session]
    for (const session of sessions) await ctx.sessions.flush(session)
    const snapshot = await audit.read(ids.root)
    return { passed: true, phase: 2, case: caseName, pid: process.pid, prior_pid: prior.pid, ids: { ...ids, implementer: prior.implementer_id, tester: prior.tester_id, missing: missing.session.id },
      external_model_calls: 0, execution: 'Actual native agents.resume in a new OS process with local LlmAdapter, durable sidecar audit, and actual session headers.', checks, calls,
      invalid_binding_turn_end: invalidEnd, missing_binding_turn_end: missingEnd, audit: { revision: snapshot.revision, head_hash: snapshot.head_hash }, native_known: sessions.map(nativeKnown), dsh_home: process.env.DSH_HOME }
  }
  async function run() {
    let report
    try { if (phase === 1) { await phaseOne(); return }; report = await phaseTwo() }
    catch (error) { report = { passed: false, phase, pid: process.pid, error: error.stack, calls, checks, external_model_calls: 0, native_failure_evidence: handles.map(handle => ({ session_id: handle.agent.session.id, endings: handle.agent.session.events.filter(event => event.type === 'turn/end').slice(-2) })) } }
    finally {
      if (report) { for (const handle of handles.reverse()) try { await handle.dispose() } catch (error) { report.cleanup_error = String(error); report.passed = false }; writeFileSync(output, JSON.stringify(report, null, 2)) }
      if (settingsBefore) try { await ctx.settings.replace('dpswarm', settingsBefore) } catch { /* host is isolated and will terminate */ }
    }
  }
  ctx.effect(() => { const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, phase, error: error.stack, calls, checks, external_model_calls: 0 }, null, 2))) }, 800); return () => clearTimeout(timer) })
}
