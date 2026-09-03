"""可选 context manager LLM（机制三 §5.1/§5.2；capsule 要素 §5.8）。

对应《DPswarm-机制架构.md》：
- §5.1 manager LLM 是按需件：是否触发由代码判断（Assembler 侧），
        触发后才瞬时唤起；它不是唯一事实源，也不要求把全局材料长期
        放在自己的窗口里。
- §5.2 只有需要跨材料概括、冲突消解或语义改写时才唤起；相同
        provider/model 的连续请求可吃前缀缓存，但这只是优化不是正确性依赖。
- §5.5 按需瞬时调用：不是节点、不占 worker 槽与节点点数（§7）；
        成本记在触发方（节点 / context job）账下；长期知识在 Memory
        Service，manager 不跨 Team 生命周期持有全局真相。
- §5.8 硬切 checkpoint capsule 要素：目标、已验收决定与不变量、当前
        node/DAG 引用、产物版本与 hash、未决问题、已确认死路、下一动作、
        memory/package revision——provider-neutral、可校验。

硬规则：压缩是零损转述——不得新增事实，矛盾处显式标注
[CONFLICT]...[/CONFLICT]，不得自行裁决（裁决权在 Lead/验收流）。
"""
from __future__ import annotations

import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple

from ..types import ModelRoute, new_id
from .assembler import AssemblerBrief, est_tokens

# complete_fn：上层注入的传输回调 fn(route, messages) -> ProviderResult
# （鸭子类型：.text / .stop_reason / .usage，解耦 providers 模块）。
CompleteFn = Callable[[ModelRoute, List[Dict[str, str]]], object]

# 零损转述系统提示（compress 与 capsule 摘要共用核心规则）
_NO_NEW_FACTS_RULES = """你被唤起为 context manager（按需瞬时调用，非节点）。
对给定候选材料做零损语义压缩，供组装 context package / checkpoint capsule 使用。
硬规则：
1) 不得新增任何事实、推测或建议——只允许对给定材料的转述、合并与去重；
2) 数字、版本号、hash、引用 ID、文件路径等关键标识必须原样保留；
3) 材料之间的矛盾不得自行裁决：在对应内容处输出 [CONFLICT] 矛盾双方原意 [/CONFLICT] 显式标注；
4) 输出为 provider-neutral 纯文本，按给定章节结构组织，不输出寒暄与解释。"""

_COMPRESS_SECTIONS = """按以下章节组织输出（材料缺失的章节写"无"）：
## 目标
## 已验收决定与不变量
## 当前引用
## 未决问题
## 下一动作"""


