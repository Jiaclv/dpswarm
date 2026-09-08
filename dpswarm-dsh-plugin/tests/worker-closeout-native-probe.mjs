/** Real DPH Lead/team/tools/sidecar; deterministic local model, no paid calls. */
import assert from 'node:assert/strict'
import { existsSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { AuditJournal } from '../lib/audit.js'
const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'settings', 'sessions', 'dpswarmBudget', 'dpswarmCM', 'tools']

export function apply(ctx) {
  const output = process.env.DPSWARM_CLOSEOUT_PROBE_OUTPUT, directory = process.env.DPSWARM_CLOSEOUT_PROBE_ATTEMPT
  if (!output || !directory || !resolve(process.env.DSH_HOME || '').startsWith(resolve(directory))) throw new Error('Isolated probe required')
  const provider = 'worker-closeout-fixture', calls = [], results = [], guardChecks = [], stepSignals = new WeakMap(), plans = new Map(), handles = []
  const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
  function returned(options, id) {
    const block = options.messages.flatMap(m => m.content || []).find(b => b.type === 'tool-result' && b.toolCallId === id)
    assert.ok(block && !block.isError, JSON.stringify(block))
    return JSON.parse(block.content.filter(b => b.type === 'text').map(b => b.text).join(''))
  }
  const call = (plan, id, name, args) => plan.preset === 'code'
    ? tool(id, 'run_code', { code: `return await tools.${name}(${JSON.stringify(args)});`, description: 'Isolated native Code Mode fixture.' })
    : tool(id, name, args)
  ctx.on('system-prompt/assemble', async (_assembly, context, next) => {
    if (context?.agent?.session) stepSignals.set(context.agent.session, context.signal)
    return next()
  }, { prepend: true, global: true })
  // Explicit fixture transition while a native Code Mode program is in flight:
  // its first write succeeds, then the next SDK tool encounters final-only.
  ctx.on('tools/pre-execute', async (exec, next) => {
    const session = exec.agent?.session, plan = plans.get(session?.header?.parentSession)
    if (plan?.name === 'code_transition' && exec.name === 'read' && exec.parent) {
      const state = await ctx.dpswarmBudget.ensure(exec.agent, stepSignals.get(session))
      const observedInput = [...state.calls.values()].at(-1).input_estimate
      // The in-flight wire assembly is already frozen. This fixture invokes the
      // same runtime state transition directly, without rewriting that request.
      await ctx.dpswarmBudget.runtime.prepareCloseout(state, { inputEstimate: observedInput, finalInputEstimate: observedInput }, stepSignals.get(session))
      assert.equal(state.closeout?.mode, 'final_only')
      guardChecks.push({ case: plan.name, kind: 'fixture_injected_closeout_before_nested_read', child: session.id })
    }
    return next()
  }, { prepend: true, global: true })
  class Fixture extends LlmAdapter {
    async resolveModel(p, id) { return { provider: p, id, name: id, defaultMaxTokens: 1024, context: { contextWindow: 1000000 } } }
    async *stream(options) {
      assert.equal(options.provider, provider)
      const session = ctx.sessions.get(options.sessionId), child = session.header.origin === 'subagent'
      const plan = plans.get(child ? session.header.parentSession : session.id)
      assert.ok(plan, 'Request belongs to a known isolated fixture')
      const n = calls.filter(c => c.session_id === session.id).length + 1
      const row = { case: plan.name, session_id: session.id, child, n, tools: (options.tools || []).map(t => t.name), purpose: options.purpose || 'main' }
      calls.push(row)
      let block
      if (child) {
        const budget = await ctx.dpswarmBudget.status({ session })
        const role = budget.policy_binding.label; row.role = role
        assert.equal(budget.tokenLimit, plan.tokens); assert.equal(budget.callLimit, plan.limit)
        assert.equal(plan.name === 'tiny_budget', false, 'Tiny budget must fail before contacting the adapter')
        if (role === 'implementer' && n === 1) {
          assert.ok((options.tools || []).some(t => t.name === (plan.preset === 'code' ? 'run_code' : 'write')), 'First implementation request retains its native tool transport')
          const args = { file_path: plan.file, content: '<!doctype html><title>fixture candidate</title><p>saved</p>' }
          block = plan.name === 'code_transition'
            ? tool('save-' + plan.name, 'run_code', { code: `await tools.write(${JSON.stringify(args)}); try { await tools.read(${JSON.stringify({ file_path: plan.file })}); return { unexpected: true }; } catch (error) { return { denied: String(error.message || error) }; }`, description: 'Save once then test the nested final-only boundary.' })
            : call(plan, 'save-' + plan.name, 'write', args)
        } else if (role === 'implementer' && plan.name === 'provider_failure') {
          throw Object.assign(new Error('SERVICE_UNAVAILABLE: Fixture service unavailable after a successful save'), { code: 'SERVICE_UNAVAILABLE' })
        } else {
          if (role === 'implementer' && ['last_call', 'code_transition'].includes(plan.name)) {
            assert.deepEqual(options.tools || [], [], 'Last allowed request must expose no tools')
            assert.equal(budget.closeout?.mode, 'final_only')
            assert.ok(JSON.stringify(options).toLowerCase().includes('closeout'))
            if (plan.name === 'code_transition') {
              const previous = returned(options, 'save-' + plan.name)
              assert.match(previous.denied, /WORKER_CLOSEOUT_FINAL_ONLY/)
              assert.equal(existsSync(plan.file), true)
              const agent = ctx.agents.get(session.id)
              const blocked = await ctx.tools.execute({ callId: 'blocked-outer-' + plan.name,
                name: 'run_code', arguments: { code: `return await tools.write(${JSON.stringify({ file_path: plan.file + '.blocked', content: 'must never write' })});`, description: 'Must be refused before Code Mode execution.' },
                agent, signal: options.signal })
              assert.equal(blocked.isError, true)
              assert.match(JSON.stringify(blocked), /WORKER_CLOSEOUT_FINAL_ONLY/)
              assert.equal(existsSync(plan.file + '.blocked'), false)
              guardChecks.push({ case: plan.name, kind: 'native_outer_run_code_and_nested_sdk_denied', child: session.id, nested_denial: previous.denied, outer_error: blocked.error || null })
            }
          }
          block = { type: 'text', text: role === 'implementer' ? `Saved candidate: ${plan.file}. No tests run; Lead must verify.` : 'Read-only fixture report. Candidate is not an official score. No tests run.' }
        }
      } else {
        assert.ok((options.tools || []).some(t => t.name === (plan.preset === 'code' ? 'run_code' : 'dpswarm_run')), 'Lead remains unrestricted')
        if (n === 1) block = call(plan, 'run', 'dpswarm_run', { task: `Create the single fixture HTML at ${plan.file}. No tests, no other edits. Return a concise handoff after saving.`, acceptance: 'Saved fixture file and honest role reports; Lead retains final acceptance.' })
        else if (n === 2) {
          plan.run = returned(options, 'run')
          if (['last_call', 'code_transition'].includes(plan.name)) {
            assert.equal(plan.run.failed.length, 0, JSON.stringify(plan.run.failed))
            assert.equal(plan.run.deliveries.length, 2)
          } else if (plan.name === 'provider_failure') {
            assert.equal(plan.run.failed.length, 1, JSON.stringify(plan.run))
            assert.equal(plan.run.failed[0].role, 'implementer')
            assert.ok(JSON.stringify(plan.run.failed[0]).includes('SERVICE_UNAVAILABLE'))
            assert.ok(JSON.stringify(plan.run.failed[0]).includes(JSON.stringify(plan.file).slice(1, -1)), 'Successful save remains a partial candidate')
          } else {
            assert.equal(plan.run.failed.length, 2, JSON.stringify(plan.run))
            assert.ok(plan.run.failed.every(f => f.code === 'WORKER_TOKEN_RESERVATION_DENIED'), JSON.stringify(plan.run.failed))
          }
          plan.reviewed = 0
          const delivery = plan.run.deliveries[0]
          block = delivery ? call(plan, 'review-0', 'dpswarm_review', { item_id: delivery.item_id, verdict: 'accept', reason: 'Fixture Lead verified terminal report and candidate provenance; no task-quality claim.' }) : { type: 'text', text: 'Tiny budget safely refused; no model dispatch or saved candidate.' }
        } else {
          const prior = plan.reviewed++; assert.equal(returned(options, 'review-' + prior).ok, true)
          const delivery = plan.run.deliveries[plan.reviewed]
          block = delivery ? call(plan, 'review-' + plan.reviewed, 'dpswarm_review', { item_id: delivery.item_id, verdict: 'accept', reason: 'Fixture Lead reviewed the report without treating a candidate as an official score.' }) : { type: 'text', text: 'Fixed-team run settled with explicit worker outcomes.' }
        }
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: 100, cacheReadTokens: 10, outputTokens: 100 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([provider], new Fixture())
  async function run() {
    let report, previous = ctx.settings.get('dpswarm')
    try {
      for (const [name, tokens, limit] of [['last_call', 1000000, 2], ['code_transition', 1000000, 2], ['provider_failure', 1000000, 6], ['tiny_budget', 1, 2]]) {
        const id = 'closeout-native-' + randomUUID()
        const plan = { name, tokens, limit, preset: name === 'code_transition' ? 'code' : 'standard', file: join(process.cwd(), name + '.html') }; plans.set(id, plan)
        await ctx.settings.update('dpswarm', { autoStart: false, sidecarUrl: process.env.DPSWARM_CLOSEOUT_PROBE_SIDECAR_URL,
          workspace: process.env.DPSWARM_CLOSEOUT_PROBE_STATE, dpswarmDir: process.env.DPSWARM_CLOSEOUT_PROBE_SOURCE,
          pythonCmd: process.env.DPSWARM_CLOSEOUT_PROBE_PYTHON, workerBudgetMode: 'manual', workerTokenLimit: tokens, workerCallLimit: limit,
          workerBudgetSessionOverrides: [], enabledSessions: [id], cmEnabledSessions: [id], cmProvider: provider, cmModel: 'fixture-cm', cmEffort: '',
          implMode: 'lead', implProvider: '', testProvider: provider, testModel: 'fixture-tester', testEffort: '', reviewerMode: 'lead', workerTimeoutSeconds: 60 })
        const h = await ctx.agents.create({ sessionId: id, meta: { cwd: process.cwd(), agentPreset: plan.preset },
          agentOptions: { provider, model: 'fixture-lead' }, setup: async scope => { await ctx.agentPresets.mount(scope, plan.preset) } })
        handles.push(h)
        h.agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: 'Perform the isolated fixed-team fixture task; no tests, no unrelated files.' }] }))
        await Promise.race([h.agent.whenIdle(), new Promise((_, reject) => setTimeout(() => reject(new Error('Native fixture timeout')), 60000))])
        const end = h.agent.session.events.filter(e => e.type === 'turn/end').at(-1)
        assert.equal(end?.data?.reason?.kind, 'completed', JSON.stringify(end))
        assert.ok(plan.run, 'Native Lead received the real fixed-team result')
        assert.equal(existsSync(plan.file), name !== 'tiny_budget')
        const audit = await new AuditJournal({ config: () => ctx.settings.get('dpswarm') }).read(id)
        assert.equal(audit.events.filter(e => e.type === 'dpswarm/cm-start').length, 0)
        const children = (await ctx.dpswarmBudget.status(h.agent)).children
        assert.equal(children.length, 2)
        assert.ok(children.every(c => c.tokenLimit === tokens && c.callLimit === limit))
        const cm_pressure_checks = children.map(child => ctx.dpswarmCM.pressureChecks.get(child.worker_session_id) || null)
        if (['last_call', 'code_transition'].includes(name)) {
          const impl = children.find(c => c.policy_binding.label === 'implementer')
          assert.equal(impl.calls, 2)
          assert.equal(ctx.dpswarmCM.pressureChecks.get(impl.worker_session_id)?.reason, 'worker_closeout')
        }
        const response = await fetch(process.env.DPSWARM_CLOSEOUT_PROBE_SIDECAR_URL + '/api/status', { headers: { 'X-DPSwarm-Session': id } })
        const status = await response.json()
        assert.equal(status.worker_diagnostics.available, true)
        assert.equal(status.worker_diagnostics.workers.length, 2)
        assert.ok(!JSON.stringify(status.worker_diagnostics).includes(JSON.stringify(plan.file).slice(1, -1)), 'Public diagnostics never reveal paths')
        assert.ok(audit.events.some(e => e.type === 'dpswarm/worker-diagnostic'))
        results.push({ case: name, run: plan.run, children, cm_pressure_checks, public_status: status.worker_diagnostics })
        await h.dispose(); handles.pop()
      }
      report = { passed: true, external_model_calls: 0, execution: 'native DPH standard preset, native write, real fixed team and Python sidecar; local deterministic LlmAdapter; code_transition explicitly injects closeout through the budget runtime transition during a native Code Mode program (its already-sent assembly is left unchanged)', calls, results, guardChecks }
    } catch (error) { report = { passed: false, error: error.stack, calls, results, guardChecks, plans: [...plans].map(([id,p]) => ({ id, ...p })) } }
    finally {
      for (const h of handles.reverse()) try { await h.dispose() } catch (e) { report.cleanup_error = String(e); report.passed = false }
      await ctx.settings.replace('dpswarm', previous)
      writeFileSync(output, JSON.stringify(report, null, 2))
    }
  }
  ctx.effect(() => { const timer = setTimeout(() => { void run().catch(e => writeFileSync(output, JSON.stringify({ passed: false, error: e.stack, calls, results }))) }, 1400); return () => clearTimeout(timer) })
}
