import { createHash, randomUUID } from 'node:crypto'

/**
 * 有界持久 mailbox（借鉴项④）：Lead↔worker 直系消息通道的准入、有界、
 * 持久化与投递路由。有界口径照抄宿主 experimental team mailbox
 * （maxPendingMessagesPerMember 默认 64 超限即拒、单条 64KiB 上限）；
 * 投递语义对齐宿主 agent 收件箱：fact → inject（静默注入不唤醒）、
 * clarify/block → followup（排队并唤醒）。控制性决策（改契约/扩权限/批准
 * 交付）不进此通道，只走控制面工具。
 */

export const MAILBOX_KINDS = Object.freeze(['fact', 'clarify', 'block'])
/** 每类消息的投递语义：inject 不唤醒，followup 排队并唤醒。 */
export const DELIVERY_BY_KIND = Object.freeze({ fact: 'inject', clarify: 'followup', block: 'followup' })
export const MAX_PENDING_PER_MEMBER = 64
export const MAX_MESSAGE_BYTES = 64 * 1024

export const MAILBOX_EVENTS = Object.freeze({
  queued: 'dpswarm/mailbox-queued',
  delivered: 'dpswarm/mailbox-delivered',
  rejected: 'dpswarm/mailbox-rejected',
})

/** 控制性意图词表：经 mailbox 出现即拒，指向控制面工具。 */
const CONTROL_KINDS = new Set(['command', 'control', 'approve', 'accept', 'terminate', 'reject', 'rework', 'review',
  'grant', 'revoke', 'authorize', 'override', 'expand', 'permission', 'contract', 'scope', 'budget', 'route', 'model',
  'submit', 'deliver'])

const MEMBER_ID_RE = /^[\w.-]{1,64}$/
const MESSAGE_ID_RE = /^[\w.-]{1,128}$/
const CONTROL_REFUSAL = 'Control decisions (contract changes, permission expansion, delivery approval) travel only through the control-plane tools (dpswarm_run / dpswarm_rework / dpswarm_review), never the mailbox'
const failure = (code, message) => Object.assign(new Error(`${code}: ${message}`), { code })
const clone = value => JSON.parse(JSON.stringify(value))
const byteLength = value => Buffer.byteLength(JSON.stringify(value), 'utf8')

/** KV 记录键：根会话 id 直接可用则原样，否则 sanitize + 短哈希防碰撞。 */
export function storageKey(rootId) {
  const raw = String(rootId)
  if (/^[A-Za-z0-9_-]{1,64}$/.test(raw)) return raw
  const digest = createHash('sha256').update(raw).digest('hex').slice(0, 16)
  return `${raw.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 32)}-${digest}`
}

function validateMessage(message, members) {
  if (!message || typeof message !== 'object' || Array.isArray(message)) {
    throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'A mailbox message is an object with run_id, from, to, kind, content and optional refs/message_id')
  }
  const allowed = ['message_id', 'run_id', 'from', 'to', 'kind', 'content', 'refs']
  if (Object.keys(message).some(key => !allowed.includes(key))) {
    throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', `Only ${allowed.join(', ')} are accepted`)
  }
  if (typeof message.run_id !== 'string' || !message.run_id.trim() || message.run_id.length > 128) {
    throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'run_id must reference the owning dpswarm run')
  }
  if (message.message_id !== undefined && (typeof message.message_id !== 'string' || !MESSAGE_ID_RE.test(message.message_id))) {
    throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'message_id must match [A-Za-z0-9_.-] within 128 characters (deduplication key)')
  }
  for (const key of ['from', 'to']) {
    const value = message[key]
    if (typeof value !== 'string' || !MEMBER_ID_RE.test(value)) throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', `${key} must be a member id matching [A-Za-z0-9_.-]{1,64}`)
    if (!members.has(value)) throw failure('DPSWARM_MAILBOX_MEMBER_UNKNOWN', `${key} "${value}" is not a member of this run (registered: ${[...members].join(', ')})`)
  }
  if (message.from === message.to) throw failure('DPSWARM_MAILBOX_SELF_MESSAGE', 'A member cannot message itself')
  if (typeof message.kind !== 'string') throw failure('DPSWARM_MAILBOX_KIND_INVALID', `kind must be one of ${MAILBOX_KINDS.join(' | ')}`)
  if (CONTROL_KINDS.has(message.kind.toLowerCase())) throw failure('DPSWARM_MAILBOX_CONTROL_REJECTED', CONTROL_REFUSAL)
  if (!MAILBOX_KINDS.includes(message.kind)) throw failure('DPSWARM_MAILBOX_KIND_INVALID', `kind must be one of ${MAILBOX_KINDS.join(' | ')}; got "${message.kind}"`)
  if (typeof message.content !== 'string' || !message.content.trim()) throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'content must be nonempty text')
  if (message.content.length > 32000) throw failure('DPSWARM_MESSAGE_TOO_LARGE', `content is ${message.content.length} characters; the per-message bound is 64 KiB framed`)
  let refs = []
  if (message.refs !== undefined) {
    if (!Array.isArray(message.refs) || message.refs.length > 16
      || message.refs.some(ref => typeof ref !== 'string' || !ref.trim() || ref.length > 256 || /[\u0000-\u001f]/.test(ref))) {
      throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'refs must be up to 16 short reference strings (artifact ids, item ids or paths)')
    }
    refs = [...message.refs]
  }
  return { message_id: message.message_id || `m-${randomUUID()}`, run_id: message.run_id.trim(),
    from: message.from, to: message.to, kind: message.kind, content: message.content, refs }
}

