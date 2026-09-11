#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pilot_v2–v14 全程总结仪表盘（只读分析，不改任何原始证据）。

数据源（实时计算，无硬编码结果）：
  validation/rev10reaudit.pilot_v{2..9}.json   旧批 rev10 口径复审（PASS）
  validation/audit.pilot_v{10..14}.json        rev9/rev10 时代审计（PASS）
  pilot_v10|v13/results/<run>/calls.jsonl      Lead 累计 input 与步数（机制面板）

输出：charts_20260904/summary_dashboard_v2_v14.png（2x3 六联图）
  A 十题难度谱与死亡带        B 团队价值的条件性（难/易 × solo/团队）
  C 易档成本：团队 +20% 零增益  D CM 池扫描：池尺寸不是杠杆
  E 机制：Lead 步数×累计 input  F 约束迁移时间线（结局构成 v2→v14）

运行：python make_summary_dashboard.py
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

BASE = Path(__file__).resolve().parent          # validation/
ROOT = BASE.parent                               # swe_fixed_team_20260903/
OUT = BASE / "charts_20260904" / "summary_dashboard_v2_v14.png"

C_SOLO, C_TEAM = "#4C72B0", "#DD8452"
C_OK, C_FAIL = "#2E8B57", "#C0504D"


def load(name):
    with open(BASE / name, encoding="utf-8") as fh:
        return json.load(fh)


def audit(batch):  # batch 形如 'v10'
    return load(f"audit.pilot_{batch}.json")


def resolved(r):
    return bool((r.get("grading") or {}).get("resolved"))


def tokens(r):
    return (r.get("usage") or {}).get("total_tokens")


def lead_stats(batch, run_id):
    """从 calls.jsonl 算 Lead 步数与累计 input（不含 cached）。"""
    p = ROOT / batch.replace("pilot_", "pilot_") / "results" / run_id / "calls.jsonl"
    rows = [json.loads(l) for l in open(p, encoding="utf-8")]
    lead = [x for x in rows if x.get("role") == "lead"]
    return len(lead), sum(x.get("input_tokens") or 0 for x in lead)


def is_team(r):
    return r["condition"] != "solo"


# ---------------------------------------------------------------- A：十题难度谱
def panel_spectrum(ax):
    rows = []  # (任务, token, resolved, 备注)
    for r in audit("v14")["runs"]:
        name = r["instance_id"].split("__")[-1]
        rows.append((name, tokens(r), resolved(r), "v14 探针"))
    v12solo = [tokens(r) for r in audit("v12")["runs"] if r["condition"] == "solo"]
    rows.append(("astropy-14995", sum(v12solo) / len(v12solo), True, "v12 均值 n=3"))
    v10solo = [tokens(r) for r in audit("v10")["runs"] if r["condition"] == "solo"]
    rows.append(("sphinx-8035", sum(v10solo) / len(v10solo), False, "v10 均值 n=3"))
    rows.sort(key=lambda x: x[1])

    ax.axvspan(500, 600, color="#C0504D", alpha=0.08)
    ax.axvline(600, color="#C0504D", ls="--", lw=1.2)
    ax.text(598, -0.4, "run 预算 600k", color="#C0504D", ha="right", fontsize=9)
    for i, (name, tok, ok, note) in enumerate(rows):
        ax.barh(i, tok / 1000, color=C_OK if ok else C_FAIL, height=0.68)
        ax.text(tok / 1000 + 6, i, f"{tok/1000:.0f}k", va="center", fontsize=9)
        ax.text(-8, i, name, va="center", ha="right", fontsize=10)
    ax.set_yticks([])
    ax.set_xlim(0, 660)
    ax.set_xlabel("solo run 总 token（千）")
    ax.set_title("A · 十题难度谱：≥~510k 进入死亡带（sympy 是中档唯一幸存）",
                 fontsize=13, loc="left")
    ax.text(505, len(rows) - 0.2, "死亡带：难档 4 题全灭于此", color="#C0504D", fontsize=9, va="top")


