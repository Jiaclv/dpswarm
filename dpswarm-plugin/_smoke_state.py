# -*- coding: utf-8 -*-
"""临时冒烟：state 投影 + invariants 校验（正例链 + 负例拒）。

覆盖任务书要求：手工构造事件序列（root_started -> work_item_created(derive)
-> lease_acquired -> node_provisioning -> node_activated -> work_item_submitted
-> work_item_finalizing -> node_drained -> lease_released -> work_item_accepted）
逐事件经 invariants.check_event 验证全部通过，并验证负例被拒
（超槽 / 深度超限 / 非 finalizing 直接 accepted / DAG 环 / id 复用 等）。

用法：python _smoke_state.py
"""
from dpswarm import invariants, state
from dpswarm.events import Event

_seq = 0


def ev(event_kind, **payload):
    global _seq
    _seq += 1
    return Event(seq=_seq, kind=event_kind, payload=payload)


ROUTE_B = {"provider": "p1", "model": "m-b", "reasoning_effort": "low",
           "level": "B", "source": "lead"}


def spec_dict(**kw):
    base = {
        "max_open_work_items": 2, "max_active_node_points": 4,
        "subteam_point_ratio": 0.5, "max_depth": 2, "max_team_workers": 3,
        "max_attempts": 3, "revision": 1, "spec_id": "spec-smoke",
    }
    base.update(kw)
    return base


def run(p, *events):
    for e in events:
        p = invariants.check_event(p, e)
    return p


def fresh(**spec_kw):
    return run(state.replay([]), ev("root_started", root_id="root-1", spec=spec_dict(**spec_kw)))


def spawn_worker(p, iid, nid, lid, points=1, level="B", dd=2, submit=False):
    """fresh 场景下的标准 worker 生命周期前缀（root team 无 lead，级别/深度
    的 lead 参照不存在时按实现约定跳过校验）。"""
    events = [
        ev("work_item_created", item_id=iid, kind="derive", parent_item=None,
           team="root", depth=2, deps=[]),
        ev("lease_acquired", lease_id=lid, node_id=nid, points=points),
        ev("node_provisioning", node_id=nid, item_id=iid, role="worker",
           route=ROUTE_B, level=level, start_type="new", lease_id=lid,
           delegation_depth=dd, team="root"),
        ev("node_activated", node_id=nid, session_id="sess-" + nid),
    ]
    if submit:
        events.append(ev("work_item_submitted", item_id=iid, attempt=1, node_id=nid))
    return run(p, *events)


results = []


def ok(label, cond):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label)


def expect_reject(p, event, code, label):
    try:
        invariants.check_event(p, event)
    except invariants.InvariantViolation as e:
        good = e.code == code
        results.append(good)
        suffix = "" if good else f"  (want {code})"
        print(("PASS " if good else "FAIL ") + label + f"  -> [{e.code}]{suffix}")
        return
    results.append(False)
    print(f"FAIL {label}  -> not rejected (want {code})")


# ===========================================================================
print("== A. positive chain (task-required sequence) ==")
chain = [
    ev("root_started", root_id="root-1", spec=spec_dict()),
    ev("work_item_created", item_id="w1", kind="derive", parent_item=None,
       team="root", depth=2, deps=[]),
    ev("lease_acquired", lease_id="l1", node_id="n1", points=2),
    ev("node_provisioning", node_id="n1", item_id="w1", role="worker",
       route=ROUTE_B, level="B", start_type="new", lease_id="l1",
       delegation_depth=2, team="root"),
    ev("node_activated", node_id="n1", session_id="sess-1"),
    ev("work_item_submitted", item_id="w1", attempt=1, node_id="n1"),
    ev("work_item_finalizing", item_id="w1"),
    ev("node_drained", node_id="n1"),
    ev("lease_released", lease_id="l1"),
    ev("package_stored", package_id="pkg-1", item_id="w1", content_hash="h", size=1),
    ev("work_item_accepted", item_id="w1", evidence_ready=True, package_id="pkg-1"),
]
p = state.replay([])
for e in chain:
    p = invariants.check_event(p, e)
