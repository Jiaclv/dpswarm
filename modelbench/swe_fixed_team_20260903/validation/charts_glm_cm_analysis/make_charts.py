# -*- coding: utf-8 -*-
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mp

plt.rcParams["font.family"] = "Microsoft YaHei"
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.size"] = 11
OUT = "."

# ================= 数据（均经 v2/v3/v5 权威审计核实） =================
# Sphinx-8035 (v3 修订3)：run名, worker模型, 官方resolved, 结局, 调用, [lead, w1, w2], [lead_calls,w1_calls,w2_calls]
S = [
    ("solo(无DPswarm)", "—", False, "budget_exhausted", 15, [512199,0,0], [15,0,0]),
    ("fixed_sol", "gpt-5.6-sol", True, "completed", 25, [200559,186182,174656], [9,8,8]),
    ("fixed_luna", "gpt-5.6-luna", False, "budget_exhausted", 24, [188889,227626,159920], [8,8,8]),
    ("fixed_terra", "gpt-5.6-terra", False, "completed", 23, [166729,144536,188366], [8,7,8]),
    ("fixed_glm5.3", "glm-5.3", True, "completed", 28, [243757,90156,75311], [10,9,9]),
    ("fixed_glm5.3f", "glm-5.3-flash", True, "budget_exhausted", 25, [353051,134872,0], [13,8,4]),
]
# Matplotlib (v2 修订2)
M = [
    ("solo(无DPswarm)", "—", True, "completed", 7, [177552,0,0], [7,0,0]),
    ("fixed_sol", "gpt-5.6-sol", True, "completed", 19, [232884,66529,147880], [9,4,6]),
    ("fixed_glm5.3", "glm-5.3", True, "completed", 26, [255850,57655,107073], [10,8,8]),
    ("fixed_glm5.3f", "glm-5.3-flash", True, "completed", 27, [299648,39532,144991], [11,8,8]),
    ("fixed_luna", "gpt-5.6-luna", True, "completed", 23, [206182,84826,246949], [10,5,8]),
    ("fixed_terra", "gpt-5.6-terra", True, "completed", 15, [119048,88134,124485], [5,5,5]),
]

# ============ 图1：Sphinx-8035 同一任务全部 run：总token + 调用 + 官方结果 ============
# 展示名：fixed_team -> team·<worker模型>，solo -> solo(单agent)
DISPLAY = {"solo(无DPswarm)":"solo 无团队\n(单agent)",
           "fixed_sol":"team·gpt-5.6-sol",
           "fixed_luna":"team·gpt-5.6-luna",
           "fixed_terra":"team·gpt-5.6-terra",
           "fixed_glm5.3":"team·glm-5.3",
           "fixed_glm5.3f":"team·glm-5.3-flash"}
# 审计权威 run 总 token（flash 含1次未知调用）
AUDIT_TOT = {"solo(无DPswarm)":512199,"fixed_sol":561397,"fixed_luna":576435,
             "fixed_terra":499631,"fixed_glm5.3":409224,"fixed_glm5.3f":494047}
UNKNOWN = {"fixed_glm5.3f":"*含1次未知调用"}
fig, ax = plt.subplots(figsize=(12, 6))
xs = range(len(S))
for i, x in enumerate(S):
    ok = x[2]
    tot = AUDIT_TOT[x[0]]
    ax.bar(i, tot, color="#2e8b57" if ok else "#c0504d", width=0.62)
    ax.text(i, tot+12000, f"{tot:,}*" if x[0] in UNKNOWN else f"{tot:,}",
            ha="center", fontsize=11, fontweight="bold", color="#2e8b57" if ok else "#a33")
    ax.text(i, tot+42000, f"{x[4]} 次调用", ha="center", fontsize=9.5, color="#444")
ax.set_xticks(list(xs)); ax.set_xticklabels([DISPLAY[x[0]] for x in S], fontsize=10.5)
ax.set_ylim(0, 660000)
ax.set_ylabel("整队已知总 token", fontsize=11)
ax.set_title("① Sphinx-8035：同一任务，DPswarm(team) vs 关掉团队(solo) 的总 token 与调用",
             fontsize=13, fontweight="bold", pad=14)
ax.text(0.5, 1.0, "* flash 组有 1 次未知用量调用（glm-5.3-flash 单一提供方超时），数字以审计为准。",
        transform=ax.transAxes, ha="center", va="bottom", fontsize=9, color="#7a7a7a")
