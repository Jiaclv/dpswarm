# -*- coding: utf-8 -*-
"""DPswarm 机制逻辑原型 — 全场景测试。

每个测试方法注释对应《DPswarm-机制架构.md》小节。
运行：python test_scenarios.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dpswarm.control import ControlPlane
from dpswarm.store import AdmissionError, ControlError
from dpswarm.types import (
    AcceptanceState,
    Attribution,
    BlockedState,
    ChildSpec,
    ChoiceSource,
    HumanInstructionKind,
    PhysicalState,
    RootExecutionSpec,
    Route,
    SeedMode,
    StopReason,
    TerminalReason,
)

S = Route("acme", "s-lead")
SW = Route("acme", "s-worker")
A = Route("acme", "a-coder")
AR = Route("acme", "a-reason")
B = Route("acme", "b-worker")
C = Route("acme", "c-worker")
D = Route("acme", "d-worker")
OFF = Route("acme", "offline")
UNK = Route("ghost", "nope")
SALT = Route("other", "s-alt")


def boot(**spec_kw) -> ControlPlane:
    p = ControlPlane(RootExecutionSpec(**spec_kw) if spec_kw else RootExecutionSpec())
    p.create_root(S, choice_source=ChoiceSource.LEAD, proposed=S)
    return p


class TestSpecAndAdmission(unittest.TestCase):
    def test_spec_only_stable_constraints_not_runtime(self):
        """§2.1 Spec 只存 root 级稳定约束，占用不得写回 Spec。"""
        p = boot()
        payload = p.proj.spec_payload()
        self.assertIn("maxOpenWorkItems", payload)
        self.assertIn("maxActiveNodePoints", payload)
        self.assertNotIn("occupancy", payload)
        self.assertNotIn("leases", payload)
        self.assertNotIn("dag", payload)
        w = p.derive("node-lead", B)
        self.assertGreater(p.proj.points_used(), 0)
        self.assertNotIn("usedPoints", p.proj.spec_payload())
        self.assertTrue(p.replay_matches_live())

    def test_subteam_shares_same_spec_id_revision(self):
        """§2.1 子 Team 不得复制可独立修改的 Spec；只引用 root_spec_id+revision。"""
        p = boot(max_semantic_depth=3, physical_max_depth=4)
        child = p.fission("node-lead", [ChildSpec(SW)])["children"][0]
        p.fission(child["node_id"], [ChildSpec(B)])
        revs = {t.spec_id: t.spec_revision for t in p.proj.teams.values()}
        self.assertEqual(len(set(t.spec_id for t in p.proj.teams.values())), 1)
        p.publish_spec_revision(max_open_work_items=7)
        self.assertTrue(all(t.spec_revision == p.proj.spec_revision for t in p.proj.teams.values()))

    def test_spec_downsize_does_not_kill_running_blocks_new(self):
        """§2.1 降容不强杀在途；停止新准入。终止须人工另行明确。"""
        p = boot(max_active_node_points=20)
        w = p.derive("node-lead", B)
        used = p.proj.points_used()
        self.assertGreaterEqual(used, 13)  # lead 10 + b 3
        p.publish_spec_revision(max_active_node_points=12)
        with self.assertRaises(AdmissionError) as cm:
            p.derive("node-lead", C)
        self.assertEqual(cm.exception.code, "POINT_CAPACITY")
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.ACTIVE)
        # 人工明确终止才回收
        p.terminate_node(w["node_id"], TerminalReason.MANUAL_STOPPED, human=True)
        self.assertEqual(p.proj.nodes[w["node_id"]].acceptance, AcceptanceState.TERMINATED)
        kinds = [h["kind"] for h in p.proj.human_instructions]
        self.assertIn(HumanInstructionKind.TERMINAL.value, kinds)
        self.assertIn(HumanInstructionKind.CONFIG.value, kinds)

    def test_human_route_wins_no_silent_replace(self):
        """§2 人工指定优先；非法/不可用不得静默改选，返回结构化原因。"""
        p = boot()
        p.human_immediate({"route": "acme/b-worker"})
        w = p.derive("node-lead", B, choice_source=ChoiceSource.HUMAN, proposed=A)
        node = p.proj.nodes[w["node_id"]]
        self.assertEqual(node.choice_source, ChoiceSource.HUMAN)
        self.assertEqual(node.resolved_route, B)
        self.assertEqual(node.proposed_route, A)
        self.assertEqual(node.policy_version, p.proj.spec.model_point_policy_version)
        with self.assertRaises(AdmissionError) as e1:
            p.derive("node-lead", UNK)
        self.assertEqual(e1.exception.code, "MODEL_NOT_FOUND")
        with self.assertRaises(AdmissionError) as e2:
            p.derive("node-lead", OFF)
        self.assertEqual(e2.exception.code, "MODEL_UNAVAILABLE")
        # 投影未偷偷换成另一个模型
        models = [n.resolved_route.model for n in p.proj.nodes.values() if n.resolved_route]
        self.assertNotIn("nope", models)


class TestFactsObservationCloseout(unittest.TestCase):
    def test_v1_injection_excludes_portraits(self):
        """§3 §8 V1 注入 = AA+目录+容量+bench；画像只攒不用。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        p.closeout_accept(w["node_id"])
        facts = p.injection_facts("coding")
        self.assertFalse(facts["portraits_injected"])
        self.assertTrue(any(x["aa_coding"] for x in facts["aa_and_catalog"]))
        self.assertIsNotNone(facts["capacity"]["available"])
        self.assertTrue(any(x["bench_decay_threshold"] for x in facts["bench"]))
        self.assertGreater(p.proj.portraits["acme/b-worker"].success, 0)

    def test_observation_two_layer_stop_and_acceptor(self):
        """§4 观测：拓扑、验收者、harness stopReason + work item 终局。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"], StopReason.COMPLETED)
        self.assertEqual(p.proj.nodes[w["node_id"]].stop_reason, StopReason.COMPLETED)
        self.assertNotEqual(p.proj.work_items[w["work_item_id"]].status, AcceptanceState.ACCEPTED)
        p.closeout_accept(w["node_id"])
        obs = [o for o in p.proj.observations if o.get("terminal") == "accepted"][0]
        self.assertEqual(obs["acceptedBy"]["level"], "S")
        self.assertEqual(obs["stopReason"], "completed")
        self.assertEqual(p.proj.work_items[w["work_item_id"]].terminal_reason, TerminalReason.ACCEPTED)

    def test_rate_limit_vs_quota(self):
        """§4 RATE_LIMIT 退避不进负面画像；QUOTA 运维审计终止、仍不进画像。"""
        p = boot()
        w = p.derive("node-lead", B)
        before = dict(p.proj.portraits)
        p.record_rate_limit(w["node_id"])
        bucket = p.proj.portraits["acme/b-worker"]
        self.assertEqual(bucket.success, 0)
        self.assertEqual(bucket.fail, 0)
        self.assertIn("RATE_LIMIT", bucket.skipped_reasons)
        self.assertFalse(any(f.get("reason") == "RATE_LIMIT" and f.get("negative_sample") for f in p.proj.failure_audit))
        p.record_quota(w["node_id"])
        self.assertTrue(any(a["kind"] == "QUOTA" for a in p.proj.ops_audit))
        self.assertEqual(p.proj.nodes[w["node_id"]].acceptance, AcceptanceState.TERMINATED)
        self.assertEqual(p.proj.portraits["acme/b-worker"].fail, 0)

    def test_token_cache_disjoint_and_cumulative_not_points(self):
        """§4 cache 与 input 不相交；§2.1 累计预算 Hook 不可回收，不与点数混用。"""
        p = boot(optional_cumulative_budget={"amount": 1000})
        w = p.derive("node-lead", B)
        pts_before_release = p.proj.points_used()
        p.record_tokens(w["node_id"], input=10, output=3, cache_read=4, cache_write=2)
        book = p.proj.tokens[w["node_id"]]
        self.assertEqual(book.input, 10)
        self.assertEqual(book.cache_read, 4)
        with self.assertRaises(ControlError) as cm:
            p.record_tokens(w["node_id"], input=1, output=0, cache_read=1, cache_write=0, cache_in_input=True)
        self.assertEqual(cm.exception.code, "TOKEN_OVERLAP")
        spent = p.proj.cumulative_spent["tokens"]
        p.submit(w["node_id"])
        p.closeout_accept(w["node_id"])
        self.assertLess(p.proj.points_used(), pts_before_release)
        self.assertEqual(p.proj.cumulative_spent["tokens"], spent)

    def test_closeout_five_steps_lease_held_until_accepted(self):
        """§4 结案五步：finalizing 仍占租约 → 证据/package → accepted 才释放并解锁。"""
        p = boot()
        a = p.fission(
            "node-lead",
            [ChildSpec(B), ChildSpec(C, depends_on=(0,))],
        )
        first, second = a["children"]
        used_fin = None
        p.submit(first["node_id"])
        p.lead_pass(first["node_id"])
        self.assertEqual(p.proj.nodes[first["node_id"]].acceptance, AcceptanceState.FINALIZING)
        used_fin = p.proj.points_used()
        self.assertGreater(used_fin, 10)
        self.assertFalse(p.proj.work_items[second["work_item_id"]].unlocked)
        p.commit_evidence(first["node_id"])
        p.publish_accepted(first["node_id"])
        self.assertEqual(p.proj.nodes[first["node_id"]].acceptance, AcceptanceState.ACCEPTED)
        self.assertLess(p.proj.points_used(), used_fin)
        self.assertTrue(p.proj.work_items[second["work_item_id"]].unlocked)
        p.start_dependent(second["work_item_id"])
        p.cleanup_window(first["node_id"])
        self.assertTrue(p.proj.nodes[first["node_id"]].window_cleaned)
        self.assertTrue(any(e.retained for e in p.proj.evidence if e.node_id == first["node_id"]))

    def test_memory_rules_and_extract_fail_no_rollback(self):
        """§4 §5.7 记忆：submitted 不能晋升；只收验收+确认失败归因；抽取失败不回滚。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        with self.assertRaises(ControlError):
            p.extract_candidate(w["node_id"], "accepted")
        p.lead_reject(w["node_id"], Attribution.CONTEXT, "missing file")
        fid = p.extract_candidate(w["node_id"], "failure_finding", confirmed=True)
        p.promote_memory(fid)
        p.retry(w["node_id"])
        p.submit(w["node_id"])
        p.closeout_accept(w["node_id"])
        mid = p.extract_candidate(w["node_id"], "accepted")
        p.promote_memory(mid)
        p.extract_and_promote_fail_does_not_rollback(w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].acceptance, AcceptanceState.ACCEPTED)
        self.assertIn(mid, p.durable_retrieval())
        n2 = p.extract_candidate(w["node_id"], "accepted")
        p.promote_memory(n2)
        p.supersede_memory(mid, n2)
        self.assertEqual(p.proj.memories[mid].status.value, "superseded")
        p.invalidate_memory(n2)
        self.assertNotIn(n2, p.durable_retrieval())
        self.assertFalse(any(e.in_default_retrieval for e in p.proj.evidence if e.kind == "rejected"))


