import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { FixedTeamController } from '../lib/fixed-team.js'
import { TeamDispatcher } from '../lib/team-dispatch.js'
import { TeamRequirement } from '../lib/team-required.js'
import { WriteScopeRegistry } from '../lib/write-scope.js'
import { HostModelRegistry } from '../lib/host-model-registry.js'
import { MemoryAuditJournal } from './helpers/memory-audit.mjs'
import {
  DELIVERY_BY_KIND, MAILBOX_EVENTS, MAX_MESSAGE_BYTES, MAX_PENDING_PER_MEMBER,
  TeamMailbox, compactMailboxEntry, renderMailboxSection, storageKey,
} from '../lib/mailbox.js'

// 有界口径照抄宿主 experimental team mailbox（maxPendingMessagesPerMember=64
// 超限 TEAM_MAILBOX_FULL / maxMessageBytes 64KiB → DPSWARM_* 结构化码）。

class MemoryKv {
  constructor() { this.tables = { messages: {} } }
  async loadAll() { return JSON.parse(JSON.stringify({ tables: this.tables, global: null })) }
  async putRecord(table, key, value) { this.tables[table][key] = JSON.parse(JSON.stringify(value)) }
  async deleteRecord(table, key) { delete this.tables[table][key] }
}

/** 磁盘整单元文档：模拟跨进程/重启冷恢复（每次实例化都从盘上读）。 */
class FileKv {
  constructor(path) { this.path = path }
  async loadAll() {
    try { return JSON.parse(readFileSync(this.path, 'utf8')) } catch { return { tables: { messages: {} }, global: null } }
  }
  async putRecord(table, key, value) {
    const doc = await this.loadAll()
    doc.tables[table][key] = JSON.parse(JSON.stringify(value))
    writeFileSync(this.path, JSON.stringify(doc))
  }
  async deleteRecord(table, key) {
    const doc = await this.loadAll()
    delete doc.tables[table][key]
    writeFileSync(this.path, JSON.stringify(doc))
  }
}

const members = () => [
  { id: 'part-a', role: 'implementer', subtask: 'part-a' }, { id: 'tester', role: 'tester' },
]
const post = (mailbox, rootId, over = {}) => mailbox.post(rootId, {
  run_id: 'run-1', from: 'lead', to: 'part-a', kind: 'fact', content: '接口签名已定为 validate(record, schema).',
  ...(over.message_id ? { message_id: over.message_id } : {}), ...over,
})

async function registeredMailbox(storage, journal) {
  const mailbox = new TeamMailbox({ storage, journal })
  await mailbox.register('root', { run_id: 'run-1', members: members() })
  return mailbox
}

// ---- 核心语义：三类消息路由（inject vs followup）----

test('routing: fact rides inject without waking, clarify/block ride followup and wake (live channel)', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  const calls = []
  mailbox.attachChannel('root', 'part-a', {
    inject: async m => calls.push(['inject', m.kind]),
    followup: async m => calls.push(['followup', m.kind]),
  })
  const fact = await mailbox.post('root', { run_id: 'run-1', from: 'lead', to: 'part-a', kind: 'fact', content: 'fact A' })
  assert.equal(fact.delivery, 'delivered')
  assert.equal(fact.message.delivered_via, 'inject')
  assert.equal(fact.message.delivered_carrier, 'live-channel')
  const clarify = await mailbox.post('root', { message_id: 'q-1', run_id: 'run-1', from: 'lead', to: 'part-a', kind: 'clarify', content: 'which encoding?' })
  const block = await mailbox.post('root', { run_id: 'run-1', from: 'lead', to: 'part-a', kind: 'block', content: 'upstream frozen' })
  assert.deepEqual(calls, [['inject', 'fact'], ['followup', 'clarify'], ['followup', 'block']])
  assert.deepEqual([clarify.message.delivered_via, block.message.delivered_via], ['followup', 'followup'])
  // 投递审计：mailbox-delivered 带 via（语义路由）与 carrier（承载通道）。
  const delivered = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.delivered)
  assert.deepEqual(delivered.map(e => [e.data.kind, e.data.via, e.data.carrier]),
    [['fact', 'inject', 'live-channel'], ['clarify', 'followup', 'live-channel'], ['block', 'followup', 'live-channel']])
})