class ContextManagerLLM:
    """按需瞬时调用的语义压缩执行者（§5.1/§5.2）。

    - 不成为唯一事实源：产出只是摘要层，权威仍在 evidence/artifact 与
      durable memory（§5.6 分层）。
    - 成本记账：每次调用返回 {input_tokens, output_tokens, cost_usd,
      invoked_route}，由触发方（节点 / context job）入账（§4 全账）。
    - 与 providers 模块解耦：传输经注入的 complete_fn，本类只负责提示
      组装、调用与记账。
    """

    def __init__(self, complete_fn: CompleteFn, route: ModelRoute) -> None:
        self.complete_fn = complete_fn
        self.route = route

    # -- 语义压缩（§5.2，由 Assembler 代码判定触发后调用） -------------------

    def compress(self, materials: List[str], brief: AssemblerBrief) -> Tuple[str, Dict]:
        """跨材料语义压缩，产出摘要层。

        输入 materials 为带来源标注的材料文本（[ref]\\n正文）；
        brief 提供任务意图与选材/排除标准（不替 Lead 做任务判断，§5.4）。
        返回 (压缩文本, 记账 dict)。
        """
        user_parts = [
            "## 任务意图（选材标准，不得改写）",
            brief.task_intent,
            f"选材关键词：{', '.join(brief.select) or '(无)'}；"
            f"排除项：{', '.join(brief.exclude) or '(无)'}；token 预算：{brief.token_budget}",
            "",
            "## 候选材料（唯一事实来源）",
        ]
        user_parts.extend(f"--- 材料 {i + 1} ---\n{m}" for i, m in enumerate(materials))
        user_parts.append("")
        user_parts.append(_COMPRESS_SECTIONS)
        messages = [
            {"role": "system", "content": _NO_NEW_FACTS_RULES},
            {"role": "user", "content": "\n".join(user_parts)},
        ]
        result = self.complete_fn(self.route, messages)
        text = (getattr(result, "text", None) or "").strip()
        return text, self._account(messages, text, result)

    # -- checkpoint capsule（§5.8 预装，provider-neutral） --------------------

    def make_capsule(self, goal: str, accepted_decisions: List[str],
                     invariants: List[str], node_ref: str,
                     artifacts_versions: List[str], open_questions: List[str],
                     dead_ends: List[str], next_action: str,
                     package_revision: str) -> Tuple[str, Dict]:
        """§5.8 硬切 checkpoint capsule：旧窗口 → successor window 的续接包。

        结构化字段（控制事实与引用）逐字保留、不经 LLM 改写；仅在
        complete_fn 可用时追加一节 LLM 压缩摘要（同样受零新增事实约束）。
        产物是 provider-neutral 可读文本，落盘时由上层补 hash（§5.8
        以带 hash 的不可变包落盘）。
        返回 (capsule 文本, 记账 dict)。
        """

        def _bullets(items: List[str]) -> List[str]:
            return [f"- {it}" for it in items] or ["- (无)"]

        lines: List[str] = [
            "# Checkpoint Capsule（provider-neutral，§5.8 硬切续接）",
            f"capsule_id: {new_id('capsule')}",
            f"generated_at: {time.time()}",
            f"package_revision: {package_revision}",
            "",
            "## 目标", goal,
            "",
            "## 已验收决定", *_bullets(accepted_decisions),
            "",
            "## 不变量", *_bullets(invariants),
            "",
            "## 当前引用（node / DAG）", f"- {node_ref}",
            "",
            "## 产物版本与 hash", *_bullets(artifacts_versions),
            "",
            "## 未决问题", *_bullets(open_questions),
            "",
            "## 已确认死路", *_bullets(dead_ends),
            "",
            "## 下一动作", next_action,
        ]
        skeleton = "\n".join(lines)
        account: Dict = {}
        summary, result = self._capsule_summary(skeleton)
        if summary:
            text = skeleton + "\n\n## 压缩摘要（context manager，零新增事实）\n" + summary
            account = self._account(
                [{"role": "user", "content": skeleton}], summary, result)
        else:
            text = skeleton
        return text, account

    # -- 内部 ----------------------------------------------------------------

    def _capsule_summary(self, skeleton: str) -> Tuple[str, Optional[object]]:
        """对 capsule 骨架做一次瞬时压缩调用（§5.8：必要时唤起 manager）。

        传输失败 / 空返回时静默降级：结构化骨架本身已可续接，
        摘要层只是增强，不是正确性依赖（§5.2 缓存同理）。
        返回 (摘要文本, 传输结果)；失败时 ("", None)。
        """
        messages = [
            {"role": "system",
             "content": _NO_NEW_FACTS_RULES + "\n" + _COMPRESS_SECTIONS},
            {"role": "user",
             "content": "对以下 checkpoint capsule 骨架做零损压缩，"
                        "输出一节摘要（保留全部引用与版本号）：\n\n" + skeleton},
        ]
        try:
            result = self.complete_fn(self.route, messages)
        except Exception:
            return "", None
        return (getattr(result, "text", None) or "").strip(), result

    def _account(self, messages: List[Dict[str, str]], output_text: str,
                 result: object) -> Dict:
        """记账（§4：索引/召回/压缩必须单独记账，不能假定免费）。

        优先取 ProviderResult.usage 的精确值；缺失时按 len//3 估算。
        """
        usage = getattr(result, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        if input_tokens is None:
            input_tokens = est_tokens("\n".join(m["content"] for m in messages))
        output_tokens = getattr(usage, "output_tokens", None)
        if output_tokens is None:
            output_tokens = est_tokens(output_text)
        cost_usd = getattr(usage, "cost_usd", None)
        if cost_usd is None:
            # ModelRoute 不含价目；无法精确计价时记 0，由上层按 observation 补账
            cost_usd = 0.0
        return {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cost_usd": float(cost_usd),
            "invoked_route": f"{self.route.provider}/{self.route.model}",
        }