ok("A1 10-event chain passes check_event one by one",
   p.work_items["w1"].acceptance.value == "accepted"
   and p.open_worker_slots_used == 0 and p.active_points == 0)
ok("A2 slot released and node tombstoned after accepted",
   "w1" in p.terminated_item_ids and "n1" in p.terminated_node_ids)
ok("A3 item_ready(w1) (no deps)", p.item_ready("w1"))
pr = state.replay(chain)
ok("A4 replay == incremental projection",
   pr.work_items["w1"].acceptance == p.work_items["w1"].acceptance
   and pr.open_worker_slots_used == p.open_worker_slots_used
   and pr.active_points == p.active_points
   and pr.graph_revision == p.graph_revision)
ok("A5 attempt accounting: first submit uses attempt=1",
   state.replay(chain[:6]).work_items["w1"].attempt == 1)

# ===========================================================================
print("== B. retry path (rejected -> retried attempt=2 -> accepted) ==")
b = spawn_worker(fresh(), "w1", "n1", "l1", submit=True)
b = run(b, ev("work_item_rejected", item_id="w1", reason="wrong output",
              attribution="capability", rejected_by={"node": "root-lead"}))
b = run(b,
        ev("node_drained", node_id="n1"),
        ev("lease_released", lease_id="l1"),
        ev("lease_acquired", lease_id="l2", node_id="n2", points=1),
        ev("node_provisioning", node_id="n2", item_id="w1", role="worker",
           route={"provider": "p1", "model": "m-a", "level": "A", "source": "lead"},
           level="A", start_type="new", lease_id="l2", delegation_depth=2, team="root"),
        ev("node_activated", node_id="n2", session_id="sess-2"),
        ev("work_item_retried", item_id="w1", attempt=2),
        ev("work_item_submitted", item_id="w1", attempt=2, node_id="n2"),
        ev("work_item_finalizing", item_id="w1"),
        ev("node_drained", node_id="n2"),
        ev("lease_released", lease_id="l2"),
        ev("package_stored", package_id="pkg-2", item_id="w1", content_hash="h", size=1),
        ev("work_item_accepted", item_id="w1", evidence_ready=True, package_id="pkg-2"))
ok("B1 rejected -> retried -> accepted with attempt=2",
   b.work_items["w1"].acceptance.value == "accepted" and b.work_items["w1"].attempt == 2)

# ===========================================================================
print("== C. fission creates child team; seal linear phases ==")
c = fresh()
c = run(c,
        ev("work_item_created", item_id="w0", kind="root", parent_item=None,
           team="root", depth=1, deps=[]),
        ev("lease_acquired", lease_id="l0", node_id="n0", points=1),
        ev("node_provisioning", node_id="n0", item_id="w0", role="root-lead",
           route={"provider": "p1", "model": "m-s", "level": "S", "source": "lead"},
           level="S", start_type="new", lease_id="l0", delegation_depth=1, team="root"),
        ev("node_activated", node_id="n0", session_id="sess-0"),
        ev("node_role_changed", node_id="n0", new_role="root-lead", team="root"),
        ev("work_item_created", item_id="wf", kind="fission", parent_item="w0",
           team="root", depth=2, deps=[], new_team_id="t1"))
ok("C1 fission by S lead creates child team t1 (cap=floor(4*0.5)=2)",
   c.teams.get("t1") is not None and c.teams["t1"].local_point_cap == 2
   and c.teams["t1"].parent_team == "root" and c.teams["root"].lead_node == "n0")
c = run(c, ev("seal_admission_cutoff", team="root"))
ok("C2 seal root -> CUTOFF", c.seal_phase["root"].value == "cutoff")
expect_reject(c, ev("work_item_created", item_id="wx", kind="derive",
                    parent_item="w0", team="root", depth=2),
              "SEALED_ADMISSION", "C3 cutoff rejects new work_item_created")
