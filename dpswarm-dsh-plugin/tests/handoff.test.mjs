import assert from 'node:assert/strict'
import test from 'node:test'
import {
  HANDOFF_ATOMIC_CAP, HANDOFF_ATOMIC_CAP_VERBATIM, HANDOFF_ATOMIC_MIN_KEEP,
  HANDOFF_FALLBACK_CHARS, HANDOFF_TRUST_NOTE,
  buildAcceptanceVisibility, buildHandoffSection, collectAtomicBlocks,
  extractAtomicFacts, handoffProfile,
} from '../lib/handoff.js'

// 语义口径：dpswarm-plugin/dpswarm/orchestrator_lg.py + test_orchestrator_lg.py
// （Python 五轮实验验证项的 JS 移植钉死）。

test('handoffProfile: 逐字依赖信号命中判 verbatim，其余 semantic（规则兜底恒 rule）', () => {
  assert.equal(handoffProfile('任务B：逐字引用 A 的 schema 实现校验器'), 'verbatim')
  assert.equal(handoffProfile('需要与上游字段保持一致'), 'verbatim')       // 一致
  assert.equal(handoffProfile('实现给定签名的封装'), 'verbatim')           // 签名
  assert.equal(handoffProfile('the doc must quote the contract'), 'verbatim')
  assert.equal(handoffProfile('a verbatim copy is required'), 'verbatim')  // " verbatim"
  assert.equal(handoffProfile('根据 A 的结论撰写报告'), 'semantic')
  assert.equal(handoffProfile(''), 'semantic')
  assert.equal(handoffProfile(null), 'semantic')
  // 与 Python 同语义：' verbatim' 信号带前导空格，词首紧邻文本不在词表。
  assert.equal(handoffProfile('verbatim'), 'semantic')
})

test('extractAtomicFacts: fenced 块逐字保真，签名/JSON 行提取，fenced 内部不重复提取', () => {
  const fenced = '```json\n{"type": "object", "required": ["id"]}\n```'
  const text = `说明段落\n\ndef validate_record(record, schema):\n    pass\n\n${fenced}\n\n其余 prose`
  const facts = extractAtomicFacts(text)
  assert.ok(facts.includes(fenced), 'fenced 代码块逐字进入 L1')
  assert.ok(facts.includes('def validate_record(record, schema):'), '函数签名行进入 L1')
  assert.equal(facts.match(/"required"/g)?.length ?? 0, 1, 'fenced 内的 JSON 键不被行级模式重复提取')
})

test('extractAtomicFacts: 未闭合围栏（截断交付）按 fenced 级收到 EOF 并标注', () => {
  const text = 'intro prose\n```json\n{"a": 1}\n尾巴没有闭合'
  const facts = extractAtomicFacts(text)
  assert.ok(facts.includes('```（原交付围栏未闭合）'))
  assert.ok(facts.includes('{"a": 1}'))
  assert.ok(!facts.includes('intro prose'), '围栏前的散文不进 L1')
  // 未闭合尾巴内的内容不被行级模式二次提取
  assert.equal(facts.match(/"a": 1/g)?.length ?? 0, 1)
})

test('extractAtomicFacts: 去重、选择按优先级、输出恢复原文顺序', () => {
  const fenced = '```json\n{"a": 1}\n```'
  const text = `"required": ["id"]\n"required": ["id"]\n\ndef check(x):\n\n${fenced}`
  const facts = extractAtomicFacts(text)
  assert.equal(facts.match(/"required": \["id"\]/g).length, 1, '相同块去重')
  // 原文顺序：json 行 → 签名 → fenced（尽管 fenced 优先级最高）
  assert.ok(facts.indexOf('"required"') < facts.indexOf('def check(x):'))
  assert.ok(facts.indexOf('def check(x):') < facts.indexOf(fenced))
})

test('extractAtomicFacts: 超上限按优先级丢弃（misc 先丢），截断保留带标注', () => {
  const fenced = '```json\n{"a": 1}\n```'            // 20
  const json = '"required": ["id"]'                  // 18
  const sig = 'def validate(record):'                // 21
  const misc = '| col | val |'                       // 13
  const text = `${misc}\nv1.2.3\n${json}\n${sig}\n\n${fenced}`
  const cap = fenced.length + json.length + sig.length
  const facts = extractAtomicFacts(text, { cap })
  assert.ok(facts.includes(fenced) && facts.includes(json) && facts.includes(sig), 'fenced/json/签名优先保留')
  assert.ok(!facts.includes(misc) && !facts.includes('v1.2.3'), 'misc（表格行/版本号）先丢')
  // 单块大于剩余额度且剩余 ≥ MIN_KEEP：截断保留并标注
  const big = '```py\n' + 'x'.repeat(500) + '\n```'
  const truncated = extractAtomicFacts(big, { cap: 300 })
  assert.ok(truncated.includes('…（超 L1 上限截断，逐字原文见 L0/读门）'))
  assert.ok(truncated.length <= 300 + '\n…（超 L1 上限截断，逐字原文见 L0/读门）'.length)
  // 剩余 < MIN_KEEP 且单块大于剩余：不截断、直接丢弃（避免碎屑）
  const first = '```py\n' + 'y'.repeat(100) + '\n```'
  const longRow = '| ' + 'a'.repeat(250) + ' |'
  const dropped = extractAtomicFacts(`${first}\n\n${longRow}`, { cap: first.length + HANDOFF_ATOMIC_MIN_KEEP - 1 })
  assert.ok(dropped.includes(first))
  assert.ok(!dropped.includes(longRow), '剩余 199 < MIN_KEEP 且 254 字表格行放不进 → 整块丢弃')
})