class TestContextAndRollover(unittest.TestCase):
    def test_hetero_cache_and_manager_trigger_by_code(self):
        """§5.1 跨模型无 KV 共享；同模型可共享。manager 由代码触发。"""
        p = boot()
        self.assertFalse(p.prefix_cache_allowed(B, A))
        self.assertTrue(p.prefix_cache_allowed(B, B))
        self.assertTrue(p.should_invoke_context_manager(heterogeneous=True, window_ratio=0.1, retrieval_conflict=False))
        self.assertFalse(p.should_invoke_context_manager(heterogeneous=False, window_ratio=0.2, retrieval_conflict=False))
        job = p.invoke_context_manager("node-lead")
        job2 = p.invoke_context_manager("node-lead")
        with self.assertRaises(AdmissionError) as cm:
            p.invoke_context_manager("node-lead")
        self.assertEqual(cm.exception.code, "SEMAPHORE_EXHAUSTED")
        p.release_context_manager(job)
        job3 = p.invoke_context_manager("node-lead")
        self.assertNotIn(job, p.proj.cm_jobs)
        self.assertEqual(p.proj.cm_jobs[job2], "node-lead")
        self.assertEqual(p.proj.cm_jobs[job3], "node-lead")

    def test_required_prefetch_and_seed_binary_and_pull(self):
        """§5.3 seed 二元；required 必须预取；live pull 允许；禁止 predecessor delta。"""
        p = boot()
        w = p.derive("node-lead", B, seed=SeedMode.FRESH, auto_activate=False, skip_prefetch=True)
        with self.assertRaises(AdmissionError) as cm:
            p.activate(w["node_id"])
        self.assertEqual(cm.exception.code, "REQUIRED_NOT_PREFETCHED")
        p.prefetch_required(w["node_id"])
        self.assertEqual(p.activate(w["node_id"]), "active")
        self.assertEqual(p.pull_optional(w["node_id"], "optional-extra")[:7], "pulled:")
        with self.assertRaises(ControlError) as cm2:
            p.pull_from_predecessor(w["node_id"])
        self.assertEqual(cm2.exception.code, "PREDECESSOR_DELTA_FORBIDDEN")
        w2 = p.derive("node-lead", C, seed=SeedMode.FORK)
        pkg = [pk for pk in p.proj.packages.values() if pk.seed_mode == SeedMode.FORK]
        self.assertTrue(pkg)
        self.assertIsNotNone(pkg[0].fork_seed_length)

    def test_hardcut_keeps_ids_lease_slot_not_retry_budget(self):
        """§5.8 硬切：同 node/wi/lease/槽；不耗重试预算；CAS 唯一 successor 及复位。"""
        p = boot()
        w = p.derive("node-lead", B)
        node = p.proj.nodes[w["node_id"]]
        lease, epoch, attempt, depth = node.lease_id, node.context_epoch, node.attempt, node.physical_depth
        slots = p.proj.open_worker_slots()
        p.set_window_usage(w["node_id"], 0.86)
        p.preload_capsule(w["node_id"], success=True, package_hash="cap-1")
        p.register_successor(w["node_id"])
        with self.assertRaises(ControlError) as cm:
            p.register_successor(w["node_id"])
        self.assertEqual(cm.exception.code, "SUCCESSOR_EXISTS")
        p.start_rollover(w["node_id"])
        n2 = p.proj.nodes[w["node_id"]]
        self.assertEqual(n2.lease_id, lease)
        self.assertEqual(n2.work_item_id, w["work_item_id"])
        self.assertEqual(n2.attempt, attempt)
        self.assertEqual(n2.physical_depth, depth)
        self.assertEqual(n2.context_epoch, epoch + 1)
        self.assertFalse(n2.successor_registered)
        self.assertEqual(p.proj.open_worker_slots(), slots)
        # 预装失败：blocked/recovery，lease 保留，复位登记
        p.preload_capsule(w["node_id"], success=False)
        self.assertEqual(p.proj.nodes[w["node_id"]].blocked, BlockedState.RECOVERY)
        self.assertFalse(p.proj.leases[lease].released)
        p.register_successor(w["node_id"])
        p.clear_successor_on_preload_fail(w["node_id"])
        self.assertFalse(p.proj.nodes[w["node_id"]].successor_registered)

    def test_rollover_reweight_wait_and_timeout_suppressed(self):
        """§5.8 换模型硬切拿不到差额则等待；§9.3 超时抑制窗口。"""
        p = boot(max_active_node_points=14)  # lead 10 + b 3 = 13, s-worker 10 差额不够
        w = p.derive("node-lead", B)
        p.preload_capsule(w["node_id"], success=True)
        st = p.start_rollover(w["node_id"], new_route=SW)
        self.assertEqual(st, "reweight_wait")
        with self.assertRaises(ControlError) as cm:
            p.on_node_timeout(w["node_id"])
        self.assertEqual(cm.exception.code, "TIMEOUT_SUPPRESSED")

    def test_three_layer_state_and_hash_mismatch_failed(self):
        """§5.6 三层状态同时取值；§9.3 hash 不符 → failed；启动失败不耗预算。"""
        p = boot()
        w = p.derive("node-lead", B, auto_activate=False)
        n = p.proj.nodes[w["node_id"]]
        self.assertEqual(n.physical, PhysicalState.PROVISIONING)
        self.assertEqual(n.acceptance, AcceptanceState.NONE)
        self.assertEqual(n.blocked, BlockedState.NONE)
        self.assertEqual(p.activate(w["node_id"], accepted_hash="wrong"), "failed")
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.FAILED)
        self.assertEqual(p.proj.nodes[w["node_id"]].attempt, 1)
        p.retry_start(w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.ACTIVE)
        self.assertEqual(p.proj.nodes[w["node_id"]].attempt, 1)
        p.submit(w["node_id"])
        n = p.proj.nodes[w["node_id"]]
        self.assertEqual(n.physical, PhysicalState.ACTIVE)
        self.assertEqual(n.acceptance, AcceptanceState.SUBMITTED)

    def test_orphan_cas_recover_and_fence(self):
        """§9.3 CAS 已登记未 provisioning：补走幂等。fence 防旧 session 覆盖。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.preload_capsule(w["node_id"], success=True)
        p.register_successor(w["node_id"])
        old_fence = p.proj.nodes[w["node_id"]].fence_token
        p.recover_orphan_successor(w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.PROVISIONING)
        with self.assertRaises(ControlError) as cm:
            p.activate(w["node_id"], fence_token=old_fence)
        self.assertEqual(cm.exception.code, "FENCE")
        self.assertEqual(p.activate(w["node_id"]), "active")


class TestTopologyResources(unittest.TestCase):
    def test_default_solo_and_depth_rules(self):
        """§7 默认单干；派生/裂变 +1 层；分裂同层；默认两层禁再派生，仍可分裂。"""
        p = boot()
        self.assertEqual(len(p.proj.work_items), 0)
        w = p.derive("node-lead", B)
        self.assertEqual(p.proj.nodes[w["node_id"]].layer, 2)
        with self.assertRaises(AdmissionError) as cm:
            p.derive(w["node_id"], C)
        self.assertEqual(cm.exception.code, "DEPTH_SEMANTIC")
        sp = p.split(w["node_id"], B)
        ast = p.proj.nodes[sp["assistant_node_id"]]
        self.assertEqual(ast.layer, 2)
        self.assertEqual(ast.physical_depth, p.proj.nodes[w["node_id"]].physical_depth + 1)
        with self.assertRaises(AdmissionError):
            p.split(w["node_id"], B)
        with self.assertRaises(ControlError):
            p.submit(sp["assistant_node_id"])

    def test_slots_points_team_size_and_waiting_accept(self):
        """§7 槽只计普通 worker；Lead/manager/reviewer 不占槽；待审仍占槽；点数至验收。"""
        p = boot(max_team_workers=3, max_open_work_items=4)
        self.assertEqual(p.proj.open_worker_slots(), 0)
        p.spawn_reviewer("node-lead", A)
        self.assertEqual(p.proj.open_worker_slots(), 0)
        kids = p.fission("node-lead", [ChildSpec(B), ChildSpec(C), ChildSpec(D)])["children"]
        self.assertEqual(p.proj.open_worker_slots(), 3)
        with self.assertRaises(AdmissionError) as cm:
            p.derive("node-lead", B)
        self.assertEqual(cm.exception.code, "TEAM_SIZE")
        p.submit(kids[0]["node_id"])
        self.assertEqual(p.proj.open_worker_slots(), 3)  # 待审仍占
        p.closeout_accept(kids[0]["node_id"])
        self.assertEqual(p.proj.open_worker_slots(), 2)
        p.derive("node-lead", B)
        self.assertEqual(p.proj.open_worker_slots(), 3)
        p.record_llm_call(kids[1]["node_id"])
        p.record_llm_call(kids[1]["node_id"])
        # 多次 LLM 不重复占点
        self.assertEqual(p.proj.nodes[kids[1]["node_id"]].llm_calls, 2)

    def test_level_direction_fission_permission_human_override(self):
        """§7 只能召唤同级或低级；仅 S 裂变；人工可越过级别方向/裂变许可。"""
        p = ControlPlane(RootExecutionSpec())
        p.create_root(A)
        with self.assertRaises(AdmissionError) as cm:
            p.derive("node-lead", SW)
        self.assertEqual(cm.exception.code, "LEVEL_DIRECTION")
        p.derive("node-lead", SW, human_override_level=True, choice_source=ChoiceSource.HUMAN)
        with self.assertRaises(AdmissionError) as cm2:
            p.fission("node-lead", [ChildSpec(B)])
        self.assertEqual(cm2.exception.code, "FISSION_PERMISSION")
        p.fission("node-lead", [ChildSpec(B)], human_override_fission=True)
        p2 = boot()
        # S 同级允许
        p2.fission("node-lead", [ChildSpec(SW)])
        p2.fission("node-lead", [ChildSpec(SALT)])

    def test_nested_fission_50pct_and_slot_kept_on_role_change(self):
        """§7 裂变者即 Lead；原槽保留；子树 50% 且 < 父；占用计入祖先。"""
        p = boot(max_semantic_depth=3, physical_max_depth=4, max_open_work_items=8)
        child = p.fission("node-lead", [ChildSpec(SW)])["children"][0]
        slots_before = p.proj.open_worker_slots()
        nested = p.fission(child["node_id"], [ChildSpec(B), ChildSpec(C)])
        self.assertEqual(p.proj.nodes[child["node_id"]].role.value, "lead")
        self.assertEqual(p.proj.open_worker_slots(), slots_before + 2)
        sub = p.proj.teams[nested["team_id"]]
        parent = p.proj.teams["team-root"]
        self.assertEqual(sub.local_point_cap, 50)
        self.assertLess(sub.local_point_cap, parent.local_point_cap)
        self.assertGreater(p.proj.points_used(nested["team_id"]), 0)
        self.assertGreaterEqual(p.proj.points_used("team-root"), p.proj.points_used(nested["team_id"]))

    def test_split_structure_peer_star_and_assistant_lifecycle(self):
        """§7 §9.5 1 槽；协助者不在 DAG；peer 仅分裂；主验收前须关闭协助者。"""
        p = boot()
        w = p.derive("node-lead", A)
        sp = p.split(w["node_id"], A)
        self.assertEqual(p.proj.open_worker_slots(), 1)
        ch = p.proj.channels[sp["channel_id"]]
        p.peer_send(w["work_item_id"], w["node_id"], "m1", "scope")
        p.peer_deliver(w["work_item_id"], "m1")
        w2 = p.derive("node-lead", B)
        with self.assertRaises(ControlError) as cm:
            p.peer_send(w2["work_item_id"], w2["node_id"], "x")
        self.assertEqual(cm.exception.code, "NOT_SPLIT_CHANNEL")
        p.submit(w["node_id"])
        p.lead_pass(w["node_id"])
        p.commit_evidence(w["node_id"])
        with self.assertRaises(ControlError) as cm2:
            p.publish_accepted(w["node_id"])
        self.assertEqual(cm2.exception.code, "ASSISTANT_STILL_OPEN")
        self.assertEqual(p.proj.nodes[w["node_id"]].acceptance, AcceptanceState.FINALIZING)
        p.close_assistant(sp["assistant_node_id"])
        p.publish_accepted(w["node_id"], assistant_signal="assistant-accepted")
        self.assertTrue(any(o.get("signal") == "assistant-accepted" for o in p.proj.observations))
        self.assertTrue(p.proj.channels[sp["channel_id"]].closed)

    def test_assistant_timeout_wakeup_not_escalate(self):
        """§7 协助者超时只 wakeup 主执行者，不上交。§9.4 通知不带状态。"""
        p = boot()
        w = p.derive("node-lead", A)
        sp = p.split(w["node_id"], A)
        st = p.on_node_timeout(sp["assistant_node_id"])
        self.assertEqual(st, "blocked")
        self.assertNotEqual(p.proj.work_items[w["work_item_id"]].status, AcceptanceState.ESCALATED)
        note = p.proj.notifications[-1]
        self.assertEqual(note.target_id, w["node_id"])
        self.assertEqual(note.kind, "changed")
        view = p.wakeup(w["node_id"])
        self.assertIsNone(view.state)
        # 被唤醒方自己读投影
        self.assertEqual(p.snapshot().nodes[sp["assistant_node_id"]].blocked, BlockedState.BLOCKED)

    def test_degenerate_terminated_not_portrait(self):
        """§7 退化：子节点 terminated，不算验收、不进画像；主 agent 继续。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.degenerate_child(w["work_item_id"])
        self.assertEqual(p.proj.work_items[w["work_item_id"]].status, AcceptanceState.TERMINATED)
        self.assertEqual(p.proj.portraits["acme/b-worker"].fail, 0)
        self.assertEqual(p.proj.portraits["acme/b-worker"].success, 0)
        self.assertEqual(p.proj.nodes["node-lead"].physical, PhysicalState.ACTIVE)
        p.derive("node-lead", C)

    def test_same_model_retry_lease_unchanged_reweight_on_change(self):
        """§7 同模型重试 lease 不变；换模型原子 reweight。"""
        p = boot()
        w = p.derive("node-lead", B)
        lease = p.proj.nodes[w["node_id"]].lease_id
        weight = p.proj.nodes[w["node_id"]].point_weight
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.DESCRIPTION)
        p.retry(w["node_id"], attribution=Attribution.DESCRIPTION)
        self.assertEqual(p.proj.nodes[w["node_id"]].lease_id, lease)
        self.assertEqual(p.proj.nodes[w["node_id"]].point_weight, weight)
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.CAPABILITY)
        p.retry(w["node_id"], new_route=A, attribution=Attribution.CAPABILITY)
        self.assertEqual(p.proj.nodes[w["node_id"]].lease_id, lease)
        self.assertEqual(p.proj.nodes[w["node_id"]].point_weight, 5)

    def test_channel_survives_primary_rollover(self):
        """§9.5 通道绑定 work item，不绑定物理 session。"""
        p = boot()
        w = p.derive("node-lead", A)
        sp = p.split(w["node_id"], A)
        p.preload_capsule(w["node_id"], success=True)
        p.start_rollover(w["node_id"])
        p.peer_send(w["work_item_id"], w["node_id"], "after-roll")
        self.assertEqual(p.proj.work_items[w["work_item_id"]].peer_channel_id, sp["channel_id"])
        self.assertFalse(p.proj.channels[sp["channel_id"]].closed)


