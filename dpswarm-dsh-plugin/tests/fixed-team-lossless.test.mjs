import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setImmediate as nextTurn } from 'node:timers/promises'
import { FixedTeamController, fixedProfile } from '../lib/fixed-team.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import { resolveHostRoot, hostModuleUrl } from '../lib/host-modules.js'

const host = resolveHostRoot()
const { isJsonValue } = await import(hostModuleUrl(host, 'dsh-util-values/lib/index.js'))

// The host rejects tool results that cannot round-trip losslessly (live session
// 88122af1: dpswarm_run executed fully, then the result was dropped with
// "value is not lossless JSON"). Report the offending paths on failure.
const losslessOffenders = (value, path = '', offenders = []) => {
  if (value === undefined) offenders.push([path, 'undefined value'])
  else if (typeof value === 'number') { if (!Number.isFinite(value) || Object.is(value, -0)) offenders.push([path, `non-finite/-0: ${value}`]) }
  else if (value === null || typeof value === 'boolean' || typeof value === 'string') {}
  else if (typeof value !== 'object') offenders.push([path, `type ${typeof value}`])
  else if (Array.isArray(value)) { if (Object.getPrototypeOf(value) !== Array.prototype && Object.getPrototypeOf(value) !== null) offenders.push([path, 'non-plain array']); else value.forEach((entry, i) => losslessOffenders(entry, `${path}[${i}]`, offenders)) }
  else if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) offenders.push([path, 'non-plain object'])
  else for (const key of Object.keys(value)) losslessOffenders(value[key], path ? `${path}.${key}` : key, offenders)
  return offenders
}

const assertLossless = result => {
  if (isJsonValue(result) !== true) {
    const offenders = losslessOffenders(result).slice(0, 10).map(([path, why]) => `${path} :: ${why}`).join('; ')
    assert.fail(`dpswarm_run result is not lossless JSON. Offenders: ${offenders || '(none found by walker)'}`)
  }
}

test('a role profile without a configured reasoning effort omits the key instead of writing undefined', () => {
  const cfg = { implProvider: 'glmcp', implModel: 'glm-5.3-flash', testProvider: 'glmcp', testModel: 'glm-5.3-flash', reviewerMode: 'model', reviewerProvider: 'glmcp', reviewerModel: 'glm-5.3-flash' }
  const profile = fixedProfile(cfg, { enabled: true, profile: null })
  for (const role of ['implementer', 'tester', 'reviewer']) {
    assert.ok(!('reasoning_effort' in profile[role]), `${role} must not carry an explicit undefined reasoning_effort`)
  }
  const lead = fixedProfile({ testProvider: 'glmcp', testModel: 'glm-5.3-flash' }, { enabled: true, profile: null },
    { provider: 'glmcp', model: 'glm-5.3-flash' })
  assert.ok(!('reasoning_effort' in lead.implementer), 'lead-mode implementer must omit an unset reasoning_effort')
})

test('dpswarm_run returns a lossless JSON result when a route has no reasoning effort and its implementer dies at the provider', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'dpswarm-lossless-')), cwd = join(directory, 'project')
  mkdirSync(cwd)
  const cfg = { sidecarUrl: 'http://127.0.0.1:8791', workspace: directory, autoStart: false,
    enabledSessions: ['parent'], subagentProvider: 'spawn', implProvider: 'glmcp', implModel: 'glm-5.3-flash',
    testProvider: 'glmcp', testModel: 'glm-5.3-flash', workerTimeoutSeconds: 600 }
  const parent = { id: 'parent', session: { id: 'parent', header: { cwd }, route: { provider: 'glmcp', model: 'glm-5.3-flash' },
    requestHeader() { return { config: this.route } } }, options: {} }
  const items = {}
  const journal = new MemoryAuditJournal()
  const sidecarFactory = snapshot => ({ cfg: snapshot, async ensure() {}, async call(method, path, body) {
    if (path === '/api/plugin-audit') {
      if (method === 'POST') return (await journal.transaction(snapshot.sessionId, () => ({ events: body.events }))).journal
      return journal.read(snapshot.sessionId)
    }
    if (path === '/api/status') return { snapshot: { work_items: items, seal_phase: {},
      open_worker_slots_used: Object.values(items).filter(i => !['accepted', 'terminated'].includes(i.acceptance)).length } }
    if (path === '/api/delegate') { const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null }
      return { items: [{ item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive' }] } }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    return { ok: true }
  } })
  const children = []
  const subagents = { async start(provider, request) {
    let resolve
    const child = { id: 'real-child-' + children.length, result: new Promise(r => { resolve = r }),
      // The live first-attempt worker died at the provider before any cleanup could
      // be confirmed; disposal failure matches the live shape (no tester child).
      async dispose() { throw new Error('native worker may still be alive') } }
    children.push({ child, resolve, request, provider })
    return child
  } }
  const controller = new FixedTeamController({ config: () => cfg, subagents, sidecarFactory, resolveSession: () => null })
  const exec = { agent: parent, signal: new AbortController().signal }

  try {
    const running = controller.run({ task: 'Create an HTML file.', acceptance: 'Single file.' }, exec)
    await nextTurn()
    // Implementer dies on its first request, exactly like the live glm run:
    // zero-usage provider rejection, terminal INVALID_REQUEST, no visible text.
    const handle = children[0].child
    const sid = handle.id
    handle.localAgent = { session: { id: sid, header: { id: sid, parentSession: 'parent', origin: 'subagent', delegationDepth: 1 },
      events: [
        { seq: 0, time: 100, type: 'turn/start', data: { turn: 1 } },
        { seq: 1, time: 101, type: 'assistant/attempt', data: { turn: 1, step: 1, stream: [
          { type: 'chunk', chunk: { type: 'usage', usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 } } },
          { type: 'chunk', chunk: { type: 'finish', reason: { kind: 'error', failure: { message: '400: {"code":"1210","message":"max_tokens"}' }, code: 'INVALID_REQUEST' } } } ] } },
        { seq: 2, time: 102, type: 'turn/end', data: { turn: 1, reason: { kind: 'error',
          error: { message: '400: {"code":"1210","message":"max_tokens参数非法：限制数值范围[1,131072]"}', code: 'INVALID_REQUEST' } } } },
      ] } }
    children[0].resolve({ output: [], stopReason: 'error' })
    const result = await running
    assertLossless(result)
  } finally {
    await controller.shutdown().catch(() => {})
  }
})