test('routing: without a live channel the message stays pending (FIFO) and later drains by delivery semantics', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  await post(mailbox, 'root', { message_id: 'm-1', content: 'fact one' })
  await post(mailbox, 'root', { message_id: 'm-2', kind: 'clarify', content: 'clarify one' })
  const pending = await mailbox.pendingFor('root', 'part-a')
  assert.deepEqual(pending.map(m => [m.message_id, m.kind]), [['m-1', 'fact'], ['m-2', 'clarify']])
  assert.equal((await mailbox.status('root')).pending['part-a'], 2)
  const drained = await mailbox.drain('root', 'part-a', 'wake-prompt')
  assert.equal(drained.delivered, 2)
  assert.match(drained.text, /## DPSwarm 团队邮箱（本次延续随带 2 条消息/)
  assert.match(drained.text, /【fact】来自 lead\nfact one/)
  assert.match(drained.text, /【clarify】来自 lead\nclarify one/)
  const events = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.delivered)
  assert.deepEqual(events.map(e => [e.data.message_id, e.data.via, e.data.carrier]),
    [['m-1', 'inject', 'wake-prompt'], ['m-2', 'followup', 'wake-prompt']], 'drain stamps via from kind, carrier from the continuation')
  assert.equal((await mailbox.status('root')).pending['part-a'], 0)
  assert.equal((await mailbox.drain('root', 'part-a', 'wake-prompt')).text, '', 'empty drain renders nothing')
})

test('routing: a failing live channel leaves the message durably pending with a structured delivery error', async () => {
  const mailbox = await registeredMailbox(new MemoryKv(), new MemoryAuditJournal())
  const detach = mailbox.attachChannel('root', 'part-a', {
    inject: async () => { throw Object.assign(new Error('boom'), { code: 'CHANNEL_DOWN' }) },
    followup: async () => {},
  })
  const result = await mailbox.post('root', { run_id: 'run-1', from: 'lead', to: 'part-a', kind: 'fact', content: 'fact' })
  assert.equal(result.delivery, 'pending')
  assert.deepEqual(result.delivery_error, { code: 'CHANNEL_DOWN', message: 'boom' })
  detach()
  assert.equal((await mailbox.pendingFor('root', 'part-a')).length, 1)
})

// ---- 有界 ----

test('bounds: the 65th pending message per member is refused with DPSWARM_MAILBOX_FULL (audited)', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  for (let i = 0; i < MAX_PENDING_PER_MEMBER; i++) await post(mailbox, 'root', { message_id: `m-${i}` })
  await assert.rejects(post(mailbox, 'root', { message_id: 'm-64' }), { code: 'DPSWARM_MAILBOX_FULL' })
  const rejected = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.rejected)
  assert.equal(rejected.length, 1)
  assert.equal(rejected[0].data.code, 'DPSWARM_MAILBOX_FULL')
  assert.equal(rejected[0].data.to, 'part-a')
  // 限额按目标成员独立计数，且 delivered 消息不再占额度。
  assert.equal((await mailbox.post('root', { run_id: 'run-1', from: 'lead', to: 'tester', kind: 'fact', content: 't' })).delivery,
    'pending', "part-a's full queue does not block another member")
  await mailbox.drain('root', 'part-a', 'wake-prompt')
  assert.equal((await post(mailbox, 'root', { message_id: 'm-65' })).delivery, 'pending', 'drained mail frees the queue slot')
})

test('bounds: a message framing over 64 KiB is refused with DPSWARM_MESSAGE_TOO_LARGE (audited)', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  // 32000 字 CJK 内容按 UTF-8 框架约 96KB，触发帧级上限（而非字符预检）。
  await assert.rejects(post(mailbox, 'root', { content: '秋'.repeat(32000) }), { code: 'DPSWARM_MESSAGE_TOO_LARGE' })
  const rejected = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.rejected)
  assert.equal(rejected.length, 1)
  assert.equal(rejected[0].data.code, 'DPSWARM_MESSAGE_TOO_LARGE')
  assert.match(rejected[0].data.detail, /bytes/)
})

// ---- 持久化 / 冷恢复 ----