class TestRetryEscalateTime(unittest.TestCase):
    def test_retry_budget_then_escalate_same_txn(self):
        """§8 首次+2 次重试=3 attempt；耗尽上交；上交无裁决不进画像；同事务释放再准入。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.CONTEXT)
        p.retry(w["node_id"])
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.DESCRIPTION)
        p.retry(w["node_id"])
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.CAPABILITY)
        with self.assertRaises(AdmissionError) as cm:
            p.retry(w["node_id"], new_route=A, attribution=Attribution.CAPABILITY)
        self.assertEqual(cm.exception.code, "RETRY_EXHAUSTED")
        slots_before = p.proj.open_worker_slots()
        out = p.escalate(w["work_item_id"], parent_action="new_work_items", new_children=[ChildSpec(C)])
        self.assertEqual(p.proj.work_items[w["work_item_id"]].terminal_reason, TerminalReason.ESCALATED)
        self.assertEqual(p.proj.portraits["acme/b-worker"].fail, 3)
        self.assertTrue(any(o.get("no_verdict") for o in p.proj.observations))
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(p.proj.open_worker_slots(), slots_before)  # 释放 1 再准入 1

    def test_upgrade_cap_equals_lead_level(self):
        """§8 升级上限=Lead 级别；A Lead 需要 S 则上交。A→S 前须归因（脚本化为 CAPABILITY 才换模）。"""
        p = ControlPlane(RootExecutionSpec())
        p.create_root(A)
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.CAPABILITY)
        with self.assertRaises(AdmissionError) as cm:
            p.retry(w["node_id"], new_route=SW, attribution=Attribution.CAPABILITY)
        self.assertEqual(cm.exception.code, "UPGRADE_EXCEEDS_LEAD")
        p.retry(w["node_id"], new_route=AR, attribution=Attribution.CAPABILITY)

    def test_node_timeout_counts_budget_provisioning_does_not(self):
        """§7 节点超时 → blocked，重试计预算。§9.3 provisioning 超时只取消启动。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.on_node_timeout(w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].blocked, BlockedState.BLOCKED)
        p.retry(w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].retries_used, 1)
        w2 = p.derive("node-lead", C, auto_activate=False)
        self.assertEqual(p.on_node_timeout(w2["node_id"]), "cancelled_provisioning")
        self.assertEqual(p.proj.nodes[w2["node_id"]].attempt, 1)

    def test_deadline_archive_cutoff_then_settle(self):
        """§9.6 准入截止（含 start_node）→ 有界结算；先回收节点再改 item 终态。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.preload_capsule(w["node_id"], success=True)
        p.register_successor(w["node_id"])
        p.fire_deadline()
        self.assertTrue(p.proj.admission_cutoff)
        with self.assertRaises(AdmissionError) as cm:
            p.derive("node-lead", C)
        self.assertEqual(cm.exception.code, "ADMISSION_CUTOFF")
        p.settle_archive()
        self.assertIn(p.proj.archive_phase.value, ("completed", "timed_out"))
        self.assertFalse(p.proj.nodes[w["node_id"]].successor_registered)
        self.assertEqual(p.proj.work_items[w["work_item_id"]].status, AcceptanceState.TERMINATED)
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.DRAINED)

    def test_finalizing_aborted_keeps_evidence_no_unlock(self):
        """§9.3 finalizing → aborted-finalize：证据保留、不解锁后继、释放 lease。"""
        p = boot()
        kids = p.fission("node-lead", [ChildSpec(B), ChildSpec(C, depends_on=(0,))])["children"]
        p.submit(kids[0]["node_id"])
        p.lead_pass(kids[0]["node_id"])
        p.commit_evidence(kids[0]["node_id"])
        p.abort_finalize(kids[0]["node_id"])
        self.assertEqual(p.proj.nodes[kids[0]["node_id"]].acceptance, AcceptanceState.ABORTED_FINALIZE)
        self.assertTrue(p.proj.nodes[kids[0]["node_id"]].evidence_retained)
        self.assertFalse(p.proj.work_items[kids[1]["work_item_id"]].unlocked)
        self.assertTrue(p.proj.leases[p.proj.nodes[kids[0]["node_id"]].lease_id].released)


class TestControlPlaneConsistency(unittest.TestCase):
    def test_event_sourced_independent_log_and_flush(self):
        """§9.1 事件独立存储、回放派生状态、flush 后才成功。"""
        p = boot()
        p.derive("node-lead", B)
        self.assertGreater(p.log.flushed_seq, 0)
        self.assertTrue(all(e.flushed for e in p.log.events))
        self.assertTrue(p.replay_matches_live())
        self.assertEqual(p.log.events[0].type, "RootCreated")

    def test_illegal_transition_and_method_level_rollback(self):
        """§9.1 §9.2 非法转换拒绝落盘；方法级事务回滚，不泄漏 accepted。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        p.lead_pass(w["node_id"])
        seq = p.log.flushed_seq
        with self.assertRaises(ControlError):
            p.publish_accepted(w["node_id"])  # 未提交 package
        self.assertEqual(p.proj.nodes[w["node_id"]].acceptance, AcceptanceState.FINALIZING)
        self.assertEqual(p.log.flushed_seq, seq)
        # submitted 不能直接 accepted
        p2 = boot()
        w2 = p2.derive("node-lead", B)
        p2.submit(w2["node_id"])
        with self.assertRaises(ControlError) as cm:
            def boom(tx):
                tx.emit("NodeAccepted", node_id=w2["node_id"])
            p2.transact(boom)
        self.assertEqual(cm.exception.code, "ILLEGAL_TRANSITION")
        self.assertEqual(p2.proj.nodes[w2["node_id"]].acceptance, AcceptanceState.SUBMITTED)

    def test_cas_cycle_id_never_reuse(self):
        """§9.2 CAS 过期拒绝；全图环检测；id 永不复用。"""
        p = boot()
        w = p.derive("node-lead", B)
        with self.assertRaises(ControlError) as cm:
            p.cas_stale(0)
        self.assertEqual(cm.exception.code, "GRAPH_REVISION_STALE")
        kids = p.fission("node-lead", [ChildSpec(C), ChildSpec(D)])["children"]
        with self.assertRaises(ControlError) as cm2:
            p.add_edge(kids[0]["work_item_id"], kids[0]["work_item_id"])
        # 自环
        self.assertEqual(cm2.exception.code, "CYCLE")
        p.terminate_node(w["node_id"])
        with self.assertRaises(ControlError) as cm3:
            p.derive("node-lead", B, work_item_id=w["work_item_id"])
        self.assertEqual(cm3.exception.code, "ID_REUSED")

    def test_watchdog_only_suggests_writer_executes(self):
        """§9.2 单写者；watchdog 只投建议。"""
        p = boot()
        w = p.derive("node-lead", B)
        sid = p.watchdog_suggest("timeout", node_id=w["node_id"])
        self.assertEqual(p.proj.nodes[w["node_id"]].blocked, BlockedState.NONE)
        p.drain_writer_queue()
        self.assertTrue(any(s.suggestion_id == sid and s.consumed for s in p.proj.suggestions))
        self.assertEqual(p.proj.nodes[w["node_id"]].blocked, BlockedState.BLOCKED)

    def test_human_three_kinds_no_pause_state(self):
        """§9.2 人工指令三类；无暂停中间态。"""
        p = boot()
        p.human_immediate({"topology": "derive"})
        p.publish_spec_revision(max_team_workers=2)
        p.accept_root_by_human()
        kinds = {h["kind"] for h in p.proj.human_instructions}
        self.assertEqual(
            kinds,
            {
                HumanInstructionKind.IMMEDIATE.value,
                HumanInstructionKind.CONFIG.value,
                HumanInstructionKind.TERMINAL.value,
            },
        )
        self.assertEqual(p.proj.nodes["node-lead"].acceptance, AcceptanceState.ACCEPTED)

    def test_recover_mismatch_and_conflict(self):
        """§9.3 对账不符 → failed；已 active 再判 failed → 类型化冲突。"""
        p = boot()
        w = p.derive("node-lead", B, auto_activate=False)
        self.assertEqual(p.recover_provisioning(w["node_id"], {"parent_match": False}), "failed")
        w2 = p.derive("node-lead", C)
        with self.assertRaises(ControlError) as cm:
            p.recover_provisioning(w2["node_id"], {"matches": False})
        self.assertEqual(cm.exception.code, "CONFLICT_ACTIVE")

    def test_batch_terminal_rejects_inflight_item(self):
        """§9.6 原型铁律：终态 item 仍有在途节点则拒绝落盘。"""
        p = boot()
        w = p.derive("node-lead", B)
        with self.assertRaises(ControlError) as cm:
            def boom(tx):
                tx.emit("WorkItemEscalated", work_item_id=w["work_item_id"])
            p.transact(boom)
        self.assertEqual(cm.exception.code, "IN_FLIGHT_ON_TERMINAL_ITEM")
        self.assertEqual(p.proj.work_items[w["work_item_id"]].status, AcceptanceState.NONE)

    def test_attribution_dispositions_scripted(self):
        """§8 归因后处置：context/description 不换模；contradiction 退化。"""
        p = boot()
        w = p.derive("node-lead", B)
        p.submit(w["node_id"])
        p.lead_reject(w["node_id"], Attribution.CONTEXT)
        p.retry(w["node_id"], attribution=Attribution.CONTEXT)
        self.assertEqual(p.proj.nodes[w["node_id"]].resolved_route, B)
        w2 = p.derive("node-lead", C)
        p.submit(w2["node_id"])
        p.lead_reject(w2["node_id"], Attribution.CONTRADICTION)
        p.degenerate_child(w2["work_item_id"])
        self.assertEqual(p.proj.work_items[w2["work_item_id"]].status, AcceptanceState.TERMINATED)


