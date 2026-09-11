// 三层交接（L2 摘要导航 / L1 逐字依据 / L0 原文回查）与验收可见性材料。
// 语义口径：Python 实验编排器 dpswarm-plugin/dpswarm/orchestrator_lg.py
// （五轮实验验证）；本文件是其确定性纯代码部分的 JS 移植。
// 插件侧有意偏差：无 CM（L2 恒为有界截断摘要）、无 PULL 通道（L0 回查 =
// 产物读门，write-scope 的 ARTIFACT_NOT_READY 对 ready 产物放行）、无
// playbook 覆盖与 LLM 判型（handoff 判型恒规则兜底，decider=rule）。

export const HANDOFF_TRUST_NOTE = '（摘要仅供导航，逐字内容以 L1/原文为准）'
//: 无 CM 时 L2 摘要层每份上游交付的截断长度（沿用 0.9.0 相位摘要口径）
export const HANDOFF_FALLBACK_CHARS = 2000
//: L1 原子事实层总量上限（每份上游交付单独计；超出按模式优先级丢弃，
//: 原文经 L0/读门可达）。逐字依赖型交接放宽到 HANDOFF_ATOMIC_CAP_VERBATIM。
export const HANDOFF_ATOMIC_CAP = 4000
export const HANDOFF_ATOMIC_CAP_VERBATIM = 8000
//: 单块大于剩余额度时的最小截断保留（低于此不截断、直接丢弃，避免碎屑）
export const HANDOFF_ATOMIC_MIN_KEEP = 200

// L1 原子事实层的确定性提取模式（纯代码，不走 LLM——逐字保真是硬要求）。
// 优先级（超上限丢弃序）：fenced 块 > schema/JSON 行 > 签名行 > 其他
// （markdown 表格行 / 配置键值 / 文件路径行 / 版本标识）。
const RE_FENCED = /```[^\n]*\r?\n[\s\S]*?```/g
const RE_FENCE_MARK = /```/g
const RE_JSON_LINE = /^[ \t]*"[\w$\-.]+"[ \t]*:[^\n]*/gm
const RE_SIGNATURE = /^[ \t]*(?:async[ \t]+def|def|class)[ \t]+\w+[^\n]*/gm
const RE_TABLE_ROW = /^[ \t]*\|[^\n]*\|[ \t]*$/gm
const RE_KV_LINE = /^[ \t]*[A-Za-z_][A-Za-z0-9_.\-]*[ \t]*=[ \t]*\S[^\n]*/gm
const RE_PATH_LINE = /^[ \t]*(?:[\w\-.]+\/)+[\w\-.]+\.\w+[ \t]*$/gm
const RE_VERSION = /\b(?:v?\d+\.\d+(?:\.\d+)+|draft-\d{4}-\d{2})\b/g

const DEFAULT_PRIORITY = { fenced: 0, json: 1, signature: 2, misc: 3 }

//: 逐字依赖信号：产物登记/任务描述命中即放宽 L1 上限并前置 L1。
//: 判定是代码的关键词探测（decider 恒 rule），语义内容仍由模型自己读。
export const VERBATIM_SIGNALS = ['逐字', '引用', '一致', 'schema', '签名', ' verbatim', 'quote']

/** 依赖类型分派（规则兜底路径）：verbatim（逐字依赖）/ semantic（语义依赖）。 */
export function handoffProfile(taskText) {
  const text = String(taskText || '')
  const lower = text.toLowerCase()
  return VERBATIM_SIGNALS.some(k => text.includes(k) || lower.includes(k)) ? 'verbatim' : 'semantic'
}

/**
 * 提取候选块 [[pos, prio, text]]，fenced 块内部不重复提取行级模式。
 * 未闭合围栏（截断交付常见）：最后一个孤立 ``` 到 EOF 按 fenced 级收。
 */