c = run(c, ev("seal_settlement_started", team="root"), ev("seal_completed", team="root"))
ok("C4 CUTOFF->SETTLEMENT->COMPLETED linear", c.seal_phase["root"].value == "completed"
   and c.teams["root"].sealed)

# ===========================================================================
print("== D. split pair peer channel (assistant + messages) ==")
d = fresh()
d = run(d,
        ev("work_item_created", item_id="w9", kind="derive", parent_item=None,
           team="root", depth=2, deps=[]),
        ev("lease_acquired", lease_id="l9", node_id="n9", points=1),
        ev("node_provisioning", node_id="n9", item_id="w9", role="worker",
           route=ROUTE_B, level="B", start_type="new", lease_id="l9",
           delegation_depth=2, team="root"),
        ev("node_activated", node_id="n9", session_id="sess-9"),
        ev("lease_acquired", lease_id="l10", node_id="n10", points=1),
        ev("node_provisioning", node_id="n10", item_id="w9", role="assistant",
           assistant_of="n9", route=ROUTE_B, level="B", start_type="new",
           lease_id="l10", delegation_depth=3, team="root"),
        ev("node_activated", node_id="n10", session_id="sess-10"),
        ev("peer_channel_opened", channel_id="c1", primary_node="n9",
           assistant_node="n10", item_id="w9"),
        ev("message_queued", message_id="m1", channel_id="c1", from_node="n9",
           to_node="n10", content="interface contract"),
        ev("message_delivered", message_id="m1"),
        ev("peer_channel_closed", channel_id="c1"))
ok("D1 assistant shares item; open->queued->delivered->closed",
   d.nodes["n10"].assistant_of == "n9" and d.nodes["n10"].item_id == "w9"
   and d.messages["m1"]["delivered"] and d.peer_channels["c1"]["closed"])
expect_reject(d, ev("message_queued", message_id="m2", channel_id="c1",
                    from_node="n9", to_node="n10", content="late"),
              "CHANNEL_CLOSED", "D2 queued after channel close rejected")
expect_reject(d, ev("message_delivered", message_id="m1"),
              "DUPLICATE_DELIVERY", "D3 duplicate delivered rejected")
expect_reject(d, ev("work_item_submitted", item_id="w9", attempt=1, node_id="n10"),
              "ASSISTANT_SUBMIT", "D4 assistant cannot submit acceptance")

# ===========================================================================
print("== E. required negatives ==")
# E1 超槽（max_open_work_items=2）
e1 = fresh()
e1 = run(e1,
         ev("work_item_created", item_id="w1", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]),
         ev("work_item_created", item_id="w2", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]))
ok("E1a two open items fill max_open_work_items=2", e1.open_worker_slots_used == 2)
expect_reject(e1, ev("work_item_created", item_id="w3", kind="derive",
                     parent_item=None, team="root", depth=2, deps=[]),
              "SLOT_EXCEEDED", "E1b 3rd open item rejected (slot over cap)")

# E2 深度超限（max_depth=2）
e2 = fresh()
e2 = run(e2, ev("work_item_created", item_id="w1", kind="derive",
                parent_item=None, team="root", depth=2, deps=[]))
expect_reject(e2, ev("work_item_created", item_id="w2", kind="derive",
                     parent_item="w1", team="root", depth=3, deps=[]),
              "DEPTH_EXCEEDED", "E2 depth 3 > max_depth 2 rejected")

# E3 非 finalizing 直接 accepted
e3 = spawn_worker(fresh(), "w1", "n1", "l1", submit=True)
expect_reject(e3, ev("work_item_accepted", item_id="w1", evidence_ready=True,
                     package_id="pkg-x"),
              "ILLEGAL_TRANSITION", "E3 accepted from SUBMITTED (not FINALIZING) rejected")

