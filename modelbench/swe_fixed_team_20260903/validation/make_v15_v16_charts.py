# pilot_v15/v15b/v16 三波合并图表生成（rev11 机制确认线收官）
# 数据源：validation/audit.pilot_v15{,b}.json、audit.pilot_v16.json（只读）
# 历史参照数字来自 PILOT_V10_V11_ANALYSIS.md / PILOT_V13_V14_ANALYSIS.md（内联注明）
# 用法：python validation/make_v15_v16_charts.py  → validation/figures_v15_v16_20260904/*.png
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

HERE = Path(__file__).resolve().parent
OUT = HERE / 'figures_v15_v16_20260904'
OUT.mkdir(exist_ok=True)

GREEN, RED, GREY, BLUE, ORANGE = '#2ca02c', '#d62728', '#9e9e9e', '#1f77b4', '#ff7f0e'


def load_runs(name):
    audit = json.loads((HERE / f'audit.{name}.json').read_text(encoding='utf-8'))
    rows = []
    for r in audit['runs']:
        f = r['delivery_forensics']
        rows.append({
            'batch': name, 'run_id': r['run_id'], 'instance': r['instance_id'],
            'arm': r['arm'], 'resolved': bool((r.get('grading') or {}).get('resolved')),
            'tokens': (r.get('usage') or {}).get('total_tokens'),
            'calls': r.get('calls'), 'death': f.get('lead_death_phase'),
            'first_edit': f.get('lead_first_edit_ordinal'),
            'patch_bytes': f.get('patch_bytes') or 0,
            'skips': f.get('cm_skipped_count', 0),
            'worker_phases': f.get('worker_death_phases') or {},
            'discards': sum(1 for c in (f.get('cleanup_discards') or []) if c.get('delta_bytes', 0) > 0),
            'team_valid': r.get('team_execution_valid'),
        })
    return rows


RUNS = load_runs('pilot_v15') + load_runs('pilot_v15b') + load_runs('pilot_v16')

# audit 不含 team_execution_valid，从 result.json 补齐（v16 团队臂）
RESULTS = HERE.parent
for r in RUNS:
    p = RESULTS / r['batch'] / 'results' / r['run_id'] / 'result.json'
    if p.exists():
        r['team_valid'] = json.loads(p.read_text(encoding='utf-8')).get('team_execution_valid')

TASK_SHORT = {'sphinx-doc__sphinx-8035': 'sphinx', 'pydata__xarray-7229': 'xarray',
              'mwaskom__seaborn-3069': 'seaborn', 'scikit-learn__scikit-learn-25232': 'sklearn',
              'sympy__sympy-16792': 'sympy', 'astropy__astropy-14995': 'astropy'}


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, c - h), min(1, c + h))


# ---------------------------------------------------------------- 图 1：任务×臂通过率矩阵
ARM_COLS = [('solo 600k\n(v15 默认)', 'pilot_v15', 'solo_gpt-5.6-sol'),
            ('solo 600k 无宵禁\n(v15)', 'pilot_v15', 'solo_gpt-5.6-sol.nocurf'),
            ('solo 600k 池24\n(v15)', 'pilot_v15', 'solo_gpt-5.6-sol.cm24'),
            ('solo 900k\n(v15b)', 'pilot_v15b', 'solo_gpt-5.6-sol.b900'),
            ('团队 OFF\n(v16)', 'pilot_v16', 'heterooff_gpt-5.6-terra__glm-5.3'),
            ('团队 ON\n(v16)', 'pilot_v16', 'hetero_gpt-5.6-terra__glm-5.3')]
TASK_ROWS = ['sphinx', 'xarray', 'seaborn', 'sklearn', 'sympy', 'astropy']

