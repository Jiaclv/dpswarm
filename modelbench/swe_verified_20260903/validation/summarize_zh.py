"""Post-process completed, audited experiments; never changes benchmark evidence."""
import argparse
from collections import Counter
import json
from pathlib import Path

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def number(value):
    return '未知' if value is None else f'{value:,.0f}'

parser = argparse.ArgumentParser()
parser.add_argument('--batch', required=True, type=Path)
parser.add_argument('--audit', required=True, type=Path)
args = parser.parse_args()
b = args.batch.resolve()
s, manifest, batch = (read(b / name) for name in ('summary.json', 'manifest.json', 'batch.json'))
audit = read(args.audit.resolve())
assert s['completed_runs'] == s['scheduled_runs'] == 20 and batch['stop'] is None
assert audit['status'] == 'PASS'
results = [read(p) for p in sorted((b / 'results').glob('*/result.json'))]
calls = [read(p) for p in sorted((b / 'results').glob('*/calls/*/metadata.json'))]
assert len(calls) == sum(r['call_count'] for r in results)
assert all(c['total_tokens'] is not None for c in calls)
total = sum(c['total_tokens'] for c in calls)
delegated = [r for r in results if r['condition'] == 'dpswarm' and r['delegations']]
protocol_errors = sum(r['protocol_errors'] for r in results)
revision = read(b / 'REVISION.json') if (b / 'REVISION.json').exists() else {}
infra_tokens = revision.get('parent_infrastructure_cost_tokens', 0)
lines = ['# SWE-bench Verified 首批实验结果', '',
    f"已完成固定 10 题 × 2 条件，共 20 run。批次墙钟 {batch['wall_seconds'] / 60:.1f} 分钟；"
    f"候选实际调用 {len(calls)} 次、{total:,} token，协议错误 {protocol_errors}。独立证据审计 PASS。", '',
    '| 条件 | 通过 / 10 | 官方完整评分 | 调用 | Token | 各 run 墙钟之和（秒） | 启用协作题数 |',
    '|---|---:|---:|---:|---:|---:|---:|']
for condition, label in (('solo', 'Sol 单 agent'), ('dpswarm', 'Sol＋仅提供 DPswarm 派生工具')):
    a = s['conditions'][condition]
    count = len(delegated) if condition == 'dpswarm' else 0
    lines.append(f"| {label} | {a['resolved']} | {a['graded']} | {a['calls']} | {a['known_tokens']:,} | {a['sum_run_wall_seconds']:.1f} | {count} |")
lines += ['', '各 run 墙钟之和包含执行与评分；两个条件并行，该和不是批次耗时。镜像准备计入批次墙钟。', '',
    f"DPswarm 条件实际在 {len(delegated)}/10 题委派子 agent，共 {sum(r['delegations'] for r in delegated)} 次委派。"]
if not delegated:
    lines += ['**本批没有触发实际协作。因此只能比较“开放可选协作工具”与单 agent，不能证明多 agent 提升，也不能据此排名五种子模型。**']
else:
    lines += ['子模型由主 agent 自主选择，调用数量不均衡，不能把这张表当作五种模型的独立同题排名。']
lines += ['', '## 每个模型的用量与时间', '',
    '| 请求模型 | 主调用 | 子调用 | 输入 token | 其中缓存 | 输出 token | 其中 reasoning | 总 token | 调用耗时之和（秒） |',
    '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for model in manifest['candidate_worker_models']:
    c = [r for r in calls if r['model_requested'] == model]
    values = []
    for key in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens'):
        values.append(sum(r[key] for r in c) if all(r.get(key) is not None for r in c) else None)
    lines.append(f"| {model} | {sum(r['role'] == 'lead' for r in c)} | {sum(r['role'] == 'worker' for r in c)} | " +
                 ' | '.join(number(v) for v in values) + f" | {sum(r.get('wall_seconds') or 0 for r in c):.1f} |")
lines += ['', '0 调用表示未被选择，不代表测得该模型能力为零。缓存已包含于输入，reasoning 已包含于输出，不重复累加。未回显的分项保留未知；美元费用没有可靠收费来源，保持 null。', '',
    'GPT 请求 max / fast，GLM 请求 thinking enabled / max；只有服务端回显才算实测设置，未回显不假定已验证。下面是实际回显数量：', '',
    '| 模型 | 调用数 | 模型名回显 | effort 回显 | service tier 回显 |', '|---|---:|---:|---:|---:|']
for model, m in s['models'].items():
    lines.append(f"| {model} | {m['calls']} | {m['model_echoes']} | {m['effort_echoes']} | {m['service_tier_echoes']} |")
lines += ['', '## 逐题配对结果', '', '| 实例 | 单 agent | 可选 DPswarm |', '|---|---|---|']
for pair in s['pairs']:
    labels = ['通过' if pair[key] is True else '未通过' if pair[key] is False else '未知'
              for key in ('solo_resolved', 'dpswarm_resolved')]
    lines.append(f"| {pair['instance_id']} | {labels[0]} | {labels[1]} |")
lines += ['', f"配对：DPswarm 独自通过 {sum(p['team_rescue'] for p in s['pairs'])} 题，"
    f"单 agent 独自通过 {sum(p['team_harm'] for p in s['pairs'])} 题。每个条件每题只跑一次，没有选择最好成绩。", '',
    '## 范围与证据', '',
    f"此前 pilot_v1 因脱敏误改 run ID 停止，2 次调用 / {infra_tokens:,} token / 0 工具执行 / 0 官方评分。"
    f"这部分作为基础设施开销单列；连同本批候选调用共 {total + infra_tokens:,} token。实验主持者、开发助手的用量不在这些候选调用账本内。", '',
    '本批使用真实 DPswarm 控制面和独立 SWE RunnerAdapter，仅暴露 DERIVE 派生。SPLIT/FISSION 未暴露，CM 未接入，必须记未测试而不是自主决定零调用；本批不是完整 DPswarm 能力评测。没有测试 DSH 宿主桥接，原桥接复审 HOLD 不因此解除。', '',
    '10 个仓库等权抽样不代表官方 500 题总体成绩，没有足够重复实验估计稳定性，不能宣称 SOTA。本批未向榜单提交。'
    '正式比较需按同一完整 split 和公开配置运行并接受官方验证。'
    '参见[官方评分说明](https://www.swebench.com/SWE-bench/guides/evaluation/)、[榜单](https://www.swebench.com/)、[提交要求](https://www.swebench.com/submit.html)。', '',
    f"- [冻结方案]({(b / 'source_snapshot/modelbench/swe_verified_20260903/PLAN.md').as_posix()})",
    f"- [逐 run 报告]({(b / 'REPORT.md').as_posix()})",
    f"- [结构化汇总]({(b / 'summary.json').as_posix()})",
    f"- [执行日志]({(b / 'run.stdout.log').as_posix()})",
    f"- [独立审计]({args.audit.resolve().as_posix()})",
    f"- [xarray 失败分析]({(b.parent / 'validation/xarray_failure_analysis.md').as_posix()})", '',
    '每次调用的请求、响应、usage、时间及设置在 results/*/calls；工具、补丁、控制面和官方评分证据在对应 run 目录。']
target = b / 'ANALYSIS_ZH.md'
with target.open('x', encoding='utf-8', newline='\n') as stream:
    stream.write('\n'.join(lines) + '\n')
print(json.dumps({'report': str(target), 'calls': len(calls), 'tokens': total,
                  'with_infrastructure_tokens': total + infra_tokens, 'delegated_tasks': len(delegated)}, ensure_ascii=False))