test('persistence: pending mail survives a cold restart (fresh instance over the same medium)', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'dpswarm-mailbox-')), 'unit.json')
  const a = await registeredMailbox(new FileKv(path), new MemoryAuditJournal())
  await post(a, 'root', { message_id: 'm-1', kind: 'block', content: '等待 layout' })
  await post(a, 'root', { message_id: 'm-2', kind: 'clarify', content: '哪个编码？' })
  // 冷恢复：全新实例（模拟重启后重新 open 同一 KV 单元）。
  const b = new TeamMailbox({ storage: new FileKv(path), journal: new MemoryAuditJournal() })
  const pending = await b.pendingFor('root', 'part-a')
  assert.deepEqual(pending.map(m => [m.message_id, m.kind, m.run_id]), [['m-1', 'block', 'run-1'], ['m-2', 'clarify', 'run-1']])
  assert.deepEqual((await b.status('root')).members, ['lead', 'part-a', 'tester'], 'membership is durable too')
  await b.drain('root', 'part-a', 'wake-prompt')
  const c = new TeamMailbox({ storage: new FileKv(path), journal: new MemoryAuditJournal() })
  assert.equal((await c.pendingFor('root', 'part-a')).length, 0, 'delivery state is durable')
  // 非 ASCII / 特殊字符 root id：存储键保持 KV 安全字符且往返一致。
  const oddRoot = '会话/奇数 id'
  const odd = new TeamMailbox({ storage: new FileKv(path), journal: new MemoryAuditJournal() })
  await odd.register(oddRoot, { run_id: 'run-1', members: [] })
  assert.match(storageKey(oddRoot), /^[A-Za-z0-9_-]+$/)
  assert.deepEqual((await new TeamMailbox({ storage: new FileKv(path), journal: new MemoryAuditJournal() }).status(oddRoot)).members, ['lead'])
})

// ---- 去重 ----

test('dedup: reposting the same message_id is an idempotent no-op, not a second pending entry', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  const first = await post(mailbox, 'root', { message_id: 'same-id' })
  const second = await post(mailbox, 'root', { message_id: 'same-id', content: 'changed later' })
  assert.equal(first.deduplicated, false)
  assert.equal(second.deduplicated, true)
  assert.equal(second.message.content, first.message.content, 'the original entry wins')
  assert.equal((await mailbox.pendingFor('root', 'part-a')).length, 1)
  assert.equal((await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.queued).length, 1, 'no duplicate queued audit')
})

// ---- 控制性命令拒绝 / 校验 ----

test('control commands are refused: kind vocabulary stays fact|clarify|block and points at control-plane tools', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  for (const kind of ['approve', 'ACCEPT', 'rework', 'grant', 'override', 'expand', 'permission']) {
    await assert.rejects(post(mailbox, 'root', { kind }), { code: 'DPSWARM_MAILBOX_CONTROL_REJECTED' })
  }
  await assert.rejects(post(mailbox, 'root', { kind: 'status-update' }), { code: 'DPSWARM_MAILBOX_KIND_INVALID' })
  const rejected = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.rejected)
  assert.equal(rejected.length, 8)
  assert.ok(rejected.every(e => e.data.code === 'DPSWARM_MAILBOX_CONTROL_REJECTED' || e.data.code === 'DPSWARM_MAILBOX_KIND_INVALID'))
})

test('validation: self-message, unknown member, run mismatch, empty content and bad refs are structured rejections', async () => {
  const mailbox = await registeredMailbox(new MemoryKv(), new MemoryAuditJournal())
  await assert.rejects(mailbox.post('root', { run_id: 'run-1', from: 'lead', to: 'lead', kind: 'fact', content: 'x' }), { code: 'DPSWARM_MAILBOX_SELF_MESSAGE' })
  await assert.rejects(post(mailbox, 'root', { to: 'ghost' }), { code: 'DPSWARM_MAILBOX_MEMBER_UNKNOWN' })
  await assert.rejects(post(mailbox, 'root', { from: 'ghost' }), { code: 'DPSWARM_MAILBOX_MEMBER_UNKNOWN' })
  await assert.rejects(post(mailbox, 'root', { run_id: 'run-2' }), { code: 'DPSWARM_MAILBOX_RUN_MISMATCH' })
  await assert.rejects(post(mailbox, 'root', { content: '  ' }), { code: 'DPSWARM_MAILBOX_MESSAGE_INVALID' })
  await assert.rejects(post(mailbox, 'root', { refs: Array.from({ length: 17 }, () => 'r') }), { code: 'DPSWARM_MAILBOX_MESSAGE_INVALID' })
  await assert.rejects(post(mailbox, 'root', { refs: ['ok', 42] }), { code: 'DPSWARM_MAILBOX_MESSAGE_INVALID' })
  await assert.rejects(post(mailbox, 'root', { extra: 1 }), { code: 'DPSWARM_MAILBOX_MESSAGE_INVALID' })
  const unregistered = new TeamMailbox({ storage: new MemoryKv(), journal: new MemoryAuditJournal() })
  await assert.rejects(unregistered.post('root', { run_id: 'r', from: 'lead', to: 'lead', kind: 'fact', content: 'x' }), { code: 'DPSWARM_MAILBOX_UNAVAILABLE' })
})