fig, ax = plt.subplots(figsize=(11, 4.6))
cell = np.full((len(TASK_ROWS), len(ARM_COLS)), np.nan)
labels = [['' for _ in ARM_COLS] for _ in TASK_ROWS]
for i, task in enumerate(TASK_ROWS):
    for j, (_, batch, arm) in enumerate(ARM_COLS):
        sel = [r for r in RUNS if r['batch'] == batch and r['arm'] == arm
               and TASK_SHORT[r['instance']] == task]
        if not sel:
            continue
        k = sum(r['resolved'] for r in sel)
        cell[i, j] = k / len(sel)
        labels[i][j] = f'{k}/{len(sel)}'
cmap = matplotlib.colors.LinearSegmentedColormap.from_list('pf', [RED, '#fdd49e', GREEN])
ax.imshow(np.ma.masked_invalid(cell), cmap=cmap, vmin=0, vmax=1, aspect='auto')
for i in range(len(TASK_ROWS)):
    for j in range(len(ARM_COLS)):
        if labels[i][j]:
            ax.text(j, i, labels[i][j], ha='center', va='center', fontsize=12, fontweight='bold')
        else:
            ax.text(j, i, '—', ha='center', va='center', color=GREY)
ax.set_xticks(range(len(ARM_COLS)), [c[0] for c in ARM_COLS], fontsize=9)
ax.set_yticks(range(len(TASK_ROWS)), TASK_ROWS, fontsize=11)
ax.set_title('任务×臂通过率（pilot_v15/v15b/v16，rev11 协议）\n'
             '绿=全过 红=全灭；历史参照：v10 sphinx 团队OFF 3/3（rev10）、v12 astropy solo 3/3',
             fontsize=11)
for spine in ax.spines.values():
    spine.set_visible(False)
fig.tight_layout()
fig.savefig(OUT / 'fig1_task_arm_matrix.png', dpi=150)
plt.close(fig)

# ------------------------------------------------------- 图 2：token 消耗与预算线散点
fig, ax = plt.subplots(figsize=(11, 5))
order = ['sphinx', 'xarray', 'seaborn', 'sklearn', 'sympy', 'astropy']
xpos = {}
idx = 0
for task in order:
    for r in [r for r in RUNS if TASK_SHORT[r['instance']] == task]:
        color = GREEN if r['resolved'] else (ORANGE if r['death'] == 'edited' else RED)
        marker = 'o' if 'solo' in r['arm'] else 's'
        ax.scatter(idx, r['tokens'] / 1000, c=color, marker=marker, s=70, zorder=3,
                   edgecolors='black', linewidths=0.5)
        xpos[idx] = task
        idx += 1
