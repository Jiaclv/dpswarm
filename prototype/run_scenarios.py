"""DPswarm 逻辑原型的场景测试：用脚本化指令驱动 Lead 决策，断言机制逻辑闭合。

每个场景对应机制文档的条款（注释标 §）。断言分两类：
- 机制必须允许的（正常路径、重试、rollover、封存内结案）
- 机制必须拒绝的（超容量、超预算、双 successor、终态后复活、协助者未关先收）
"""

from control_plane import (
    Acceptance, Blocking, BudgetError, CapacityError, ControlPlane,
    InvariantError, Physical,
)

RESULTS = []


def scenario(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((name, "PASS", ""))
        except Exception as e:
            RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))
        return fn
    return deco


def expect_raise(exc_types, fn):
    try:
        fn()
    except exc_types:
        return
    raise AssertionError(f"应拒绝但未拒绝：{exc_types}")


# S1 正常闭环（§4 结案五步）：accepted 释放 lease、解锁后继、画像带 acceptedBy
@scenario("S1 正常闭环：结案释放+解锁后继+画像记账")
def s1():
    cp = ControlPlane()
    root = cp.init_root()
    a = cp.new_item(root)
    na = cp.start_node(a, "B-model", points=2)
    cp.activate(na)
    b = cp.new_item(root, deps=(a,))
    nb_fail = lambda: cp.start_node(b, "B-model", points=2)
    expect_raise(InvariantError, nb_fail)          # 前置未 accepted，后继不得启动
    cp.submit(na)
    cp.finalize_begin(a)
    cp.finalize_write_evidence(a, "diff.patch#sha1")
    cp.accept(a, by="lead-1")
    assert cp.items[a].acceptance is Acceptance.ACCEPTED
    assert cp.used_points() == 2                   # 只剩 root lead 占点
    nb = cp.start_node(b, "B-model", points=2)     # 后继已解锁
    cp.activate(nb)
    assert cp.profile == [{"item": a, "verdict": "accepted", "by": "lead-1"}]


# S2 双重准入（§7 护栏）：槽满拒新 item、点满拒新节点
@scenario("S2 容量双重准入：槽满拒 item、点满拒 node")
def s2():
    cp = ControlPlane(max_open_work_items=2, max_active_node_points=5)
    root = cp.init_root(lead_points=2)             # 在用点 2
    cp.new_item(root)
    cp.new_item(root)                              # 槽 2/2
    expect_raise(CapacityError, lambda: cp.new_item(root))
    i1 = next(i for i in cp.items.values() if i.parent_id == root)
    cp.start_node(i1.item_id, "B-model", points=2)  # 点 4/5
    i2 = [i for i in cp.items.values() if i.parent_id == root][1]
    expect_raise(CapacityError,
                 lambda: cp.start_node(i2.item_id, "B-model", points=2))  # 4+2>5


# S3 重试预算与上交原子交接（§8 + B1）：首次+2 重试耗尽 → escalate 单事务
@scenario("S3 重试预算≤2、超限上交单事务原子交接")
def s3():
    cp = ControlPlane(max_open_work_items=2, max_active_node_points=6)
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    n = cp.start_node(a, "C-model", points=2)
    cp.activate(n)
    for round_ in range(3):                        # 首次 + 2 次重试 = 3 次 attempt
        cp.submit(cp.nodes[cp.items[a].nodes[-1]].node_id)
        cp.reject(a, by="lead-1", reason=f"round-{round_}")
        if round_ < 2:
            nid = cp.retry(a)
            cp.activate(nid)
    assert cp.items[a].retries_used == 2
    expect_raise(BudgetError, lambda: cp.retry(a))  # 第 3 次重试被预算拒绝

    def takeover(cp_):                             # 父级接管：同事务分解新 item
        b = cp_.new_item(root)
        nb = cp_.start_node(b, "S-model", points=3)
        cp_.activate(nb)
    cp.escalate(a, takeover)                       # 释放 2 点 → 新 3 点准入，无死锁
    assert cp.items[a].acceptance is Acceptance.ESCALATED
    assert all(p["verdict"] != "escalated" for p in cp.profile)  # 无裁决不进画像
    assert cp.used_slots() == 1 and cp.used_points() == 2 + 3

    cp2 = ControlPlane(max_open_work_items=1)      # 原子性：takeover 失败整体回滚
    r2 = cp2.init_root(lead_points=0)
    x = cp2.new_item(r2)
    nx = cp2.start_node(x, "B-model", points=1)
    cp2.activate(nx)
    cp2.submit(nx)
    cp2.reject(x, by="lead", reason="bad")
    def bad_takeover(cp_):
        cp_.new_item(r2)
        cp_.new_item(r2)                           # 超槽 → 整个上交事务回滚
    expect_raise(CapacityError, lambda: cp2.escalate(x, bad_takeover))
    assert cp2.items[x].acceptance is Acceptance.REJECTED  # 回滚：未误入 escalated