# E4 DAG 环
e4 = fresh()
e4 = run(e4,
         ev("work_item_created", item_id="wa", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]),
         ev("work_item_created", item_id="wb", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]),
         ev("work_item_dependency_added", before="wa", after="wb",
            expected_graph_revision=0))
ok("E4a dependency CAS bumps graph_revision to 1",
   e4.graph_revision == 1 and ("wa", "wb") in e4.edges and not e4.item_ready("wb"))
expect_reject(e4, ev("work_item_dependency_added", before="wb", after="wa",
                     expected_graph_revision=1),
              "CYCLE", "E4b cycle wa->wb->wa rejected by full-graph recheck")
ok("E4c original projection untouched after rejection",
   e4.graph_revision == 1 and len(e4.edges) == 1)

# E5 id 复用
e5 = run(fresh(), *chain[1:])  # w1 已 accepted（terminated_item_ids 含 w1）
expect_reject(e5, ev("work_item_created", item_id="w1", kind="derive",
                     parent_item=None, team="root", depth=2, deps=[]),
              "ID_REUSE", "E5a terminated work item id reuse rejected")
expect_reject(e5, ev("node_provisioning", node_id="n1", item_id="w1", role="worker",
                     route=ROUTE_B, level="B", start_type="rollover", lease_id="l1",
                     delegation_depth=2, team="root"),
              "ID_REUSE", "E5b terminated node id reuse rejected")
e5b = spawn_worker(fresh(), "w1", "n1", "l1")
expect_reject(e5b, ev("node_provisioning", node_id="n1", item_id="w1", role="worker",
                      route=ROUTE_B, level="B", start_type="new", lease_id="l1",
                      delegation_depth=2, team="root"),
              "ID_REUSE", "E5c existing node + NEW start_type rejected")

# ===========================================================================
print("== F. extra negatives ==")
f1 = fresh()
f1 = run(f1,
         ev("work_item_created", item_id="wa", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]),
         ev("work_item_created", item_id="wb", kind="derive", parent_item=None,
            team="root", depth=2, deps=[]),
         ev("work_item_dependency_added", before="wa", after="wb",
            expected_graph_revision=0))
expect_reject(f1, ev("work_item_dependency_added", before="wa", after="wb",
                     expected_graph_revision=7),
              "CAS_MISMATCH", "F1 stale expected_graph_revision rejected")

f2 = run(spawn_worker(fresh(), "w1", "n1", "l1", submit=True),
         ev("work_item_finalizing", item_id="w1"))
expect_reject(f2, ev("work_item_accepted", item_id="w1", evidence_ready=False,
                     package_id="pkg-x"),
              "EVIDENCE_NOT_READY", "F2 accepted without evidence_ready rejected")

f3 = spawn_worker(fresh(), "w1", "n1", "l1")  # 未提交
expect_reject(f3, ev("work_item_submitted", item_id="w1", attempt=2, node_id="n1"),
              "ATTEMPT_MISMATCH", "F3 submit with attempt=2 (current 1) rejected")

# F4 裂变权限：A 级 lead 不得裂变（§7）
f4 = fresh()
f4 = run(f4,
         ev("work_item_created", item_id="w0", kind="root", parent_item=None,
            team="root", depth=1, deps=[]),
         ev("lease_acquired", lease_id="l0", node_id="n0", points=1),
         ev("node_provisioning", node_id="n0", item_id="w0", role="root-lead",
            route={"provider": "p1", "model": "m-a", "level": "A", "source": "lead"},
            level="A", start_type="new", lease_id="l0", delegation_depth=1, team="root"),
         ev("node_activated", node_id="n0", session_id="s0"),
         ev("node_role_changed", node_id="n0", new_role="root-lead", team="root"))
expect_reject(f4, ev("work_item_created", item_id="wf", kind="fission",
                     parent_item="w0", team="root", depth=2, new_team_id="t1"),
              "FISSION_FORBIDDEN", "F4 fission by A-level lead rejected")

