"""Write the user-requested immutable experiment record and handoff, without running models."""
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
BATCH = HERE / "pilot_v2"
V = HERE / "validation"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def link(label, path):
    assert path.exists(), path
    return f"[{label}]({path.resolve().as_posix()})"


def write(path, content):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def shown(value):
    if value is None:
        return "null"
    if type(value) is float:
        return f"{value:.3f}"
    if type(value) is int:
        return f"{value:,}"
    return str(value)


def main():
    manifest = read(BATCH / "manifest.json")
    summary = read(BATCH / "summary.json")
    batch = read(BATCH / "batch.json")
    audit = read(V / "pilot_v2.final8.audit.json")
    accounts = read(V / "pilot_v2.agent_accounting.final8/agent_accounting.json")
    shutdown = read(V / "shutdown_snapshot.json")
    draft = read(V / "transport_revision3_wip/status.json")
    results = {p.parent.name: read(p) for p in (BATCH / "results").glob("*/result.json")}
    before = {key: sha(BATCH / "results" / key / "result.json") for key in results}
    assert len(results) == 8 and summary["completed_runs"] == 8
    assert audit["status"] == "PASS"
    assert len(accounts["agents"]) == 22
    assert sum(a["role"] == "worker" for a in accounts["agents"]) == 14
    assert not shutdown["owned_active_containers"]
    assert (BATCH / "STOP_AFTER_PAIR").exists() and not (HERE / "pilot_v3").exists()
    assert all(sha(REPO / p) == h for p, h in manifest["runtime_sources"].items())

    bindings = {}
    for agent in accounts["agents"]:
        for call in agent["call_ids"]:
            assert call not in bindings
            bindings[call] = agent
    calls = []
    for path in sorted(BATCH.glob("results/*/calls/*/metadata.json")):
        record = read(path)
        owner = bindings[record["call_id"]]
        record.update(agent_id=owner["agent_id"], node_id=owner["node_id"],
                      item_id=owner["item_id"], session_id=owner["session_id"],
                      parent_agent_id=owner["parent_agent_id"], arm=owner["arm"],
                      metadata_path=str(path), metadata_sha256=sha(path))
        calls.append(record)
    known = sum(c["total_tokens"] for c in calls if type(c.get("total_tokens")) is int)
    unknown = [c["call_id"] for c in calls if c.get("total_tokens") is None]
    assert len(calls) == 162 and known == 3462637 and len(unknown) == 1
    columns = ["run_id", "arm", "task_id", "agent_id", "role", "node_id", "item_id", "session_id",
               "parent_agent_id", "call_id", "model_requested", "model_reported", "adapter_mode",
               "effort_requested", "effort_reported", "service_tier_requested", "service_tier_reported",
               "transport_attempt_count", "started_at", "completed_at", "wall_seconds",
               "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
               "error", "protocol_error", "metadata_path", "metadata_sha256"]
    with (V / "pilot_v2.calls.final8.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for call in calls:
            writer.writerow({k: "null" if call.get(k) is None else
                             json.dumps(call[k], ensure_ascii=False) if isinstance(call[k], (dict, list))
                             else call[k] for k in columns})

    retained = [r for r in summary["rows"] if r["instance_id"].startswith("matplotlib__")]
    excluded = [r for r in summary["rows"] if r["instance_id"].startswith("sphinx-doc__")]
    unstarted = [e["run_id"] for e in manifest["schedule"] if e["run_id"] not in results]
    evidence = [
        "pilot_v2.final8.audit.json",
        "pilot_v2.agent_accounting.final8/agent_accounting.json",
        "pilot_v2.agent_accounting.final8/agents.csv",
        "pilot_v2.agent_accounting.final8/context_managers.csv",
        "pilot_v2.agent_accounting.final8/call_assignments.json",
        "pilot_v2.timing.final8/timing_summary.json",
        "pilot_v2.timing.final8/agents.csv",
        "pilot_v2.timing.final8/runs.csv",
        "pilot_v2.calls.final8.csv",
        "LUNA_PROTOCOL_ANALYSIS.md", "TERRA_CANCEL_USAGE_RECONCILIATION.md",
        "WORKER_BUDGET_ANALYSIS.md", "GLM_TIMEOUT_ANALYSIS.md",
        "ACTIVATION_ANALYSIS_ZH.md", "shutdown_snapshot.json",
        "transport_revision3_wip/status.json", "transport_revision3_wip/transport.py",
        "transport_revision3_wip/transport.patch",
    ]
    closeout = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status": "USER_PAUSED_AFTER_CURRENT_ROUND",
        "no_more_candidate_runs_authorized_now": True,
        "batch_manifest_sha256": sha(BATCH / "manifest.json"),
        "completed_runs": 8, "scheduled_runs": 12, "unstarted_runs": unstarted,
        "actual_leads": 8, "actual_workers": 14, "call_records": 162,
        "known_total_tokens": known, "total_tokens": None,
        "unknown_usage_call_ids": unknown, "unresolved_admission_reservation": 48761,
        "reservation_is_measured_usage": False, "pending_calls": 0,
        "batch_wall_seconds": batch["wall_seconds"],
        "retained_unaffected_matplotlib_runs": [r["run_id"] for r in retained],
        "excluded_sphinx_pair": [r["run_id"] for r in excluded],
        "exclusion_decided_before_pair_verdicts": True,
        "raw_official_results": {r["run_id"]: r["resolved"] for r in summary["rows"]},
        "raw_result_sha256": before,
        "scope": {"activation": "experiment_protocol", "derive": "real",
                  "autonomous_activation_evaluated": False, "split": "not_exposed",
                  "fission": "not_exposed", "CM": "not_integrated", "CM_usage": None,
                  "DSH_bridge": "not_exercised", "SOTA_claim": False},
        "current_runtime_matches_pilot_v2_manifest": True,
        "transport_fix_draft": draft,
        "pilot_v3_exists": False,
        "all_experiment_history_call_records": 270,
        "all_experiment_history_known_tokens": 6851280,
        "all_experiment_history_total_tokens": None,
        "evidence_sha256": {p: sha(V / p) for p in evidence},
    }
    write(BATCH / "USER_CLOSEOUT.json", json.dumps(closeout, ensure_ascii=False, indent=2) + "\n")

    record = [
        "# 固定 Agent Team 本轮记录（2026-09-03）", "",
        "状态：按用户要求停止后续实验。本批已收束，没有运行中的候选请求或本实验容器。pilot_v3 未创建、未启动。",
        "原计划两题六组共 12 run，实际完成 8 run，剩余 4 run 未启动。当前保留第一题六组作为未触发已知消息拼接缺陷的比较记录；第二题 Luna/Terra pair 单列为受基础设施缺陷影响的记录。", "",
        "本批真实使用 8 名 Sol Lead、14 名 worker，共 22 个 agent 实例和 162 个模型调用记录。已知 Token 小计 3,462,637，另有一次取消调用用量未知，所以实测总 Token 为 null。48,761 是该调用的预算预留，不是实测用量。",
        f"批次墙钟 {batch['wall_seconds']:.3f} 秒（约 32 分 20 秒），包括准备、候选执行、官方评分和清理；各 run/调用并行，不能把它们的耗时相加当作批次墙钟。", "",
        "## 实际测了什么", "",
        "每个固定组在 Sol Lead 首次调用前由实验协议准入两名真实 DPswarm DERIVE worker：生产实现与回归测试。五个候选为 GLM-5.3、GLM-5.3-Flash、GPT Sol、Terra、Luna。每名 worker 有独立 node/item/session、环境、交付与调用账本。",
        "这证明了真实派生团队的运行与记账，不证明模型自主判断开启团队。产品的自动启用策略未被本轮校准。SPLIT/FISSION 未暴露，CM 未接入且用量为 null；没有真实 CM 实例，不能把占位行算成 agent 或零成本测量。DSH 原生桥接仍不在本次验证范围。",
        "GPT 请求 max/fast；GLM 请求 thinking enabled/max。未回显的模型、effort、tier 保留 null。GPT 走 Codex 文本工具适配器，GLM 走原生工具 API；接入方式、提示开销和输出 cap 不同，因此 Token/延迟反映模型加适配器的实际运行，不是纯模型内在能力。",
        "每 run 最多 28 次调用、600,000 Token 准入、1,800 秒执行；worker 各 8 次调用。全局四个模型槽、四个容器槽，两组并行时会排队。", "",
        "## 第一题 Matplotlib：六组完整比较", "",
        "六组官方结果全部 resolved=true。单次任务不能支持稳定排名或 SOTA；也没有在本题证明固定团队提高最终正确率。", "",
        "| 组（Lead 均为 Sol） | 官方通过 | worker 正式完成 | CP 正式采纳 | 调用 | 总 Token | run 墙钟秒 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    mechanisms = {m["run_id"]: m for m in accounts["mechanisms"]}
    for row in retained:
        d = mechanisms[row["run_id"]]["derive"]
        workers = "—" if row["condition"] == "solo" else f"{d['completed']}/2"
        adopted = "—" if row["condition"] == "solo" else str(d["adopted"])
        record.append(f"| {row['arm']} | 是 | {workers} | {adopted} | {row['usage']['calls']} | {shown(row['usage']['total_tokens'])} | {row['wall_seconds']:.3f} |")
    record += [
        "", "两个 GLM 组的四名 worker 均达到 8 次上限，没有正式 finish；四份 delta 非空。每轮提示都包含 local_call/local_limit，不能称为预算漏传。运行器禁止采纳未完成交付，Lead 后续收尾，整题仍通过。交付是否有参考价值、单个 worker 补丁是否正确，不能仅由正式完成状态或整队分数推断。",
        "三个 GPT 组的六名 worker 均完整完成，六份交付获 CP 正式采纳。墙钟受容器排队影响，不据这张表给模型排速度名次。",
        "Matplotlib 的 117 个记录中，85 个 GPT 调用各只有一个非空原始 agent_message；按终端消息解析的动作与旧记录一致。其余 32 个 GLM 调用不走消息拼接路径。", "",
        "## 第二题 Sphinx：保留原始结果，不进入有效模型比较", "",
        "| 组 | 原始官方结果 | 调用 | Token | worker 正式完成 | 处理 |",
        "|---|---|---:|---|---:|---|",
    ]
    for row in excluded:
        d = mechanisms[row["run_id"]]["derive"]
        amount = shown(row["usage"]["total_tokens"])
        if row["usage"]["total_tokens"] is None:
            amount += f"（已知小计 {shown(row['usage']['known_subtotals']['total_tokens'])}）"
        record.append(f"| {row['arm']} | {'通过' if row['resolved'] else '未通过'} | {row['usage']['calls']} | {amount} | {d['completed']}/2 | 整对排除，计入基础设施开销 |")
    record += [
        "", "排除决定在这对官方结果产生前已通过 STOP_AFTER_PAIR 固化。Luna 的 7 个 JSON 错误共消耗 194,640 Token：原始事件内每个非空 JSON 都合法，适配器换行拼接多个消息后才出现 Extra data。原始事件没有 phase 字段，不能凭空称为 commentary/final，但消息边界丢失已证实。",
        "Terra 的 Lead 预算预留失败后，运行器取消了已准入的 worker 请求。它运行了 58.437 秒；“exceeded 600 seconds”是错误文案，不是真实 600 秒超时。原始流只有 thread.started/turn.started，没有完成 usage；调用使用 --ephemeral，按精确 thread ID 未找到可恢复的本地会话。该调用用量继续为 unknown。",
        "未经处理的总表显示 7/8 官方通过，这是混有基础设施异常的原始记录，不能用作 87.5% 模型能力结论。Sphinx 的其余四组（Sol、Flash、GLM、solo）未运行。", "",
        "## 按模型和角色记账", "",
        "| 路由模型 | 角色 | 调用 | Input（含 cache） | Cache | Output（含 reasoning） | Reasoning | Total | 累计调用秒 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, data in summary["models"].items():
        for role, value in data["roles"].items():
            if value["calls"]:
                record.append(f"| {model} | {role} | " + " | ".join(shown(value[k]) for k in
                    ["calls", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "sum_call_wall_seconds"]) + " |")
    record += [
        "", "null 表示未观测到完整值，已知小计保存在 JSON/CSV；cache 和 reasoning 是 input/output 的子维度，不能重复累计。父 agent 不包含子 agent 用量，即使都使用 Sol，也分开归属。取消/错误调用保留在账本中。未核实实际收费，美元费用为 null。",
        "逐 agent、逐调用及独立时间表：", "",
        "- " + link("22 个 agent 的用量 CSV", V / "pilot_v2.agent_accounting.final8/agents.csv"),
        "- " + link("162 次调用的完整索引 CSV", V / "pilot_v2.calls.final8.csv"),
        "- " + link("逐 agent 时间 CSV", V / "pilot_v2.timing.final8/agents.csv"),
        "- " + link("逐 run 时间 CSV", V / "pilot_v2.timing.final8/runs.csv"),
        "- " + link("CM 未接入占位记录", V / "pilot_v2.agent_accounting.final8/context_managers.csv"),
        "- " + link("机制准入、激活、交付与采纳表", V / "pilot_v2.agent_accounting.final8/mechanisms.md"),
        "", "时间表把 admission→activation（等待加环境准备）、累计模型请求墙钟、worker 交付墙钟、评分阶段分开。纯队列时间、纯模型计算时间、纯测试时间无法独立测得时为 null。", "",
        "## 可靠性、修复与停止状态", "",
        "本轮启动前 47 项验证通过，含真实隔离、fork、delta 合并；Matplotlib 官方负对照 completed=true/resolved=false。冻结后实际发现了测试未覆盖的消息拼接问题，这说明前置测试通过不等于运行管线没有缺陷。",
        "最终 8-run 证据审计 PASS，162 条调用的身份、用量字段、预算预留、补丁及官方评分哈希一致；这只是证据完整性通过，不会把受缺陷影响的 Sphinx pair 变成有效比较。",
        "已修复并验证：混合文件属主的容器权限初始化，候选仍 UID1000、无网络/宿主挂载、无 capabilities。",
        "尚未完成：GPT 最终消息提取修复。草稿已另存，未配套测试、未运行候选；当前运行源码已恢复至 pilot_v2 冻结版本以便复现。保留 STOP_AFTER_PAIR，不能直接移除后续跑旧缺陷。pilot_v3 没有创建。",
        "取消后的用量丢失、错误超时文案、预算耗尽后的交付处理，以及完整 SPLIT/FISSION/CM 接线，都仍是后续事项。用户当前要求停止，不自动推进。", "",
        "## 历史消耗边界", "",
        "| 批次 | 调用 | 已知 Token | 说明 |",
        "|---|---:|---:|---|",
        "| 旧 SWE pilot_v1 | 2 | 33,281 | 早期脱敏错误，基础设施失败 |",
        "| 旧 SWE pilot_v2 | 106 | 3,355,362 | 5 题/10 run，无 worker，两条件各 3/5 |",
        "| fixed pilot_v1 | 0 | 0 | 两 run 均在容器初始化失败 |",
        "| fixed pilot_v2（本轮） | 162 | 3,462,637 + unknown | 8 run，14 名真实 worker |",
        "| 合计 | 270 | 6,851,280 + unknown | 不是精确总量，也不是美元成本 |",
        "", "开发/审计助手、离线测试、镜像准备和负对照没有混入候选模型 Token 账本。正式评分结果属于整队最终补丁，不属于单个 worker。", "",
        "## 证据入口", "",
        "- " + link("最终完整性审计", V / "pilot_v2.final8.audit.json"),
        "- " + link("关闭状态与全部证据哈希", BATCH / "USER_CLOSEOUT.json"),
        "- " + link("消息拼接取证", V / "LUNA_PROTOCOL_ANALYSIS.md"),
        "- " + link("取消与未知用量取证", V / "TERRA_CANCEL_USAGE_RECONCILIATION.md"),
        "- " + link("worker 预算与交付取证", V / "WORKER_BUDGET_ANALYSIS.md"),
        "- " + link("GLM timeout 参数错误与恢复", V / "GLM_TIMEOUT_ANALYSIS.md"),
        "- " + link("为何旧批次不派生", V / "ACTIVATION_ANALYSIS_ZH.md"),
        "", "使用 SWE-bench Verified 官方数据与未改动的 v4.1.0 run_instance。只有一题形成完整六组比较，不能宣称榜单成绩或 SOTA。官方入口：[评分说明](https://www.swebench.com/SWE-bench/guides/evaluation/)、[提交规则](https://www.swebench.com/submit.html)。", "",
    ]
    write(HERE / "RUN_RECORD_20260903.md", "\n".join(record))

    handoff = [
        "# DPswarm 固定团队实验 Handoff（2026-09-03）", "",
        "## 第一优先级：用户已要求停止", "",
        "不要自动开启下一轮，不要创建 pilot_v3，不要删除 STOP_AFTER_PAIR 后盲目续跑。当前只做收尾。需要用户重新提出继续，才推进后续实现或模型实验。",
        "当前模型请求、候选线程和本实验容器均已收束；exec session 34858 已退出。Flask 验证镜像以及 Matplotlib/Sphinx 本轮镜像已按所属记录清理，其他容器未动。没有定时任务或后台继续运行安排。", "",
        "## 先看这三个文件", "",
        "- " + link("本轮结果和解释", HERE / "RUN_RECORD_20260903.md"),
        "- " + link("关闭状态、计量边界、证据哈希", BATCH / "USER_CLOSEOUT.json"),
        "- " + link("冻结的实际运行 manifest", BATCH / "manifest.json"),
        "", "实际进度：12 个预定 run 完成 8 个；Matplotlib 六组完整、全部官方通过。Sphinx 只执行 Luna/Terra pair，发现基础设施消息拼接缺陷，整对在评分前决定排除；其原始结果仍保存（Luna 通过、Terra 未通过）。其余四组未启动。",
        "当前 162 个调用，已知 3,462,637 Token，1 个取消调用总用量 unknown；48,761 只作 admission reservation。8 Lead +14 worker，共 22 个 agent。全实验历史含之前旧批次 270 调用、已知 6,851,280 Token +unknown。", "",
        "## 当前代码状态", "",
        "当前 runtime_sources 全部与 fixed pilot_v2 manifest 一致。环境权限修复已验证，environment.py SHA256：037fd1e4fa625239218b830ee3d7d15633e37e4de97a5d378d7741526a8761d7。",
        "GPT terminal 提取修复只是一份未验证草稿，未应用到当前运行源码，也未被任何候选调用使用。停止时助手曾写入 transport.py；收尾已把该字节版本和 diff 单独保存，再恢复 frozen source。没有丢弃草稿。",
        "- " + link("未应用的 transport 草稿", V / "transport_revision3_wip/transport.py"),
        "- " + link("相对冻结版的 patch", V / "transport_revision3_wip/transport.patch"),
        "- " + link("草稿状态和两个源码哈希", V / "transport_revision3_wip/status.json"),
        "测试文件尚未更新。当前旧 GPT 测试替身不会生成独立最终消息文件；直接应用草稿后跑现有测试预计需要配套适配。不能写成“修复已完成”或“gate 已通过”。", "",
        "## 用户重新要求继续后，建议顺序", "",
        "1. 审查最终消息草稿。方案是 SWE 专属 --output-last-message、保留完整 stdout 和真实 argv，并校验独立最终消息与单次成功 turn 的末条 Agent 消息一致。不能按“哪个 JSON 可解析”选择响应，也不能把多个 envelope 全执行。GLM 原生路径不应变化。",
        "2. 增加有区分力的消息边界测试：中间说明文字、重复 JSON、不同中间/最终 JSON、最后消息非法而较早消息合法、缺失/不匹配 terminal 文件、多 turn、损坏 JSONL、取消及失败用量。用已有 7 次故障 raw 数据离线回放；复核 Matplotlib 85 次 GPT 的动作保持一致。不要调用模型生成这些测试数据。",
        "3. 修复或明确预算停止的收束语义：Lead 无法再预留预算时，不应把“已准入但尚未返回”的请求与“新请求禁止”混为一件事。设计有界的已发请求收束，保留真实用量；用户明确取消、真实时间上限仍须生效。修正 cancelled 却显示 exceeded600 的错误文案。未知历史用量不能用预留值填平。",
        "4. 单独处理 worker 8 次上限与 finish/交付状态的关系。每轮已显示 local_call/local_limit；不是隐藏预算。可研究收尾预算或受控的未完成工件审阅，但不能绕过控制面、把 failed 直接伪装为 completed，也不能用本题成绩反向调参后混入原比较。",
        "5. 完成代码审查与针对性验证后重新冻结新 gate/manifest。重新核算候选预算时保留 1 次 unknown 与其 reservation 的区分。原计划上限 336 calls、7.2M Token 准入、3 小时；这些不是严格服务端实际 Token 封顶保证。",
        "6. 若用户仍希望完成本组比较，可执行修订 3 草案：保留全部六个未触发缺陷的 Matplotlib run；Sphinx 六组在新版本、新上下文统一执行一次，顺序 Luna、Terra、Sol、Flash、GLM、solo。不要把旧答案、取证分析或官方测试反馈送给候选。组合报告逐项引用两个版本原始证据，不改写旧结果。这只是待确认的续作安排，尚未执行。",
        "7. 自动组队策略及完整 SPLIT/FISSION/CM、DSH 桥接属于之后单独的接线与评估工作。本轮只有协议强制 DERIVE，不能宣称自动调度或完整 DPswarm 机制已通过。",
        "", "## 已证实的问题", "",
        "- GPT 拼接：Sphinx Luna 7 次错误的每条非空原始消息都合法；换行拼接制造 Extra data。它们共 194,640 Token。无 phase 字段，不能凭空标记 commentary/final。",
        "- 取消：Terra call 6f82fad6-9908-4d6a-bffb-5db273681612 的 Lead 预算预留失败，引发 runner 主动取消 worker；真实 58.437 秒，非 600 秒超时。--ephemeral、无完成 usage，无法恢复精确消耗。",
        "- GLM 预算：Matplotlib 四名 GLM/Flash worker 最后两轮都为 bash，没有 finish；均有非空 delta。正式采纳被生命周期状态拒绝。部分测试存在失败，不能统统解释成纯收尾问题，也不能因整队通过就说每份 delta 正确。",
        "- 自动派生缺失：旧 prompt 明确 optional/start alone 并强调费用，没有“非简单任务必须组队”的可审计规则；旧 adapter 又未暴露 split/fission、未接 CM。",
        "", "## 日志与读取方法", "",
        "候选原始记录位于 pilot_v2/results/<run_id>/calls/<call-folder>/：metadata.json、请求与原始响应。events.jsonl 与 control-plane/ledger 提供 node/item/session/attempt/epoch 归属。每个 result.json 只有一次终态写入；patch 与官方报告都有 SHA。",
        "CSV/JSON 导出均在 validation/pilot_v2.agent_accounting.final8、pilot_v2.timing.final8 和 pilot_v2.calls.final8.csv。CM CSV 只是 not_integrated 占位，不进入 agent/call 总数。缓存含于输入、reasoning 含于输出；同模型的 Lead 与 worker 分开计数。",
        "完整性审计 PASS 只证明原始账目一致，不解除 Sphinx 的实验有效性问题。source_snapshot、inputs_snapshot、validation_snapshot 与旧 manifest 不可改写。",
        "若仅重新生成只读审计，请使用新的输出名称；历史源码发生变化时使用 --snapshot-only。不要运行 cli run 来查看状态。",
        "",
        chr(96) * 3 + "powershell",
        "python modelbench/swe_fixed_team_20260903/validation/status.py modelbench/swe_fixed_team_20260903/pilot_v2",
        "python modelbench/swe_fixed_team_20260903/validation/audit_results.py --batch modelbench/swe_fixed_team_20260903/pilot_v2 --output <新的审计文件> --snapshot-only",
        chr(96) * 3,
        "",
        "入口文件：" + link("修订 3 停止前草案", V / "REVISION3_PLAN.md") + "；" +
        link("机制缺口说明", REPO / "modelbench/swe_verified_20260903/validation/FULL_MECHANISM_GAP.md") + "。",
        "",
        "本目录不是 Git checkout 的根仓库，之前 git status 已返回 not a git repository；交接依赖冻结源码、hash 和独立草稿 patch，不应假定存在可提交的 Git diff。",
        "不上传、不发榜单、不声称 SOTA；当前只有一个任务形成完整六组比较。",
        "",
    ]
    write(HERE / "HANDOFF_20260903.md", "\n".join(handoff))
    assert before == {key: sha(BATCH / "results" / key / "result.json") for key in results}
    print(json.dumps({"record": str(HERE / "RUN_RECORD_20260903.md"),
                      "handoff": str(HERE / "HANDOFF_20260903.md"), "call_rows": len(calls),
                      "known_tokens": known, "unknown_calls": len(unknown),
                      "original_results_unchanged": True}))


if __name__ == "__main__":
    main()
