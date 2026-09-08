/** Real native DSH tool/Code Mode gate probe; exclusively local deterministic LLM. */
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve, relative } from 'node:path'
import { createHash, randomUUID } from 'node:crypto'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { AuditJournal } from '../lib/audit.js'
import { Sidecar } from '../lib/sidecar.js'
const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmBudget', 'dpswarmRequirement', 'settings', 'sessions', 'subagents']
const sha = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex')
const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
const textOf = result => (result?.content || []).filter(b => b.type === 'text').map(b => b.text).join('\n')
export function apply(ctx) {
  const directory = process.env.DPSWARM_REQUIRED_DIR
  const caseName = process.env.DPSWARM_REQUIRED_CASE
  const url = process.env.DPSWARM_REQUIRED_SIDECAR_URL
  const source = process.env.DPSWARM_REQUIRED_SOURCE
  const python = process.env.DPSWARM_REQUIRED_PYTHON
  assert.ok(directory && caseName && url && source && python)
  assert.ok(process.env.DSH_HOME && !relative(resolve(directory), resolve(process.env.DSH_HOME)).startsWith('..'), 'isolated DSH_HOME required')
  const cases = {
    manual: { preset: 'standard', mode: 'manual' }, auto: { preset: 'standard', mode: 'auto' },
    unlimited: { preset: 'standard', mode: 'unlimited' }, code: { preset: 'code', mode: 'manual' },
    text_only: { preset: 'standard', mode: 'manual', stubborn: true },
  }
  const selected = cases[caseName]; assert.ok(selected, 'known local fixture case')
  const provider = 'team-required-fixture', calls = [], checks = [], rootId = 'team-required-' + caseName + '-' + randomUUID()
  const limits = { implementer: { tokenLimit: 80000, callLimit: 40, reason: 'Lead read the task and selects a bounded implementation allowance.' },
    tester: { tokenLimit: 20000, callLimit: 10, reason: 'Lead read the acceptance criteria and assigns a smaller independent validation allowance.' } }
  const project = process.cwd(), blockedFile = join(project, 'must-not-exist.html'), finalFile = join(project, 'result.html'), inputFile = join(project, 'input.txt')
  const finalContent = '<!doctype html><title>Native gate fixture</title><p>Lead wrote after both roles completed.</p>\n'
  let parent, handle, settingsBefore, runResult, cancelled = false
  const nativeSessions = new Map()
  const output = join(directory, 'result.json')
  const childRows = () => calls.filter(c => c.child)
  function returned(options, id, { allowError = false } = {}) {
    const result = options.messages.flatMap(m => Array.isArray(m.content) ? m.content : []).find(b => b.type === 'tool-result' && b.toolCallId === id)
    assert.ok(result, 'Native result missing: ' + id)
    const text = textOf(result)
    if (!allowError) assert.ok(!result.isError && !/^Error:/.test(text), id + ': ' + text)
    let value; try { value = JSON.parse(text) } catch { value = text }
    return { raw: result, text, value }
  }
  function call(id, name, args) {
    if (selected.preset !== 'code') return tool(id, name, args)
    const code = `return await tools.${name}(${JSON.stringify(args)});`
    return tool(id, 'run_code', { code, description: `Native fixture invokes ${name} through the real Code Mode SDK.` })
  }
  function assertBudgetError(result) {
    assert.match(result.text, /WORKER_BUDGET_DECISION_REQUIRED|USER_WORKER_LIMITS_AUTHORITATIVE/)
    assert.equal(childRows().length, 0, 'Invalid or model-overridden budgets cannot start a child')
    checks.push('invalid-budget-tool-call-refused-before-child')
  }
  class Fixture extends LlmAdapter {
    async resolveModel(p, id) { return { provider: p, id, name: id, defaultMaxTokens: 1024, reasoning: { efforts: [{ id: 'max', name: 'Max' }] } } }
    async *stream(options) {
      assert.equal(options.provider, provider, 'No external provider allowed')
      assert.ok(!String(options.system || '').includes('estimating resources for one worker'), 'No hidden planner')
      const session = ctx.sessions.get(options.sessionId)
      assert.ok(session, 'Every provider call belongs to a native session')
      nativeSessions.set(session.id, session)
      const child = session.header.origin === 'subagent'
      const row = { session_id: session.id, child, provider: options.provider, model: options.model, maxTokens: options.maxTokens,
        purpose: options.purpose || 'main', system_sha256: sha(options.system || ''), messages_sha256: sha(options.messages),
        tool_names: (options.tools || []).map(t => t.name), at_utc: new Date().toISOString() }
      calls.push(row)
      let block
      if (child) {
        const budget = await ctx.dpswarmBudget.status({ session })
        row.role = budget.policy_binding?.label; row.budget_at_provider_entry = budget
        assert.ok(['implementer', 'tester'].includes(row.role))
        assert.equal(budget.mode, selected.mode)
        if (selected.mode === 'manual') { assert.equal(budget.tokenLimit, 600000); assert.equal(budget.callLimit, 28) }
        if (selected.mode === 'auto') {
          assert.equal(budget.tokenLimit, limits[row.role].tokenLimit); assert.equal(budget.callLimit, limits[row.role].callLimit)
          assert.equal(budget.decision.reason, limits[row.role].reason)
        }
        if (selected.mode === 'unlimited') {
          assert.equal(budget.remaining_tokens, null); assert.equal(budget.remaining_calls, null)
          assert.equal(options.maxTokens, 1024, 'Unlimited preserves the native output default despite retained 1/1 values')
        }
        const ownCalls = calls.filter(c => c.child && c.session_id === session.id).length
        if (selected.mode === 'unlimited' && ownCalls === 1) block = tool('unlimited-child-read-' + row.role, 'read', { file_path: inputFile })
        else {
          if (selected.mode === 'unlimited') { assert.equal(ownCalls, 2); assert.match(returned(options, 'unlimited-child-read-' + row.role).text, /READ_ONLY_SENTINEL/) }
          block = { type: 'text', text: `${row.role} completed the no-op fixture role. No files were changed and no tests were run by this role. Lead can perform the authorized final fixture write.` }
        }
      } else {
        assert.equal(session.id, rootId)
        const n = calls.filter(c => !c.child).length
        assert.ok(n <= 12, 'Native Lead failed to converge; fixture request safety bound reached')
        if (selected.stubborn) {
          assert.ok(n <= 8, 'Plain-text refusal must stop before repeated unbounded requests')
          block = { type: 'text', text: 'I will finish without calling any team tool.' }
        } else if (n === 1) {
          assert.ok(row.tool_names.includes(selected.preset === 'code' ? 'run_code' : 'write'))
          block = call('early-write', 'write', { file_path: blockedFile, content: 'This write must never occur.' })
        } else if (n === 2) {
          const early = returned(options, 'early-write', { allowError: true })
          assert.match(early.text, /DPSWARM.*REQUIRED|TEAM_REQUIRED|TEAM.*REQUIRED/i, 'Refusal must originate from the team gate, not sandbox/observation policy')
          assert.equal(existsSync(blockedFile), false)
          checks.push(selected.preset === 'code' ? 'real-run-code-nested-write-denied' : 'first-native-write-denied')
          block = call('read-input', 'read', { file_path: inputFile })
        } else if (n === 3) {
          const read = returned(options, 'read-input'); assert.match(read.text, /READ_ONLY_SENTINEL/)
          checks.push('native-read-permitted-before-team')
          const invalid = { task: 'No-op team fixture. Return reports only. Do not edit files or run tests.' }
          if (selected.mode !== 'auto') invalid.worker_budgets = limits
          block = call('invalid-budget', 'dpswarm_run', invalid)
        } else if (n === 4) {
          assertBudgetError(returned(options, 'invalid-budget', { allowError: true }))
          block = call('valid-team', 'dpswarm_run', { task: 'No-op team fixture. Return an implementation report and an independent validation report. Do not edit files or run tests.',
            acceptance: 'Exactly two native roles finish; Lead reviews both before writing the final HTML fixture.',
            ...(selected.mode === 'auto' ? { worker_budgets: limits } : {}) })
        } else if (n === 5) {
          runResult = returned(options, 'valid-team').value
          assert.equal(typeof runResult, 'object', 'Team tool must return lossless JSON')
          assert.equal(runResult.failed.length, 0, JSON.stringify(runResult.failed)); assert.equal(runResult.deliveries.length, 2)
          assert.equal(childRows().length, selected.mode === 'unlimited' ? 4 : 2); assert.equal(new Set(childRows().map(r => r.session_id)).size, 2)
          assert.deepEqual(runResult.deliveries.map(d => d.role), ['implementer', 'tester'])
          checks.push('real-team-tool-two-native-children', 'worker-budget-policy-observed-at-provider-entry')
          block = call('review-impl', 'dpswarm_review', { item_id: runResult.deliveries[0].item_id, verdict: 'accept', reason: 'Lead confirms the native fixture role completed.' })
        } else if (n === 6) {
          assert.equal(returned(options, 'review-impl').value.ok, true)
          block = call('review-tester', 'dpswarm_review', { item_id: runResult.deliveries[1].item_id, verdict: 'accept', reason: 'Lead confirms independent fixture validation completed.' })
        } else if (n === 7) {
          assert.equal(returned(options, 'review-tester').value.ok, true)
          block = call('final-write', 'write', { file_path: finalFile, content: finalContent })
        } else if (n === 8) {
          returned(options, 'final-write'); assert.equal(readFileSync(finalFile, 'utf8'), finalContent)
          assert.equal(existsSync(blockedFile), false)
          checks.push('native-lead-write-allowed-after-team', 'lead-reviewed-both-deliveries')
          block = { type: 'text', text: 'The fixed team completed and the Lead saved the authorized fixture HTML.' }
        } else throw new Error('Unexpected extra request after team completion')
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 10, outputTokens: 20 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([provider], new Fixture())
  async function exportEvidence(report) {
    const evidenceDir = join(directory, 'native'); mkdirSync(evidenceDir, { recursive: true })
    const ids = new Set([rootId, ...calls.map(c => c.session_id)]), exports = []
    for (const id of ids) {
      const session = ctx.sessions.get(id) || nativeSessions.get(id); assert.ok(session, 'Native evidence must retain disposed child ' + id)
      const data = { id, header: session.header, events: session.events }, text = JSON.stringify(data, null, 2)
      const path = join(evidenceDir, id + '.json'); writeFileSync(path, text); exports.push({ session_id: id, path, sha256: sha(text) })
    }
    assert.equal(exports.length, ids.size, 'Every observed native root/child must have an exported event log')
    report.native_exports = exports
    try {
      const audit = await new AuditJournal({ config: () => ctx.settings.get('dpswarm') }).read(rootId)
      const path = join(directory, 'plugin-audit.json'), text = JSON.stringify(audit, null, 2)
      writeFileSync(path, text); report.audit_export = { path, sha256: sha(text), revision: audit.revision, missing: audit.missing || false }
      report.requirement_events = audit.events.filter(e => e.type.startsWith('dpswarm/team-required-'))
    } catch (error) { report.audit_export_error = String(error) }
  }
  async function run() {
    let report
    settingsBefore = ctx.settings.get('dpswarm')
    try {
      const workspace = join(directory, 'sidecar-state')
      writeFileSync(inputFile, 'READ_ONLY_SENTINEL: native read is allowed before the fixed team.\n')
      await ctx.settings.update('dpswarm', { sidecarUrl: url, workspace, pythonCmd: python, dpswarmDir: source, autoStart: false,
        workerBudgetMode: selected.mode, workerTokenLimit: selected.mode === 'unlimited' ? 1 : 600000, workerCallLimit: selected.mode === 'unlimited' ? 1 : 28,
        workerBudgetSessionOverrides: [], workerTimeoutSeconds: 45, implMode: 'lead', implProvider: '', implModel: '', implEffort: '',
        testProvider: provider, testModel: 'glm-5.3-flash', testEffort: 'max', reviewerMode: 'lead', enabledSessions: [rootId], cmEnabledSessions: [] })
      handle = await ctx.agents.create({ sessionId: rootId, meta: { cwd: project, agentPreset: selected.preset },
        agentOptions: { provider, model: 'deepseek-v4-flash', reasoningEffort: 'max' },
        setup: async scope => { await ctx.agentPresets.mount(scope, selected.preset) } })
      parent = handle.agent
      parent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Use the enabled fixed team for this local native integration task, let its two roles return no-op reports without tests, then have Lead create result.html in this workspace.' }] }))
      let timeout
      try { await Promise.race([parent.whenIdle(), new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Native fixture did not become idle within 75 seconds')), 75000) })]) }
      finally { clearTimeout(timeout) }
      assert.ok(!cancelled)
      const events = parent.session.events, leadCalls = calls.filter(c => !c.child)
      if (selected.stubborn) {
        assert.ok(leadCalls.length >= 2 && leadCalls.length <= 8, 'Plain text finish should be steered then bounded')
        const steering = events.filter(e => e.type === 'user/message' && e.data?.source?.kind !== 'user' && /dpswarm|team/i.test(JSON.stringify(e.data)))
        assert.ok(steering.length > 0, 'Native transcript must preserve actual team steering')
        assert.equal(childRows().length, 0); assert.equal(existsSync(finalFile), false)
        checks.push('plain-text-finish-steered', 'stubborn-text-finish-has-bounded-requests')
        report = { passed: true, steering_events: steering, root_turn_ends: events.filter(e => e.type === 'turn/end') }
      } else {
        assert.equal(leadCalls.length, 8)
        const children = (await ctx.dpswarmBudget.status(parent)).children
        const perChildCalls = selected.mode === 'unlimited' ? 2 : 1
        assert.equal(children.length, 2); assert.ok(children.every(s => s.calls === perChildCalls && s.observed_tokens_lower_bound === 130 * perChildCalls))
        if (selected.mode === 'unlimited') checks.push('each-unlimited-child-exceeds-retained-one-call-allowance')
        const sidecar = new Sidecar({ sidecarUrl: url, workspace, sessionId: rootId, sessionIsolation: true, autoStart: false })
        const status = await sidecar.call('GET', '/api/status')
        assert.equal(status.snapshot.open_worker_slots_used, 0)
        assert.equal(Object.values(status.snapshot.work_items).filter(item => item.acceptance === 'accepted').length, 2)
        const leases = join(workspace, 'workspace-leases'); assert.ok(!existsSync(leases) || readdirSync(leases).length === 0)
        if (selected.preset === 'code') assert.ok(events.some(e => e.type === 'tool/code-dispatch' && /dpswarm_run/.test(JSON.stringify(e.data))), 'Real code-dispatch event proves nested team SDK execution')
        report = { passed: true, children, sidecar_status: status, run_result: runResult, final_artifact: { path: finalFile, sha256: sha(readFileSync(finalFile, 'utf8')) }, root_turn_ends: events.filter(e => e.type === 'turn/end') }
      }
    } catch (error) { report = { passed: false, error: error.stack } }
    finally {
      report = { ...report, case: caseName, preset: selected.preset, mode: selected.mode, root_session_id: rootId, checks, calls,
        external_model_calls: 0, execution: 'Installed native DSH AgentLoop/preset + real tools/Code Mode + worktree Python sidecar + deterministic local LlmAdapter', completed_at_utc: new Date().toISOString() }
      try { await exportEvidence(report) } catch (error) { report.export_error = String(error); report.passed = false }
      try { await handle?.dispose() } catch (error) { report.cleanup_error = String(error); report.passed = false }
      try { await ctx.settings.replace('dpswarm', settingsBefore) } catch (error) { report.settings_restore_error = String(error); report.passed = false }
      writeFileSync(output, JSON.stringify(report, null, 2))
    }
  }
  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, case: caseName, error: error.stack, calls, checks }, null, 2))) }, 500)
    return () => { cancelled = true; clearTimeout(timer) }
  })
}