# F5/F6 级别方向与 human_override（决策 5）
f4 = run(f4,
         ev("work_item_created", item_id="w1", kind="derive", parent_item="w0",
            team="root", depth=2, deps=[]),
         ev("lease_acquired", lease_id="l1", node_id="n1", points=1))
expect_reject(f4, ev("node_provisioning", node_id="n1", item_id="w1", role="worker",
                     route={"provider": "p1", "model": "m-s", "level": "S", "source": "lead"},
                     level="S", start_type="new", lease_id="l1", delegation_depth=2,
                     team="root"),
              "LEVEL_DIRECTION", "F5 S worker under A lead rejected")
f4 = run(f4,
         ev("node_provisioning", node_id="n1", item_id="w1", role="worker",
            route={"provider": "p1", "model": "m-s", "level": "S", "source": "human"},
            level="S", start_type="new", lease_id="l1", delegation_depth=2,
            team="root", human_override=True),
         ev("node_activated", node_id="n1", session_id="s1"))
ok("F6 human_override=True skips level-direction check", f4.nodes["n1"].level.value == "S")

# F7/F8 lease reweight（决策 14）
f7 = spawn_worker(fresh(), "w1", "n1", "l1", submit=True)
expect_reject(f7, ev("lease_reweight", lease_id="l1", old_points=5, new_points=6, delta=1),
              "REWEIGHT_MISMATCH", "F7 reweight with wrong old_points rejected")
f7 = run(f7, ev("lease_reweight", lease_id="l1", old_points=1, new_points=2, delta=1))
ok("F8 reweight applies new_points atomically",
   f7.leases["l1"].points == 2 and f7.active_points == 2)

# F9/F10 successor CAS（决策 8）
f9 = spawn_worker(fresh(), "w1", "n1", "l1", submit=True)
f9 = run(f9, ev("successor_registered", node_id="n1", context_epoch=0,
                capsule_ref="cap-1", capsule_hash="h1", control_state_revision=1))
expect_reject(f9, ev("successor_registered", node_id="n1", context_epoch=0,
                     capsule_ref="cap-2", capsule_hash="h2", control_state_revision=2),
              "DOUBLE_SUCCESSOR", "F9 duplicate successor (node,epoch) rejected")
f9 = run(f9, ev("successor_reset", node_id="n1", context_epoch=0,
                cause="activated-success"))
ok("F10 successor_reset clears registration", ("n1", 0) not in f9.successor_regs)

# F11/F12 路由对账：人工优先不得静默替换（决策 12）
expect_reject(f9, ev("route_resolved", node_id="n1", source="human",
                     proposed={"provider": "p1", "model": "m-a", "reasoning_effort": "high"},
                     resolved={"provider": "p1", "model": "m-b", "reasoning_effort": "high"}),
              "SILENT_OVERRIDE", "F11 human route silently replaced rejected")
f9 = run(f9, ev("route_resolved", node_id="n1", source="human",
                proposed={"provider": "p1", "model": "m-a", "reasoning_effort": "high"},
                resolved={"provider": "p1", "model": "m-a", "reasoning_effort": "high"}))
ok("F12 matching human route resolution recorded", len(f9.route_resolutions) == 1)

# G 点数超上限（决策 4）
g = fresh(max_active_node_points=3)
g = run(g,
        ev("work_item_created", item_id="w1", kind="derive", parent_item=None,
           team="root", depth=2, deps=[]),
        ev("lease_acquired", lease_id="l1", node_id="n1", points=2))
expect_reject(g, ev("lease_acquired", lease_id="l2", node_id="n2", points=2),
              "POINTS_EXCEEDED", "G1 active points over root cap rejected")

# ===========================================================================
failed = sum(1 for r in results if not r)
print()
print(f"total={len(results)} passed={len(results) - failed} failed={failed}")
if failed:
    raise SystemExit(1)
print("SMOKE OK")