test('buildHandoffSection: semantic 保持 L2 摘要在前，三层齐备且措辞不含"不可信摘要"', () => {
  const section = buildHandoffSection({ profile: 'semantic', upstreams: [
    { id: 'layout', title: '布局契约', text: '契约正文\n```json\n{"a": 1}\n```\n' + '长'.repeat(3000), paths: ['src/layout/**'] },
  ] })
  assert.match(section, /## 上游交付交接（三层：L2 摘要导航 \/ L1 逐字依据 \/ L0 原文回查）（handoff_profile=semantic）/)
  assert.ok(section.indexOf('### L2 摘要层') < section.indexOf('### L1 原子事实层'), 'semantic：L2 在 L1 前')
  assert.ok(section.indexOf('### L1 原子事实层') < section.indexOf('### L0 原文层'), 'L0 兜底在最后')
  assert.match(section, /前序相位交付摘要/, '保留插件 L2 摘要定位词（既有表征兼容）')
  assert.ok(section.includes(HANDOFF_TRUST_NOTE), '摘要仅供导航措辞')
  assert.doesNotMatch(section, /不可信/, '"不可信摘要"措辞退役')
  assert.doesNotMatch(section, /禁止凭记忆复述/, 'semantic 不带直贴契约')
  // L2 截断：正文 3000 字被截到 HANDOFF_FALLBACK_CHARS
  const l2 = section.slice(section.indexOf('### L2 摘要层'), section.indexOf('### L1 原子事实层'))
  assert.ok(l2.length < 3000 && l2.includes('长'.repeat(100)), 'L2 保留有界截断摘要')
  assert.ok(!l2.includes('长'.repeat(HANDOFF_FALLBACK_CHARS + 1)))
  // L1 逐字 + L0 产物 id/路径/读门指引
  assert.ok(section.includes('```json\n{"a": 1}\n```'), 'L1 逐字保真')
  assert.match(section, /### L0 原文层[\s\S]*【layout】[\s\S]*src\/layout\/\*\*[\s\S]*读门/)
})

test('buildHandoffSection: verbatim 前置 L1、放宽上限并带直贴契约', () => {
  const bigBlock = '```json\n' + '"k": "v",\n'.repeat(500) + '"end": true\n```'  // > 4000，< 8000
  const section = buildHandoffSection({ profile: 'verbatim', upstreams: [
    { id: 'schema', title: '契约', text: `引言\n${bigBlock}`, paths: ['src/schema/**'] },
  ] })
  assert.match(section, /handoff_profile=verbatim/)
  assert.ok(section.indexOf('### L1 原子事实层') < section.indexOf('### L2 摘要层'), 'verbatim：L1 排在摘要前')
  assert.ok(section.includes(bigBlock), 'verbatim 上限 8000 完整保留大块')
  assert.match(section, /\*\*逐字内容禁止凭记忆复述\*\*：交付中需要逐字引用上游内容时，必须先读取上游产物原文/)
  assert.match(section, /再逐字直贴；L1 层仅供定位预览。/)
  // 同内容 semantic 下该块超 4000 上限被截断
  const semantic = buildHandoffSection({ profile: 'semantic', upstreams: [
    { id: 'schema', title: '契约', text: `引言\n${bigBlock}`, paths: ['src/schema/**'] },
  ] })
  assert.ok(!semantic.includes(bigBlock) && semantic.includes('…（超 L1 上限截断'), 'semantic 上限 4000')
  assert.ok(HANDOFF_ATOMIC_CAP < bigBlock.length && bigBlock.length < HANDOFF_ATOMIC_CAP_VERBATIM)
})

test('buildHandoffSection: 无可提取内容时省略 L1 层（L2+L0 仍在）', () => {
  const section = buildHandoffSection({ profile: 'semantic', upstreams: [
    { id: 'notes', text: '纯散文交付，没有代码块也没有键值。', paths: [] },
  ] })
  assert.match(section, /### L2 摘要层/)
  assert.doesNotMatch(section, /### L1 原子事实层/)
  assert.match(section, /### L0 原文层/)
  assert.match(section, /（未登记写范围）/)
})

test('buildAcceptanceVisibility: ready 上游逐字携带，未 ready 记缺材料', () => {
  const text = buildAcceptanceVisibility([{ id: 'bike', title: '自行车', deps: [
    { id: 'layout', title: '布局契约', state: 'ready', paths: ['src/layout/**'], item_id: 'item-0', text: '契约逐字内容 v1.0.0' },
  ] }])
  assert.match(text, /## 验收可见性（上游依赖交付；跨产物依赖约束必须对照上游交付逐字核验；缺材料不可验收通过。）/)
  assert.match(text, /产物「自行车」（bike）的上游依赖核验材料/)
  assert.match(text, /【layout】「布局契约」（状态 ready；路径：src\/layout\/\*\*；交付全文：dpswarm_report\("item-0"\) 分页读取）逐字交付：\n契约逐字内容 v1\.0\.0/)
  const missing = buildAcceptanceVisibility([{ id: 'bike', title: '自行车', deps: [
    { id: 'layout', title: '布局契约', state: 'claimed', paths: ['src/layout/**'], item_id: 'item-0', text: '契约逐字内容' },
  ] }])
  assert.match(missing, /交付内容不可见——缺材料不可验收通过/)
  assert.ok(!missing.includes('契约逐字内容'), '未 ready 的上游交付内容不泄进验收材料')
  assert.equal(buildAcceptanceVisibility([{ id: 'solo', deps: [] }]), '', '无 deps 的产物不产生验收材料')
})

test('buildAcceptanceVisibility: 超上限降级 L1 逐字段 + 路径引用，verbatim 产物带逐字核对提示', () => {
  const big = '报告正文\n```json\n' + '"k": "v",\n'.repeat(1200) + '"end": true\n```' + '\n尾注'
  const text = buildAcceptanceVisibility([{ id: 'bike', title: '自行车', profile: 'verbatim', deps: [
    { id: 'layout', title: '布局契约', state: 'ready', paths: ['src/layout/**'], item_id: 'item-0', text: big },
  ] }])
  assert.match(text, /该产物判型为逐字依赖（verbatim），引用一致性须逐字核对/)
  assert.match(text, /超上限，L1 逐字段（仅供定位；逐字原文读上述路径\/报告）/)
  assert.ok(text.includes('src/layout/**'), '路径引用保留')
  assert.ok(text.includes('```json'), 'L1 逐字段仍提取到 fenced 块头部')
  assert.ok(!text.includes('尾注'), '超出 per-delivery 上限的部分不进入材料')
  // 无可提取块的超长交付：回退逐字截断并标注
  const prose = '散'.repeat(6000)
  const fallback = buildAcceptanceVisibility([{ id: 'bike', deps: [
    { id: 'layout', state: 'ready', paths: ['src/layout/**'], item_id: 'item-0', text: prose },
  ] }])
  assert.match(fallback, /逐字交付（截断；全文读上述路径\/报告）：/)
  assert.ok(fallback.includes('…（截断）'))
})

test('buildAcceptanceVisibility: 总量上限耗尽的后续产物只列引用', () => {
  const dep = id => ({ id, state: 'ready', paths: [`src/${id}/**`], item_id: `item-${id}`, text: '交付'.repeat(1500) })
  const text = buildAcceptanceVisibility([
    { id: 'a', deps: [dep('u1')] },   // 3000 逐字（余 8000 → 5000）
    { id: 'b', deps: [dep('u2')] },   // 3000 逐字（余 5000 → 2000）
    { id: 'c', deps: [dep('u3')] },   // 余 2000 < 3000：L1 无块可提 → 逐字截断
    { id: 'd', deps: [dep('u4')] },   // 余 0 < MIN_KEEP → 仅列引用
  ])
  assert.ok(text.includes(`【u1】（状态 ready；路径：src/u1/**；交付全文：dpswarm_report("item-u1") 分页读取）逐字交付：\n${'交付'.repeat(1500)}`), 'u1 全量逐字')
  assert.ok(text.includes('逐字交付：\n' + '交付'.repeat(1500)), 'u2 全量逐字')
  assert.match(text, /【u3】[\s\S]*?逐字交付（截断；全文读上述路径\/报告）：/, 'u3 超剩余额度降级逐字截断')
  assert.match(text, /【u4】（状态 ready；路径：src\/u4\/\*\*；交付全文：dpswarm_report\("item-u4"\) 分页读取）超总量上限，仅列引用/, 'u4 只留路径/报告引用')
  assert.ok(!text.includes('交付'.repeat(1501)), 'u3/u4 的完整文本均未进入材料')
})
