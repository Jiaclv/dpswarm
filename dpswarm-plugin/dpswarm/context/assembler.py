"""Context Assembler：确定性骨架 + 按需语义压缩（机制三 §5.2/§5.3/§5.4）。

对应《DPswarm-机制架构.md》：
- §5.1 异构反转：不追跨模型前缀共享，改追检索与裁剪；是否触发语义压缩
        由代码按模型构成/预算/检索结果判断，不让 LLM 决定"要不要调用自己"。
- §5.2 确定性骨架：先按作用域、权限、版本、预算选材装配；仅靠规则得不到
        可用小包时才经注入的 compress_fn 唤起 context manager LLM。
- §5.3 通道与落盘：裁剪产物落盘（包是资产、路径是引用）；entries 标
        required/optional 与 inline；排列顺序稳定内容在前、任务特定在后。
- §5.4 质量保险：Lead 只给选材标准（AssemblerBrief 只表达任务意图/要什么/
        排除什么），不替 Lead 做任务判断；裁掉的材料降级为 optional，经
        pull 通道兜底可达。

token 估算口径：est_tokens = len(content) // 3（中文近似，纯标准库）。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..types import ContextPackage, ModelRoute, PackageEntry, new_id
from .memory import MemoryEntry, MemoryService

# compress_fn 签名：materials（含来源标注的材料文本）+ brief → 摘要层文本。
# 由上层注入（典型实现 = ContextManagerLLM.compress 的第一返回值包装）。
CompressFn = Callable[[List[str], "AssemblerBrief"], str]

# 同构"近阈值"口径（§5.8 软阈值约 70% 的量级参考）
NEAR_THRESHOLD_RATIO = 0.70
# retrieve 默认拉取的记忆条数上限（确定性骨架的固定窗口）
MEMORY_RETRIEVE_LIMIT = 16


def est_tokens(text: str) -> int:
    """中文近似 token 估算：len//3。"""
    return max(0, len(text)) // 3


@dataclass
class AssemblerBrief:
    """Lead 的语义输入（§5.4 分工）。

    只表达任务意图、选材标准与排除项——拼包劳动归 Assembler /
    可选 manager，任务判断仍归 Lead，不让二次概括者自由改写目标。
    """

    task_intent: str
    select: List[str]                       # 要什么（关键词/材料名）
    exclude: List[str] = field(default_factory=list)  # 排除什么
    scope: str = "team"
    token_budget: int = 8000                # 包内容总预算（裁剪目标）
    inline_token_limit: int = 2000          # 低于此值可 inline 进 persona/prompt（§5.3）


@dataclass
class _Material:
    """装配中间态：一段待排材料（稳定记忆在前 / 任务材料在后）。"""
    ref: str            # 引用（memory:<id> 或共享工作区 ref）
    content: str
    description: str = ""


