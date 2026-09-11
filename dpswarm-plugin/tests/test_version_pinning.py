"""借鉴项③：按输入版本解锁 + 版本失效传播。

- 下游晋级时锚定消费的上游版本（input_versions_consumed）：解锁依据从"上游
  item 到达 accepted"收紧为"上游**当前版本**到达 accepted"。
- 候选装配时核对消费锚定与上游当前版本：不一致 → 该下游证据过期，剔除候选
  并记 dependency_version_drift（上游返工改接口、下游还拿着旧实现这一类）。
"""
from __future__ import annotations

from dpswarm.types import DelegationKind, ModelRoute, Level
from test_open_items_o import TASK2, make_cp, make_orch, _accepted_item


def _dep_chain(cp):
    """A accepted；B 依赖 A（deps=[A]），未启动。"""
    a, a_pkg = _accepted_item(cp, text="上游交付 v1")
    b = cp.create_work_item(DelegationKind.FISSION, parent_item=cp._root_item_id(),
                            deps=[a.item_id], team="root")
    return a, a_pkg, b


class TestInputVersionPinning:
    def test_consumption_anchored_on_promotion(self, tmp_path):
        cp = make_cp(tmp_path)
        a, a_pkg, b = _dep_chain(cp)
        orch = make_orch(cp, [])
        orch._record_input_consumption(b.item_id)
        assert orch._consumed_versions[b.item_id] == {a.item_id: str(a_pkg)}
        ev = [e for e in cp.store.read_all() if e.kind == "input_versions_consumed"]
        assert ev and ev[-1].payload["consumed"] == {a.item_id: str(a_pkg)}

    def test_version_drift_detected_and_excluded_from_candidate(self, tmp_path):
        cp = make_cp(tmp_path)
        a, a_pkg, b = _dep_chain(cp)
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        orch._record_input_consumption(b.item_id)
        # 上游随后被返工出新版本（模拟"改了接口"）
        cp.proj.work_items[a.item_id].submission_package_id = "pkg-v2-new"
        stale = orch._dependency_drift([cp.proj.work_items[a.item_id],
                                        cp.proj.work_items[b.item_id]])
        assert stale == [b.item_id]
        cand = orch._assemble_candidate([cp.proj.work_items[a.item_id],
                                         cp.proj.work_items[b.item_id]])
        assert b.item_id in cand["drift"]
        assert str(a_pkg) not in cand["selected_submission_package_ids"] or \
            all(sid != str(a_pkg) for sid in cand["selected_submission_package_ids"])
        ev = [e for e in cp.store.read_all()
              if e.kind == "dependency_version_drift"]
        assert ev and ev[-1].payload["upstream"] == a.item_id

    def test_no_false_positive_when_versions_match(self, tmp_path):
        cp = make_cp(tmp_path)
        a, a_pkg, b = _dep_chain(cp)
        orch = make_orch(cp, [])
        orch._init_task_registry(TASK2)
        orch._record_input_consumption(b.item_id)
        assert orch._dependency_drift([cp.proj.work_items[b.item_id]]) == []
        ev = [e for e in cp.store.read_all()
              if e.kind == "dependency_version_drift"]
        assert not ev
