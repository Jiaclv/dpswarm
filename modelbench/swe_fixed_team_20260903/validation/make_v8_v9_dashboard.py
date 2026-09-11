#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pilot_v8 + pilot_v9 结果仪表盘（只读分析，不改任何原始证据）。

数据源（与本脚本同目录）：
  pilot_v8.rev8compat.audit.json            v8 批（16 run）rev8 兼容复审审计，PASS
  pilot_v9.final3.audit.json                v9 批（3 run，deepseek-v4-flash）审计，PASS
  pilot_v8.agent_accounting.final16/        v8 per-agent 记账（agents.csv / context_managers.csv）
  pilot_v9.agent_accounting.final3/         v9 per-agent 记账
  pilot_v6.agent_accounting.final3/         v6 批 per-agent 记账（worker 探索演进对照）
  pilot_v2.rev8compat.audit.json            无 CM 时代基线（glm / flash worker 探索）
  pilot_v3.rev8compat.audit.json            无 CM 时代基线（luna worker 探索）

输出：charts_20260903/v8_v9_dashboard.png（2x2 四联图）
  A 通过矩阵  B 通过成本排名  C worker 重复探索演进  D 异构通过的 token 分解与采纳归因

运行：python make_v8_v9_dashboard.py
"""
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

BASE = Path(__file__).resolve().parent
OUT = BASE / "charts_20260903" / "v8_v9_dashboard.png"

V8_AUDIT = BASE / "pilot_v8.rev8compat.audit.json"
V9_AUDIT = BASE / "pilot_v9.final3.audit.json"
V2_AUDIT = BASE / "pilot_v2.rev8compat.audit.json"
V3_AUDIT = BASE / "pilot_v3.rev8compat.audit.json"
V8_ACC = BASE / "pilot_v8.agent_accounting.final16"
V9_ACC = BASE / "pilot_v9.agent_accounting.final3"
V6_ACC = BASE / "pilot_v6.agent_accounting.final3"

SHORT = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
    "glm-5.3": "glm",
    "glm-5.3-flash": "flash",
    "deepseek-v4-flash": "deepseek",
}
COND_LABEL = {"solo": "solo 单干", "fixed_team": "fixed 同构", "hetero_team": "hetero 异构"}
COND_COLOR = {"solo": "#4C72B0", "fixed_team": "#55A868", "hetero_team": "#DD8452"}


def num(x):
    """agents.csv 里 null 写成字面量 'null'；统一转 float 或 None。"""
    if x in (None, "", "null"):
        return None
    return float(x)


def short_arm(arm):
    for p in ("solo_", "fixed_", "hetero_"):
        if arm.startswith(p):
            arm = arm[len(p):]
    for full, ab in sorted(SHORT.items(), key=lambda kv: -len(kv[0])):
        arm = arm.replace(full, ab)
    return arm


def load_audit(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_agents(acc_dir):
    with open(acc_dir / "agents.csv", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_cm(acc_dir):
    with open(acc_dir / "context_managers.csv", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def resolved(run):
    return bool((run.get("grading") or {}).get("resolved"))


# ---------------------------------------------------------------- 面板 A：通过矩阵
def panel_matrix(ax, runs):
    order = {"hetero_team": 0, "fixed_team": 1, "solo": 2}
    runs = sorted(runs, key=lambda r: (order[r["condition"]], r["arm"]))
    ylabels, colors, ytxt = [], [], []
    y = 0
    group_stat = {}
    for r in runs:
        ok = resolved(r)
        lbl = short_arm(r["arm"])
        ax.barh(y, 1, color="#2E8B57" if ok else "#C0504D", height=0.86)
        ax.text(0.5, y, lbl, ha="center", va="center", color="white", fontsize=10)
        ylabels.append(y)
        ytxt.append(r["condition"])
        st = group_stat.setdefault(r["condition"], [0, 0])
        st[1] += 1
        st[0] += 1 if ok else 0
        y += 1
    # 组分隔与右侧通过率标注
    bounds = {}
    for yy, cond in zip(ylabels, ytxt):
        bounds.setdefault(cond, [yy, yy])
        bounds[cond][1] = yy
    for cond, (a, b) in bounds.items():
        n, t = group_stat[cond]
        ax.text(1.04, (a + b) / 2, f"{COND_LABEL[cond]}\n{n}/{t} 通过", va="center", fontsize=11)
        if b + 1 <= max(ylabels):
            ax.axhline(b + 0.5, color="#888", lw=0.8, ls=":")
    ax.set_xlim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.invert_yaxis()
    ax.set_title("A · 通过矩阵（Sphinx-8035，v8+v9 共 19 run）", fontsize=13, loc="left")


# ---------------------------------------------------------------- 面板 B：通过成本排名
def panel_cost_rank(ax, runs):
    rows = []
    for r in runs:
        if not resolved(r):
            continue
        u = r.get("usage") or {}
        tok = u.get("total_tokens")
        approx = False
        if tok is None:  # 含未知用量调用：用已知小计并标注
            tok = (u.get("known_subtotals") or {}).get("total_tokens")
            approx = True
        rows.append((tok, r["arm"], r["condition"], approx))
    rows.sort()
    for i, (tok, arm, cond, approx) in enumerate(rows):
        ax.barh(i, tok / 1000, color=COND_COLOR[cond], height=0.7)
        tag = "≥" if approx else ""
        ax.text(tok / 1000 + 5, i, f"{tag}{tok/1000:.0f}k", va="center", fontsize=9)
        ax.text(-8, i, short_arm(arm), va="center", ha="right", fontsize=9)
    ax.set_yticks([])
    ax.invert_yaxis()
    ax.set_xlabel("run 总 token（千）")
    ax.set_title("B · 通过 run 成本排名（全部 13 个通过，含 CM 开销）", fontsize=13, loc="left")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in COND_COLOR.values()]
    ax.legend(handles, COND_LABEL.values(), loc="upper right", fontsize=9)  # 顶部行最短，右上无遮挡
    ax.set_xlim(0, max(r[0] for r in rows) / 1000 * 1.30)


# ---------------------------------------------------------------- 面板 C：worker 重复探索演进
def worker_input_from_accounting(acc_dir, arm, model):
    """某批某 fixed 臂中，指定模型全部 worker 的 input 合计；返回 (已知合计, 未知用量 worker 数)。"""
    known, unknown_workers = 0, 0
    for r in load_agents(acc_dir):
        if r["arm"] == arm and r["role"] == "worker" and r["model"] == model:
            v = num(r["input_tokens"])
            if v is None:
                unknown_workers += 1
            else:
                known += v
    return known, unknown_workers


def worker_input_from_audit(path, arm, model):
    """无 CM 时代（v2/v3）：fixed 臂 model_usage 里该模型即两名 worker（lead 恒为 sol）。"""
    d = load_audit(path)
    for r in d["runs"]:
        if r["arm"] == arm:
            mu = (r.get("model_usage") or {}).get(model) or {}
            return mu.get("input_tokens") or 0
    return None


def panel_exploration(ax):
    groups = [  # (组标签, [(批次标签, 值, 备注)])
        ("glm-5.3", [
            ("v2 无CM", worker_input_from_audit(V2_AUDIT, "fixed_glm-5.3", "glm-5.3"), ""),
            ("v6 自压缩CM", worker_input_from_accounting(V6_ACC, "fixed_glm-5.3", "glm-5.3")[0], ""),
            ("v8 团队装配", worker_input_from_accounting(V8_ACC, "fixed_glm-5.3", "glm-5.3")[0], ""),
        ]),
        ("glm-5.3-flash", [
            ("v2 无CM", worker_input_from_audit(V2_AUDIT, "fixed_glm-5.3-flash", "glm-5.3-flash"), ""),
            ("v8 团队装配", worker_input_from_accounting(V8_ACC, "fixed_glm-5.3-flash", "glm-5.3-flash")[0], "+未知"),
        ]),
        ("gpt-5.6-luna", [
            ("v3 无CM", worker_input_from_audit(V3_AUDIT, "fixed_gpt-5.6-luna", "gpt-5.6-luna"), ""),
            ("v6 自压缩CM", worker_input_from_accounting(V6_ACC, "fixed_gpt-5.6-luna", "gpt-5.6-luna")[0], ""),
            ("v8 团队装配", worker_input_from_accounting(V8_ACC, "fixed_gpt-5.6-luna", "gpt-5.6-luna")[0], ""),
        ]),
        ("deepseek-v4-flash", [
            ("v9 团队装配", worker_input_from_accounting(V9_ACC, "fixed_deepseek-v4-flash", "deepseek-v4-flash")[0], ""),
        ]),
    ]
    batch_color = {"v2 无CM": "#C0504D", "v3 无CM": "#C0504D",
                   "v6 自压缩CM": "#E8A33D", "v8 团队装配": "#4C72B0", "v9 团队装配": "#4C72B0"}
    width = 0.26
    for gi, (glabel, bars) in enumerate(groups):
        n = len(bars)
        for bi, (blabel, val, note) in enumerate(bars):
            x = gi + (bi - (n - 1) / 2) * width
            ax.bar(x, val / 1000, width * 0.9, color=batch_color[blabel])
            ax.text(x, val / 1000 + 6, f"{val/1000:.0f}k{note}", ha="center", fontsize=9)
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels([g for g, _ in groups], fontsize=10)
    ax.set_ylabel("两名 worker 全周期 input 合计（千 token）")
    ax.set_ylim(0, 420)
    ax.set_title("C · worker 重复探索成本演进（fixed 同构臂）", fontsize=13, loc="left")
    handles = [plt.Rectangle((0, 0), 1, 1, color="#C0504D"),
               plt.Rectangle((0, 0), 1, 1, color="#E8A33D"),
               plt.Rectangle((0, 0), 1, 1, color="#4C72B0")]
    ax.legend(handles, ["v2/v3 无CM", "v6 自压缩CM", "v8/v9 团队装配CM"], fontsize=9)
    ax.text(0.99, 0.97, "* flash v8：另有 1 名 worker（2 调用）用量未知（提供方超时）",
            transform=ax.transAxes, ha="right", va="top", fontsize=8, color="#666")


# ---------------------------------------------------------------- 面板 D：异构通过分解
def panel_hetero_decomp(ax):
    agents_v8, agents_v9 = load_agents(V8_ACC), load_agents(V9_ACC)
    cm_rows = list(load_cm(V8_ACC)) + list(load_cm(V9_ACC))
    runs = []
    for src, agents in (("v8", agents_v8), ("v9", agents_v9)):
        by_run = {}
        for r in agents:
            by_run.setdefault(r["run_id"], []).append(r)
        for run_id, rows in by_run.items():
            row0 = rows[0]
            if row0["condition"] != "hetero_team" or row0["run_official_resolved"] != "True":
                continue
            lead = [r for r in rows if r["role"] == "lead"]
            workers = [r for r in rows if r["role"] == "worker"]
            a_model, b_model = row0["arm"].replace("hetero_", "").split("__")
            w1 = [r for r in workers if r["model"] == a_model]
            w2 = [r for r in workers if r["model"] == b_model]
            cm = next((c for c in cm_rows if c["run_id"] == run_id), None)
            adopted = [r["model"] for r in workers if r["status"] == "adopted"]
            runs.append({
                "arm": row0["arm"], "batch": src,
                "lead": sum(num(r["total_tokens"]) or 0 for r in lead),
                "w1": sum(num(r["total_tokens"]) or 0 for r in w1),
                "w2": sum(num(r["total_tokens"]) or 0 for r in w2),
                "cm": num(cm["total_tokens"]) if cm else 0,
                "w1m": SHORT[a_model], "w2m": SHORT[b_model], "adopted": adopted,
            })
    runs.sort(key=lambda r: r["lead"] + r["w1"] + r["w2"] + r["cm"])
    for i, r in enumerate(runs):
        total = r["lead"] + r["w1"] + r["w2"] + r["cm"]
        segs = [("lead (sol)", r["lead"], "#4C72B0"),
                (f"worker {r['w1m']}·实现", r["w1"], "#DD8452"),
                (f"worker {r['w2m']}·回归", r["w2"], "#55A868"),
                ("CM", r["cm"], "#937860")]
        left = 0
        for name, val, color in segs:
            ax.barh(i, val / 1000, left=left / 1000, color=color, height=0.7)
            left += val
        star = f"  ★{SHORT[r['adopted'][0]]}被采纳" if r["adopted"] else "  Lead主导"
        ax.text(left / 1000 + 5, i, f"{total/1000:.0f}k{star}", va="center", fontsize=9)
        ax.text(-8, i, short_arm(r["arm"]), va="center", ha="right", fontsize=9)
    ax.set_yticks([])
    ax.invert_yaxis()
    ax.set_xlabel("run 总 token（千），按角色堆叠")
    ax.set_title("D · 异构 7/7 通过的归因分解：仅 2 例有 worker 补丁被采纳", fontsize=13, loc="left")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in segs]
    ax.legend(handles, ["lead", "worker 实现", "worker 回归", "CM"],
              loc="upper right", fontsize=9)  # 顶部行最短，右上无遮挡
    ax.set_xlim(0, max(r["lead"] + r["w1"] + r["w2"] + r["cm"] for r in runs) / 1000 * 1.30)


def main():
    v8 = load_audit(V8_AUDIT)
    v9 = load_audit(V9_AUDIT)
    runs = v8["runs"] + v9["runs"]

    fig, axes = plt.subplots(2, 2, figsize=(20, 12))
    panel_matrix(axes[0][0], runs)
    panel_cost_rank(axes[0][1], runs)
    panel_exploration(axes[1][0])
    panel_hetero_decomp(axes[1][1])
    fig.suptitle("pilot_v8 + pilot_v9 仪表盘（Sphinx-8035；数据源：rev8compat / final3 审计 + agent 记账，只读）",
                 fontsize=15)
    fig.text(0.01, 0.012,
             "口径：1 run/arm 方向性结论；审计 PASS（v8 429 调用 +2 未知用量，v9 84 调用零未知）；"
             "全部团队 run 的 worker 在 8 调用上限内无一正式 completed（team_execution_valid 全 false）。",
             fontsize=9, color="#555")
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150)
    print(f"已生成: {OUT}")

    # 控制台摘要
    n_res = sum(1 for r in runs if resolved(r))
    print(f"通过 {n_res}/{len(runs)}；v8 {v8['calls']} 调用 {v8['known_total_tokens']:,} token "
          f"(未知用量 {v8['unknown_total_usage_calls']} 次)；v9 {v9['calls']} 调用 "
          f"{v9['known_total_tokens']:,} token (未知用量 {v9['unknown_total_usage_calls']} 次)")


if __name__ == "__main__":
    main()
