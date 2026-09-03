"""DPswarm 事件词汇表与事件存储（§9.1：事件先于状态，校验先于落盘）。

控制面事件属于 root task，不依附任何 agent 的物理 session（选址：独立存储）。
事件是唯一真源；全部状态（DAG、节点、lease、roster、点数占用）由回放派生。

词汇分组对应机制文档：
  root/spec        §2.1   边界合同 revision
  work_item        §5.7   验收状态机
  node             §9.3   两阶段启动 / 唤醒通知（§9.4 不带状态）
  resource         §7     槽位与点数 lease（含原子 reweight）
  rollover         §5.8   CAS 唯一 successor + 三路复位
  seal             §9.6   封存三段式
  peer             §9.5   分裂对双向通道（其余星型）
  route            §2     路由来源/提议/解析 持久化对账
  observation      §4     观测全账（token 分账、stopReason、验收者身份）
  memory           §5.6/5.7  分层记忆与晋升
  human            §9.2   人工指令三类
  watchdog         §9.2   只投建议事件，由唯一写者消费
  economics        §6     委派经济性（Lead 消耗 vs 委派节省）
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .types import HumanDirective

# ---------------------------------------------------------------------------
# Event：值对象。payload 字段名是 state/invariants/control 三方契约。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    seq: int                 # 单调递增，per-root 连续
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "payload": self.payload, "ts": self.ts}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Event":
        return Event(seq=d["seq"], kind=d["kind"], payload=d.get("payload", {}), ts=d.get("ts", 0.0))


EVENT_KINDS = {
    # root / spec（§2.1）
    "root_started",
    "spec_published",
    # work item 验收流（§5.7）
    "work_item_created",
    "work_item_dependency_added",
    "work_item_submitted",
    "work_item_finalizing",
    "work_item_accepted",
    "work_item_rejected",
    "work_item_retried",
    "work_item_timeout_retried",   # §7 时间护栏①：超时重试（计预算），acceptance 不变
    "work_item_escalated",
    "work_item_terminated",
    "work_item_aborted_finalize",
    # 节点物理生命周期（§9.3）与通知（§9.4）
    "node_provisioning",
    "node_activated",
    "node_failed",
    "node_blocked",
    "node_unblocked",
    "node_drained",
    "node_role_changed",
    "node_wakeup",
    # 资源：worker 槽（占/放由 item 终态推导）+ 点数 lease（§7）
    "lease_acquired",
    "lease_released",
    "lease_reweight",
    # §9.3 reweight-wait 抑制窗口：换模型增重点数差额不足 → 节点标记等待，
    # tick 超时豁免；waiting=True/False 记进/出（出由成功 reweight 同事务携带）
    "node_reweight_wait",
    # 硬切窗口 CAS（§5.8）
    "successor_registered",
    "successor_reset",
    # 封存三段式（§9.6）
    "seal_admission_cutoff",
    "seal_settlement_started",
    "seal_completed",
    "seal_timed_out",
    # 分裂对 peer 通道（§9.5）
    "peer_channel_opened",
    "message_queued",
    "message_delivered",
    "peer_channel_closed",
    # 路由对账（§2）
    "route_resolved",
    # 观测（§4）
    "observation_recorded",
    "token_usage_recorded",
    "stop_reason_recorded",
    # 记忆与语义包（§5.3 / §5.6 / §5.7）
    "package_stored",
    "memory_candidate",
    "memory_promoted",
    "memory_superseded",
    "memory_invalidated",
    "memory_rejected",            # §5.7：候选未过 promotion check（≠ durable 失效）
    # 人工指令（§9.2）
    "human_directive",
    # watchdog 建议事件（§9.2：不直接执行变更）
    "watchdog_suggested",
    # 委派经济性（§6）
    "delegation_economics_recorded",
}


class EventStore:
    """JSONL 事件日志（§9.1：事件是唯一真源，状态由回放派生）。

    可靠性（事务 envelope，替代逐条追加）：
    - 一次事务 = 一行 envelope（{"txn": id, "events": [...]}），单次 write +
      flush + fsync——崩溃只可能留下**残缺尾行**，不存在半个事务
    - 磁盘写成功后才推进内存（旧实现相反，磁盘异常会造成内存/盘面分叉）
    - 回放 fail-closed：尾行残缺丢弃并告警；文件中段损坏、seq 不严格 +1
      连续 → 直接抛错（宁可不启动，不带着脏账本启动）
    - 跨进程文件锁（<path>.lock，标准库 msvcrt/fcntl）：单写者纪律的物理
      兜底——第二个进程对同一日志建 ControlPlane 即刻失败，而不是各写各的
      内存计数把 seq 写重（实测缺陷：双写者尾部 seq=[7,7]）
    - 兼容旧格式：裸事件行（无 envelope）按单事件事务回放

    单写者进程（§9.2）：一个 root 一条事务链一个写者；本类不做并发控制，
    串行化由 control.ControlPlane 的队列保证。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._events: List[Event] = []
        self._txn_id = 0
        self._lock_fh = None
        if path is not None:
            self._acquire_writer_lock(Path(path))
            if Path(path).exists():
                self._load()

    # -- 单写者文件锁 -------------------------------------------------------

    def _acquire_writer_lock(self, path: Path) -> None:
        lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+b")
            # 先锁后写 pid：锁上 1 字节区，成功者把 pid 写进自己持有的区域
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            try:
                fh.close()
            except Exception:
                pass
            raise RuntimeError(
                f"EVENT_LOG_LOCKED: {path} 已被另一个写者进程持有（单写者纪律，§9.2）。"
                f"若确无其他进程，删除 {lock_path} 后重试。({e})") from e
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(str(os.getpid()).encode() + b"\n")
            fh.flush()
        except OSError:
            pass  # pid 仅为诊断信息，写不进不影响锁语义
        self._lock_fh = fh

    def close(self) -> None:
        """释放写者锁（测试里同进程重建 ControlPlane 复盘时用）。"""
        if self._lock_fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._lock_fh.seek(0)
                    msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    # -- 读取 ---------------------------------------------------------------

    def read_all(self) -> List[Event]:
        return list(self._events)

    @property
    def last_seq(self) -> int:
        return self._events[-1].seq if self._events else -1

    # -- 写入（flush 后才返回）----------------------------------------------

    def append(self, kind: str, payload: Dict[str, Any]) -> Event:
        """单事件事务（envelope 一行）。"""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        return self.append_txn([Event(seq=self.last_seq + 1, kind=kind, payload=payload)])[0]

    def append_txn(self, events: List[Event]) -> List[Event]:
        """整事务单行 envelope 追加：一行 = 一次 write+flush+fsync。

        事件序号由调用方按 last_seq+1 连续编好；这里做最后核对（防线）。
        磁盘成功后才推进内存。"""
        if not events:
            return []
        for i, ev in enumerate(events):
            if ev.kind not in EVENT_KINDS:
                raise ValueError(f"unknown event kind: {ev.kind}")
            if ev.seq != self.last_seq + 1 + i:
                raise RuntimeError(
                    f"TXN_SEQ_GAP: 事件 seq 不连续（期望 {self.last_seq + 1 + i}，"
                    f"得到 {ev.seq}）——事务拒绝落盘")
        self._txn_id += 1
        if self.path is not None:
            envelope = {"txn": self._txn_id, "events": [e.to_dict() for e in events]}
            line = json.dumps(envelope, ensure_ascii=False, default=_json_default) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        self._events.extend(events)
        return events

    # -- 恢复 ---------------------------------------------------------------

    def _load(self) -> None:
        raw_lines: List[str] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                raw_lines.append(line)
        # fail-closed：仅允许**尾行**残缺（崩溃窗口）；中段损坏必须炸
        parsed: List[Dict[str, Any]] = []
        for i, line in enumerate(raw_lines):
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(raw_lines) - 1:
                    print(f"[dpswarm] 事件日志尾行残缺（崩溃残留），整事务丢弃: {line[:80]}…")
                    break
                raise RuntimeError(f"EVENT_LOG_CORRUPT: 第 {i + 1} 行不是合法 JSON")
        expect_seq = 0
        for d in parsed:
            events = d.get("events") if isinstance(d, dict) and "events" in d else [d]
            if not isinstance(events, list):
                raise RuntimeError("EVENT_LOG_CORRUPT: envelope 缺 events 数组")
            for ed in events:
                try:
                    ev = Event.from_dict(ed)
                except (KeyError, TypeError) as e:
                    raise RuntimeError(f"EVENT_LOG_CORRUPT: 事件字段缺失 {e}") from e
                if ev.seq != expect_seq:
                    raise RuntimeError(
                        f"EVENT_LOG_CORRUPT: seq 不连续（期望 {expect_seq}，"
                        f"得到 {ev.seq}）——拒绝带脏账本启动")
                expect_seq += 1
                self._events.append(ev)
        self._txn_id = len(parsed)