export function collectAtomicBlocks(text, priority = null) {
  const prioOf = { ...DEFAULT_PRIORITY, ...(priority || {}) }
  const spans = [], blocks = []
  for (const m of text.matchAll(RE_FENCED)) {
    spans.push([m.index, m.index + m[0].length])
    blocks.push([m.index, prioOf.fenced, m[0]])
  }
  const marks = [...text.matchAll(RE_FENCE_MARK)]
  if (marks.length % 2 === 1) {
    const start = marks.at(-1).index
    blocks.push([start, prioOf.fenced, text.slice(start).replace(/\s+$/, '') + '\n```（原交付围栏未闭合）'])
    spans.push([start, text.length])
  }
  const inside = pos => spans.some(([s, e]) => s <= pos && pos < e)
  for (const [regex, cat] of [[RE_JSON_LINE, 'json'], [RE_SIGNATURE, 'signature'],
    [RE_TABLE_ROW, 'misc'], [RE_KV_LINE, 'misc'], [RE_PATH_LINE, 'misc']]) {
    if (priority && !(cat in priority)) continue
    for (const m of text.matchAll(regex)) {
      if (!inside(m.index)) blocks.push([m.index, prioOf[cat], m[0].replace(/\s+$/, '')])
    }
  }
  if (!priority || 'misc' in priority) {
    for (const m of text.matchAll(RE_VERSION)) {
      if (!inside(m.index)) blocks.push([m.index, prioOf.misc, m[0]])
    }
  }
  return blocks
}

/**
 * L1 原子事实层：从上游交付逐字提取关键块（确定性，无 LLM 改写）。
 * 去重；选择按优先级优先（同级按原文序）、总量超 cap 时低优先先丢；单块
 * 大于剩余额度且剩余 ≥ HANDOFF_ATOMIC_MIN_KEEP 时截断保留并标注；输出恢复
 * 原文顺序。
 */
export function extractAtomicFacts(text, { cap = HANDOFF_ATOMIC_CAP, priority = null } = {}) {
  const seen = new Set(), uniq = []
  for (const [pos, prio, block] of collectAtomicBlocks(String(text || ''), priority)) {
    if (!seen.has(block)) { seen.add(block); uniq.push([pos, prio, block]) }
  }
  const selected = []
  let remaining = cap
  for (const [pos, prio, block] of uniq.sort((a, b) => a[1] - b[1] || a[0] - b[0])) {
    if (block.length <= remaining) { selected.push([pos, block]); remaining -= block.length }
    else if (remaining >= HANDOFF_ATOMIC_MIN_KEEP) {
      selected.push([pos, block.slice(0, remaining) + '\n…（超 L1 上限截断，逐字原文见 L0/读门）'])
      remaining = 0
    }
  }
  selected.sort((a, b) => a[0] - b[0])
  return selected.map(([, block]) => block).join('\n\n')
}

/**
 * staged 相位交接三层包。upstreams: [{ id, title?, text, paths? }]（前序相位
 * 的每份交付一份）。verbatim：L1 上限放宽且排在摘要前，头部带直贴契约；
 * semantic：L2 摘要在前。L0 列产物 id + 文件路径 + 读门回查指引。
 */
export function buildHandoffSection({ upstreams, profile }) {
  const cap = profile === 'verbatim' ? HANDOFF_ATOMIC_CAP_VERBATIM : HANDOFF_ATOMIC_CAP
  const l2 = `### L2 摘要层（前序相位交付摘要）${HANDOFF_TRUST_NOTE}\n`
    + upstreams.map(u => `【${u.id}】${String(u.text || '').slice(0, HANDOFF_FALLBACK_CHARS)}`).join('\n\n')
  const labeled = upstreams.map(u => [u, extractAtomicFacts(u.text, { cap })]).filter(([, facts]) => facts)
  const l1 = labeled.length
    ? '### L1 原子事实层（逐字提取；逐字引用请以此层为准）\n'
      + labeled.map(([u, facts]) => `【${u.id}】\n${facts}`).join('\n\n')
    : null
  const l0 = '### L0 原文层（产物引用，逐字原文经读门回查）\n' + upstreams.map(u =>
    `- 【${u.id}】${u.title ? `「${u.title}」` : ''}文件路径：${u.paths?.length ? u.paths.join('、') : '（未登记写范围）'}；`
    + '逐字原文：直接读取上述路径取回（ready 产物读门放行；被 ARTIFACT_NOT_READY 拒绝表示尚未就绪，先完成自己部分等待唤醒）').join('\n')
  const layers = profile === 'verbatim' && l1 ? [l1, l2, l0] : [l2, l1, l0]
  let header = `\n\n## 上游交付交接（三层：L2 摘要导航 / L1 逐字依据 / L0 原文回查）（handoff_profile=${profile}）`
  if (profile === 'verbatim') {
    // Python r4 实证：verbatim 下游凭摘要/L1 记忆复述而不取原文——逐字保真
    // 最后一厘米靠显式契约（prompt 层，不改机制语义）。
    header += '\n\n**逐字内容禁止凭记忆复述**：交付中需要逐字引用上游内容时，必须先读取上游产物原文'
      + '（L0 层列有产物 id 与文件路径，ready 产物经读门放行），再逐字直贴；L1 层仅供定位预览。'
  }
  return header + '\n\n' + layers.filter(Boolean).join('\n\n')
}