# S4 启动失败（§9.3 对账 + §8 不耗预算）
@scenario("S4 启动失败对账：failed 释点、不耗重试预算")
def s4():
    cp = ControlPlane()
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2)
    assert cp.used_points() == 4                   # provisioning 期间占点
    cp.reconcile(n, session_ok=True, hash_ok=False)  # hash 不符 → failed
    assert cp.nodes[n].physical is Physical.FAILED
    assert cp.used_points() == 2                   # 启动事务回滚，点数归还
    assert cp.items[a].retries_used == 0           # 不耗执行重试预算
    assert cp.items[a].acceptance is Acceptance.RUNNING


# S5 终态优先（§9.3/B2）：terminated 树上的 provisioning 补完被作废
@scenario("S5 终态优先：terminated 后恢复补完作废、drain、不复活")
def s5():
    cp = ControlPlane()
    root = cp.init_root()
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2)      # 崩在 provisioning 半截
    cp.terminate_tree("manual-stop")
    assert cp.nodes[n].physical is Physical.DRAINED
    cp.reconcile(n, session_ok=True, hash_ok=True)  # 恢复者补完 → 必须被压下
    assert cp.nodes[n].physical is Physical.DRAINED
    expect_raise(InvariantError, lambda: cp.activate(n))
    assert cp.used_points() == 0 and cp.tree_terminal == "manual-stop"


# S6 超时时钟从 active 起算 + rollover 抑制窗口（§7 护栏② + §9.3）
@scenario("S6 单节点超时：active 起算、rollover 期间抑制、超时转 blocked")
def s6():
    cp = ControlPlane(node_timeout=5)
    root = cp.init_root()
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2)
    cp.tick(3)                                     # provisioning 期间不计时
    assert cp.nodes[n].active_ticks == 0
    cp.activate(n)
    cp.tick(3)
    assert cp.nodes[n].active_ticks == 3
    cp.rollover_begin(n)                           # 进入抑制窗口
    cp.tick(3)
    assert cp.nodes[n].active_ticks == 3           # rollover 期间不累计
    cp.rollover_complete(n)
    cp.tick(2)
    assert cp.nodes[n].blocking is Blocking.BLOCKED  # 累计 5 → 超时


# S7 树级 deadline 封存三段式（§7 护栏③ + §9.6）
@scenario("S7 deadline 封存：准入截止、finalizing 窗口内可结案、超时强收尾")
def s7():
    cp = ControlPlane(deadline=8, settle_limit=3)
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    na = cp.start_node(a, "B-model", points=2)
    cp.activate(na)
    cp.submit(na)
    cp.finalize_begin(a)
    cp.finalize_write_evidence(a, "report.md#sha2")
    b = cp.new_item(root)
    cp.tick(8)                                     # 触发 deadline：准入截止
    assert cp.admission_closed
    expect_raise(CapacityError, lambda: cp.new_item(root))
    expect_raise(CapacityError,                    # 结算期禁止新启动（准入截止的物理臂）
                 lambda: cp.start_node(b, "B-model", points=1))
    cp.accept(a, by="lead-1")                      # 在途 finalizing 允许结案
    assert cp.items[a].acceptance is Acceptance.ACCEPTED
    cp.tick(3)                                     # settle 超时 → 强收尾
    assert cp.tree_terminal == "deadline"
    assert cp.items[b].acceptance is Acceptance.DEADLINE_STOPPED
    assert cp.used_points() == 0 and cp.used_slots() == 0  # 全树资源闭合


# S8 rollover：CAS 唯一 successor、lease 与深度保持（§5.8/§9.3）
@scenario("S8 rollover：双 successor CAS 拒绝、lease/深度保持、epoch+1")
def s8():
    cp = ControlPlane()
    root = cp.init_root()
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2, depth=2)
    cp.activate(n)
    lease_before = cp.nodes[n].lease_id
    cp.rollover_begin(n)
    expect_raise(InvariantError, lambda: cp.rollover_begin(n))  # 双 successor
    cp.rollover_complete(n)
    node = cp.nodes[n]
    assert node.epoch == 1 and node.lease_id == lease_before   # 同 lease 跨窗口
    assert node.depth == 2                             # 深度保持，不 +1
    assert cp.used_points() == 2 + 2                   # lease 未释放重取
    # capsule 预装失败路径：转 blocked、lease 保持、successor 登记复位
    cp.rollover_begin(n)
    cp.rollover_complete(n, capsule_ok=False)
    assert node.blocking is Blocking.BLOCKED and node.physical is Physical.ACTIVE
    assert node.epoch == 1 and not node.successor_registered


