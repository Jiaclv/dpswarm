"""AA 外部评分快照测试（§3/§8：V1 选型依据 = AA 分维按任务类型匹配）。

快照文件本身也是被测对象：schema、真实分维、级别分档映射、注册路径的
AA 优先 / 声明值兜底、注入文本的声明值标记。
"""
from __future__ import annotations

import pytest

from dpswarm.aa import AASnapshot, normalize_name, register_route_model
from dpswarm.types import Level, ModelCatalog, ModelFacts


@pytest.fixture(scope="module")
def snap():
    s = AASnapshot.load()
    assert s is not None, "AA 快照文件必须存在且合法"
    return s


def test_snapshot_meta(snap):
    meta = snap.meta()
    assert meta["snapshot_date"] == "2026-09-01"
    assert meta["models"] > 20
    assert meta["level_bands"]["S"] == 60.0


def test_normalize_aliases():
    assert normalize_name("DeepSeek V4 Pro 0813") == "deepseekv4pro"
    assert normalize_name("deepseek/deepseek-v4-pro") == "deepseekv4pro"
    assert normalize_name("Kimi K3 (max)") == "kimik3"
    assert normalize_name("GLM-5.3 (max)") == "glm53"
    # (high) 是独立低分条目，不得并入主条目
    assert normalize_name("DeepSeek V4 Pro (High)") == "deepseekv4prohigh"


def test_lookup_hit_and_miss(snap):
    e = snap.lookup("deepseek/deepseek-v4-pro")
    assert e is not None and e["overall"] == pytest.approx(53.2)
    assert e["coding"] == pytest.approx(68.8)
    assert snap.lookup("DeepSeek V4 Pro (high)")["overall"] == pytest.approx(43.8)
    assert snap.lookup("totally-unknown-model") is None


def test_dimensions_only_real(snap):
    dims = snap.dimensions(snap.lookup("glm-5.3"))
    # 快照只有 overall/coding 两维：缺维不硬造，选型时走 overall 兜底
    assert set(dims) == {"overall", "coding"}


def test_level_bands(snap):
    assert snap.level_for({"overall": 65.7}) == Level.S
    assert snap.level_for({"overall": 59.5}) == Level.A   # GLM-5.3
    assert snap.level_for({"overall": 51.8}) == Level.A
    assert snap.level_for({"overall": 45.4}) == Level.B
    assert snap.level_for({"overall": 36.0}) == Level.C
    assert snap.level_for({"overall": 20.0}) == Level.D


def test_register_with_aa_hit():
    aa = AASnapshot.load()
    cat = ModelCatalog()
    f = register_route_model(cat, aa, "deepseek", "deepseek-v4-pro")
    assert f.aa_source.startswith("aa@")
    assert f.level == Level.A
    assert f.aa_dimensional["coding"] == pytest.approx(68.8)
    # 先到者优先：AA 注册后不被声明值覆盖
    f2 = register_route_model(cat, aa, "deepseek", "deepseek-v4-pro",
                              level_hint=Level.S, aa_hint={"coding": 99.0})
    assert f2 is f


def test_register_declared_fallback():
    aa = AASnapshot.load()
    cat = ModelCatalog()
    f = register_route_model(cat, aa, "myprov", "mystery-model",
                             level_hint=Level.B, aa_hint={"coding": 6.5})
    assert f.aa_source == "declared"
    assert f.level == Level.B
    assert f.aa_dimensional == {"coding": 6.5}


def test_register_without_snapshot():
    cat = ModelCatalog()
    f = register_route_model(cat, None, "p", "m")
    assert f.aa_source == "declared" and f.level == Level.A


def test_fact_sheet_tags_declared():
    cat = ModelCatalog()
    cat.register(ModelFacts("p", "m", Level.A))  # 默认 declared
    sheet = cat.fact_sheet(8, 1)
    assert "declared·声明值" in sheet
    aa = AASnapshot.load()
    register_route_model(cat, aa, "deepseek", "deepseek-v4-pro")
    line = [l for l in cat.fact_sheet(8, 1).splitlines() if "deepseek-v4-pro" in l][0]
    assert "声明值" not in line and "AA[coding]=68.8" in line