/** 投递进延续 prompt 的有界段落（FIFO 队列序）。 */
export function renderMailboxSection(messages) {
  if (!messages.length) return ''
  const lines = messages.map(m => `【${m.kind}】来自 ${m.from}${m.refs?.length ? `（引用：${m.refs.join('、')}）` : ''}\n${m.content}`)
  return `\n\n## DPSwarm 团队邮箱（本次延续随带 ${messages.length} 条消息；fact=事实更新（静默）、clarify=澄清请求（已随唤醒送达）、block=阻塞通知（已随唤醒送达））\n${lines.join('\n\n')}`
}

/** 返回体/状态用的有界消息摘要。 */
export function compactMailboxEntry(message, { content = 2000 } = {}) {
  return { message_id: message.message_id, run_id: message.run_id, from: message.from, to: message.to,
    kind: message.kind, refs: message.refs || [], ts: message.ts ?? null,
    content: typeof message.content === 'string' && message.content.length > content
      ? message.content.slice(0, content) + '…[truncated]' : message.content }
}

export class TeamMailbox {
  /**
   * @param storage KV 单元形状 { loadAll(), putRecord(table,key,value), deleteRecord(table,key) }；
   *   单调用原子，跨调用串行化由本类按根会话链式保证。
   * @param journal 审计账本形状 { append(rootId, type, data) }；审计失败按 worker 诊断
   *   先例收进结果字段，不制造虚假成功也不阻断机制。
   */
  constructor({ storage, journal, limits = {}, now = () => Date.now() } = {}) {
    if (!storage || typeof storage.loadAll !== 'function' || typeof storage.putRecord !== 'function') {
      throw failure('DPSWARM_MAILBOX_STORAGE_UNAVAILABLE', 'The mailbox requires a durable KV storage unit')
    }
    this.storage = storage
    this.journal = journal || null
    this.limits = { maxPendingPerMember: limits.maxPendingPerMember ?? MAX_PENDING_PER_MEMBER,
      maxMessageBytes: limits.maxMessageBytes ?? MAX_MESSAGE_BYTES }
    this.now = now
    this.chains = new Map()
    this.channels = new Map()
  }

  chained(rootId, operation) {
    const prior = this.chains.get(rootId) || Promise.resolve()
    const current = prior.catch(() => undefined).then(operation)
    this.chains.set(rootId, current)
    return current.finally(() => { if (this.chains.get(rootId) === current) this.chains.delete(rootId) })
  }

  async _load(rootId) {
    const snapshot = await this.storage.loadAll()
    const record = snapshot?.tables?.messages?.[storageKey(rootId)]
    if (record === undefined || record === null) return { version: 1, root_session_id: rootId, run: null, messages: [] }
    if (record.version !== 1 || record.root_session_id !== rootId || !Array.isArray(record.messages) || !record.run) {
      throw failure('DPSWARM_MAILBOX_STORAGE_CORRUPT', 'The stored mailbox record for this session is unreadable')
    }
    return record
  }

  async _store(rootId, record) {
    await this.storage.putRecord('messages', storageKey(rootId), record)
  }

  async _audit(rootId, type, data) {
    if (!this.journal) return null
    try { await this.journal.append(rootId, type, data); return null }
    catch (error) {
      return { code: error?.code || 'MAILBOX_AUDIT_FAILED', message: String(error?.message ?? error) }
    }
  }

