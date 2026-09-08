/**
 * Native DSH proof for the fixed-team implementer rework path.
 *
 * The adapter is local and deterministic.  This validates host plumbing,
 * session separation, binding, budget attribution and actual native write
 * tool dispatch.  It deliberately does not claim a real model follows role
 * prompts in production.
 */
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { join, relative, resolve } from 'node:path'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { AuditJournal } from '../lib/audit.js'
import { Sidecar } from '../lib/sidecar.js'

const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))

export const inject = ['agents', 'agentPresets', 'llm', 'settings', 'sessions', 'tools', 'dpswarmBudget', 'dpswarmCM', 'dpswarmRequirement']

const digest = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex')
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
const text = blocks => (blocks || []).filter(block => block.type === 'text').map(block => block.text).join('')

export function apply(ctx) {
  const directory = process.env.DPSWARM_REWORK_PROBE_ATTEMPT
  const output = process.env.DPSWARM_REWORK_PROBE_OUTPUT
  const source = process.env.DPSWARM_REWORK_PROBE_SOURCE
  const stateDir = process.env.DPSWARM_REWORK_PROBE_STATE
  const python = process.env.DPSWARM_REWORK_PROBE_PYTHON
  const sidecarUrl = process.env.DPSWARM_REWORK_PROBE_SIDECAR_URL
  const caseName = process.env.DPSWARM_REWORK_PROBE_CASE
  if (!directory || !output || !source || !stateDir || !python || !sidecarUrl || !caseName) throw new Error('Isolated rework probe environment required')
  if (!process.env.DSH_HOME || relative(resolve(directory), resolve(process.env.DSH_HOME)).startsWith('..')) throw new Error('Probe needs its own DSH_HOME')

  const cases = {
    manual_standard: { preset: 'standard', mode: 'manual', tokenLimit: 600000, callLimit: 28 },
    manual_code: { preset: 'code', mode: 'manual', tokenLimit: 600000, callLimit: 28 },
    auto_standard: { preset: 'standard', mode: 'auto', tokenLimit: 1, callLimit: 1 },
    unlimited_standard: { preset: 'standard', mode: 'unlimited', tokenLimit: 1, callLimit: 1 },
  }
  const plan = cases[caseName]
  if (!plan) throw new Error('Unknown probe case: ' + caseName)

  const provider = 'worker-rework-fixture'
  const initialAuto = {
    implementer: { tokenLimit: 70000, callLimit: 4, reason: 'Local fixture implementation allowance.' },
    tester: { tokenLimit: 22000, callLimit: 2, reason: 'Local fixture independent review allowance.' },
  }
  const calls = [], nativeSessions = new Map(), checks = []
  const rootId = 'native-rework-' + randomUUID()
  const project = join(directory, 'project')
  const artifact = join(project, 'candidate.html')
  const initialText = '<!doctype html><title>initial fixture</title><p>initial candidate</p>\n'
  const reworkedText = '<!doctype html><title>reworked fixture</title><p>necessary correction applied</p>\n'
  let settingsBefore, handle, rootRun, reworkRun, cancelled = false

  const callTool = (id, name, args) => plan.preset === 'code'
    ? tool(id, 'run_code', { code: `return await tools.${name}(${JSON.stringify(args)});`, description: 'Isolated native rework fixture.' })
    : tool(id, name, args)
  const toolResult = (options, id) => {
    const block = options.messages.flatMap(message => message.content || []).find(value => value.type === 'tool-result' && value.toolCallId === id)
    assert.ok(block, 'Missing native result ' + id)
    assert.equal(block.isError, false, id + ': ' + text(block.content))
    return JSON.parse(text(block.content))
  }
  const toolSucceeded = (options, id) => {
    const block = options.messages.flatMap(message => message.content || []).find(value => value.type === 'tool-result' && value.toolCallId === id)
    assert.ok(block, 'Missing native result ' + id)
    assert.equal(block.isError, false, id + ': ' + text(block.content))
    return text(block.content)
  }
  const route = session => {
    const config = session.requestHeader?.()?.config
    assert.ok(config, 'Native request header is required')
    return { provider: config.provider, model: config.model, ...(Object.hasOwn(config, 'reasoningEffort') ? { reasoningEffort: config.reasoningEffort } : {}) }
  }
  const assertOriginalBudget = (budget, role) => {
    assert.equal(budget.mode, plan.mode, 'initial child mode')
    if (plan.mode === 'manual') {
      assert.equal(budget.tokenLimit, plan.tokenLimit); assert.equal(budget.callLimit, plan.callLimit)
    } else if (plan.mode === 'auto') {
      assert.equal(budget.tokenLimit, initialAuto[role].tokenLimit); assert.equal(budget.callLimit, initialAuto[role].callLimit)
      assert.equal(budget.decision.reason, initialAuto[role].reason)
    } else {
      assert.equal(budget.remaining_tokens, null); assert.equal(budget.remaining_calls, null)
    }
  }
  const assertReworkBudget = budget => {
    assert.equal(budget.mode, 'unlimited', 'rework child must not reuse/cap the original allowance')
    assert.equal(budget.remaining_tokens, null); assert.equal(budget.remaining_calls, null)
    assert.equal(Object.hasOwn(budget, 'tokenLimit'), false, 'unlimited rework has no token cap')
    assert.equal(Object.hasOwn(budget, 'callLimit'), false, 'unlimited rework has no call cap')
  }

  class Fixture extends LlmAdapter {
    async resolveModel(p, id) {
      return { provider: p, id, name: id, defaultMaxTokens: 1024, context: { contextWindow: 1000000 }, reasoning: { efforts: [{ id: 'max', name: 'Max' }] } }
    }
    async *stream(options) {
      assert.equal(options.provider, provider, 'No external model adapter may run')
      const session = ctx.sessions.get(options.sessionId)
      assert.ok(session, 'Provider request must belong to a native session')
      nativeSessions.set(session.id, session)
      const child = session.header.origin === 'subagent'
      const system = options.system || ''
      const row = { session_id: session.id, child, request_number: calls.filter(value => value.session_id === session.id).length + 1,
        route: route(session), adapter_route: { provider: options.provider, model: options.model, ...(options.reasoningEffort ? { reasoningEffort: options.reasoningEffort } : {}) },
        system_sha256: digest(system), system_has_lead_guide: system.includes('## DPSwarm Lead:'),
        system_has_worker_guide: system.includes('## DPSwarm worker:'), message_sha256: digest(options.messages),
        tool_names: (options.tools || []).map(value => value.name) }
      calls.push(row)
      let block
      if (child) {
        const budget = await ctx.dpswarmBudget.status({ session })
        row.role = budget.policy_binding?.label
        row.budget_at_provider_entry = budget
        assert.ok(['implementer', 'tester'].includes(row.role), 'expected fixed role')
        assert.equal(row.system_has_lead_guide, false, 'worker first request must not inherit Lead system guide')
        assert.equal(row.system_has_worker_guide, true, 'worker receives generic worker system guidance')
        const assigned = JSON.stringify(options.messages)
        if (row.role === 'implementer') assert.match(assigned, /You own production implementation/)
        else assert.match(assigned, /You own independent checking/)
        const isRework = assigned.includes('linked rework of the original implementer')
        const ownCalls = calls.filter(value => value.session_id === session.id).length
        row.is_rework = isRework
        if (isRework) {
          assert.equal(row.role, 'implementer')
          assertReworkBudget(budget)
          assert.match(assigned, /necessary correction applied|Replace the initial fixture/, 'concrete feedback reaches rework worker')
          assert.equal(existsSync(artifact), true, 'rework begins from existing initial candidate')
          if (ownCalls === 1) block = callTool('rework-read', 'read', { file_path: artifact })
          else if (ownCalls === 2) {
            toolSucceeded(options, 'rework-read')
            block = callTool('rework-save', 'write', { file_path: artifact, content: reworkedText })
          } else {
            assert.equal(ownCalls, 3, 'rework fixture needs read, save, then one delivery')
            toolSucceeded(options, 'rework-save')
            assert.equal(readFileSync(artifact, 'utf8'), reworkedText)
            block = { type: 'text', text: 'Rework implementer fixture delivery: saved the requested correction; no tests were run.' }
          }
        } else {
          assertOriginalBudget(budget, row.role)
          if (row.role === 'implementer' && ownCalls === 1) block = callTool('initial-save', 'write', { file_path: artifact, content: initialText })
          else if (row.role === 'implementer') {
            assert.equal(ownCalls, 2, 'initial fixture needs one save then one delivery')
            toolSucceeded(options, 'initial-save')
            assert.equal(readFileSync(artifact, 'utf8'), initialText)
            block = { type: 'text', text: 'Implementer fixture delivery: saved the initial candidate; no tests were run.' }
          } else block = { type: 'text', text: 'Tester fixture report: inspected task constraints only; no tests or production edits were run.' }
        }
      } else {
        assert.equal(session.id, rootId)
        assert.equal(row.system_has_lead_guide, true, 'root sees Lead guide')
        assert.equal(row.system_has_worker_guide, false, 'root must not be classified as a worker')
        const n = calls.filter(value => !value.child).length
        assert.ok(n <= 7, 'fixture Lead must converge')
        if (n === 1) {
          block = callTool('team-run', 'dpswarm_run', { task: `Create ${artifact} as one HTML fixture. No tests. Let the implementer save the candidate and tester report independently.`,
            acceptance: 'Lead will request one concrete implementer correction before accepting the final delivery.',
            ...(plan.mode === 'auto' ? { worker_budgets: initialAuto } : {}) })
        } else if (n === 2) {
          rootRun = toolResult(options, 'team-run')
          assert.equal(rootRun.failed.length, 0, JSON.stringify(rootRun.failed)); assert.equal(rootRun.deliveries.length, 2)
          assert.equal(existsSync(artifact), true); assert.equal(readFileSync(artifact, 'utf8'), initialText)
          const impl = rootRun.deliveries.find(item => item.role === 'implementer')
          const tester = rootRun.deliveries.find(item => item.role === 'tester')
          assert.ok(impl && tester)
          // Keep the implementer submitted: rework itself must supersede it.
          block = callTool('review-tester', 'dpswarm_review', { item_id: tester.item_id, verdict: 'accept', reason: 'Fixture Lead accepts the no-tests tester report.' })
        } else if (n === 3) {
          assert.equal(toolResult(options, 'review-tester').ok, true)
          const impl = rootRun.deliveries.find(item => item.role === 'implementer')
          block = callTool('rework', 'dpswarm_rework', { item_id: impl.item_id,
            feedback: 'Replace the initial fixture content with the necessary correction applied. Preserve no-tests scope; do not add optional work.' })
        } else if (n === 4) {
          reworkRun = toolResult(options, 'rework')
          assert.equal(reworkRun.mode, 'fixed-implementer-rework-v1')
          assert.equal(reworkRun.failed.length, 0, JSON.stringify(reworkRun.failed)); assert.equal(reworkRun.deliveries.length, 1)
          assert.equal(reworkRun.worker_budget_policy.mode, 'unlimited')
          assert.equal(existsSync(artifact), true); assert.equal(readFileSync(artifact, 'utf8'), reworkedText)
          block = callTool('review-rework', 'dpswarm_review', { item_id: reworkRun.deliveries[0].item_id, verdict: 'accept', reason: 'Fixture Lead accepts the linked rework delivery.' })
        } else if (n === 5) {
          assert.equal(toolResult(options, 'review-rework').ok, true)
          block = { type: 'text', text: 'Initial worker and separate unlimited linked rework completed; Lead made no production edit.' }
        } else throw new Error('Unexpected extra Lead request')
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 120, cacheReadTokens: 20, outputTokens: 40 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([provider], new Fixture())

  async function exportEvidence(report) {
    const evidenceDir = join(directory, 'native'); mkdirSync(evidenceDir, { recursive: true })
    const ids = new Set([rootId, ...calls.map(value => value.session_id)])
    report.native_exports = []
    for (const id of ids) {
      const session = ctx.sessions.get(id) || nativeSessions.get(id)
      assert.ok(session, 'Observed session must remain exportable: ' + id)
      const payload = JSON.stringify({ id, header: session.header, events: session.events }, null, 2)
      const path = join(evidenceDir, id + '.json'); writeFileSync(path, payload)
      report.native_exports.push({ session_id: id, path, sha256: digest(payload) })
    }
    const audit = await new AuditJournal({ config: () => ctx.settings.get('dpswarm') }).read(rootId)
    const auditPath = join(directory, 'plugin-audit.json'); const auditText = JSON.stringify(audit, null, 2)
    writeFileSync(auditPath, auditText)
    report.audit_export = { path: auditPath, sha256: digest(auditText), revision: audit.revision, missing: audit.missing || false }
    report.rework_events = audit.events.filter(event => event.type === 'dpswarm/worker-rework')
  }

  async function run() {
    let report
    try {
      settingsBefore = ctx.settings.get('dpswarm')
      await ctx.settings.update('dpswarm', { sidecarUrl, workspace: stateDir, pythonCmd: python, dpswarmDir: source, autoStart: false,
        workerBudgetMode: plan.mode, workerTokenLimit: plan.tokenLimit, workerCallLimit: plan.callLimit, workerBudgetSessionOverrides: [], workerTimeoutSeconds: 60,
        implMode: 'lead', implProvider: '', implModel: '', implEffort: '', testProvider: provider, testModel: 'fixture-tester', testEffort: 'max', reviewerMode: 'lead',
        enabledSessions: [rootId], cmEnabledSessions: [] })
      handle = await ctx.agents.create({ sessionId: rootId, meta: { cwd: project, agentPreset: plan.preset },
        agentOptions: { provider, model: 'fixture-lead', reasoningEffort: 'max' }, setup: async scope => { await ctx.agentPresets.mount(scope, plan.preset) } })
      handle.agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Use the configured fixed team. Preserve the no-tests constraint. A concrete correction will be requested after the first implementer delivery.' }] }))
      await Promise.race([handle.agent.whenIdle(), new Promise((_, reject) => setTimeout(() => reject(new Error('Native rework fixture did not become idle')), 90000))])
      assert.equal(cancelled, false)
      assert.ok(rootRun && reworkRun, 'both fixed-team and rework responses were received')
      const children = (await ctx.dpswarmBudget.status(handle.agent)).children
      assert.equal(children.length, 3, 'initial implementer/tester plus new rework implementer')
      const initialImplementer = children.find(value => value.policy_binding?.label === 'implementer' && value.policy_binding?.authority === 'fixed-team-run')
      const reworkImplementer = children.find(value => value.policy_binding?.label === 'implementer' && value.policy_binding?.authority === 'fixed-team-rework')
      assert.ok(initialImplementer && reworkImplementer)
      assert.notEqual(initialImplementer.worker_session_id, reworkImplementer.worker_session_id, 'rework is a new native child attempt')
      const initialRoute = calls.find(value => value.child && !value.is_rework && value.role === 'implementer')?.route
      const reworkRoute = calls.find(value => value.child && value.is_rework && value.role === 'implementer')?.route
      assert.deepEqual(reworkRoute, initialRoute, 'rework preserves the exact implementer native route')
      assert.equal(readFileSync(artifact, 'utf8'), reworkedText)
      const rootToolCalls = handle.agent.session.events.filter(event => event.type === 'tool/call').map(event => event.data?.name)
      assert.ok(!rootToolCalls.includes('write'), 'Lead performs no direct production write')
      assert.ok(!rootToolCalls.includes('edit'), 'Lead performs no direct production edit')
      const reworkCalls = calls.filter(value => value.child && value.is_rework)
      assert.equal(reworkCalls.length, 3, 'fixture performs read, write and final handoff in the rework child')
      assert.ok(reworkCalls.every(value => value.budget_at_provider_entry?.mode === 'unlimited'))
      assert.ok(calls.some(value => value.child && !value.is_rework && value.role === 'implementer'))
      checks.push('first-request-root-worker-prompts-isolated', 'initial-and-rework-native-children-distinct', 'rework-unlimited-independent-profile', 'exact-implementer-route-preserved', 'native-rework-write-without-lead-edit')
      report = { passed: true, external_model_calls: 0, case: caseName, initial_worker_mode: plan.mode, root_run: rootRun, rework_run: reworkRun, children, calls,
        execution: 'Isolated native DSH AgentLoop, real fixed-team/rework tools and Python sidecar, local deterministic LlmAdapter only. Prompt observations verify injected context, not real-model instruction compliance.' }
    } catch (error) {
      report = { passed: false, external_model_calls: 0, case: caseName, error: error.stack, calls, root_events: handle?.agent?.session?.events || [] }
    } finally {
      report = { ...report, checks, root_session_id: rootId, completed_at_utc: new Date().toISOString() }
      try { await exportEvidence(report) } catch (error) { report.export_error = String(error); report.passed = false }
      try { await handle?.dispose() } catch (error) { report.cleanup_error = String(error); report.passed = false }
      try { if (settingsBefore) await ctx.settings.replace('dpswarm', settingsBefore) } catch (error) { report.settings_restore_error = String(error); report.passed = false }
      writeFileSync(output, JSON.stringify(report, null, 2))
    }
  }

  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, error: error.stack, calls }, null, 2))) }, 500)
    return () => { cancelled = true; clearTimeout(timer) }
  })
}
