/** Native DSH Lead tools -> real Python sidecar -> native workers, local adapter only. */
import assert from 'node:assert/strict'
import { appendFileSync, existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { Sidecar } from '../lib/sidecar.js'
import { AuditJournal } from '../lib/audit.js'
const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmBudget', 'settings', 'sessions', 'subagents']

export function apply(ctx) {
  const output = process.env.DPSWARM_FIXED_PROBE_OUTPUT
  const directory = process.env.DPSWARM_FIXED_PROBE_DIR
  const python = process.env.DPSWARM_FIXED_PROBE_PYTHON
  const source = process.env.DPSWARM_FIXED_PROBE_SOURCE
  if (!output || !directory || !python || !source || !process.env.DSH_HOME
    || !resolve(process.env.DSH_HOME).startsWith(resolve(directory))) throw new Error('Isolated probe environment required')
  // Model keys use existing trusted sidecar catalog facts. Responses are still
  // produced exclusively by this local adapter, never those real models.
  const provider = 'worker-budget-fixed-fixture', calls = [], checks = []
  let parent, handle, sidecarProcess, sidecar, settingsBefore, runResult, cancelled = false
  const limits = { implementer: { tokenLimit: 80000, callLimit: 40, reason: 'Lead read the task and assigned one bounded implementation report.' },
    tester: { tokenLimit: 20000, callLimit: 10, reason: 'Lead read the acceptance scope; focused independent review requires fewer requests.' } }
  function returned(options, id) {
    const blocks = options.messages.flatMap(m => m.content || [])
    const result = blocks.find(b => b.type === 'tool-result' && b.toolCallId === id)
    assert.ok(result, `Missing real tool output ${id}`)
    assert.ok(!result.isError, JSON.stringify(result))
    return JSON.parse(result.content.filter(b => b.type === 'text').map(b => b.text).join(''))
  }
  const tool = (id, name, args = {}) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
  class Fixture extends LlmAdapter {
    async resolveModel(p, id) { return { provider: p, id, name: id, defaultMaxTokens: id === 'glm-5.3-flash' ? 32768 : 256000, reasoning: { efforts: [{ id: 'max', name: 'Max' }] } } }
    async *stream(options) {
      assert.equal(options.provider, provider)
      assert.ok(!String(options.system || '').includes('estimating resources for one worker'), 'No hidden planner')
      const session = ctx.sessions.get(options.sessionId)
      const child = session.header.origin === 'subagent'
      const row = { session_id: session.id, child, model: options.model, effort: options.reasoningEffort, maxTokens: options.maxTokens }
      calls.push(row)
      let block
      if (child) {
        assert.match(JSON.stringify(options.messages), /do not run tests or add tests/)
        const budget = await ctx.dpswarmBudget.status({ session })
        row.role = budget.policy_binding?.label
        assert.equal(budget.mode, 'auto'); assert.equal(budget.callLimit, limits[row.role].callLimit)
        assert.equal(budget.tokenLimit, limits[row.role].tokenLimit)
        assert.equal(budget.decision.reason, limits[row.role].reason)
        const latest = budget.recent.at(-1)
        row.native_output_default = options.model === 'glm-5.3-flash' ? 32768 : 256000
        row.input_estimate = latest.input_estimate
        row.reserved_tokens = latest.reserved_tokens
        row.token_limit = budget.tokenLimit
        assert.ok(options.maxTokens > 0 && options.maxTokens < row.native_output_default)
        assert.ok(latest.input_estimate > 10000, 'Probe must include the real large system/tools/messages envelope')
        assert.ok(latest.reserved_tokens <= budget.tokenLimit, 'Full reservation must stay within the unchanged worker allowance')
        if (row.role === 'implementer') {
          // Simulate a settings edit after the first worker starts. The tester
          // must retain its initial Auto policy despite the new manual values.
          await ctx.settings.update('dpswarm', { workerBudgetMode: 'manual', workerTokenLimit: 1, workerCallLimit: 1 })
          checks.push('isolated-settings-changed-mid-pipeline')
        }
        block = { type: 'text', text: row.role === 'implementer'
          ? 'Implementation fixture delivery: no production changes required by this no-op integration probe. No tests were run.'
          : 'Independent fixture validation: the assigned task is a no-op integration probe. No production edits or test commands were needed. Lead retains final acceptance.' }
      } else {
        const n = calls.filter(c => !c.child).length
        if (n === 1) {
          for (const name of ['dpswarm_models', 'dpswarm_run', 'dpswarm_review']) assert.ok(options.tools.some(t => t.name === name), `Native preset missing ${name}`)
          block = tool('fixture-models', 'dpswarm_models')
        } else if (n === 2) {
          const models = returned(options, 'fixture-models')
          assert.equal(models.worker_budget.configured.mode, 'auto')
          assert.equal(models.configured_profile.implementer.provider, provider)
          assert.equal(models.configured_profile.reviewer.mode, 'lead')
          checks.push('real-models-tool-lossless-json')
          block = tool('fixture-run', 'dpswarm_run', { task: 'No-op integration probe: return an implementation report and an independent validation report. Do not edit files or run tests.',
            acceptance: 'Exactly two native workers; honest no-op reports; Lead reviews both.', worker_budgets: limits })
        } else if (n === 3) {
          runResult = returned(options, 'fixture-run')
          assert.equal(runResult.failed.length, 0, JSON.stringify(runResult.failed))
          assert.equal(runResult.deliveries.length, 2)
          assert.equal(runResult.worker_budget_policy.mode, 'auto')
          assert.deepEqual(runResult.deliveries.map(d => d.role), ['implementer', 'tester'])
          checks.push('real-team-tool-lossless-json-two-deliveries')
          block = tool('fixture-review-0', 'dpswarm_review', { item_id: runResult.deliveries[0].item_id, verdict: 'accept', reason: 'Lead verified the no-op fixture report and native execution evidence.' })
        } else if (n === 4) {
          assert.equal(returned(options, 'fixture-review-0').ok, true)
          block = tool('fixture-review-1', 'dpswarm_review', { item_id: runResult.deliveries[1].item_id, verdict: 'accept', reason: 'Lead verified independent no-op validation and both worker identities.' })
        } else if (n === 5) {
          assert.equal(returned(options, 'fixture-review-1').ok, true)
          block = tool('fixture-status', 'dpswarm_status')
        } else if (n === 6) {
          const status = returned(options, 'fixture-status')
          assert.equal(status.snapshot.open_worker_slots_used, 0)
          assert.equal(status.worker_budget.configured.mode, 'manual')
          assert.equal(status.worker_budget.children.length, 2)
          assert.ok(status.worker_budget.children.every(s => s.mode === 'auto'))
          checks.push('native-lead-reviews-release-control-slots')
          block = { type: 'text', text: 'Fixed-team native integration completed with both deliveries accepted.' }
        } else throw new Error('Unexpected extra Lead request')
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 10, outputTokens: 20 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([provider], new Fixture())
  async function startSidecar(workspace) {
    sidecarProcess = spawn(python, ['-m', 'dpswarm.session_server', '--port', '0', '--workspace', workspace],
      { cwd: source, windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] })
    let transcript = ''
    sidecarProcess.stdout.on('data', data => { transcript += data.toString(); appendFileSync(join(directory, 'sidecar.stdout.log'), data) })
    sidecarProcess.stderr.on('data', data => appendFileSync(join(directory, 'sidecar.stderr.log'), data))
    const deadline = Date.now() + 20000
    while (Date.now() < deadline) {
      const match = /http:\/\/127\.0\.0\.1:(\d+)/.exec(transcript)
      if (match) return `http://127.0.0.1:${match[1]}`
      if (sidecarProcess.exitCode !== null) throw new Error(`Sidecar exited ${sidecarProcess.exitCode}: ${transcript}`)
      await new Promise(r => setTimeout(r, 100))
    }
    throw new Error('Own isolated sidecar startup timed out')
  }
  async function run() {
    const workspace = join(directory, 'sidecar-state')
    mkdirSync(workspace, { recursive: true })
    settingsBefore = ctx.settings.get('dpswarm')
    let report
    try {
      const url = await startSidecar(workspace)
      const id = `fixed-team-native-${randomUUID()}`
      await ctx.settings.update('dpswarm', { sidecarUrl: url, workspace, pythonCmd: python, dpswarmDir: source, autoStart: false,
        workerBudgetMode: 'auto', workerTokenLimit: 600000, workerCallLimit: 28, workerBudgetSessionOverrides: [],
        implMode: 'lead', implProvider: '', testProvider: provider, testModel: 'glm-5.3-flash', testEffort: 'max',
        reviewerMode: 'lead', enabledSessions: [id], cmEnabledSessions: [], workerTimeoutSeconds: 60 })
      handle = await ctx.agents.create({ sessionId: id, meta: { cwd: process.cwd(), agentPreset: 'standard' },
        agentOptions: { provider, model: 'deepseek-v4-flash', reasoningEffort: 'max' },
        setup: async scope => { await ctx.agentPresets.mount(scope, 'standard') } })
      parent = handle.agent
      parent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Read this no-op task, inspect fixed role settings, choose independent Auto worker budgets, execute the fixed team, and review both deliveries.' }] }))
      await Promise.race([parent.whenIdle(), new Promise((_, reject) => setTimeout(() => reject(new Error('Probe Lead did not finish within 60 seconds')), 60000))])
      assert.ok(!cancelled)
      const errors = parent.session.events.filter(e => e.type === 'tool/result' && e.data?.isError)
      assert.equal(errors.length, 0, JSON.stringify(errors))
      assert.equal(calls.filter(c => !c.child).length, 6)
      assert.equal(calls.filter(c => c.child).length, 2)
      assert.equal(new Set(calls.filter(c => c.child).map(c => c.session_id)).size, 2)
      const children = (await ctx.dpswarmBudget.status(parent)).children
      assert.deepEqual(children.map(s => s.policy_binding.label).sort(), ['implementer', 'tester'])
      assert.ok(children.every(s => s.calls === 1 && s.observed_tokens_lower_bound === 130))
      const audit = await new AuditJournal({ config: () => ctx.settings.get('dpswarm') }).read(parent.session.id)
      assert.equal(audit.events.filter(e => e.type === 'dpswarm/worker-budget-team-run-ended').length, 1)
      assert.ok(parent.session.events.every(e => !e.type.startsWith('dpswarm/')), 'No plugin records enter the native journal')
      const leases = join(workspace, 'workspace-leases')
      assert.ok(!existsSync(leases) || readdirSync(leases).length === 0, 'Project lease must be released after Lead reviews')
      sidecar = new Sidecar({ sidecarUrl: url, workspace, sessionId: id, sessionIsolation: true, autoStart: false })
      const status = await sidecar.call('GET', '/api/status')
      assert.equal(status.snapshot.open_worker_slots_used, 0)
      assert.equal(Object.values(status.snapshot.work_items).filter(i => i.acceptance === 'accepted').length, 2)
      checks.push('real-sidecar-two-accepted-items-and-no-project-lease', 'two-native-workers-distinct-auto-limits-frozen', 'native-output-defaults-256000-and-32768-fit-80000-and-20000-allowances', 'zero-external-llm-calls')
      report = { passed: true, external_model_calls: 0, execution: 'Installed native DSH standard preset + Lead tools + real Python session_server + native spawn + local LlmAdapter',
        checks, calls, children, sidecar_url: url, sidecar_status: status, run_result: runResult }
    } catch (error) {
      report = { passed: false, error: error.stack, calls, checks, parent_events: parent?.session.events || [] }
    } finally {
      try { await handle?.dispose() } catch (error) { report.cleanup_error = String(error); report.passed = false }
      try { if (settingsBefore) await ctx.settings.replace('dpswarm', settingsBefore) } catch (error) { report.settings_restore_error = String(error); report.passed = false }
      if (sidecarProcess && sidecarProcess.exitCode === null) sidecarProcess.kill()
      writeFileSync(output, JSON.stringify(report, null, 2))
    }
  }
  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(output, JSON.stringify({ passed: false, error: error.stack, calls, checks }, null, 2))) }, 1400)
    return () => { cancelled = true; clearTimeout(timer); if (sidecarProcess && sidecarProcess.exitCode === null) sidecarProcess.kill() }
  })
}