test('a new run registration supersedes undelivered mail from the old run with an audited refusal', async () => {
  const journal = new MemoryAuditJournal(), mailbox = await registeredMailbox(new MemoryKv(), journal)
  await post(mailbox, 'root', { message_id: 'stale-1' })
  await mailbox.register('root', { run_id: 'run-2', members: members() })
  assert.equal((await mailbox.pendingFor('root', 'part-a')).length, 0)
  const rejected = (await journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.rejected)
  assert.equal(rejected[0].data.code, 'DPSWARM_MAILBOX_RUN_SUPERSEDED')
  assert.equal(rejected[0].data.message_id, 'stale-1')
  const status = await mailbox.status('root')
  assert.equal(status.run_id, 'run-2')
  assert.deepEqual(Object.keys(status.pending), ['lead', 'part-a', 'tester'])
})

test('compactMailboxEntry truncates content and renderMailboxSection stays bounded', () => {
  const entry = compactMailboxEntry({ message_id: 'm', run_id: 'r', from: 'lead', to: 'part-a', kind: 'fact', refs: ['a'], ts: 1, content: 'x'.repeat(3000) }, { content: 100 })
  assert.equal(entry.content.length, 100 + '…[truncated]'.length)
  assert.ok(!entry.content.includes('x'.repeat(101)))
  assert.equal(renderMailboxSection([]), '')
  assert.match(renderMailboxSection([{ kind: 'block', from: 'bird', refs: ['bike'], content: 'waiting' }]), /【block】来自 bird（引用：bike）\nwaiting/)
})

// ---- 控制器集成：staged 挂起/唤醒路径 + worker 报告阻塞路径 ----