ax.axhline(600, color=BLUE, ls='--', lw=1.2)
ax.text(idx - 0.5, 610, '600k 默认预算', color=BLUE, ha='right', fontsize=9)
ax.axhline(900, color=GREY, ls=':', lw=1.2)
ax.text(idx - 0.5, 910, '900k B支预算', color=GREY, ha='right', fontsize=9)
centers, labels_c = [], []
for task in order:
    idxs = [i for i in xpos if xpos[i] == task]
    if idxs:
        centers.append(idxs[len(idxs) // 2])
        labels_c.append(task)
ax.set_xticks(centers, labels_c, rotation=0, fontsize=10)
# 任务分组分隔
bounds, last = [], None
for i in sorted(xpos):
    if xpos[i] != last and last is not None:
        bounds.append(i - 0.5)
    last = xpos[i]
for b in bounds:
    ax.axvline(b, color='#dddddd', lw=0.8, zorder=1)
ax.set_ylabel('run 总 token（千）')
ax.set_title('28 个 run 的 token 消耗：红=no_edit 死 / 橙=edited 未通过 / 绿=通过；圆=solo 方=团队\n'
             '关键读数：失败 run 全部贴死 600k 天花板；sphinx 900k 两个通过 run（778k/827k）都越过 600k 线',
             fontsize=11)
ax.set_ylim(0, 1000)
fig.tight_layout()
fig.savefig(OUT / 'fig2_tokens_vs_budget.png', dpi=150)
plt.close(fig)

# ------------------------------------------------------- 图 3：首编辑时点（P2 归因签名，仅 solo 臂）
# v16 团队臂的 first_edit 是 lead 局部 call 序而 calls 是全队总数，单位不同构，不进此图
fig, ax = plt.subplots(figsize=(8.5, 5.6))
for r in RUNS:
    if r['first_edit'] is None or r['calls'] is None or 'solo' not in r['arm']:
        continue
    color = GREEN if r['resolved'] else ORANGE
    short = TASK_SHORT[r['instance']]
    ax.scatter(r['calls'], r['first_edit'], c=color, s=80, edgecolors='black', linewidths=0.5, zorder=3)
    ax.annotate(short, (r['calls'], r['first_edit']), textcoords='offset points',
                xytext=(6, 4), fontsize=8)
mx = max(r['calls'] for r in RUNS if r['calls'])
ax.plot([0, mx], [0, mx], ls='--', color=GREY, lw=1)
ax.text(mx - 1, mx - 3, '首编辑=最后一个 call\n（死前才动手）', fontsize=9, color=GREY, ha='right')
ax.axvline(12, color=BLUE, ls=':', lw=1)
ax.text(12.2, mx * 0.75, '池12耗尽点≈第12次cm', fontsize=8, color=BLUE)
ax.set_xlabel('run 总 call 数')
ax.set_ylabel('首编辑所在 call 序')
ax.set_title('P2 归因签名（仅 solo 臂，12 个编辑过的 run）：首编辑全部挤在池耗尽之后\n'
             '绿=通过 橙=edited 未通过；no_edit 死（6 个 run）不在图上——它们从未编辑', fontsize=11)
fig.tight_layout()
fig.savefig(OUT / 'fig3_first_edit_timing.png', dpi=150)
plt.close(fig)

# ------------------------------------------------------- 图 4：v16 worker 交付链
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={'width_ratios': [3, 2]})
PHASES = ['adopted', 'delivered_not_adopted', 'edited_no_delivery', 'no_edit']
PHASE_CN = ['adopted（采用）', 'delivered_not_adopted', 'edited_no_delivery', 'no_edit']
PHASE_C = [GREEN, ORANGE, RED, GREY]
for k, (label, pred) in enumerate([('团队 OFF 臂（12 worker）', lambda r: 'heterooff' in r['arm']),
                                   ('团队 ON 臂（8 worker）', lambda r: r['arm'].startswith('hetero_'))]):
    sel = [r for r in RUNS if r['batch'] == 'pilot_v16' and pred(r)]
    counts = [sum(1 for r in sel for ph in r['worker_phases'].values() if ph == p) for p in PHASES]
    bottom = 0
    for c, ph, cnt in zip(PHASE_C, PHASE_CN, counts):
        ax1.bar(k, cnt, bottom=bottom, color=c, label=ph if k == 0 else None, width=0.5)
        if cnt:
            ax1.text(k, bottom + cnt / 2, str(cnt), ha='center', va='center', fontsize=11,
                     fontweight='bold', color='white')
        bottom += cnt
ax1.set_xticks([0, 1], ['团队 OFF 臂\n(12 worker 实例)', '团队 ON 臂\n(8 worker 实例)'])
ax1.set_ylabel('worker 实例数')
ax1.set_title('v16 worker 终态分布（P6）\n45% 编辑了却没（被）交付；ON 臂独占 3 例 cleanup 丢弃', fontsize=10)
ax1.legend(fontsize=8, loc='upper right')
tv = [(sum(1 for r in RUNS if r['batch'] == 'pilot_v16' and 'heterooff' in r['arm'] and r['team_valid']),
       sum(1 for r in RUNS if r['batch'] == 'pilot_v16' and 'heterooff' in r['arm'])),
      (sum(1 for r in RUNS if r['batch'] == 'pilot_v16' and r['arm'].startswith('hetero_') and r['team_valid']),
       sum(1 for r in RUNS if r['batch'] == 'pilot_v16' and r['arm'].startswith('hetero_')))]
bars = ax2.bar(['OFF (6 run)', 'ON (4 run)'], [k / n for k, n in tv], color=[BLUE, ORANGE], width=0.45)
for b, (k, n) in zip(bars, tv):
    ax2.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.02, f'{k}/{n}', ha='center', fontsize=11)