//: 验收可见性：每份上游 ready 交付的逐字上限与总量上限（对齐 Python
//: orchestrator._upstream_evidence 的 4000/8000）；超限时降级为 L1 逐字段 +
//: 路径引用（插件侧无 submission_package 引用，回查路径 = 文件路径 +
//: dpswarm_report 分页）。
export const REVIEW_UPSTREAM_PER_DELIVERY_CAP = 4000
export const REVIEW_UPSTREAM_TOTAL_CAP = 8000
export const ACCEPTANCE_VISIBILITY_RULE = '跨产物依赖约束必须对照上游交付逐字核验；缺材料不可验收通过。'

/**
 * 验收可见性材料。entries: [{ id, title?, deps: [{ id, title?, state, paths?,
 * item_id?, text? }] }]；dep.state ∈ ready/frozen/done 且有交付文本 → 逐字
 * 携带（超上限降级 L1 逐字段 + 路径/报告引用）；否则记缺材料行。返回 ''
 * 表示没有任何带 deps 的产物。
 */
export function buildAcceptanceVisibility(entries, {
  perCap = REVIEW_UPSTREAM_PER_DELIVERY_CAP, totalCap = REVIEW_UPSTREAM_TOTAL_CAP,
} = {}) {
  const sections = []
  let remaining = totalCap
  for (const entry of entries) {
    if (!entry.deps?.length) continue
    const lines = [`### 产物「${entry.title || entry.id}」（${entry.id}）的上游依赖核验材料`
      + (entry.profile === 'verbatim' ? '——该产物判型为逐字依赖（verbatim），引用一致性须逐字核对' : '')]
    for (const dep of entry.deps) {
      const ref = `【${dep.id}】${dep.title ? `「${dep.title}」` : ''}（状态 ${dep.state || 'unknown'}；`
        + `路径：${dep.paths?.length ? dep.paths.join('、') : '（未登记写范围）'}`
        + `${dep.item_id ? `；交付全文：dpswarm_report("${dep.item_id}") 分页读取` : ''}）`
      const text = typeof dep.text === 'string' && dep.text ? dep.text : ''
      if (!['ready', 'frozen', 'done'].includes(dep.state) || !text) {
        lines.push(`- ${ref}交付内容不可见——缺材料不可验收通过`)
        continue
      }
      if (text.length <= Math.min(perCap, remaining)) {
        lines.push(`- ${ref}逐字交付：\n${text}`)
        remaining -= text.length
        continue
      }
      const facts = remaining >= HANDOFF_ATOMIC_MIN_KEEP
        ? extractAtomicFacts(text, { cap: Math.min(perCap, remaining) }) : ''
      if (facts) {
        lines.push(`- ${ref}超上限，L1 逐字段（仅供定位；逐字原文读上述路径/报告）：\n${facts}`)
        remaining -= facts.length
      } else if (remaining >= HANDOFF_ATOMIC_MIN_KEEP) {
        const keep = Math.min(perCap, remaining)
        lines.push(`- ${ref}逐字交付（截断；全文读上述路径/报告）：\n${text.slice(0, keep)}\n…（截断）`)
        remaining -= keep
      } else {
        lines.push(`- ${ref}超总量上限，仅列引用——逐字原文读上述路径/报告`)
      }
    }
    sections.push(lines.join('\n'))
  }
  if (!sections.length) return ''
  return `\n\n## 验收可见性（上游依赖交付；${ACCEPTANCE_VISIBILITY_RULE}）\n` + sections.join('\n\n')
}
