"""原子事实确定性提取（L1 层）与逐字依赖判型规则——中立模块，无编排依赖。

机制定位（§5.4/§5.6）：提取是确定性代码（正则模式 + 优先级丢弃），永不走
LLM；逐字保真是硬要求。供交接包三层（orchestrator_lg）与验收材料保真
（orchestrator §4）两侧共用。

优先级（超上限丢弃序）：fenced 块 > schema/JSON 行 > 签名行 > 其他
（markdown 表格行 / 配置键值 / 文件路径行 / 版本标识）；playbook 的
`l1_priority: a > b > c` 配置行可覆盖顺序（仅编排层解析，提取动作不变）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

#: L1 原子事实层总量上限（超出按模式优先级丢弃；原文经引用/PULL 可达）。
#: 逐字依赖型交接放宽到 ATOMIC_CAP_VERBATIM。
ATOMIC_CAP = 4000
ATOMIC_CAP_VERBATIM = 8000
#: 单块大于剩余额度时的最小截断保留（低于此不截断、直接丢弃，避免碎屑）
ATOMIC_MIN_KEEP = 200

_RE_FENCED = re.compile(r"```[^\n]*\r?\n.*?```", re.S)
_RE_FENCE_MARK = re.compile(r"```")
_RE_JSON_LINE = re.compile(r'^[ \t]*"[\w$\-.]+"[ \t]*:[^\n]*', re.M)
_RE_SIGNATURE = re.compile(r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+\w+[^\n]*", re.M)
_RE_TABLE_ROW = re.compile(r"^[ \t]*\|[^\n]*\|[ \t]*$", re.M)
_RE_KV_LINE = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_.\-]*[ \t]*=[ \t]*\S[^\n]*", re.M)
_RE_PATH_LINE = re.compile(r"^[ \t]*(?:[\w\-.]+/)+[\w\-.]+\.\w+[ \t]*$", re.M)
_RE_VERSION = re.compile(r"\b(?:v?\d+\.\d+(?:\.\d+)+|draft-\d{4}-\d{2})\b")

#: 逐字依赖信号：任务书/验收线索命中即按 verbatim 处理（规则兜底判型）
VERBATIM_SIGNALS = ("逐字", "引用", "一致", "schema", "签名", " verbatim", "quote")

#: L1 模式优先级默认（超上限丢弃序）
DEFAULT_PRIORITY = {"fenced": 0, "json": 1, "signature": 2, "misc": 3}


def handoff_profile(task_text: str) -> str:
    """依赖类型分派（规则兜底路径）：verbatim（逐字依赖）/ semantic（语义依赖）。"""
    lower = task_text.lower()
    return ("verbatim" if any(k in task_text or k in lower for k in VERBATIM_SIGNALS)
            else "semantic")


def playbook_priority(playbook: str) -> Optional[Dict[str, int]]:
    """解析 playbook 的 `l1_priority: a > b > c` 配置行（仅编排层解析；
    未知类别忽略、未列类别不提取——提取动作仍是确定性代码）。"""
    for line in playbook.splitlines():
        m = re.match(r"\s*l1_priority\s*:\s*(.+)$", line)
        if m:
            known = [t.strip() for t in m.group(1).split(">")
                     if t.strip() in DEFAULT_PRIORITY]
            if known:
                return {name: idx for idx, name in enumerate(known)}
    return None


def collect_atomic_blocks(text: str,
                          priority: Optional[Dict[str, int]] = None) -> List[tuple]:
    """提取候选块 [(pos, prio, text)]，fenced 块内部不重复提取行级模式。
    priority 缺省 DEFAULT_PRIORITY（playbook 可覆盖顺序）。"""
    prio_of = dict(DEFAULT_PRIORITY)
    if priority:
        prio_of.update(priority)
    spans = [m.span() for m in _RE_FENCED.finditer(text)]
    blocks = [(m.start(), prio_of["fenced"], m.group(0))
              for m in _RE_FENCED.finditer(text)]
    # 未闭合围栏（截断交付常见）：最后一个孤立 ``` 到 EOF 按 fenced 级收
    marks = list(_RE_FENCE_MARK.finditer(text))
    if len(marks) % 2 == 1:
        start = marks[-1].start()
        blocks.append((start, prio_of["fenced"],
                       text[start:].rstrip() + "\n```（原交付围栏未闭合）"))
        spans.append((start, len(text)))

    def _inside(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    for regex, cat in ((_RE_JSON_LINE, "json"), (_RE_SIGNATURE, "signature"),
                       (_RE_TABLE_ROW, "misc"), (_RE_KV_LINE, "misc"),
                       (_RE_PATH_LINE, "misc")):
        if priority and cat not in priority:
            continue   # playbook 覆盖时未列类别不提取
        blocks.extend((m.start(), prio_of[cat], m.group(0).rstrip())
                      for m in regex.finditer(text) if not _inside(m.start()))
    if not priority or "misc" in priority:
        blocks.extend((m.start(), prio_of["misc"], m.group(0))
                      for m in _RE_VERSION.finditer(text) if not _inside(m.start()))
    return blocks


def extract_atomic_facts(text: str, cap: int = ATOMIC_CAP,
                         priority: Optional[Dict[str, int]] = None) -> str:
    """L1 原子事实层：从交付文本逐字提取关键块（确定性，无 LLM 改写）。

    去重；总量超 cap 时按优先级丢弃（低优先先丢），单块大于剩余额度且剩余
    ≥ ATOMIC_MIN_KEEP 时截断保留并标注；输出按原文顺序排列。"""
    candidates = collect_atomic_blocks(text, priority)
    seen = set()
    uniq = []
    for pos, prio, block in candidates:
        if block not in seen:
            seen.add(block)
            uniq.append((pos, prio, block))
    # 选择：优先级优先（同级按原文序）；输出：恢复原文序
    selected: List[tuple] = []
    remaining = cap
    for pos, prio, block in sorted(uniq, key=lambda b: (b[1], b[0])):
        if len(block) <= remaining:
            selected.append((pos, block))
            remaining -= len(block)
        elif remaining >= ATOMIC_MIN_KEEP:
            selected.append((pos, block[:remaining]
                             + "\n…（超上限截断，逐字原文见引用/PULL）"))
            remaining = 0
    selected.sort(key=lambda b: b[0])
    return "\n\n".join(block for _, block in selected)