ax2.axhline(1 / 3, color=GREY, ls='--', lw=1)
ax2.text(1.35, 1 / 3 + 0.02, 'v10 sphinx 基线 1/3', fontsize=8, color=GREY, ha='right')
ax2.set_ylim(0, 0.55)
ax2.set_ylabel('team_execution_valid 率')
ax2.set_title('team_valid 较 v10 基线不升反降\n（P6 第二项未达标）', fontsize=10)
fig.tight_layout()
fig.savefig(OUT / 'fig4_worker_delivery.png', dpi=150)
plt.close(fig)

# ------------------------------------------------------- 图 5：sphinx 效率对照（核心叙事）
fig, ax = plt.subplots(figsize=(9.5, 4.8))
groups = [
    ('solo 600k\n(v15 默认)', [564071, 560014, 562853], [0, 0, 1]),
    ('solo 600k 无宵禁\n(v15)', [572487, 558388], [1, 1]),
    ('solo 900k\n(v15b)', [778452, 827391], [1, 1]),
    ('团队 OFF 600k\n(v16 回归)', [559680, 559797], [1, 1]),
]
xlabels, means, rates, allpts = [], [], [], []
for name, toks, res in groups:
    pts = [(t, r) for t, r in zip(toks, res) if t is not None]
    means.append(np.mean([t for t, _ in pts]) / 1000)
    rates.append(f'{sum(r for _, r in pts)}/{len(pts)}')
    allpts.append(pts)
    xlabels.append(name)
x = np.arange(len(groups))
colors = [GREEN if r.startswith(str(len(allpts[i]))) else ORANGE for i, r in enumerate(rates)]
colors = [GREEN if rates[i].split('/')[0] == rates[i].split('/')[1] else ORANGE for i in range(len(rates))]
bars = ax.bar(x, means, color=colors, width=0.55, alpha=0.85)
for i, pts in enumerate(allpts):
    ax.scatter([i] * len(pts), [t / 1000 for t, _ in pts], color='black', zorder=3, s=28)
for b, rate in zip(bars, rates):
    ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 12, f'通过 {rate}', ha='center',
            fontsize=11, fontweight='bold')
ax.axhline(600, color=BLUE, ls='--', lw=1)
ax.text(-0.4, 615, '600k 预算线', color=BLUE, fontsize=9)
ax.annotate('', xy=(2.72, 565), xytext=(2.28, 800),
            arrowprops=dict(arrowstyle='->', color=GREY, lw=1.4))
ax.text(0.55, 780, '团队 600k ≈ solo ~800k\n（分工 ≈ 1.35× 预算）', fontsize=10, color='#333333')
ax.set_xticks(x, xlabels, fontsize=9)
ax.set_ylabel('run 总 token（千）')
ax.set_ylim(0, 950)
ax.set_title('sphinx-8035 效率对照：什么臂、花多少 token、过什么率（v15/v15b/v16 实测）\n'
             '历史参照（不在柱内）：v10 团队 OFF 3/3（rev10 协议）、v13 池6 运气型 2/2', fontsize=11)
fig.tight_layout()
fig.savefig(OUT / 'fig5_sphinx_efficiency.png', dpi=150)
plt.close(fig)

print('figures written to', OUT)
for p in sorted(OUT.glob('*.png')):
    print(' ', p.name, p.stat().st_size, 'bytes')