  /** 注册一次运行的成员表（'lead' 恒在）。新 run_id 接管时旧 run 未投递消息按 RUN_SUPERSEDED 拒绝留痕。 */
  register(rootId, { run_id, members }) {
    if (typeof run_id !== 'string' || !run_id.trim() || run_id.length > 128) {
      throw failure('DPSWARM_MAILBOX_RUN_INVALID', 'register needs the owning run_id')
    }
    const ids = new Set(['lead'])
    const normalized = [{ id: 'lead', role: 'lead' }]
    for (const member of members || []) {
      if (!member || typeof member !== 'string' && typeof member?.id !== 'string') {
        throw failure('DPSWARM_MAILBOX_MEMBER_INVALID', 'members need unique ids matching [A-Za-z0-9_.-]{1,64}')
      }
      const id = typeof member === 'string' ? member : member.id
      if (!MEMBER_ID_RE.test(id) || ids.has(id)) throw failure('DPSWARM_MAILBOX_MEMBER_INVALID', `member "${id}" must be unique and match [A-Za-z0-9_.-]{1,64}`)
      ids.add(id)
      normalized.push(typeof member === 'string' ? { id, role: 'worker' } : { id, role: member.role || 'worker', ...(member.subtask ? { subtask: member.subtask } : {}) })
    }
    return this.chained(rootId, async () => {
      const record = await this._load(rootId)
      if (record.run && record.run.run_id !== run_id) {
        const stale = record.messages.filter(m => !m.delivered)
        record.messages = record.messages.filter(m => m.delivered)
        for (const message of stale) {
          await this._audit(rootId, MAILBOX_EVENTS.rejected, { version: 1, root_session_id: rootId,
            run_id: message.run_id, message_id: message.message_id, from: message.from, to: message.to,
            kind: message.kind, code: 'DPSWARM_MAILBOX_RUN_SUPERSEDED',
            detail: `run ${record.run.run_id} was superseded by ${run_id}; undelivered mail dropped`, at: this.now() })
        }
      }
      record.run = { run_id, members: normalized, registered_at: this.now() }
      await this._store(rootId, record)
      return { run_id, members: normalized.map(m => m.id) }
    })
  }

  /** 邮箱当前归属的 run_id（调用方给消息盖同源 run 戳用）。 */
  async runOf(rootId) {
    return (await this._load(rootId)).run?.run_id ?? null
  }

  post(rootId, message) {
    return this.chained(rootId, () => this._post(rootId, message))
  }