ax.grid(axis="y", linestyle="--", alpha=0.3)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
ax.legend(handles=[mp.Patch(color="#2e8b57", label="官方通过"),
                   mp.Patch(color="#c0504d", label="官方未通过")],
          loc="upper right", frameon=False, fontsize=10)
plt.tight_layout(); plt.savefig(f"{OUT}/01_sphinx_overview.png", dpi=150); plt.close()

# ============ 图2：同一任务、同一 lead 模型(gpt-5.6-sol)，DPswarm 开/关 对比 ============
fig, axes = plt.subplots(1, 2, figsize=(12, 5.6))
for ax, task, data in zip(axes, ["Matplotlib", "Sphinx-8035"],
                          [dict(solo=177552, dsp=447293, sc=7, dc=19),
                           dict(solo=512199, dsp=561397, sc=15, dc=25)]):
    labels = ["团队关闭\n(solo 单agent)", "开启 team\n(3 个 agent)"]
    vals = [data["solo"], data["dsp"]]
    calls = [data["sc"], data["dc"]]
    bars = ax.bar([0,1], vals, width=0.5, color=["#9fb6cc","#e0821f"])
    for b,v,c in zip(bars, vals, calls):
        ax.text(b.get_x()+b.get_width()/2, v+9000, f"{v:,}\n{c} 次调用", ha="center",
                fontsize=10, fontweight="bold", color="#333")
    d = data["dsp"]-data["solo"]
    ax.annotate(f"Δ {d:+,}\n({d/data['solo']*100:+.1f}%)", xy=(0.5, max(vals)+42000),
                ha="center", fontsize=11, color="#c0392b", fontweight="bold")
    ax.set_xticks([0,1]); ax.set_xticklabels(labels, fontsize=10.5)
    ax.set_ylim(0, 700000); ax.set_ylabel("总 token", fontsize=10.5)
    ax.set_title(f"{task} 任务：DPswarm 开关对比 (同 lead 模型)", fontsize=12, fontweight="bold")
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
fig.suptitle("② 用 DPswarm 的代价：token 更多、调用更多；收益：难任务(单agent完不成)也能完成",
             fontsize=13, fontweight="bold", y=1.03)
plt.tight_layout(); plt.savefig(f"{OUT}/02_dpswarm_on_vs_off.png", dpi=150); plt.close()

# ============ 图3：DPswarm 各节点 token 拆分（Sphinx 有团队 run） ============
fig, ax = plt.subplots(figsize=(12, 6))
dsp = [x for x in S if "solo" not in x[0]]
xs = range(len(dsp))
lead = [x[5][0] for x in dsp]; w1 = [x[5][1] for x in dsp]; w2 = [x[5][2] for x in dsp]
ax.bar(xs, lead, 0.6, color="#4a7dbd", label="Lead (gpt-5.6-sol)")
ax.bar(xs, w1, 0.6, bottom=lead, color="#8fb8e0", label="worker-1")
ax.bar(xs, w2, 0.6, bottom=[l+w for l,w in zip(lead,w1)], color="#c9d9ec", label="worker-2")
for i,x in enumerate(dsp):
    n = x[5]; y = 0
    for t in n:
        if t == 0: continue
        ax.text(i, y+t/2, f"{t:,}", ha="center", fontsize=9, color="#2b3a4a")
        y += t
    ax.text(i, y+15000, f"总 {y:,}", ha="center", fontsize=10, fontweight="bold", color="#333")
ax.set_xticks(list(xs)); ax.set_xticklabels([DISPLAY[x[0]] for x in dsp], fontsize=10.5)
ax.set_ylim(0, 640000); ax.set_ylabel("token", fontsize=11)
ax.set_title("③ DPswarm(team) 各节点 token 拆分（Sphinx-8035 team run）", fontsize=13, fontweight="bold", pad=14)
ax.text(0.5, 1.0, "* flash 组 worker-2 有 1 次未知用量调用未计入节点；审计 run 总数为 494,047。glm-5.3 两 worker 均失败(未采纳)。",
        transform=ax.transAxes, ha="center", va="bottom", fontsize=9, color="#7a7a7a")