class TestCoverageGaps(unittest.TestCase):
    def test_context_manager_does_not_occupy_points_or_slots(self):
        """§7 context manager 不是节点、不占点数；成本记触发方。"""
        p = boot()
        used = p.proj.points_used()
        slots = p.proj.open_worker_slots()
        p.invoke_context_manager("node-lead")
        self.assertEqual(p.proj.points_used(), used)
        self.assertEqual(p.proj.open_worker_slots(), slots)

    def test_physical_depth_cap_and_duplicate_edge(self):
        """§7 物理上限须容纳同层 fork；§9.2 重复边拒绝。"""
        p = boot(physical_max_depth=2, max_semantic_depth=2)
        w = p.derive("node-lead", B)
        self.assertEqual(p.proj.nodes[w["node_id"]].physical_depth, 2)
        with self.assertRaises(AdmissionError) as cm:
            p.split(w["node_id"], B)
        self.assertEqual(cm.exception.code, "DEPTH_PHYSICAL")
        p2 = boot()
        kids = p2.fission("node-lead", [ChildSpec(B), ChildSpec(C)])["children"]
        p2.add_edge(kids[0]["work_item_id"], kids[1]["work_item_id"])
        with self.assertRaises(ControlError) as cm2:
            p2.add_edge(kids[0]["work_item_id"], kids[1]["work_item_id"])
        self.assertEqual(cm2.exception.code, "DEP_DUPLICATE")

    def test_peer_dedup_and_closed_channel(self):
        """§9.5 去重键 msg_id+sender；关闭后拒收；queued 有界丢弃。"""
        p = boot()
        w = p.derive("node-lead", A)
        sp = p.split(w["node_id"], A)
        p.peer_send(w["work_item_id"], w["node_id"], "m1")
        p.peer_deliver(w["work_item_id"], "m1")
        p.peer_send(w["work_item_id"], w["node_id"], "m1")
        p.peer_deliver(w["work_item_id"], "m1")
        delivered = p.proj.channels[sp["channel_id"]].delivered
        self.assertEqual(sum(1 for m in delivered if m["msg_id"] == "m1"), 1)
        p.peer_send(w["work_item_id"], w["node_id"], "m2")
        p.close_assistant(sp["assistant_node_id"])
        self.assertTrue(p.proj.channels[sp["channel_id"]].discarded)
        with self.assertRaises(ControlError) as cm:
            p.peer_send(w["work_item_id"], w["node_id"], "m3")
        self.assertEqual(cm.exception.code, "CHANNEL_CLOSED")

    def test_deadline_voids_provisioning_and_cas(self):
        """§9.3 终态优先：deadline 后未完成 provisioning 作废、CAS 登记作废。"""
        p = boot()
        w = p.derive("node-lead", B, auto_activate=False)
        p.preload_capsule("node-lead", success=True)
        p.register_successor("node-lead")
        p.fire_deadline()
        p.settle_archive()
        self.assertEqual(p.proj.nodes[w["node_id"]].physical, PhysicalState.DRAINED)
        self.assertFalse(p.proj.nodes["node-lead"].successor_registered)

    def test_large_package_is_ref_not_inline(self):
        """§5.3 低于 inline 上限可进 persona/prompt；大包只传不可变引用。"""
        spec = RootExecutionSpec(inline_token_limit=2000)
        self.assertEqual(spec.inline_token_limit, 2000)
        p = boot()
        w = p.derive("node-lead", B)
        pkgs = list(p.proj.packages.values())
        self.assertTrue(any(not e.inline for pkg in pkgs for e in pkg.entries if not e.required))
        self.assertTrue(any(e.inline for pkg in pkgs for e in pkg.entries if e.required))


if __name__ == "__main__":
    unittest.main(verbosity=2)
