"""全场景机制测试。每个场景注释标出对应文档小节。"""
import os, tempfile, unittest
from control_plane import ControlPlane, RootSpec, RuleViolation


class DPswarmScenarios(unittest.TestCase):
    def cp(self, **kw):
        return ControlPlane(path=os.path.join(tempfile.gettempdir(), "luna-test.jsonl"),
                            spec=RootSpec(**kw))

    def item_node(self, cp, item="w", node="n", **kw):
        depth = kw.pop("depth", 1)
        cp.create_item(item, depth=depth)
        cp.start(node, item, "lead", kw.pop("model", "deepseek"), kw.pop("points", 2),
                 depth=depth, kind=kw.pop("kind", "worker"))

    # §9.1：事件先于状态、append+flush、回放投影
    def test_event_log_replay_and_flush(self):
        cp = self.cp(); self.item_node(cp)
        restored = ControlPlane.recover(cp.events, cp.spec)
        self.assertEqual(restored.p.items["w"]["status"], "running")
        with open(cp.path, encoding="utf8") as log:
            self.assertEqual(len(cp.events), len(log.readlines()))

    # §9.1/§9.2：非法状态迁移拒绝且不泄漏状态
    def test_state_machine_and_transaction_rollback(self):
        cp = self.cp(); self.item_node(cp)
        with self.assertRaises(RuleViolation): cp.accept("w", "lead")
        self.assertEqual(cp.p.items["w"]["status"], "running")
        cp.submit("w")
        with self.assertRaises(RuleViolation): cp.reject("w", "bad", 3)
        self.assertEqual(cp.p.items["w"]["status"], "submitted")

    # §7：root 共享 worker 槽、节点点数双重准入
    def test_capacity_gates_and_release(self):
        cp = self.cp(max_open=1, max_points=2); self.item_node(cp)
        cp.create_item("w2")
        with self.assertRaises(RuleViolation): cp.start("n2", "w2", "lead", "m", 1)
        with self.assertRaises(RuleViolation): cp.start("n3", "w", "lead", "m", 1)
        cp.submit("w"); cp.accept("w", "lead")
        self.assertEqual((cp.p.used_slots, cp.p.used_points), (0, 0))

    # §7：同层分裂协助者不占槽，但每个常驻节点占点（以 kind 控制）
    def test_split_assistant_point_without_worker_slot(self):
        cp = self.cp(max_open=1, max_points=4); self.item_node(cp, points=2)
        cp.start("assistant", "w", "main", "same-model", 2, kind="assistant")
        self.assertEqual(cp.p.used_slots, 1); self.assertEqual(cp.p.used_points, 4)

    # §7/§8：深度、级别方向和裂变规模由代码门禁；测试深度
    def test_depth_gate(self):
        cp = self.cp(max_depth=2); cp.create_item("w")
        with self.assertRaises(RuleViolation):
            cp.create_item("too-deep", depth=3)

    # §9.2：DAG 全图环检测与 revision CAS
    def test_dag_cas_and_cycle(self):
        cp = self.cp(); self.item_node(cp, "a", "na"); cp.create_item("b")
        cp.add_dependency("a", "b", 0)
        with self.assertRaises(RuleViolation): cp.add_dependency("b", "a", 0)
        with self.assertRaises(RuleViolation): cp.add_dependency("b", "a", 1)

    # §4/§5.7：submitted→finalizing→accepted，证据/package 先于 accepted，资源释放
    def test_acceptance_five_steps(self):
        cp = self.cp(); self.item_node(cp); cp.submit("w")
        cp.accept("w", "lead-model", b"artifact")
        types = [e["type"] for e in cp.events]
        self.assertLess(types.index("finalizing"), types.index("package"))
        self.assertLess(types.index("package"), types.index("accepted"))
        self.assertEqual(cp.p.items["w"]["status"], "accepted")

    # §8：共用最多两次重试，attempt=首次+2；超限后上交且资源释放
    def test_retry_budget_and_escalation(self):
        cp = self.cp(); self.item_node(cp); cp.submit("w"); cp.reject("w", "weak", 0)
        cp.retry("w", 1); cp.submit("w", 1); cp.reject("w", "weak", 1)
        cp.retry("w", 2); cp.submit("w", 2); cp.reject("w", "weak", 2)
        with self.assertRaises(RuleViolation): cp.retry("w", 3)
        cp.escalate("w", "needs-parent")
        self.assertEqual(cp.p.items["w"]["status"], "escalated")
        self.assertEqual(cp.p.used_points, 0)

    # §5.8/§9.3：rollover 保持 node/item/depth/lease，不增加槽、点数或 retry
    def test_rollover_same_lease_and_depth(self):
        cp = self.cp(); self.item_node(cp, depth=2)
        cp.rollover("n", b"capsule")
        successor = cp.p.nodes["n@1"]
        self.assertEqual(successor["depth"], 2)
        self.assertEqual(cp.p.used_slots, 1); self.assertEqual(cp.p.used_points, 2)
        self.assertEqual(cp.p.successors, {})

    # §9.3：两阶段启动意图、hash 确认与恢复投影
    def test_provisioning_is_not_active_before_commit(self):
        cp = self.cp(); cp.create_item("w")
        cp.append("node_provisioning", id="n", item="w", owner="lead", depth=1,
                  model="m", points=1, lease="lease-n", kind="worker")
        self.assertEqual(cp.p.nodes["n"]["physical"], "provisioning")
        cp.append("node_active", id="n", session="s", manifest_hash="h")
        self.assertEqual(cp.p.nodes["n"]["physical"], "active")

    # §9.4/§9.5：通知只表示变化；peer mailbox queued/delivered 去重
    def test_peer_mailbox(self):
        cp = self.cp(); self.item_node(cp)
        cp.send("m1", "main", "assistant", "split")
        self.assertEqual(cp.p.mailbox["m1"]["state"], "queued")
        cp.deliver("m1")
        with self.assertRaises(RuleViolation): cp.send("m1", "main", "assistant", "duplicate")

    # §7/§9.4：active 节点超时转 blocked，lease 保守保留
    def test_timeout_preserves_lease(self):
        now = [0]
        cp = ControlPlane(path=os.path.join(tempfile.gettempdir(), "luna-time.jsonl"),
                          clock=lambda: now[0])
        self.item_node(cp); now[0] = 2; cp.check_timeout("n")
        self.assertEqual(cp.p.items["w"]["status"], "blocked")
        self.assertEqual(cp.p.used_points, 2)

    # §9.6：封存先截止准入，再回收节点，最后终态；之后禁止 start
    def test_seal_order_and_gate(self):
        cp = self.cp(); self.item_node(cp); cp.seal()
        types = [e["type"] for e in cp.events]
        self.assertLess(types.index("seal_begin"), types.index("node_drain"))
        self.assertLess(types.index("node_drain"), types.index("terminated"))
        self.assertTrue(cp.p.sealed)
        cp.create_item("late") if False else None
        with self.assertRaises(RuleViolation): cp.start("late-node", "w", "lead", "m", 1)

    # §5.3/§5.4：包 hash 可复核；大包走引用的语义由 package 记录表达
    def test_package_hash_and_evidence(self):
        cp = self.cp(); self.item_node(cp); cp.submit("w"); cp.accept("w", "lead", b"x")
        pkg = cp.p.packages["pkg-w"]
        self.assertEqual(len(pkg["hash"]), 64)
        self.assertEqual(cp.p.evidence[0]["package"], "pkg-w")

    # §4/§8：429/QUOTA 属于观测策略；原型保留 failure audit，不将拒绝答案写 durable memory
    def test_failure_audit_not_memory(self):
        cp = self.cp(); self.item_node(cp); cp.submit("w"); cp.reject("w", "RATE_LIMIT", 0)
        self.assertEqual(cp.p.audit[0]["reason"], "RATE_LIMIT")
        self.assertFalse(hasattr(cp.p, "durable_memory"))

    # §9.2：终态优先，终止释放资源且不能恢复
    def test_terminal_precedence(self):
        cp = self.cp(); self.item_node(cp); cp.terminate("w", "manual")
        self.assertEqual(cp.p.used_points, 0)
        with self.assertRaises(RuleViolation): cp.terminate("w")


if __name__ == "__main__":
    unittest.main(verbosity=2)