# S9 分裂主从（§7 + B3）：1 槽 2 点、协助者未关不得结案、超时只 wakeup 主
@scenario("S9 分裂：1槽2点、协助者未关闭拒绝 accepted、协助者超时不上交")
def s9():
    cp = ControlPlane(node_timeout=4)
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    nm = cp.start_node(a, "S-model", points=3)
    cp.activate(nm)
    ns = cp.split(a, "S-model", points=2)          # 协助者：占点不占槽
    cp.activate(ns)
    assert cp.used_slots() == 1                    # 整对只占 1 槽
    assert cp.used_points() == 2 + 3 + 2           # 主+副各自占点
    assert cp.nodes[ns].depth == cp.nodes[nm].depth + 1  # 物理 fork child
    cp.submit(nm)
    cp.finalize_begin(a)
    expect_raise(InvariantError, lambda: cp.accept(a, by="lead-1"))  # 协助者未关
    cp.tick(4)                                     # 主副同时超时
    assert cp.nodes[ns].blocking is Blocking.BLOCKED
    assert cp.wakeups == [nm]                      # 只唤醒主执行者
    assert cp.items[a].acceptance is Acceptance.FINALIZING  # 未上交、未终态
    cp.assistant_close(ns, verdict="assistant-accepted", by_main=nm)
    cp.accept(a, by="lead-1")
    assert any(p.get("role") == "assistant" for p in cp.profile)  # 主代写奖励信号


# S10 DAG 门禁（§9.1 invariant）：依赖不存在拒、deps 未 accepted 拒
@scenario("S10 DAG 门禁：幽灵依赖与未验收前置都被拒")
def s10():
    cp = ControlPlane()
    root = cp.init_root()
    expect_raise(InvariantError, lambda: cp.new_item(root, deps=("ghost",)))
    a = cp.new_item(root)
    b = cp.new_item(root, deps=(a,))
    expect_raise(InvariantError,
                 lambda: cp.start_node(b, "B-model", points=1))


# S11 finalizing × 人工停止（§9.3/B2）：aborted-finalize 保证据、不解锁、释放
@scenario("S11 finalizing 遇人工停止：aborted-finalize、证据保留、后继仍锁")
def s11():
    cp = ControlPlane()
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    na = cp.start_node(a, "B-model", points=2)
    cp.activate(na)
    cp.submit(na)
    cp.finalize_begin(a)
    cp.finalize_write_evidence(a, "partial.patch#sha3")
    b = cp.new_item(root, deps=(a,))
    cp.terminate_tree("manual-stop")
    assert cp.items[a].acceptance is Acceptance.ABORTED_FINALIZE
    assert cp.items[a].evidence == ["partial.patch#sha3"]  # 证据保留
    assert cp.used_points() == 0                   # lease 释放
    expect_raise((CapacityError, InvariantError),  # 后继仍不解锁（终态树禁新启动）
                 lambda: cp.start_node(b, "B-model", points=1))


# S12 失败归类（§4）：RATE_LIMIT 背压不进画像；QUOTA 上交+运维审计
@scenario("S12 RATE_LIMIT 只退避、QUOTA 上交人工并进运维审计")
def s12():
    cp = ControlPlane()
    root = cp.init_root()
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2)
    cp.activate(n)
    cp.llm_failure(n, "RATE_LIMIT")
    assert cp.profile == [] and cp.items[a].acceptance is Acceptance.RUNNING
    cp.llm_failure(n, "QUOTA")
    assert cp.items[a].acceptance is Acceptance.ESCALATED
    assert cp.ops_audit == [{"node": n, "code": "QUOTA"}]
    assert all(p.get("verdict") != "quota" for p in cp.profile)  # 不污染能力画像


# S13 满点时 rollover 与 manager 路径不被卡死（B4：manager 不占点数）
@scenario("S13 点数占满：新节点被拒，但 rollover（lease 内）照常")
def s13():
    cp = ControlPlane(max_active_node_points=4)
    root = cp.init_root(lead_points=2)
    a = cp.new_item(root)
    n = cp.start_node(a, "B-model", points=2)      # 4/4 满
    cp.activate(n)
    b = cp.new_item(root)
    expect_raise(CapacityError, lambda: cp.start_node(b, "B-model", points=1))
    cp.rollover_begin(n)                           # lease 内续接不新增点数
    cp.rollover_complete(n)                        # manager 不占点数：预装无准入路径
    assert cp.nodes[n].epoch == 1 and cp.used_points() == 4


if __name__ == "__main__":
    width = max(len(name) for name, _, _ in RESULTS)
    for name, status, detail in RESULTS:
        line = f"[{status}] {name}"
        if detail:
            line += f"  -- {detail}"
        print(line)
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"\n{passed}/{len(RESULTS)} 场景通过")