ax.legend(loc="upper right", frameon=False, fontsize=10)
ax.grid(axis="y", linestyle="--", alpha=0.3)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
plt.tight_layout(); plt.savefig(f"{OUT}/03_node_token_split.png", dpi=150); plt.close()

# ============ 图4：DPswarm 各节点调用次数（Sphinx 有团队 run） ============
fig, ax = plt.subplots(figsize=(12, 5))
lead_c = [x[6][0] for x in dsp]; w1_c = [x[6][1] for x in dsp]; w2_c = [x[6][2] for x in dsp]
ax.bar(xs, lead_c, 0.6, color="#4a7dbd", label="Lead")
ax.bar(xs, w1_c, 0.6, bottom=lead_c, color="#8fb8e0", label="worker-1")
ax.bar(xs, w2_c, 0.6, bottom=[a+b for a,b in zip(lead_c,w1_c)], color="#c9d9ec", label="worker-2")
for i,x in enumerate(dsp):
    c = x[6]; y = 0
    for t in c:
        if t == 0: continue
        ax.text(i, y+t/2, f"{t}", ha="center", fontsize=10, color="#1c2b3a")
        y += t
    ax.text(i, y+0.4, f"共{y}次", ha="center", fontsize=10, fontweight="bold")
ax.set_xticks(list(xs)); ax.set_xticklabels([DISPLAY[x[0]] for x in dsp], fontsize=10.5)
ax.set_ylim(0, 33); ax.set_ylabel("调用次数", fontsize=11)
ax.set_title("④ DPswarm(team) 各节点调用次数（Sphinx-8035 team run）", fontsize=13, fontweight="bold", pad=12)
ax.legend(loc="upper right", frameon=False, fontsize=10)
ax.grid(axis="y", linestyle="--", alpha=0.3)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
plt.tight_layout(); plt.savefig(f"{OUT}/04_node_calls.png", dpi=150); plt.close()

# ============ 图5：worker token vs 采纳/失败 结局 ============
fig, ax = plt.subplots(figsize=(12, 6))
adopt_map = {"fixed_sol":(True,True),"fixed_luna":(True,True),"fixed_terra":(True,True),
             "fixed_glm5.3":(False,False),"fixed_glm5.3f":(False,False)}
bar_y = []; bar_val = []; bar_col = []; bar_lab = []
for i,x in enumerate(dsp):
    a1,a2 = adopt_map[x[0]]
    if x[5][1]:
        bar_y.append(i-0.18); bar_val.append(x[5][1]); bar_col.append("#2e8b57" if a1 else "#c0504d")
        bar_lab.append(f"w1 {'采纳' if a1 else '失败'}")
    if x[5][2]:
        bar_y.append(i+0.18); bar_val.append(x[5][2]); bar_col.append("#2e8b57" if a2 else "#c0504d")
        bar_lab.append(f"w2 {'采纳' if a2 else '失败'}")
ax.bar(bar_y, bar_val, 0.3, color=bar_col)
for y,v,l in zip(bar_y, bar_val, bar_lab):
    ax.text(y, v+3000, f"{v:,}\n{l}", ha="center", fontsize=9, color="#333")
ax.set_xticks(range(len(dsp))); ax.set_xticklabels([DISPLAY[x[0]] for x in dsp], fontsize=10.5)
ax.set_ylim(0, 260000); ax.set_ylabel("worker token", fontsize=11)
ax.set_title("⑤ worker 消耗的 token 与其结局：绿=被Lead采纳，红=失败(blocked/预算耗尽)",
             fontsize=13, fontweight="bold", pad=12)
ax.legend(handles=[mp.Patch(color="#2e8b57", label="worker 被采纳"),
                   mp.Patch(color="#c0504d", label="worker 失败(未采纳)")],
          loc="upper right", frameon=False, fontsize=10)
ax.grid(axis="y", linestyle="--", alpha=0.3)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
plt.tight_layout(); plt.savefig(f"{OUT}/05_worker_token_vs_outcome.png", dpi=150); plt.close()

