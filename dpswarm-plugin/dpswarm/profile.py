"""画像存储（§8：V1 只攒不用 + §4 failure audit）。

V1 边界（§8，明确）：**画像只攒不用**——本存储持续积累"某模型在某任务桶
的实测表现"，但不参与模型选择；V1 选型依据 = AA 分维度评分 + 目录价目
+ bench 产物（§3 事实注入），自家生产画像不回流注入。画像何时接管选择
由实验层按数据成熟度判定，是 V1 之后的开关。

§4 排除规则（负面样本卫生）：
- **rate-limit（HTTP 429）是背压信号**：退避重试即可，不进 failure
  audit、不进能力画像——本类将其路由到 ops 运维表；
- **quota（401/402/403，余额/鉴权耗尽）不是背压**：明确终止/上交人工，
  进 ops 运维审计表，同样不进能力画像（它不是模型能力失败）；
- **rejected 样本进 failures 表**：打回理由是免费的失败归因数据
  （§4/§8），全量保留供 failure_audit() 审计。

实现：sqlite3 标准库；单写者模型（§9.2），本类不做并发控制。
path=None 时使用内存库（:memory:），适合测试与一次性聚合。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    -- 画像正表（V1 只攒不用，§8）：按 (model, bucket) 攒实测。
    -- rate-limit / quota 样本不写入此表（见 ops 表），不污染画像（§4）。
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    outcome     TEXT NOT NULL,          -- accepted/rejected/rejected-exhausted/escalated/timeout/...
    attribution TEXT,                   -- 打回归因四分支（§8），可为 None
    stop_reason TEXT,                   -- harness 侧五值（§4），可为 None
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_model_bucket ON attempts(model, bucket);

CREATE TABLE IF NOT EXISTS failures (
    -- failure audit（§4/§8）：全部 rejected 样本，含理由与归因。
    -- 供失败归因晋升 failure memory / 换模型重试决策使用。
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    attribution TEXT,
    stop_reason TEXT,
    reason      TEXT,                   -- 打回理由（免费失败归因，§4）
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ops (
    -- 运维审计（§4）：rate-limit（背压，可重试）与 quota（配额/鉴权，
    -- 明确终止/上交人工）分列记录，均不进能力画像。
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- 'rate-limit' | 'quota'
    model       TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    outcome     TEXT,
    stop_reason TEXT,
    detail      TEXT,
    ts          REAL NOT NULL
);
"""


def _is_rejected(outcome: Optional[str]) -> bool:
    return bool(outcome) and str(outcome).startswith("rejected")


class ProfileStore:
    """画像：按 (model, task_bucket) 攒实测。V1 只写入、不参与选择（§8）。

    sqlite3 标准库实现；``path=None`` 使用内存库。使用 ``rate_limit=True``
    或 ``quota=True``（或 outcome/stop_reason 字符串中含 "rate-limit"/"quota"）
    的样本进 ops 运维表，不进 attempts 画像表（§4 排除规则）。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._conn = sqlite3.connect(path or ":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- 写入 ----------------------------------------------------------------

    def record_attempt(self, model: str, task_bucket: str, outcome: str,
                       attribution: Optional[str] = None,
                       stop_reason: Optional[str] = None, *,
                       rate_limit: bool = False,
                       quota: bool = False,
                       reason: Optional[str] = None) -> None:
        """记一笔 attempt。

        :param outcome: accepted / rejected / rejected-exhausted / escalated /
            timeout / ...（§4 第二层终局）
        :param attribution: 打回归因（capability/context/description/
            contradiction，§8），非打回样本为 None
        :param stop_reason: harness 侧五值（§4），可为 None
        :param rate_limit: True 时该样本为 429 背压——进 ops 表、不进画像
        :param quota: True 时该样本为配额/鉴权耗尽——进 ops 表、不进画像
        :param reason: 打回理由自由文本（进 failures 表，§4 免费归因）
        """
        # 兜底自动识别：显式 flag 优先，outcome/stop_reason 文本兜底
        hint = " ".join(filter(None, [str(outcome or ""), str(stop_reason or "")])).lower()
        if not rate_limit and ("rate-limit" in hint or "rate_limit" in hint or "429" in hint):
            rate_limit = True
        if not quota and "quota" in hint:
            quota = True
        ts = time.time()
        if rate_limit or quota:
            kind = "rate-limit" if rate_limit else "quota"
            self._conn.execute(
                "INSERT INTO ops(kind, model, bucket, outcome, stop_reason, detail, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (kind, model, task_bucket, outcome, stop_reason, reason, ts))
            self._conn.commit()
            return
        # §8「原 attempt 记 escalated——无裁决、不进画像（它不是模型能力失败）」；
        # §7 退化收回记 terminated「不算验收、不进画像」——与 429/QUOTA 同走 ops 表。
        if outcome in ("escalated", "terminated", "deadline-stopped", "manual-stopped"):
            self._conn.execute(
                "INSERT INTO ops(kind, model, bucket, outcome, stop_reason, detail, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                ("no-verdict", model, task_bucket, outcome, stop_reason, reason, ts))
            self._conn.commit()
            return
        self._conn.execute(
            "INSERT INTO attempts(model, bucket, outcome, attribution, stop_reason, ts) "
            "VALUES(?,?,?,?,?,?)",
            (model, task_bucket, outcome, attribution, stop_reason, ts))
        if _is_rejected(outcome):
            self._conn.execute(
                "INSERT INTO failures(model, bucket, outcome, attribution, "
                "stop_reason, reason, ts) VALUES(?,?,?,?,?,?,?)",
                (model, task_bucket, outcome, attribution, stop_reason, reason, ts))
        self._conn.commit()

    # -- 读取（V1 只攒不用：以下查询供审计与实验层消费，不接路由）-----------

    def bucket_stats(self, model: str, task_bucket: str) -> dict:
        """该 (model, bucket) 的实测画像汇总（V1 不注入、不参与选择，§8）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS attempts,"
            " SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted,"
            " SUM(CASE WHEN outcome LIKE 'rejected%' THEN 1 ELSE 0 END) AS rejected"
            " FROM attempts WHERE model=? AND bucket=?",
            (model, task_bucket)).fetchone()
        attempts = int(row["attempts"] or 0)
        accepted = int(row["accepted"] or 0)
        rejected = int(row["rejected"] or 0)
        return {
            "attempts": attempts,
            "accepted": accepted,
            "rejected": rejected,
            "accept_rate": (accepted / attempts) if attempts else 0.0,
        }

    def failure_audit(self) -> List[dict]:
        """全部 rejected 样本（含打回理由与归因，§4/§8 failure audit）。"""
        rows = self._conn.execute(
            "SELECT model, bucket, outcome, attribution, stop_reason, reason, ts "
            "FROM failures ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def ops_audit(self) -> List[dict]:
        """rate-limit / quota 运维审计（§4：与能力画像分离）。"""
        rows = self._conn.execute(
            "SELECT kind, model, bucket, outcome, stop_reason, detail, ts "
            "FROM ops ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # -- 生命周期 ------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProfileStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
