/**
 * dsh-session ≥0.1.5 宿主兼容读取层（机制冻结轮的宿主适配，不改变任何语义）：
 * 新宿主 Session 删除了 `events` getter（改 `snapshotEvents()`），fork 边界从
 * `header.seedLength` 迁到构造参数与实例字段 `inheritedEventCount`（公开读法
 * `ownEvents()`）。测试假件仍是带 `events` 数组的裸对象——两条路径都要工作。
 */

/** 会话事件数组：裸对象假件优先 `events`，真宿主 Session 走 `snapshotEvents()`。 */
export const sessionEvents = session => {
  if (Array.isArray(session?.events)) return session.events
  if (typeof session?.snapshotEvents === 'function') return session.snapshotEvents()
  return []
}

/** fork 继承前缀长度：旧宿主 header.seedLength 优先，新宿主实例字段兜底，缺省 0。 */
export const forkBoundary = session => {
  if (Number.isSafeInteger(session?.header?.seedLength) && session.header.seedLength >= 0) return session.header.seedLength
  if (Number.isSafeInteger(session?.inheritedEventCount) && session.inheritedEventCount >= 0) return session.inheritedEventCount
  return 0
}

/**
 * dsh-session ≥0.1.5 的 canonicalHeader 丢弃 header.system（系统提示改走
 * system/message 面事件）。探测一次：宿主仍在 header 里携带 system 时，调用
 * 方不得再显式补价（避免双重计价）；宿主丢弃时必须显式补价以保持机制语义
 * （系统提示始终参与窗口压力/额度测算）。
 * @param canonicalHeader - 从宿主 dsh-session 导入的 canonicalHeader。
 */
export const headerPricesSystem = canonicalHeader => {
  if (headerPricesSystem.cache === undefined) {
    headerPricesSystem.cache = canonicalHeader({ config: { provider: 'dpswarm-probe', model: 'm' }, system: 'dpswarm-probe' }).system !== undefined
  }
  return headerPricesSystem.cache
}

/** 系统提示文本的固定启发式计价（走 tokenMeter.estimateMessage，不可用时 0）。 */
export const systemMessageTokens = (meter, systemText) => {
  if (!systemText || typeof meter?.estimateMessage !== 'function') return 0
  const tokens = meter.estimateMessage({ role: 'system', content: [{ type: 'text', text: systemText }] })
  return Number.isSafeInteger(tokens) && tokens > 0 ? tokens : 0
}

/** Last effective native system text, in surface order (replacement seqs need
 * not be monotonic). Empty tail nodes do not override an earlier prompt. */
export function retainedSystemText(session) {
  const bySeq = new Map(sessionEvents(session).map(event => [event.seq, event]))
  const texts = (session?.surface?.nodes || []).map(seq => bySeq.get(seq))
    .filter(event => event?.type === 'system/message')
    .map(event => (event.data?.message?.content || []).filter(block => block?.type === 'text').map(block => block.text).join('\n'))
  return texts.findLast(text => text !== '') || ''
}

/** Measure before the host commits its system projection. The prepared route's
 * in-history capability is not available at pre-step or agent/request time.
 * An unchanged effective prompt is already priced by the surface. A changed
 * prompt is conservatively priced as an append: exact for in-history updates,
 * an upper bound when native projection instead replaces/clears earlier nodes.
 * Never reuse an observed usage anchor after the effective system changes. */
export function measureSystemRequest(meter, session, header, systemText, canonicalHeader) {
  const measured = meter?.measure?.(session, header)
  if (!measured || headerPricesSystem(canonicalHeader)) return measured
  const changed = retainedSystemText(session) !== (systemText || '')
  let basis = measured
  if (changed && measured.baseline?.kind === 'usage') {
    // Use the same public meter on a detached empty native session to price
    // only the effective tool envelope; do not invent header fields to evade
    // the usage anchor or mutate the live session's surface.
    const empty = session?.constructor?.create?.('dpswarm-system-price')
    if (!empty || !Number.isSafeInteger(measured.surfaceTokens)) throw new Error('SYSTEM_PRESSURE_ESTIMATE_UNAVAILABLE')
    const tools = meter.measure(empty, header)?.totalTokens
    if (!Number.isSafeInteger(tools) || tools < 0) throw new Error('SYSTEM_PRESSURE_ESTIMATE_UNAVAILABLE')
    const tokens = measured.surfaceTokens + tools
    basis = { ...measured, totalTokens: tokens, surfaceDeltaTokens: 0, baseline: { kind: 'estimated', tokens } }
  }
  const pending = changed ? systemMessageTokens(meter, systemText) : 0
  return pending ? { ...basis, totalTokens: basis.totalTokens + pending } : basis
}

/** Keep every retained system message for possible in-history continuation;
 * only omit the separate serialized system field when it is already present. */
export function pendingSystemText(messages, rendered) {
  const texts = (messages || []).filter(message => message?.role === 'system')
    .map(message => (message.content || []).filter(block => block?.type === 'text').map(block => block.text).join('\n'))
  const retained = texts.findLast(text => text !== '') || ''
  return retained === (rendered || '') ? '' : rendered || ''
}
