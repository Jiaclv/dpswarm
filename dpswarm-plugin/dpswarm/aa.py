"""AA（Artificial Analysis）外部评分快照（§3/§8：V1 选型依据的外部数据源）。

机制层定位（文档已确立）：
- V1 选型依据 = AA 分维度评分按任务类型匹配，全局总分兜底；
- AA 只配当**冷启动先验**（调研报告 §2.2），不参与自家生产画像闭环；
- 全局分级 S/A/B/C/D 是自定口径——本模块按快照里的 level_bands 把 AA
  Intelligence Index（0-100）映射到五档。阈值校准于 2026-09 快照分布的
  自然断档（≥60 前沿簇 / ≥50 主力 / ≥40 够用 / ≥30 轻量 / 其余 D），
  属实现层决策，可在 aa_scores.json 覆盖。

数据维护：dpswarm/data/aa_scores.json 为人工/脚本录入的榜单快照
（本版来源 AA 官网 FAQ + BenchLM 镜像，快照日期见文件）。更新 = 换文件，
不改代码。缺的维度（如 reasoning）不硬造——aa_score() 自然走 overall 兜底。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .types import Level, ModelCatalog, ModelFacts

DEFAULT_SNAPSHOT = Path(__file__).parent / "data" / "aa_scores.json"

# 归一化时丢弃的括注（同名模型的评测变体，分数同源）与 MMDD 日期后缀
# （如 0813/0731 是版本快照日，主条目已收录）。"(high)" 保留——它是
# 独立低分条目，必须与主条目区分。
_STRIP_PARENS = ("reasoning", "adaptive", "preview", "max", "thinking", "default fallback")
_DATE_SUFFIX = re.compile(r"^(0[1-9]|1[0-2])[0-3]\d$")


def normalize_name(name: str) -> str:
    """模型名归一：小写 → 去 provider 前缀（取 / 最后段）→ 去噪括注与
    MMDD 版本日后缀 → 只留字母数字。"""
    s = (name or "").strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    s = re.sub(r"\(([^)]*)\)", lambda m: (
        "" if m.group(1).strip() in _STRIP_PARENS else m.group(1)), s)
    tokens = [t for t in re.split(r"[^a-z0-9]+", s) if t]
    if tokens and _DATE_SUFFIX.fullmatch(tokens[-1]):
        tokens.pop()
    return "".join(tokens)


class AASnapshot:
    """快照只读视图：索引与映射都是纯函数，不落任何状态。"""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data or {}
        models = self.data.get("models", {}) if isinstance(self.data.get("models"), dict) else {}
        self._index: Dict[str, Dict[str, Any]] = {}
        for key, entry in models.items():
            if not isinstance(entry, dict):
                continue
            for cand in [key] + list(entry.get("aliases", []) or []):
                n = normalize_name(str(cand))
                if n and n not in self._index:  # 先收录者优先（key 先于 alias）
                    self._index[n] = entry
        bands = self.data.get("level_bands")
        self.bands: Dict[str, float] = {
            "S": 60.0, "A": 50.0, "B": 40.0, "C": 30.0,
        }
        if isinstance(bands, dict):
            for k, v in bands.items():
                try:
                    self.bands[str(k).upper()] = float(v)
                except (TypeError, ValueError):
                    continue

    @classmethod
    def load(cls, path: Path = DEFAULT_SNAPSHOT) -> Optional["AASnapshot"]:
        """快照缺失/损坏 → None：注册路径退回声明值，绝不阻断装载。"""
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(json.load(fh))
        except (OSError, ValueError):
            return None

    # load 的语义别名（PanelState 装载用）
    load_default = load

    # -- 查询 -------------------------------------------------------------

    def lookup(self, model_name: str) -> Optional[Dict[str, Any]]:
        return self._index.get(normalize_name(model_name))

    def level_for(self, entry: Dict[str, Any]) -> Level:
        """AA Intelligence Index → 自定五档（快照 level_bands 可覆盖）。"""
        try:
            overall = float(entry.get("overall"))
        except (TypeError, ValueError):
            return Level.A
        if overall >= self.bands.get("S", 60.0):
            return Level.S
        if overall >= self.bands.get("A", 50.0):
            return Level.A
        if overall >= self.bands.get("B", 40.0):
            return Level.B
        if overall >= self.bands.get("C", 30.0):
            return Level.C
        return Level.D

    def dimensions(self, entry: Dict[str, Any]) -> Dict[str, float]:
        """只收录快照里实际有的分维（缺维不硬造，选型时走 overall 兜底）。"""
        dims: Dict[str, float] = {}
        for dim in ("overall", "coding", "reasoning", "math", "search"):
            v = entry.get(dim)
            if isinstance(v, (int, float)):
                dims[dim] = float(v)
        return dims

    def source_tag(self) -> str:
        return "aa@" + str(self.data.get("snapshot_date", "unknown"))

    def meta(self) -> Dict[str, Any]:
        return {
            "snapshot_date": self.data.get("snapshot_date"),
            "source": self.data.get("source"),
            "level_bands": self.bands,
            "models": len(self._index),
        }


def register_route_model(
    catalog: ModelCatalog,
    aa: Optional[AASnapshot],
    provider: str,
    model: str,
    level_hint: Optional[Level] = None,
    aa_hint: Optional[Dict[str, float]] = None,
) -> ModelFacts:
    """路由模型注册的统一入口（§2：级别由目录解析，非调用方声称）。

    优先级：AA 快照命中 → 分维/级别/来源都用外部数据；未命中 → 声明值
    （level_hint / aa_hint），aa_source='declared'——注入文本会带声明值
    标记，Lead 知道这不是外部事实。重复注册以先到者为准（catalog.register
    语义），已是 AA 数据时不被声明值覆盖。
    """
    existing = catalog.resolve(provider, model)
    if existing is not None:
        return existing
    entry = aa.lookup(model) if aa is not None else None
    if entry is not None:
        facts = ModelFacts(
            provider, model, aa.level_for(entry),
            aa_dimensional=aa.dimensions(entry),
            aa_source=aa.source_tag())
    else:
        facts = ModelFacts(
            provider, model, level_hint or Level.A,
            aa_dimensional=aa_hint if aa_hint else {"coding": 7.0},
            aa_source="declared")
    catalog.register(facts)
    return facts