class ContextAssembler:
    """确定性检索装配器（§5.2）。

    - memory：MemoryService（durable 记忆 = 稳定层）。
    - artifacts：共享工作区材料池 ref -> 内容（任务特定层）。
    - compress_fn：可选语义压缩回调，由持有 context manager 的上层注入；
      Assembler 只在代码判定需要时调用（§5.1 触发权在代码）。
    """

    def __init__(self, memory: MemoryService,
                 artifacts: Optional[Dict[str, str]] = None,
                 compress_fn: Optional[CompressFn] = None) -> None:
        self.memory = memory
        self.artifacts: Dict[str, str] = dict(artifacts or {})
        self.compress_fn = compress_fn

    # -- 装配 ---------------------------------------------------------------

    def assemble(self, brief: AssemblerBrief, target_route: ModelRoute,
                 heterogeneous: bool) -> ContextPackage:
        """确定性骨架装配（§5.2/§5.3/§5.4）。

        步骤：
        1. 选材：select 关键词驱动 memory.retrieve + artifacts 匹配；
           exclude 命中 ref 或正文的材料一律排除。
        2. 排列：稳定内容（durable memory）在前、任务特定（artifacts）在后。
        3. 预算：est = len//3，包含任务骨架、标题和全部来源引用；仅完整材料
           可标 required，放不下的降级 optional（pull 兜底可达，§5.4）。
           骨架与引用也放不下时抛 ValueError，不生成可落盘包。
        4. 压缩触发（代码判断，§5.1）：heterogeneous（目标模型与源材料
           生产者不同模型）且确定性裁剪发生信息损失（选材总量超预算）→
           调 compress_fn 产出摘要层；纯同构且未近阈值（总量 < 70% 预算）
           → 直连不压缩。
        5. inline：整包 est ≤ inline_token_limit → entries 全部 inline；
           否则 entries 标 inline=False（大包只传不可变引用，§5.3）。
        """
        if (not isinstance(brief.token_budget, int) or isinstance(brief.token_budget, bool)
                or brief.token_budget <= 0):
            raise ValueError("CONTEXT_BUDGET_INVALID: token_budget must be a positive integer")
        matched = self._select(brief)
        raw_est = sum(est_tokens(m.content) for m in matched)
        full_content = self._render(brief, target_route, heterogeneous, "", matched, [])
        needs_trim = est_tokens(full_content) > brief.token_budget
        if needs_trim:
            # Task instructions and all original pull references are indivisible.
            # Reject impossible framing before spending a compression call.
            minimum = self._render(brief, target_route, heterogeneous, "", [], matched)
            if est_tokens(minimum) > brief.token_budget:
                raise ValueError(
                    "CONTEXT_BUDGET_TOO_SMALL: task framing and source references need "
                    f"{est_tokens(minimum)} estimated tokens; budget is {brief.token_budget}")

        near_threshold = raw_est >= int(brief.token_budget * NEAR_THRESHOLD_RATIO)
        trigger_compress = needs_trim and (heterogeneous or near_threshold)
        summary_text = ""
        if trigger_compress and self.compress_fn is not None:
            materials = [f"[{m.ref}]\n{m.content}" for m in matched]
            summary_text = self.compress_fn(materials, brief) or ""
            if not isinstance(summary_text, str):
                raise ValueError("CONTEXT_COMPRESSION_INVALID: compressor must return text")
            # Never truncate a semantic summary: removing its conclusion may
            # change its meaning. Discard an oversized summary as a whole.
            if est_tokens(self._render(brief, target_route, heterogeneous,
                                       summary_text, [], matched)) > brief.token_budget:
                summary_text = ""

        if summary_text:
            included, optional = self._layout_with_summary(
                brief, matched, summary_text, target_route, heterogeneous)
        elif needs_trim:
            included, optional = self._trim_to_budget(brief, matched, target_route, heterogeneous)
        else:
            included, optional = matched, []

        content = self._render(brief, target_route, heterogeneous,
                               summary_text, included, optional)
        est = est_tokens(content)
        if est > brief.token_budget:
            raise ValueError(f"CONTEXT_BUDGET_EXCEEDED: rendered package needs {est} estimated tokens; "
                             f"budget is {brief.token_budget}")
        inline_all = est <= brief.inline_token_limit

        entries: List[PackageEntry] = []
        if summary_text:
            entries.append(PackageEntry(
                ref=f"summary:{brief.scope}", required=True, inline=inline_all,
                description="语义压缩摘要层（context manager，零新增事实）"))
        for m in included:
            entries.append(PackageEntry(
                ref=m.ref, required=True, inline=inline_all, description=m.description))
        for m in optional:
            entries.append(PackageEntry(
                ref=m.ref, required=False, inline=False,
                description=(m.description or "补充材料") + "；超预算未入包，pull 通道可达"))

        # 来源指针：入选 + 降级的全部 provenance（§5.3 manifest 要求）
        source_pointers = [m.ref for m in included] + [m.ref for m in optional]
        return ContextPackage(
            package_id=new_id("pkg"),
            revision=1,
            content=content,
            entries=entries,
            source_pointers=source_pointers,
        )

    # -- 落盘（§5.3） --------------------------------------------------------

    def write_package(self, pkg: ContextPackage, store_dir: Path) -> Tuple[str, str]:
        """裁剪产物落盘：包是资产、路径是引用。

        产物两个文件：
        - {package_id}.rev{revision}.md          包正文（最终裁剪结果）
        - {package_id}.rev{revision}.manifest.json  manifest（内容 sha256、
          entries 的 required/inline、source_pointers、revision）

        启动前 runtime 预取时校验 manifest 的 revision 与 sha256（§5.3
        预装契约）。返回 (ref, hash)：ref = 正文文件绝对路径，hash = 正文
        sha256 十六进制。
        """
        store_dir = Path(store_dir)
        store_dir.mkdir(parents=True, exist_ok=True)
        content_hash = hashlib.sha256(pkg.content.encode("utf-8")).hexdigest()
        stem = f"{pkg.package_id}.rev{pkg.revision}"
        body_path = store_dir / f"{stem}.md"
        body_path.write_text(pkg.content, encoding="utf-8")
        manifest = {
            "package_id": pkg.package_id,
            "revision": pkg.revision,
            "content_file": f"{stem}.md",
            "content_sha256": content_hash,
            "est_tokens": est_tokens(pkg.content),
            "entries": [
                {"ref": e.ref, "required": e.required, "inline": e.inline,
                 "description": e.description}
                for e in pkg.entries
            ],
            "source_pointers": list(pkg.source_pointers),
            "written_at": time.time(),
        }
        (store_dir / f"{stem}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(body_path), content_hash

    # -- 内部：选材 ----------------------------------------------------------

    def _select(self, brief: AssemblerBrief) -> List[_Material]:
        """确定性选材：memory（稳定层）在前、artifacts（任务层）在后。"""
        keywords = [k.lower() for k in brief.select if k.strip()]
        excluded = [k.lower() for k in brief.exclude if k.strip()]

        def _hit(text: str, kws: List[str]) -> bool:
            low = text.lower()
            return any(k in low for k in kws)

        materials: List[_Material] = []
        # ① 稳定层：durable memory（status=active、ttl 有效、visibility 可见）
        entries: List[MemoryEntry] = self.memory.retrieve(
            brief.scope, query=" ".join(brief.select),
            limit=MEMORY_RETRIEVE_LIMIT)
        for e in entries:
            if excluded and _hit(e.content + " " + e.memory_id, excluded):
                continue  # 排除项命中：Lead 显式排除，不进包
            if keywords and not _hit(e.content + " " + e.memory_id, keywords):
                # retrieve 已按命中率排序；零命中的稳定条目仅在无关键词时保留
                continue
            materials.append(_Material(
                ref=f"memory:{e.memory_id}", content=e.content,
                description=f"scope={e.scope} rev={e.revision}"))
        # ② 任务层：共享工作区 artifacts
        for ref, content in self.artifacts.items():
            hay = ref + "\n" + content
            if excluded and _hit(hay, excluded):
                continue
            if keywords and not _hit(hay, keywords):
                continue
            materials.append(_Material(ref=ref, content=content,
                                       description="共享工作区材料"))
        return materials

    # -- 内部：预算裁剪 ------------------------------------------------------

    def _trim_to_budget(self, brief: AssemblerBrief, matched: List[_Material],
                        route: ModelRoute, heterogeneous: bool
                        ) -> Tuple[List[_Material], List[_Material]]:
        """完整选材并逐次核对最终正文，放不下的整项降级 optional。"""
        included: List[_Material] = []
        for i, material in enumerate(matched):
            candidate = self._render(brief, route, heterogeneous, "",
                                     included + [material], matched[i + 1:])
            if est_tokens(candidate) > brief.token_budget:
                return included, matched[i:]
            included.append(material)
        return included, []

    def _layout_with_summary(self, brief: AssemblerBrief, matched: List[_Material],
                             summary_text: str, route: ModelRoute, heterogeneous: bool
                             ) -> Tuple[List[_Material], List[_Material]]:
        """摘要层为主干，仅在最终正文仍合规时额外保留完整稳定记忆。

        任务材料转原始引用，所有引用开销也在整体预算内。
        """
        included: List[_Material] = []
        optional: List[_Material] = []
        for i, material in enumerate(matched):
            candidate = self._render(brief, route, heterogeneous, summary_text,
                                     included + [material], optional + matched[i + 1:])
            if material.ref.startswith("memory:") and est_tokens(candidate) <= brief.token_budget:
                included.append(material)
            else:
                optional.append(material)
        return included, optional

    # -- 内部：正文渲染 ------------------------------------------------------

    def _render(self, brief: AssemblerBrief, route: ModelRoute, heterogeneous: bool,
                summary_text: str, included: List[_Material],
                optional: List[_Material]) -> str:
        """渲染最终正文：稳定在前、任务特定在后（§5.3 排列顺序原则）。"""
        lines: List[str] = [
            f"# Context Package（Assembler 确定性装配）",
            f"- 任务意图：{brief.task_intent}",
            f"- 目标路由：{route.provider}/{route.model} [level {route.level.value}]",
            f"- 异构分发：{'是（检索裁剪策略）' if heterogeneous else '否（可直连统一前缀）'}",
            f"- 语义压缩：{'已采用（摘要层）' if summary_text else '未采用'}",
            f"- 选材关键词：{', '.join(brief.select) or '(无)'}",
            f"- 排除项：{', '.join(brief.exclude) or '(无)'}",
        ]
        if summary_text:
            lines += ["", "## 摘要层（语义压缩，零新增事实）", "", summary_text]
        if included:
            lines += ["", "## 稳定知识与任务材料（按序）"]
            for m in included:
                lines += ["", f"### [{m.ref}]" +
                          (f"（{m.description}）" if m.description else ""),
                          m.content]
        if optional:
            lines += ["", "## 未入包材料（optional，经 pull 通道可达）"]
            for m in optional:
                lines.append(f"- {m.ref}" +
                             (f"：{m.description}" if m.description else ""))
        return "\n".join(lines) + "\n"