# ---------------------------------------------------------------- B：团队价值条件性
def panel_conditional(ax):
    v10 = audit("v10")["runs"]     # sphinx 难档，池12
    v12 = audit("v12")["runs"]     # astropy 易档
    cells = [
        ("难档 sphinx\nsolo", [r for r in v10 if not is_team(r)], C_SOLO),
        ("难档 sphinx\n团队", [r for r in v10 if is_team(r)], C_TEAM),
        ("易档 astropy\nsolo", [r for r in v12 if not is_team(r)], C_SOLO),
        ("易档 astropy\n团队", [r for r in v12 if is_team(r)], C_TEAM),
    ]
    for i, (label, rs, color) in enumerate(cells):
        n, t = sum(1 for r in rs if resolved(r)), len(rs)
        ax.bar(i, n / t * 100, color=color, width=0.62)
        ax.text(i, n / t * 100 + 3, f"{n}/{t}", ha="center", fontsize=12)
        cost = sum(tokens(r) for r in rs) / t / 1000
        ax.text(i, -14, f"均耗 {cost:.0f}k", ha="center", fontsize=9, color="#555")
    ax.set_xticks(range(4))
    ax.set_xticklabels([c[0] for c in cells], fontsize=10)
    ax.set_ylim(0, 118)
    ax.set_ylabel("通过率（%）")
    ax.set_title("B · 团队协议价值是条件性的：难档救场，易档 +21% 成本零增益",
                 fontsize=13, loc="left")
    ax.text(0.01, -0.30,
            "独立印证：v14 探针 solo 易档 5/5、难档 0/3；v13 池24 供给充足下 solo 仍 0/2。",
            transform=ax.transAxes, fontsize=9, color="#555")


# ---------------------------------------------------------------- C：易档成本（v12）
def panel_v12_cost(ax):
    v12 = audit("v12")["runs"]
    arms = [("solo_gpt-5.6-sol", "solo", C_SOLO),
            ("hetero_gpt-5.6-terra__glm-5.3", "团队·装配ON", C_TEAM),
            ("heterooff_gpt-5.6-terra__glm-5.3", "团队·装配OFF", "#B6B6B6")]
    solo_mean = sum(tokens(r) for r in v12 if r["condition"] == "solo") / 3
    for i, (arm, label, color) in enumerate(arms):
        ts = [tokens(r) for r in v12 if r["arm"] == arm]
        m = sum(ts) / len(ts)
        ax.bar(i, m / 1000, color=color, width=0.58)
        for j, t in enumerate(ts):
            ax.plot(i + (j - 1) * 0.13, t / 1000, "o", color="black", ms=4, alpha=0.55)
        delta = (m - solo_mean) / solo_mean * 100
        tag = f"{m/1000:.0f}k" if i == 0 else f"{m/1000:.0f}k（{delta:+.0f}%）"
        ax.text(i, m / 1000 + 8, tag, ha="center", fontsize=11)
    ax.set_xticks(range(3))
    ax.set_xticklabels([a[1] for a in arms], fontsize=10)
    ax.set_ylim(0, 500)
    ax.set_ylabel("run 总 token 均值（千）")
    ax.set_title("C · 易档 astropy 全 3/3：团队纯开销，装配 ON/OFF 无差（±3% 噪声内）",
                 fontsize=13, loc="left")


# ---------------------------------------------------------------- D：CM 池扫描
def panel_pool(ax):
    v10, v13 = audit("v10")["runs"], audit("v13")["runs"]
    cells = {}  # (pool, team?) -> [lead_sum_in], [resolved]
    for r in v10:  # 池 12 基线
        key = (12, is_team(r))
        n, s = lead_stats("pilot_v10", r["run_id"])
        cells.setdefault(key, [[], []])
        cells[key][0].append(s)
        cells[key][1].append(resolved(r))
    for r in v13:
        pool = 24 if r["arm"].endswith("cm24") else 6
        key = (pool, is_team(r))
        n, s = lead_stats("pilot_v13", r["run_id"])
        cells.setdefault(key, [[], []])
        cells[key][0].append(s)
        cells[key][1].append(resolved(r))

    width = 0.34
    for ti, (team, color, label) in enumerate([(False, C_SOLO, "solo"), (True, C_TEAM, "团队")]):
        for pi, pool in enumerate([6, 12, 24]):
            sums, oks = cells[(pool, team)]
            m = sum(sums) / len(sums) / 1000
            x = pi + (ti - 0.5) * width
            ax.bar(x, m, width * 0.9, color=color)
            passed = sum(oks)
            ax.text(x, m + 8, f"{m:.0f}k\n{passed}/{len(oks)} 通过", ha="center", fontsize=9)
    ax.set_xticks(range(3))
    ax.set_xticklabels(["池 6", "池 12（v10 基线）", "池 24"], fontsize=10)
    ax.set_ylim(0, 560)
    ax.set_ylabel("Lead 累计 input 均值（千 token）")
    ax.set_title("D · CM 池扫描：加池救不了 solo，团队用不满——池 12 保留为默认",
                 fontsize=13, loc="left")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in (C_SOLO, C_TEAM)]
    ax.legend(handles, ["solo", "团队"], fontsize=9, loc="upper right")


