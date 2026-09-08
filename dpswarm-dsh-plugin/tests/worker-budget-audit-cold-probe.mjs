/** Real two-process DSH + durable sidecar restart probe; local adapter only. */
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'
import { Sidecar } from '../lib/sidecar.js'
const host = resolveHostRoot()
const { LlmAdapter, createUserMessage } = await import(hostModuleUrl(host, 'dsh-llm/lib/index.js'))
const { KNOWN_SESSION_EVENT_TYPES } = await import(hostModuleUrl(host, 'dsh-session/lib/index.js'))
export const inject = ['agents', 'agentPresets', 'llm', 'dpswarmBudget', 'dpswarmCM', 'settings', 'sessions', 'subagents', 'tokenMeter']
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))
async function deadline(promise, ms, label) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timeout after ${ms} ms`)), ms) })]) }
  finally { clearTimeout(timer) }
}

export function apply(ctx) {
  const directory = process.env.DPSWARM_COLD_DIR, phase = Number(process.env.DPSWARM_COLD_PHASE)
  const sidecarUrl = process.env.DPSWARM_COLD_SIDECAR_URL
  if (!directory || ![1, 2].includes(phase) || !sidecarUrl || !process.env.DSH_HOME
      || !resolve(process.env.DSH_HOME).startsWith(resolve(directory))) throw new Error('Unique isolated DSH home required')
  const provider = 'worker-budget-audit-cold-fixture'
  const calls = [], checks = [], handles = [], roots = [], grants = {}
  const checkpointPath = join(directory, 'phase1-checkpoint.json')
  const resultPath = join(directory, `phase${phase}-result.json`)
  const prior = phase === 2 ? JSON.parse(readFileSync(checkpointPath, 'utf8')) : null
  const ids = prior?.ids || { lead: `cold-lead-${randomUUID()}`, hung: `cold-hung-${randomUUID()}`, cm: `cold-cm-${randomUUID()}` }
  const limits = { hung: 80000, issueOnly: 80000 }
  const tasks = { hung: 'Read this bounded child task and produce a short no-op report. The fixture pauses during its first model request.',
    issueOnly: 'After the later host restart, describe the fixed number 42 without editing files or running tests.' }
  let lead, cmRoot, hung, hungEnteredResolve, cancelled = false
  const hungEntered = new Promise(resolve => { hungEnteredResolve = resolve })
  let cmEvidence = null
  const tool = (id, name, args) => ({ type: 'tool-call', id, name, arguments: JSON.stringify(args) })
  function returned(options, id) {
    const result = options.messages.flatMap(m => m.content || []).find(b => b.type === 'tool-result' && b.toolCallId === id)
    assert.ok(result, `Missing actual Lead tool result ${id}`)
    assert.ok(!result.isError, JSON.stringify(result))
    return JSON.parse(result.content.filter(x => x.type === 'text').map(x => x.text).join(''))
  }
  function nativeKnown(agent) {
    const unknown = [...new Set(agent.session.events.map(e => e.type).filter(t => !KNOWN_SESSION_EVENT_TYPES.has(t)))]
    assert.deepEqual(unknown, [], `Unknown native event vocabulary in ${agent.session.id}`)
    assert.ok(!agent.session.events.some(e => e.type.startsWith('dpswarm/')), 'Plugin audit must not enter native session journal')
    return { session_id: agent.session.id, event_count: agent.session.events.length, unknown, plugin_native_events: 0 }
  }
  class Fixture extends LlmAdapter {
    async resolveModel(p, id) { return { provider: p, id, name: id, defaultMaxTokens: 256000,
      reasoning: { efforts: [{ id: 'max', name: 'Max' }] } } }
    async *stream(options) {
      assert.equal(options.provider, provider, 'No external model provider is permitted')
      assert.ok(!String(options.system || '').includes('estimating resources for one worker'), 'No hidden Auto planner')
      const row = { phase, session_id: options.sessionId, purpose: options.purpose || 'main', maxTokens: options.maxTokens }
      calls.push(row)
      let block
      if (options.purpose === 'compaction') {
        block = { type: 'text', text: 'Goal: preserve the local no-op fixture. Confirmed decisions and invariants: number 42 is fixed. References and evidence: original fixture messages. Unresolved questions: none. Next actions already stated: continue the no-op fixture.' }
      } else if (options.sessionId === ids.lead && phase === 1) {
        const n = calls.filter(c => c.session_id === ids.lead && c.purpose === 'main').length
        if (n === 1) {
          assert.ok(options.tools.some(t => t.name === 'dpswarm_prepare_worker'))
          const input = JSON.stringify(options.messages)
          assert.ok(input.includes(tasks.hung) && input.includes(tasks.issueOnly), 'Lead must receive actual task text before allocating')
          block = tool('cold-grant-hung', 'dpswarm_prepare_worker', { task: tasks.hung, tokenLimit: limits.hung, callLimit: 1,
            reason: 'Current Lead read the child task; one request and 80000 lifetime tokens suffice for this bounded fixture.' })
        } else if (n === 2) {
          grants.hung = returned(options, 'cold-grant-hung')
          block = tool('cold-grant-issue-only', 'dpswarm_prepare_worker', { task: tasks.issueOnly, tokenLimit: limits.issueOnly, callLimit: 1,
            reason: 'Current Lead read this future child task and grants one independently bound request.' })
        } else if (n === 3) {
          grants.issueOnly = returned(options, 'cold-grant-issue-only')
          block = { type: 'text', text: 'Two task-specific allocations have been issued. No hidden model evaluated these decisions.' }
        } else throw new Error('Unexpected Lead fixture request')
      } else if (options.sessionId === ids.hung) {
        if (phase !== 1) throw new Error('COLD_RESUME_BUDGET_BYPASS: exhausted worker reached the provider after restart')
        const status = await ctx.dpswarmBudget.status(hung)
        assert.equal(status.calls, 1); assert.equal(status.callLimit, 1); assert.equal(status.mode, 'auto')
        assert.ok(status.committed_tokens > 0); assert.equal(status.unknown_usage_calls, 1)
        await ctx.sessions.flush(lead.session); await ctx.sessions.flush(hung.session)
        row.provider_entered = true; row.status_at_entry = status
        hungEnteredResolve({ status })
        // The launcher uses hard process termination. Do not settle this request
        // or yield usage before that checkpoint: its reservation must stay unknown.
        await new Promise(() => {})
        return
      } else {
        block = { type: 'text', text: options.sessionId === ids.cm
          ? 'Native CM fixture reply: the fixed value remains 42; no files or tests.'
          : 'One-shot native child fixture completed; no files or tests.' }
      }
      yield { type: 'block-end', index: 0, block }
      yield { type: 'usage', usage: { inputTokens: options.sessionId === ids.cm && options.purpose !== 'compaction'
        ? Math.ceil(JSON.stringify(options.messages).length / 3) : 100, cacheReadTokens: 10, outputTokens: 20 } }
      yield { type: 'finish', reason: { kind: 'stop' } }
    }
  }
  ctx.llm.registerAdapter([provider], new Fixture())
  async function create(id, parent = null) {
    const handle = await ctx.agents.create({ sessionId: id,
      meta: { cwd: process.cwd(), ...(parent ? { parentSession: parent.session.id, delegationDepth: 1, origin: 'subagent' } : { agentPreset: 'standard' }) },
      agentOptions: { provider, model: 'cold-fixture', reasoningEffort: 'max' },
      setup: async scope => { if (parent) ctx.agentPresets.composeFrom(scope, parent.ctx); else await ctx.agentPresets.mount(scope, 'standard') } })
    handles.push(handle); if (!parent) roots.push(handle.agent)
    return handle.agent
  }
  async function resume(id, parent = null) {
    // This is the production persistence service path, in a distinct OS process.
    const handle = await ctx.agents.resume({ resumeSessionId: id,
      agentOptions: { provider, model: 'cold-fixture', reasoningEffort: 'max' },
      setup: async scope => { if (parent) ctx.agentPresets.composeFrom(scope, parent.ctx); else await ctx.agentPresets.mount(scope, 'standard') } })
    handles.push(handle); if (!parent) roots.push(handle.agent)
    checks.push(`native-agents-resume:${id}`)
    return handle.agent
  }
  async function prompt(agent, text) {
    agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text }] }))
    await deadline(agent.whenIdle(), 30000, `Native prompt ${agent.session.id}`)
  }
  async function configure() {
    await ctx.settings.update('dpswarm', { autoStart: false, sidecarUrl, workspace: join(directory, 'sidecar-state'),
      pythonCmd: process.env.DPSWARM_COLD_PYTHON, dpswarmDir: process.env.DPSWARM_COLD_SOURCE,
      workerBudgetMode: 'auto', workerBudgetSessionOverrides: [], enabledSessions: [], cmEnabledSessions: [ids.cm],
      cmProvider: provider, cmModel: 'cold-fixture', cmEffort: 'max' })
  }
  async function phaseOne() {
    await configure()
    lead = await create(ids.lead)
    await prompt(lead, `Read both no-op tasks, then use the normal dpswarm_prepare_worker tool twice to choose and record their separate Auto allowances. Do not edit files or run tests.\nFirst task: ${tasks.hung}\nSecond task: ${tasks.issueOnly}`)
    assert.ok(grants.hung?.prompt && grants.issueOnly?.prompt)
    await ctx.sessions.flush(lead.session)
    checks.push('actual-current-lead-tools-issued-two-durable-grants')
    cmRoot = await create(ids.cm)
    for (let n = 0; n < 7; n++) {
      const facts = Array.from({ length: 2200 }, (_, i) => `fixture_fact_${n}_${i}=42`).join(' ')
      await prompt(cmRoot, `Store these source facts for the local no-op CM fixture. No files or tests.\n${facts}`)
      const status = await ctx.dpswarmCM.status(cmRoot)
      if (status.adopted > 0) break
    }
    const cmStatus = await ctx.dpswarmCM.status(cmRoot)
    const automaticCMObserved = cmStatus.adopted > 0
    assert.ok(cmStatus.adopted > 0, `A real native compaction must be adopted before the cold restart: ${JSON.stringify({ cmStatus, measure: ctx.tokenMeter.measure(cmRoot.session), node_count: cmRoot.session.surface.nodes.length })}`)
    const compactions = cmRoot.session.events.filter(e => e.type.includes('compact'))
    assert.ok(compactions.length > 0, 'Native compaction transaction was not recorded')
    const originals = cmRoot.session.events.filter(e => e.type === 'user/message').map(e => ({ seq: e.seq, sha256: hash(e) }))
    assert.ok(originals.length > 1)
    await ctx.sessions.flush(cmRoot.session)
    cmEvidence = { status: cmStatus, automatic_trigger_observed: automaticCMObserved,
      trigger: automaticCMObserved ? 'native-pre-step' : 'explicit-real-engine-pressure-call; automatic hook not validated', native_compactions: compactions.map(e => ({ seq: e.seq, type: e.type, sha256: hash(e) })), original_messages: originals }
    checks.push('real-cm-only-native-compaction-and-original-messages-durable')
    hung = await create(ids.hung, lead)
    hung.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: grants.hung.prompt }] }))
    const entered = await deadline(Promise.race([hungEntered, hung.whenIdle().then(() => {
      throw new Error(`Worker stopped before the hanging provider checkpoint: ${JSON.stringify(hung.session.events.filter(e => e.type === 'turn/end').at(-1))}`)
    })]), 30000, 'First limited worker provider entry')
    const known = [nativeKnown(lead), nativeKnown(hung), nativeKnown(cmRoot)]
    const checkpoint = { ready_for_hard_restart: true, phase: 1, pid: process.pid, ids, grants, tasks, limits, checks, calls,
      hung_budget: entered.status, cm: cmEvidence, native_known: known, dsh_home: process.env.DSH_HOME,
      sidecar_url: sidecarUrl, external_model_calls: 0, created_at: new Date().toISOString() }
    writeFileSync(checkpointPath, JSON.stringify(checkpoint, null, 2))
    // Deliberately keep host and child active for the launcher's owned-PID kill.
    await new Promise(() => {})
  }
  async function phaseTwo() {
    assert.notEqual(process.pid, prior.pid, 'Cold resume requires a different OS process')
    assert.equal(resolve(process.env.DSH_HOME), resolve(prior.dsh_home))
    await configure()
    lead = await resume(ids.lead)
    hung = await resume(ids.hung, lead)
    await deadline(hung.whenIdle(), 20000, 'Resumed interrupted worker initial idle')
    const restored = await ctx.dpswarmBudget.status(hung)
    assert.equal(restored.calls, 1); assert.equal(restored.callLimit, 1)
    assert.equal(restored.unknown_usage_calls, 1); assert.equal(restored.observed_tokens_lower_bound, 0)
    assert.equal(restored.committed_tokens, prior.hung_budget.committed_tokens)
    assert.equal(restored.tokenLimit, prior.hung_budget.tokenLimit)
    assert.ok(restored.recent.every(c => c.usage_complete === false))
    await prompt(hung, 'Attempt the next worker request after the process restart; the original lifetime budget still applies.')
    assert.equal(calls.filter(c => c.session_id === ids.hung).length, 0, 'Call limit must reject before local provider entry')
    const ends = hung.session.events.filter(e => e.type === 'turn/end')
    assert.match(JSON.stringify(ends.at(-1)), /WORKER_CALL_LIMIT_REACHED/)
    checks.push('cold-worker-unknown-reservation-preserved', 'second-worker-call-denied-before-provider')
    const issue = await create(`cold-issue-child-${randomUUID()}`, lead)
    await prompt(issue, prior.grants.issueOnly.prompt)
    const issuedStatus = await ctx.dpswarmBudget.status(issue)
    assert.equal(issuedStatus.calls, 1); assert.equal(issuedStatus.mode, 'auto')
    assert.equal(calls.filter(c => c.session_id === issue.session.id).length, 1)
    assert.equal(issuedStatus.decision.allocation_id, prior.grants.issueOnly.allocation_id)
    const replay = await create(`cold-replay-child-${randomUUID()}`, lead)
    await prompt(replay, prior.grants.issueOnly.prompt)
    assert.equal(calls.filter(c => c.session_id === replay.session.id).length, 0)
    const replayEnd = replay.session.events.filter(e => e.type === 'turn/end').at(-1)
    assert.equal(replayEnd?.data.reason.kind, 'error')
    assert.match(JSON.stringify(replayEnd), /WORKER_BUDGET_DECISION_REQUIRED|Auto requires the current Lead to decide this worker allocation before delegation/)
    checks.push('issue-only-grant-survives-process-restart', 'grant-bound-once-cross-child-replay-refused')
    cmRoot = await resume(ids.cm)
    const current = new Map(cmRoot.session.events.map(e => [e.seq, e]))
    for (const message of prior.cm.original_messages) assert.equal(hash(current.get(message.seq)), message.sha256, 'Original native user message changed after cold resume')
    for (const event of prior.cm.native_compactions) assert.equal(hash(current.get(event.seq)), event.sha256, 'Native compaction changed after cold resume')
    const cmStatus = await ctx.dpswarmCM.status(cmRoot)
    const automaticCMObserved = cmStatus.adopted > 0
    assert.ok(cmStatus.adopted >= prior.cm.status.adopted, 'CM audit must restore with native compaction')
    checks.push('cm-only-real-native-cold-resume', 'original-messages-and-native-compaction-preserved')
    const native = [lead, hung, issue, replay, cmRoot].map(nativeKnown)
    for (const agent of [lead, hung, issue, replay, cmRoot]) await ctx.sessions.flush(agent.session)
    const sidecar = new Sidecar({ sidecarUrl, workspace: join(directory, 'sidecar-state'), sessionId: ids.lead, sessionIsolation: true, autoStart: false })
    await sidecar.ensure()
    const status = await sidecar.call('GET', '/api/status')
    return { passed: true, phase: 2, pid: process.pid, prior_pid: prior.pid, external_model_calls: 0,
      execution: 'Actual native agents.resume from persisted DSH files in a second OS process; real sidecar restarted against the same durable directory',
      checks, calls, ids, replay_native_end: replayEnd, restored_hung_budget: restored, issue_child_budget: issuedStatus, cm_status: cmStatus,
      native_known: native, cm_trigger: prior.cm.trigger, automatic_cm_hook_validated: prior.cm.automatic_trigger_observed, sidecar_status: status, dsh_home: process.env.DSH_HOME }
  }
  async function run() {
    let report
    try {
      if (phase === 1) { await phaseOne(); return }
      report = await phaseTwo()
    } catch (error) { report = { passed: false, phase, pid: process.pid, error: error.stack, calls, checks, ids, external_model_calls: 0,
      native_failure_evidence: handles.map(h => ({ session_id: h.agent.session.id, events: h.agent.session.events.filter(e => ['tool/result', 'turn/end'].includes(e.type)).slice(-8) })) } }
    finally {
      if (report) {
        for (const h of handles.reverse()) try { await h.dispose() } catch (e) { report.cleanup_error = String(e); report.passed = false }
        writeFileSync(resultPath, JSON.stringify(report, null, 2))
      }
    }
  }
  ctx.effect(() => {
    const timer = setTimeout(() => { void run().catch(error => writeFileSync(resultPath, JSON.stringify({ passed: false, phase, error: error.stack, calls, checks }, null, 2))) }, 1600)
    return () => { cancelled = true; clearTimeout(timer) }
  })
}