def _json_default(o: Any) -> Any:
    if isinstance(o, HumanDirective):
        return {"kind": o.kind, "payload": o.payload, "issued_at": o.issued_at}
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(f"unserializable: {type(o)}")


# ---------------------------------------------------------------------------
# 观测记录（§4 全账）。字段级完整 schema 属延伸设计，此处为机制要求的最小集。
# ---------------------------------------------------------------------------


@dataclass
class DelegationRecord:
    """每次委派记一笔全账：谁委派给谁、路由、拓扑上下文、验收者、终局。"""

    record_id: str
    item_id: str
    node_id: str
    lead_node_id: str
    route: Dict[str, Any]                 # provider/model/effort/level/source
    topology: str                         # derive / split-primary / split-assistant / fission-worker
    team: str
    attempt: int = 1
    stop_reason: Optional[str] = None     # completed/error/max-tokens/aborted/refusal
    outcome: Optional[str] = None         # accepted/rejected-exhausted/escalated/timeout/...
    accepted_by: Optional[Dict[str, Any]] = None   # 验收 Lead 路由与级别（分层验收 §4）
    rejected_by: Optional[Dict[str, Any]] = None
    attribution: Optional[str] = None     # 打回归因（免费失败归因数据）
    token_input: int = 0
    token_output: int = 0
    token_cache_read: int = 0             # 缓存读写与 input 不相交，单独计账（§4）
    token_cache_write: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0

    def to_payload(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id, "item_id": self.item_id,
            "node_id": self.node_id, "lead_node_id": self.lead_node_id,
            "route": self.route, "topology": self.topology, "team": self.team,
            "attempt": self.attempt, "stop_reason": self.stop_reason,
            "outcome": self.outcome, "accepted_by": self.accepted_by,
            "rejected_by": self.rejected_by, "attribution": self.attribution,
            "token_input": self.token_input, "token_output": self.token_output,
            "token_cache_read": self.token_cache_read,
            "token_cache_write": self.token_cache_write,
            "cost_usd": self.cost_usd, "wall_seconds": self.wall_seconds,
        }