/** 与 fixed-team-parallel 同构的 staged fixture，外加 mailbox 存储与工具钩子。 */
function controllerFixture() {
  const workspace = mkdtempSync(join(tmpdir(), 'dpswarm-mailbox-ct-')), cwd = join(workspace, 'project'); mkdirSync(cwd)
  const cfg = { workspace, sidecarUrl: 'http://127.0.0.1:8791', autoStart: false, enabledSessions: ['root'],
    subagentProvider: 'spawn', implMode: 'lead', testProvider: 'fixture', testModel: 'tester',
    workerTimeoutSeconds: 600, workerBudgetMode: 'unlimited', reworkBudgetMode: 'unlimited',
    teamModeOverrides: [{ sessionId: 'root', mode: 'staged' }] }
  const parent = { id: 'root', options: {}, session: { id: 'root', header: { id: 'root', cwd, origin: 'root', delegationDepth: 0 },
    events: [{ type: 'user/message', seq: 0, data: { id: 'u1', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'Build both.' }] } }],
    requestHeader() { return { config: this.route } }, route: { provider: 'fixture', model: 'lead', reasoningEffort: 'max' } } }
  const journal = new MemoryAuditJournal(), items = {}, children = [], sessions = new Map(), allocations = new Map()
  const h = { cfg, parent, journal, items, children, sessions, allocations, spec: { max_team_workers: 8 }, submits: [], onSubmit: null }
  const sidecarFactory = cfg => ({ cfg, async ensure() {}, async call(method, path, body) {
    if (path === '/api/plugin-audit') {
      if (method === 'POST') return (await journal.transaction(cfg.sessionId, () => ({ events: body.events }))).journal
      return journal.read(cfg.sessionId)
    }
    if (path === '/api/status') return { spec: { max_team_workers: h.spec.max_team_workers }, snapshot: { work_items: items,
      open_worker_slots_used: Object.values(items).filter(item => !['accepted', 'terminated'].includes(item.acceptance)).length,
      seal_phase: {} } }
    if (path === '/api/delegate') {
      return { items: body.subtasks.map((st, i) => {
        const item_id = `item-${Object.keys(items).length}`; items[item_id] = { acceptance: 'active', submission_package_id: null, kind: 'derive' }
        return { item_id, node_id: item_id, session_id: `reservation-${item_id}`, context_epoch: 0, attempt: 1, kind: 'derive', subtask_index: i }
      }) }
    }
    if (path === '/api/execution/bind') return { context_epoch: 0, session_id: body.execution_session_id }
    if (path === '/api/submit') { items[body.item_id].acceptance = 'submitted'; items[body.item_id].submission_package_id = `package-${body.item_id}`
      h.submits.push(body.item_id); if (h.onSubmit) await h.onSubmit(body) }
    if (path === '/api/execution/fail') { items[body.item_id].acceptance = 'terminated' }
    if (path === '/api/review') { items[body.item_id].acceptance = body.verdict === 'accept' ? 'accepted' : 'terminated' }
    return { ok: true }
  } })
  const budget = {
    async beginTeamRun(_p, { roles, expectedProfile }) { return { profile: expectedProfile, roles } },
    async finishTeamRun() {},
    async issueTeamWorker(_p, _run, args) { return { prompt: args.task, role: args.label, subtask: args.subtask ?? null } },
    async diagnosticsForSession(workerId) { return { root_session_id: 'root', worker_session_id: workerId, mode: 'unlimited', calls: 1, committed_tokens: 100 } },
    async issueRework(_p, args) {
      if ([...allocations.values()].some(a => a.source === args.workerSessionId && !a.revoked)) throw Object.assign(new Error('REWORK_SOURCE_SUPERSEDED'), { code: 'REWORK_SOURCE_SUPERSEDED' })
      const id = `alloc-${allocations.size}`; allocations.set(id, { source: args.workerSessionId, bound: false, revoked: false })
      return { allocation_id: id, prompt: `ALLOCATION:${id}\n${args.task}`, role: 'implementer', profile: { mode: 'unlimited' }, source_worker_session_id: args.workerSessionId }
    },
    async revokeRework(_p, id) { const a = allocations.get(id); if (a && !a.bound) a.revoked = true },
  }
  const subagents = { async start(provider, request) {
    const id = `child-${children.length}`
    const session = { id, header: { id, parentSession: parent.id, origin: 'subagent', delegationDepth: 1 }, events: [] }
    sessions.set(id, { id, session })
    if (h.readyOnStart?.[id]) h.writeScope.updateArtifactState('root', h.readyOnStart[id], 'ready', 2)
    const allocationId = request.prompt[0].text.match(/^ALLOCATION:(\S+)/)?.[1]
    if (allocationId) allocations.get(allocationId).bound = true
    session.events.push({ seq: 0, type: 'turn/end', data: { reason: { kind: 'completed' } }, time: Date.now() })
    const marker = h.markerFor?.[id]
    const child = { id, request, provider, session, localAgent: { session },
      result: Promise.resolve({ output: [{ type: 'text', text: marker ? `partial progress saved\n\n[DPSWARM_WAITING: ${marker}]` : (h.outputFor?.[id] ?? `done:${id}`) }],
        stopReason: 'completed' }),
      async dispose() {} }
    children.push(child)
    return child
  } }
  const modelRegistry = new HostModelRegistry(() => ({ async resolveCallConfig(route) { return route } }))
  const writeScope = new WriteScopeRegistry({ journal })
  h.kv = new MemoryKv()
  h.controller = new FixedTeamController({ config: () => cfg, budget, subagents, modelRegistry, sidecarFactory, writeScope,
    mailboxStorage: h.kv, resolveSession: id => sessions.get(id) })
  h.requirement = new TeamRequirement({ config: () => cfg, journal })
  h.dispatcher = new TeamDispatcher({ controller: h.controller, requirement: h.requirement })
  h.exec = { agent: parent, signal: new AbortController().signal }
  h.writeScope = writeScope
  h.staged = {
    phases: [{ id: 'build', task: 'Build both parts.' }],
    artifacts: [
      { id: 'bike', title: '自行车', task: 'Build the bicycle.', write_globs: ['src/bike/**'], phase: 'build' },
      { id: 'bird', title: '鹈鹕', task: 'Build the pelican.', write_globs: ['src/bird/**'], phase: 'build', deps: ['bike'] },
    ],
  }
  h.run = () => h.dispatcher.run({ task: 'Build.', acceptance: 'Done.', staged: h.staged }, h.exec)
  return h
}

const workerExec = session => ({ agent: { session } })