# ============ 图6：GLM 两 arm 加 CM 前后（v3 无CM vs v5 有CM，审计 run 总数） ============
fig, ax = plt.subplots(figsize=(11, 6))
names = ["glm-5.3", "glm-5.3-flash"]
v3v = [409224, 494047]
v5v = [472197, 523608]
xs = range(len(names)); w = 0.36
ax.bar([i-w/2 for i in xs], v3v, width=w, color="#9fb6cc", label="v3 无 CM（基线）")
ax.bar([i+w/2 for i in xs], v5v, width=w, color="#e0821f", label="v5 有 CM（修订5）")
for i in xs:
    ax.text(i-w/2, v3v[i]+9000, f"{v3v[i]:,}" + ("*" if names[i].endswith("flash") else ""),
            ha="center", fontsize=10, color="#4a6275")
    ax.text(i+w/2, v5v[i]+9000, f"{v5v[i]:,}" + ("*" if names[i].endswith("flash") else ""),
            ha="center", fontsize=10, color="#c05f0a", fontweight="bold")
    d = v5v[i]-v3v[i]
    ax.annotate(f"Δ {d:+,}  ({d/v3v[i]*100:+.1f}%)", xy=(i, max(v3v[i],v5v[i])+46000),
                ha="center", fontsize=12, color="#c0392b", fontweight="bold")
ax.set_xticks(list(xs)); ax.set_xticklabels(names, fontsize=12)
ax.set_ylim(0, 640000); ax.set_ylabel("整队已知总 token", fontsize=11)
ax.set_title("⑥ GLM 加 CM(v5) vs 不加 CM(v3)：整队总 token 反而都略升",
             fontsize=13, fontweight="bold", pad=14)
ax.text(0.5, 1.0, "* flash 组各含 1 次未知用量调用。加 CM 后总 token 反升 = CM 触发太稀(1–3次) + CM 自身成本 + 摘要冗长 + run 方差；",
        transform=ax.transAxes, ha="center", va="bottom", fontsize=9, color="#7a7a7a")
ax.legend(loc="upper right", frameon=False, fontsize=11)
ax.grid(axis="y", linestyle="--", alpha=0.3)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
plt.tight_layout(); plt.savefig(f"{OUT}/06_glm_v3_vs_v5.png", dpi=150); plt.close()

# ============ 图7：CM 压缩削减 dumbbell ============
comp = [("glm-5.3","worker-2",1,24323,12991),
        ("glm-5.3-flash","worker-1",1,24556,24021),
        ("glm-5.3-flash","worker-1",2,28638,22849),
        ("glm-5.3-flash","worker-1",3,31480,16084)]
fig, ax = plt.subplots(figsize=(11, 6.2))
SP = 1.9
ys = [-(len(comp)-1-i)*SP for i in range(len(comp))]
cols = ["#2f6fb3" if m=="glm-5.3" else "#3aa76d" for m,*_ in comp]
for i,(m,w,n,b,a) in enumerate(comp):
    y = ys[i]
    ax.plot([b,a],[y,y],color=cols[i],lw=4,zorder=2,solid_capstyle="round")
    ax.scatter([b],[y],s=170,facecolor="white",edgecolor="#888",lw=2,zorder=3)
    ax.scatter([a],[y],s=200,color=cols[i],zorder=4)
    ax.text(b-500,y,f"{b:,}",ha="right",va="center",fontsize=10,color="#666")
    ax.text(a+500,y,f"{a:,}",ha="left",va="center",fontsize=11,color=cols[i],fontweight="bold")
    pct = (b-a)/b*100
    ax.text((b+a)/2,y+0.42,f"削减 {pct:.1f}%  (-{b-a:,})",ha="center",va="bottom",
            fontsize=11,color="#c0392b",fontweight="bold")
ax.set_yticks(ys); ax.set_yticklabels([f"{m} · {w}触发#{n}" for m,w,n,_,_ in comp], fontsize=11)
ax.set_ylim(min(ys)-1.2, max(ys)+1.6)
ax.set_xlim(9500,35500); ax.set_xlabel("worker 历史上下文 est tokens（越小越好）", fontsize=11)
ax.set_title("⑦ CM 每次触发压缩，把 worker 历史上下文削掉多少", fontsize=13, fontweight="bold", pad=22)
ax.grid(axis="x", linestyle="--", alpha=0.35)
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
ax.scatter([],[],color="#2f6fb3",s=120,label="glm-5.3（触发1次）")
ax.scatter([],[],color="#3aa76d",s=120,label="glm-5.3-flash（触发3次）")
ax.legend(loc="lower right", frameon=False, fontsize=10)
plt.tight_layout(); plt.savefig(f"{OUT}/07_cm_reduction_dumbbell.png", dpi=150); plt.close()

print("charts done: 7")