  async _post(rootId, raw) {
    const record = await this._load(rootId)
    if (!record.run) throw failure('DPSWARM_MAILBOX_UNAVAILABLE', 'No run is registered for this mailbox; the Lead must start a dpswarm_run first')
    let message
    try {
      message = validateMessage(raw, new Set(record.run.members.map(m => m.id)))
    } catch (error) {
      // 校验拒绝（含控制性命令）同样留痕：调用方字段不全时置 null。
      await this._audit(rootId, MAILBOX_EVENTS.rejected, { version: 1, root_session_id: rootId,
        run_id: typeof raw?.run_id === 'string' ? raw.run_id : null,
        message_id: typeof raw?.message_id === 'string' ? raw.message_id : null,
        from: typeof raw?.from === 'string' ? raw.from : null, to: typeof raw?.to === 'string' ? raw.to : null,
        kind: typeof raw?.kind === 'string' ? raw.kind : null,
        code: error.code || 'DPSWARM_MAILBOX_MESSAGE_INVALID', detail: String(error.message ?? error), at: this.now() })
      throw error
    }
    if (message.run_id !== record.run.run_id) {
      throw failure('DPSWARM_MAILBOX_RUN_MISMATCH', `message run_id ${message.run_id} does not match the active run ${record.run.run_id}`)
    }
    const existing = record.messages.find(m => m.message_id === message.message_id)
    if (existing) return { deduplicated: true, message: clone(existing), delivery: existing.delivered ? 'delivered' : 'pending' }
    const pendingForTarget = record.messages.filter(m => m.to === message.to && !m.delivered).length
    if (pendingForTarget >= this.limits.maxPendingPerMember) {
      await this._audit(rootId, MAILBOX_EVENTS.rejected, { version: 1, root_session_id: rootId, run_id: message.run_id,
        message_id: message.message_id, from: message.from, to: message.to, kind: message.kind,
        code: 'DPSWARM_MAILBOX_FULL', detail: `"${message.to}" already holds ${pendingForTarget} pending messages (limit ${this.limits.maxPendingPerMember})`, at: this.now() })
      throw failure('DPSWARM_MAILBOX_FULL', `mailbox for "${message.to}" already holds ${pendingForTarget} pending messages (limit ${this.limits.maxPendingPerMember}); deliver or acknowledge them first`)
    }
    const stored = { ...message, ts: this.now(), delivered: false, delivered_at: null, delivered_via: null, delivered_carrier: null }
    const framed = byteLength(stored)
    if (framed > this.limits.maxMessageBytes) {
      await this._audit(rootId, MAILBOX_EVENTS.rejected, { version: 1, root_session_id: rootId, run_id: message.run_id,
        message_id: message.message_id, from: message.from, to: message.to, kind: message.kind,
        code: 'DPSWARM_MESSAGE_TOO_LARGE', detail: `frames to ${framed} bytes (limit ${this.limits.maxMessageBytes})`, at: this.now() })
      throw failure('DPSWARM_MESSAGE_TOO_LARGE', `message frames to ${framed} bytes; the limit is ${this.limits.maxMessageBytes}`)
    }
    record.messages.push(stored)
    await this._store(rootId, record)
    const auditError = await this._audit(rootId, MAILBOX_EVENTS.queued, { version: 1, root_session_id: rootId,
      run_id: stored.run_id, message_id: stored.message_id, from: stored.from, to: stored.to, kind: stored.kind,
      delivery: DELIVERY_BY_KIND[stored.kind], refs: stored.refs, ts: stored.ts,
      pending_for_target: pendingForTarget + 1, at: this.now() })
    // 在场成员（宿主 continuable 子会话）立即投递：fact→inject 静默、clarify/block→followup 唤醒。
    const channel = this.channels.get(`${rootId}::${stored.to}`)
    let deliveryError = null
    if (channel) {
      try {
        await channel[DELIVERY_BY_KIND[stored.kind]](stored)
        stored.delivered = true
        stored.delivered_at = this.now()
        stored.delivered_via = DELIVERY_BY_KIND[stored.kind]
        stored.delivered_carrier = 'live-channel'
        await this._store(rootId, record)
        const deliveredAudit = await this._audit(rootId, MAILBOX_EVENTS.delivered, { version: 1, root_session_id: rootId,
          run_id: stored.run_id, message_id: stored.message_id, from: stored.from, to: stored.to, kind: stored.kind,
          via: stored.delivered_via, carrier: stored.delivered_carrier, at: this.now() })
        return { deduplicated: false, message: clone(stored), delivery: 'delivered',
          ...(deliveredAudit ? { audit_error: deliveredAudit } : {}), ...(auditError ? { audit_error: auditError } : {}) }
      } catch (error) {
        // 投递失败不影响已排队事实：消息保持 pending，错误结构化回给调用方。
        deliveryError = { code: error?.code || 'MAILBOX_CHANNEL_FAILED', message: String(error?.message ?? error) }
      }
    }
    return { deduplicated: false, message: clone(stored), delivery: 'pending',
      ...(deliveryError ? { delivery_error: deliveryError } : {}), ...(auditError ? { audit_error: auditError } : {}) }
  }

  /** 未投递消息（FIFO），只读。 */
  async pendingFor(rootId, memberId) {
    if (typeof memberId !== 'string' || !MEMBER_ID_RE.test(memberId)) throw failure('DPSWARM_MAILBOX_MEMBER_INVALID', 'memberId must match [A-Za-z0-9_.-]{1,64}')
    const record = await this._load(rootId)
    return record.messages.filter(m => m.to === memberId && !m.delivered).map(clone)
  }

  /**
   * 延续派发（唤醒/返工）时排空目标成员的 pending：按投递语义盖 via
   * （fact=inject、clarify/block=followup），carrier 记录承载通道，返回可
   * 直接拼进 prompt 的段落。标记投递即消费——延续派发失败对 Lead 可见，
   * 由 Lead 决定重发。
   */
  drain(rootId, memberId, carrier) {
    if (typeof carrier !== 'string' || !carrier.trim()) throw failure('DPSWARM_MAILBOX_CARRIER_REQUIRED', 'drain needs the continuation carrier (wake-prompt | rework-prompt)')
    return this.chained(rootId, async () => {
      const record = await this._load(rootId)
      const pending = record.messages.filter(m => m.to === memberId && !m.delivered)
      if (!pending.length) return { text: '', delivered: 0, audit_errors: [] }
      for (const message of pending) {
        message.delivered = true
        message.delivered_at = this.now()
        message.delivered_via = DELIVERY_BY_KIND[message.kind]
        message.delivered_carrier = carrier
      }
      await this._store(rootId, record)
      const auditErrors = []
      for (const message of pending) {
        const auditError = await this._audit(rootId, MAILBOX_EVENTS.delivered, { version: 1, root_session_id: rootId,
          run_id: message.run_id, message_id: message.message_id, from: message.from, to: message.to,
          kind: message.kind, via: message.delivered_via, carrier, at: this.now() })
        if (auditError) auditErrors.push(auditError)
      }
      return { text: renderMailboxSection(pending), delivered: pending.length, audit_errors: auditErrors }
    })
  }