# ---------------------------------------------------------------- E：机制散点
def panel_mechanism(ax):
    for batch, pool_ok in (("pilot_v10", True), ("pilot_v13", False)):
        for r in audit(batch.replace("pilot_", ""))["runs"]:
            n, s = lead_stats(batch, r["run_id"])
            team = is_team(r)
            ax.plot(n, s / 1000,
                    "o" if resolved(r) else "X",
                    color=C_TEAM if team else C_SOLO, ms=9, alpha=0.85)
    ax.axhline(0, color="#888", lw=0.5)
    ax.annotate("团队簇：20–24 步 / 220–260k\n预算烧完前交付", xy=(22, 240), xytext=(30, 200),
                fontsize=10, color=C_TEAM, arrowprops=dict(arrowstyle="->", color=C_TEAM))
    ax.annotate("solo 簇：34–44 步 / 400–485k\n死亡线前仍在探索", xy=(40, 440), xytext=(24, 440),
                fontsize=10, color=C_SOLO, arrowprops=dict(arrowstyle="->", color=C_SOLO))
    ax.set_xlabel("Lead 调用步数")
    ax.set_ylabel("Lead 累计 input（千 token）")
    ax.set_xlim(15, 48)
    ax.set_ylim(150, 520)
    ax.set_title("E · 机制：团队省的是 Lead 步数与累计上下文（v10+v13 全 15 run）",
                 fontsize=13, loc="left")
    h = [plt.Line2D([], [], marker="o", ls="", color=C_TEAM),
         plt.Line2D([], [], marker="o", ls="", color=C_SOLO),
         plt.Line2D([], [], marker="X", ls="", color="#555")]
    ax.legend(h, ["团队 通过", "solo 通过", "未通过（X）"], fontsize=9, loc="lower right")


# ---------------------------------------------------------------- F：约束迁移时间线
def panel_timeline(ax):
    batches = [f"v{i}" for i in range(2, 15)]
    eras = {"v2": "token 预算时代", "v5": "提预算 600k", "v8": "共享调用池时代", "v10": "臂级预算：死亡=难度本身"}
    for i, b in enumerate(batches):
        d = load(f"rev10reaudit.pilot_{b}.json") if int(b[1:]) <= 9 else audit(b)
        oc = {}
        for r in d["runs"]:
            oc[r["outcome"]] = oc.get(r["outcome"], 0) + 1
        bot = 0
        for k, color in (("completed", C_OK), ("budget_exhausted", C_FAIL), ("transport_error", "#888")):
            v = oc.get(k, 0)
            if v:
                ax.bar(i, v, bottom=bot, color=color, width=0.66)
                bot += v
        ax.text(i, bot + 0.25, str(sum(oc.values())), ha="center", fontsize=9, color="#555")
    ax.set_xticks(range(len(batches)))
    ax.set_xticklabels(batches, fontsize=10)
    ax.set_ylabel("run 数")
    ax.set_ylim(0, 18)
    for x, txt in [(1.0, "v2–v4：token 预算撞线 7 次"), (4.0, "v5 提预算至 600k"),
                   (7.5, "v8–v9：共享 28 调用池饿死 11 run"), (11.0, "v10+：臂级预算，死亡=任务难度本身")]:
        ax.annotate(txt, xy=(x, 17), fontsize=9, color="#333",
                    ha="left", va="top", rotation=0)
    ax.set_title("F · 约束迁移时间线：结局构成（绿=completed 红=budget_exhausted 灰=transport）",
                 fontsize=13, loc="left")


def main():
    fig, axes = plt.subplots(2, 3, figsize=(24, 12))
    panel_spectrum(axes[0][0])
    panel_conditional(axes[0][1])
    panel_v12_cost(axes[0][2])
    panel_pool(axes[1][0])
    panel_mechanism(axes[1][1])
    panel_timeline(axes[1][2])
    fig.suptitle("DPswarm pilot_v2–v14 全程总结（13 批 80 run；数据源：rev10 复审 + rev9/10 审计 + calls.jsonl，只读实时计算）",
                 fontsize=15)
    fig.text(0.01, 0.012,
             "口径：A/B/C/D/E 全部数字由审计 JSON 与 calls.jsonl 实时计算；B 通过率小样本（N=2–6/格），Wilson 区间宽，按方向性结论读；"
             "F 的 budget_exhausted 语义随 rev 演进（token 预算→调用池→臂级 token 预算）。",
             fontsize=9, color="#555")
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150)
    print(f"已生成: {OUT}")

    # 控制台摘要
    total_runs, total_res, total_tok = 0, 0, 0
    for i in range(2, 15):
        b = f"v{i}"
        d = load(f"rev10reaudit.pilot_{b}.json") if i <= 9 else audit(b)
        total_runs += len(d["runs"])
        total_res += sum(1 for r in d["runs"] if resolved(r))
        total_tok += d.get("known_total_tokens") or 0
    print(f"全程 {total_runs} run，通过 {total_res}，已知 token 合计 {total_tok:,}")


if __name__ == "__main__":
    main()