test('staged wiring: a waiting worker reports block into the Lead inbox and lead mail rides the wake continuation', async () => {
  const h = controllerFixture()
  h.markerFor = { 'child-1': 'bike' }     // bird 的首 pass 报告等待 bike
  h.readyOnStart = { 'child-1': 'bike' }  // bike 在 bird 运行中翻 ready → 唤醒循环
  // bird 首 pass 提交（注册已生效、唤醒尚未派发）时：worker 发 fact 给 lead，lead 发 clarify 给 bird。
  h.onSubmit = async body => {
    if (h.postedOnce || h.children.length < 2 || h.controller.resolveTeamWorker(h.children[1].session.id)?.member !== 'bird') return
    h.postedOnce = true
    await h.controller.mailbox({ action: 'post', kind: 'fact', content: '发现 bike 的 schema 需要显式 version 字段。', refs: ['bike'] }, workerExec(h.children[1].session))
    await h.controller.mailbox({ action: 'post', to: 'bird', kind: 'clarify', content: '锚点用相对坐标还是绝对坐标？', message_id: 'q-anchor' }, h.exec)
  }
  const result = await h.run()
  assert.equal(result.failed.length, 0)
  // 唤醒延续 prompt 承载 lead→bird 的 pending 消息（clarify 随唤醒送达）。
  const wakePrompt = h.children[2].request.prompt[0].text
  assert.match(wakePrompt, /唤醒继续（bird）/)
  assert.match(wakePrompt, /## DPSwarm 团队邮箱（本次延续随带 1 条消息/)
  assert.match(wakePrompt, /【clarify】来自 lead\n锚点用相对坐标还是绝对坐标？/)
  // run 返回体呈出 worker→lead 的 pending（bird 的 fact + 等待报告 block）。
  assert.ok(result.mailbox, 'run result surfaces the lead inbox')
  assert.deepEqual(result.mailbox.pending_for_lead.map(m => [m.from, m.kind]).sort(), [['bird', 'block'], ['bird', 'fact']])
  const block = result.mailbox.pending_for_lead.find(m => m.kind === 'block')
  assert.match(block.content, /报告阻塞：需等待上游产物「bike」就绪/)
  assert.deepEqual(block.refs, ['bike'])
  // 审计词汇与顺序：submit 期 fact/clarify 先入队，等待解析的 block 随后。
  const events = (await h.journal.read('root')).events.filter(e => e.type.startsWith('dpswarm/mailbox'))
  assert.deepEqual(events.filter(e => e.type === MAILBOX_EVENTS.queued).map(e => [e.data.kind, e.data.from, e.data.delivery]),
    [['fact', 'bird', 'inject'], ['clarify', 'lead', 'followup'], ['block', 'bird', 'followup']])
  assert.deepEqual(events.filter(e => e.type === MAILBOX_EVENTS.delivered).map(e => [e.data.message_id, e.data.via, e.data.carrier]),
    [['q-anchor', 'followup', 'wake-prompt']])
  // lead read 返回并确认收件箱；status 面呈现 mailbox 段（只读不确认）。
  const read = await h.controller.mailbox({ action: 'read' }, h.exec)
  assert.equal(read.inbox.length, 2)
  assert.equal(read.acknowledged, 2)
  assert.equal((await h.controller.mailbox({ action: 'read' }, h.exec)).inbox.length, 0)
  const status = await h.controller.status(h.parent)
  assert.equal(status.mailbox.available, true)
  assert.equal(status.mailbox.run_id, read.run_id)
  assert.equal(status.mailbox.pending.bird, 0)
})

test('worker tool discipline: workers address only the lead; control kinds are refused through the tool too', async () => {
  const h = controllerFixture()
  await h.run()
  const tester = h.sessions.get('child-2').session     // staged 两实现者后的 tester
  const bird = h.sessions.get('child-1').session
  await assert.rejects(h.controller.mailbox({ action: 'post', to: 'bird', kind: 'fact', content: 'hi' }, workerExec(bird)),
    { code: 'DPSWARM_MAILBOX_ROUTE_REJECTED' })
  await assert.rejects(h.controller.mailbox({ action: 'post', kind: 'approve', content: 'accept my delivery' }, workerExec(bird)),
    { code: 'DPSWARM_MAILBOX_CONTROL_REJECTED' })
  const posted = await h.controller.mailbox({ action: 'post', kind: 'fact', content: 'tester 独立结论：候选可用。' }, workerExec(tester))
  assert.equal(posted.from, 'tester')
  assert.equal(posted.delivery, 'pending')
  const read = await h.controller.mailbox({ action: 'read' }, workerExec(tester))
  assert.deepEqual([read.member, read.pending.length], ['tester', 0])
  // 未知会话（既非 root 也非在册 worker）被拒。
  await assert.rejects(h.controller.mailbox({ action: 'read' }, workerExec({ id: 'stranger', header: { delegationDepth: 1 } })),
    (err => /PARENT_IDENTITY_REQUIRED|MAILBOX_CALLER_UNKNOWN/.test(err.message)))
  // Lead 端同样拒绝控制性命令与未注册成员。
  await assert.rejects(h.controller.mailbox({ action: 'post', to: 'bird', kind: 'terminate', content: 'x' }, h.exec), { code: 'DPSWARM_MAILBOX_CONTROL_REJECTED' })
  await assert.rejects(h.controller.mailbox({ action: 'post', to: 'ghost', kind: 'fact', content: 'x' }, h.exec), { code: 'DPSWARM_MAILBOX_MEMBER_UNKNOWN' })
  await assert.rejects(h.controller.mailbox({ action: 'archive' }, h.exec), { code: 'MAILBOX_ACTION_REQUIRED' })
})

test('rework wiring: post-run lead mail rides the implementer rework continuation (rework-prompt carrier)', async () => {
  const h = controllerFixture()
  const result = await h.run()
  const bird = result.deliveries.find(d => d.subtask === 'bird')
  // run 已结：lead 给 bird 留一条 block + 一条 fact；返工延续承载它们。
  await h.controller.mailbox({ action: 'post', to: 'bird', kind: 'block', content: '上游 bike 冻结，勿改其文件。', message_id: 'b-freeze' }, h.exec)
  await h.controller.mailbox({ action: 'post', to: 'bird', kind: 'fact', content: 'bike 的锚点常量在 layout.ts。', message_id: 'f-layout' }, h.exec)
  const reworked = await h.dispatcher.rework({ item_id: bird.item_id, feedback: 'Fix the perch only.' }, h.exec)
  assert.equal(reworked.failed.length, 0)
  // children：run 三个（bike/bird/tester）+ 返工实现者（child-3）+ tester 复验（child-4）。
  assert.deepEqual(h.children.map(c => c.id), ['child-0', 'child-1', 'child-2', 'child-3', 'child-4'])
  const reworkPrompt = h.children[3].request.prompt[0].text
  assert.match(reworkPrompt, /## DPSwarm 团队邮箱（本次延续随带 2 条消息/)
  assert.match(reworkPrompt, /【block】来自 lead\n上游 bike 冻结，勿改其文件。/)
  assert.ok(reworkPrompt.includes('bike 的锚点常量在 layout.ts。'))
  const delivered = (await h.journal.read('root')).events.filter(e => e.type === MAILBOX_EVENTS.delivered)
  assert.deepEqual(delivered.map(e => [e.data.message_id, e.data.via, e.data.carrier]),
    [['b-freeze', 'followup', 'rework-prompt'], ['f-layout', 'inject', 'rework-prompt']])
  // 返工延续子会话也是合法 worker 发件人（run_id 盖邮箱注册的 run，而非 reworkId）。
  const fromRework = await h.controller.mailbox({ action: 'post', kind: 'fact', content: '返工已完成自查。' }, workerExec(h.children[3].session))
  assert.equal(fromRework.from, 'bird')
  assert.equal(fromRework.message.run_id, (await h.controller.status(h.parent)).mailbox.run_id)
})

test('storage unavailability degrades to a closed mailbox without breaking the run', async () => {
  const h = controllerFixture()
  h.controller.mailboxStorage = null        // 宿主未组合 storage：叠加层关闭
  h.markerFor = { 'child-1': 'bike' }
  const result = await h.run()              // bike 永不 ready → 等待失败路径也走通
  assert.ok(result.failed.some(f => f.code === 'ARTIFACT_WAIT_TIMEOUT'))
  assert.equal(result.mailbox, undefined)
  const status = await h.controller.status(h.parent)
  assert.equal(status.mailbox.available, false)
  await assert.rejects(h.controller.mailbox({ action: 'read' }, h.exec), { code: 'DPSWARM_MAILBOX_UNAVAILABLE' })
})