  /** Lead 读取收件箱后的确认（mailbox-delivered via=acknowledge）。 */
  acknowledge(rootId, messageIds) {
    if (!Array.isArray(messageIds) || messageIds.some(id => typeof id !== 'string')) {
      throw failure('DPSWARM_MAILBOX_MESSAGE_INVALID', 'acknowledge needs an array of message ids')
    }
    return this.chained(rootId, async () => {
      const record = await this._load(rootId)
      const ids = new Set(messageIds)
      const affected = record.messages.filter(m => ids.has(m.message_id) && !m.delivered)
      for (const message of affected) {
        message.delivered = true
        message.delivered_at = this.now()
        message.delivered_via = 'acknowledge'
        message.delivered_carrier = 'lead-read'
      }
      if (affected.length) await this._store(rootId, record)
      const auditErrors = []
      for (const message of affected) {
        const auditError = await this._audit(rootId, MAILBOX_EVENTS.delivered, { version: 1, root_session_id: rootId,
          run_id: message.run_id, message_id: message.message_id, from: message.from, to: message.to,
          kind: message.kind, via: 'acknowledge', carrier: 'lead-read', at: this.now() })
        if (auditError) auditErrors.push(auditError)
      }
      return { acknowledged: affected.length, audit_errors: auditErrors }
    })
  }

  /** 状态面：run、成员、每成员 pending 计数。 */
  async status(rootId) {
    const record = await this._load(rootId)
    if (!record.run) return { run_id: null, members: [], pending: {} }
    const pending = {}
    for (const member of record.run.members) pending[member.id] = 0
    for (const message of record.messages) if (!message.delivered && message.to in pending) pending[message.to] += 1
    return { run_id: record.run.run_id, members: record.run.members.map(m => m.id), pending }
  }

  /** 挂接在场成员的投递通道（宿主 continuable 子会话：inject 静默 / followup 唤醒）。 */
  attachChannel(rootId, memberId, channel) {
    if (!channel || typeof channel.inject !== 'function' || typeof channel.followup !== 'function') {
      throw failure('DPSWARM_MAILBOX_CHANNEL_INVALID', 'a live channel needs async inject(message) and followup(message)')
    }
    this.channels.set(`${rootId}::${memberId}`, channel)
    return () => this.detachChannel(rootId, memberId)
  }

  detachChannel(rootId, memberId) { this.channels.delete(`${rootId}::${memberId}`) }
}

/** 宿主 ctx.storage KV 单元描述（json backend，single 布局整单元一份文档）。 */
export const MAILBOX_KV_UNIT = Object.freeze({ name: 'dpswarm_mailbox', version: 1, tables: ['messages'], hasGlobal: false, layout: 'single' })

/**
 * ctx.storage KV 适配器：懒打开一次单元，prefer 注册名 'json'，否则取第一个
 * 带 kv facet 的 backend。宿主未组合 storage 时 available() 为 false，控制器
 * 降级关闭 mailbox（叠加层不阻断运行）；storage 在场而写失败则响亮失败。
 */
export class KvMailboxStorage {
  constructor(ctx) {
    this.ctx = ctx
    this.unit = null
  }

  available() {
    const hub = this.ctx?.storage
    return Boolean(hub?.backend && typeof hub.backend.get === 'function' && (hub.backend.names?.().length || 0) > 0)
  }

  async _unit() {
    if (this.unit) return this.unit
    const hub = this.ctx?.storage
    if (!hub?.backend || typeof hub.backend.get !== 'function') {
      throw failure('DPSWARM_MAILBOX_STORAGE_UNAVAILABLE', 'The host storage hub is not composed; the persistent mailbox cannot run')
    }
    const names = hub.backend.names?.() || []
    const order = ['json', ...names.filter(name => name !== 'json')]
    let last = null
    for (const name of order) {
      let backend = null
      try { backend = hub.backend.get(name) } catch (error) { last = error; continue }
      if (backend?.kv) {
        this.unit = await backend.kv.open(MAILBOX_KV_UNIT)
        return this.unit
      }
    }
    throw failure('DPSWARM_MAILBOX_STORAGE_UNAVAILABLE', `No storage backend with a KV facet is registered (${last ? String(last?.message ?? last) : 'none'})`)
  }

  async loadAll() { return (await this._unit()).loadAll() }
  async putRecord(table, key, value) { return (await this._unit()).putRecord(table, key, value) }
  async deleteRecord(table, key) { return (await this._unit()).deleteRecord(table, key) }
}
