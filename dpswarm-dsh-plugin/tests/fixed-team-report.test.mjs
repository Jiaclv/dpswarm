import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FixedTeamController } from '../lib/fixed-team.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'

function fixture(outputText) {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-report-')), cwd = join(workspace, 'project'); mkdirSync(cwd)
  const cfg = { workspace, sidecarUrl: 'http://127.0.0.1:8791', autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    workerTimeoutSeconds: 600, workerBudgetMode: 'unlimited' }
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [{ type: 'user/message', seq: 0, data: { id: 'user-task-1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Create an HTML file.' }] } }],
    requestHeader() { return { config: { provider: 'fixture', model: 'lead' } } } } }
  const journal = new MemoryAuditJournal(), items = {}
  const sidecarFactory = cfg => ({ cfg, async ensure() {}, async call(method, path, body) {
    if (path === '/api/plugin-audit') {
      if (method === 'POST') return (await journal.transaction(cfg.sessionId, () => ({ events: body.events }))).journal
      return journal.read(cfg.sessionId)
    }
    if (path === '/api/status') return { snapshot: { work_items: items, seal_phase: {},
      open_worker_slots_used: Object.values(items).filter(item => !['accepted', 'terminated'].includes(item.acceptance)).length } }
    if (path === '/api/delegate') { const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null }
      return { items: [{ item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive' }] } }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') { items[body.item_id].acceptance = 'submitted'; items[body.item_id].submission_package_id = `package-${body.item_id}` }
    if (path === '/api/review') { items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated' }
    return { ok: true }
  } })
  const subagents = { async start(provider, request) {
    const id = `child-${Object.keys(items).length}`
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 },
      events: [{ seq: 0, type: 'turn/end', data: { reason: { kind: 'completed' } }, time: 123 }] }
    return { id, request, provider, session, localAgent: { session },
      result: Promise.resolve({ output: [{ type: 'text', text: outputText }], stopReason: 'completed' }),
      async dispose() {} }
  } }
  const controller = new FixedTeamController({ config: () => cfg, budget: null, subagents, sidecarFactory, resolveSession: () => null })
  const exec = { agent: parent, signal: new AbortController().signal }
  return { controller, exec, parent, items, journal, cfg, sidecarFactory }
}

test('dpswarm_report pages the unabridged worker report that the run view truncates', async () => {
  const outputText = '实现报告。'.repeat(2000) // 10,000 chars
  const h = fixture(outputText)
  const result = await h.controller.run({ task: 'Create an HTML file.', acceptance: 'Single file.' }, h.exec)
  const item = result.deliveries[0]
  assert.equal(item.output_truncated, true)
  assert.equal(item.output_original_chars, 10000)
  assert.match(item.detail_source, /dpswarm_report/)
  const page1 = await h.controller.report({ item_id: item.item_id }, h.exec)
  assert.equal(page1.text.length, 4000)
  assert.equal(page1.total_chars, 10000)
  assert.equal(page1.truncated, true)
  assert.equal(page1.report_source, 'native-result')
  assert.equal(page1.completion, 'completed')
  const page2 = await h.controller.report({ item_id: item.item_id, offset: 4000, limit: 4000 }, h.exec)
  const page3 = await h.controller.report({ item_id: item.item_id, offset: 8000, limit: 4000 }, h.exec)
  assert.equal(page1.text + page2.text + page3.text, outputText)
  assert.equal(page3.truncated, false)
  const pastEnd = await h.controller.report({ item_id: item.item_id, offset: 20000 }, h.exec)
  assert.equal(pastEnd.text, '')
  assert.equal(pastEnd.truncated, false)
})

test('dpswarm_report reads the durable ledger cold, after the delivering controller is gone', async () => {
  const h = fixture('冷恢复报告。'.repeat(100)) // 600 chars
  const result = await h.controller.run({ task: 'Create an HTML file.', acceptance: 'Single file.' }, h.exec)
  const item = result.deliveries[0]
  const cold = new FixedTeamController({ config: () => h.cfg, budget: null, subagents: { start() { throw new Error('must not start') } },
    sidecarFactory: h.sidecarFactory, resolveSession: () => null })
  const page = await cold.report({ item_id: item.item_id, limit: 100 }, h.exec)
  assert.equal(page.total_chars, 600)
  assert.equal(page.text, '冷恢复报告。'.repeat(100).slice(0, 100))
})

test('dpswarm_report rejects unknown items, bad ranges and non-root callers', async () => {
  const h = fixture('报告。')
  const result = await h.controller.run({ task: 'Create an HTML file.', acceptance: 'Single file.' }, h.exec)
  const item = result.deliveries[0]
  await assert.rejects(h.controller.report({ item_id: 'item-nope' }, h.exec), /REPORT_NOT_FOUND/)
  await assert.rejects(h.controller.report({ item_id: item.item_id, offset: -1 }, h.exec), /REPORT_RANGE_INVALID/)
  await assert.rejects(h.controller.report({ item_id: item.item_id, limit: 0 }, h.exec), /REPORT_RANGE_INVALID/)
  await assert.rejects(h.controller.report({ item_id: item.item_id, limit: 40001 }, h.exec), /REPORT_RANGE_INVALID/)
  const worker = { id: 'child-1', options: {}, session: { id: 'child-1', header: { id: 'child-1', parentSession: 'root', origin: 'subagent', delegationDepth: 1 } } }
  await assert.rejects(h.controller.report({ item_id: item.item_id }, { agent: worker }), /PARENT_IDENTITY_REQUIRED|NESTED_DELEGATION_UNSUPPORTED/)
})

test('dpswarm_report fails closed when the worker produced no report text', async () => {
  const h = fixture('')
  const result = await h.controller.run({ task: 'Create an HTML file.', acceptance: 'Single file.' }, h.exec)
  assert.equal(result.deliveries.length, 0)
  assert.equal(result.failed[0].code, 'SUBAGENT_EMPTY_DELIVERY')
  // The failed worker's diagnostic was still audited, but it holds no report text.
  await assert.rejects(h.controller.report({ item_id: result.failed[0].item_id }, h.exec), /REPORT_UNAVAILABLE/)
})
