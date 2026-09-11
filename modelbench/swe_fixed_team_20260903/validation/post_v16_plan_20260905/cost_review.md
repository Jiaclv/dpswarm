# 后续匹配实验的成本与预算审查

日期：2026-09-05。状态：**仅规划，未启动模型、API 或新实验**。价格沿用 `consolidated_v16_20260905/data/api_rate_card.json` 的 2026-09-04 冻结情景，没有查询或替换当前价格。

## 1. 可执行性结论

当前 rev11 **没有美元准入或美元硬上限**。`runner.py:198` 只创建调用/token/时间预算；`RunBudget` 只接受 `max_calls`、`token_limit`、`deadline_seconds` 和 `cm_call_allowance`。`REV11_FIX_PLAN_20260904.md` 也明确本轮不加美元准入。`cost_usd=None` 是原运行记录的事实；派生的 API 等价成本不是实付账单。

当前 GPT 通过 Codex CLI 调用，`max_tokens_requested=32768` 只是记录，`cap_enforced=False`；所有 1,842 次 GPT 调用请求 Fast，实际 tier 回显全部缺失。因此，现有执行器即使加“花到 $2/$4 就停止下一次调用”的检查，也只能得到美元准入阈值，不能保证美元硬封顶。真正的 equal-USD 实验须先完成可强制输出上限的 adapter、原子美元账本、并发及未知用量门禁，再将所有对照臂在同一新冻结版本重跑；不能只给某个臂换 adapter。

`cost_baseline.csv` 提供 100 行：78 个严格同题同 config_key 的历史配置，以及 22 个明示用途的 planning reference/proxy。它保留 n、成功失败、未知调用、Fast/Standard/CM 成本的 median/p90/min/max/mean、token、wall time、来源 run keys 与 SHA256。**统计包含失败运行；代理行不新增实验样本。**

## 2. 模型与 Worker 顺序的现有支持度

`runner.py:33,164–187` 接受 Sol、Terra、Luna、GLM-5.3、GLM-5.3-Flash、DeepSeek-v4-Flash；`hetero_team` 要求两个不同且有序的 `worker_models`，同型号双 Worker 使用 `fixed_team`。W1 为 production implementation，W2 为 independent regression tests。`cli.py:150–163` 已能解析有序异构组合，但新的完整矩阵还须登记新 wave、schedule 和 gate，不能修改历史 manifest 后续跑。

主线 Team 统一固定 **Sol Lead**。v16 的 10 次 Team 运行全部为 Terra 实现 → GLM-5.3 测试，不能称为 Lead 的 GPT/GLM 反转试验。

| 规划臂 | 执行器能力 | 现有观测范围 |
|---|---|---|
| Solo Sol | 已支持 | 当前六题有 v15 观测 |
| Terra → GLM-5.3 | 已支持 | v16 三题；历史 Sphinx/Astropy；新六题覆盖不完整 |
| GLM-5.3 → Terra | 有序列表已支持，需登记 | 未测；不能用 GLM→Sol 代替 |
| Terra×2 / GLM-5.3×2 | fixed_team 已支持 | 历史 Sphinx/Matplotlib，非当前 rev11 六题匹配证据 |
| Terra→Luna / Luna→Terra / Luna×2 | 已支持，需登记 | 前者与同型双 Luna 有历史观测；Luna→Terra 未测 |
| GLM-5.3→Flash / Flash→GLM-5.3 / Flash×2 | 已支持，需登记 | 跨型号双向未测；Flash×2 有历史观测 |

历史 v8 Sphinx 的 Sol→GLM 与 GLM→Sol 确实只交换 Worker 职责，所有非模型 effective limits 和冻结 runner 相同；各 n=1，费用 $2.006774/$1.823984，均官方通过，均未实现双 Worker 完整交付。它支持“应补角色顺序对照”，不足以给新 Terra/GLM 六题费用或胜率作点预测。P4 若晚于 P3 执行，加入同期 Terra×2、GLM×2 锚点。

## 3. 冻结计价情景与完整成本

费率单位为每百万 token。GLM 价格保留人民币原单位，最终除以冻结汇率 `6.7260 CNY/USD`；该汇率只是共同报告换算。

| 模型 | Fast：输入 / 缓存输入 / 输出 | Standard：输入 / 缓存输入 / 输出 | 币种 |
|---|---|---|---|
| GPT Sol | 8 / 0.8 / 40 | 4 / 0.4 / 20 | USD |
| GPT Terra | 4 / 0.4 / 24 | 2 / 0.2 / 12 | USD |
| GPT Luna | 0.4 / 0.04 / 2.4 | 0.2 / 0.02 / 1.2 | USD |
| GLM-5.3 | 同 Standard | 8 / 2 / 28 | CNY |
| GLM-5.3-Flash | 同 Standard | 0.4 / 0.115 / 1.4 | CNY |
| DeepSeek-v4-Flash | 同 Standard | 0.22 / 0.007 / 0.66 | USD |

GLM-Flash 的表值是冻结促销情景，不能声称未来仍有效。DeepSeek 继续按冻结时间带规则报价：UTC 工作日 01:00–04:00、06:00–10:00 为 2 倍，其余为 1 倍；历史重计价使用每次调用的 started_at，跨边界单列标记。未来准入若沿用该情景，应为在途、跨边界或启动时间不确定的调用按 2 倍预留，结算仍标明这只是冻结费率等价值；不要将其写成未来实付承诺。

设 I 为含缓存的输入、H 为缓存命中、W 为明确记录的缓存写入、O 为含 reasoning 的输出：

```text
GPT = ((I-H-W)*p_in + H*p_cached + W*1.25*p_in + O*p_out) / 1e6
GLM/DeepSeek = ((I-H)*p_in + H*p_cached + O*p_out) / 1e6
Run/episode = sum(Lead + all Workers + all CM + selector + every paid attempt)
```

不能把总 token 统一乘一个单价。H 已包含在 I，reasoning 已包含在 O；两者都不能再次加到总 token 或输出费。1,841 次已知 GPT usage 的原始 stdout 均明确 W=0，剩余 1 次 GPT 调用用量未知；新实验仍须逐调用恢复 W，不能把历史零值当将来的默认。GLM/DeepSeek 不另收缓存写附加项，是费率乘数为 1，并非声称未知 W=0。I/O 完整时可求 total=I+O；只有 total 不能可靠拆输入/输出来定价。

Fast 是主报告的“按请求 Fast API 价格重计价”情景，`tier_reported=null` 必须保留。Standard 只对同一已观测用量换费率，**不能由此推断真实 Standard 运行的时延、产出或通过率**；美元封顶实验若改用 Standard 作为预算记账货币，会改变能准入多少调用，应视为另一个预注册条件。

### CM 并非免费，也不是额外 token 池

当前 CM 是 DeepSeek-v4-Flash，thinking 请求 disabled，`cm_max_tokens=4096` 是单次输出上限。按需压缩、scout distill 和 bootstrap package 产生的真实 CM 调用均走同一 `_cm_call`，计入 CM 费用、全局模型槽、同一个 run 的 token/时间/将来的美元账本。`cm_package_budget=6000` 是材料包目标，不是额外免费的生成额度。

`cm_call_allowance=12` 只把 CM 调用次数独立出来：**28 次工作调用 + 最多 12 次 CM 调用，仍共享 600k token 准入池**。CM24 也不增加 token 总池。`cm_call_allowance=None` 才回到旧版 CM 占工作调用槽的语义。F3 收尾调用豁免的是 Lead 保留调用约束，不豁免全局 token/时间或未来美元预算。

新 28 runs 的 CM 已知费用：v15 $0.627299 / 166 calls；v15b $0.174618 / 47 calls；v16 $0.256054 / 76 calls；合计 **$1.057970**，全部已经包含在各 run 总费中。直接 CM 费虽小，压缩可能改变后续 Lead/Worker 输入量，不能只用 CM 单价评价该机制。

## 4. 单次运行的规划区间

下表为 Fast API 等价美元；每格为 **median / p90；min–max（n）**。p90 用线性插值 `h=(n-1)*0.9`，小 n 时只是对已观测点的描述，既不是未来90%覆盖率，也不是硬上限。

| 任务 | Solo：v15 默认 | Team OFF：Terra→GLM 模板 |
|---|---|---|
| Astropy | 1.532 / 1.532；1.532–1.532（1） | 1.284 / 1.738；1.158–1.851（3）* |
| Sphinx | 2.761 / 3.276；2.662–3.405（3） | 2.432 / 2.496；2.351–2.512（2） |
| xarray | 2.622 / 2.628；2.614–2.630（2） | 2.327 / 2.357；2.290–2.365（2） |
| seaborn | 2.261 / 2.266；2.255–2.267（2） | 2.669 / 2.886；2.398–2.940（2） |
| sklearn | 2.606 / 2.606；2.606–2.606（1） | 无本题 Team；代理 2.382 / 2.726；2.290–2.940（6）** |
| sympy | 2.643 / 2.643；2.643–2.643（1） | 无本题 Team；代理同上（同6条数据，不是新增样本）** |

\* Astropy Team 为 v12/rev9 旧观测，不与 v15 Solo 作同协议效应比较。\*\* 两个缺失格各借用 v16 三题 OFF 的6条记录仅作采购规划。其他 Team 格为 v16/rev11；Solo F5 ON，Team F5 OFF。所有模型、机制和任务外推均可能超出该区间。

900k/40 的 Solo 规划参考：Sphinx median $4.908320、p90 $5.230820、范围 $4.505195–5.311445；xarray $4.819620、$5.296300、$4.223770–5.415470，各 n=2。900k Team 仅借 v11 Sphinx Terra→GLM、装配ON、rev9 的两条作弱代理，median $2.698868、p90 $2.717566，范围 $2.675497–2.722240；不是新协议或 xarray 的预算效应证据。

## 5. 分阶段额度公式：三个主包互斥选择

令 `s_t` 为上表每题 Solo 统计量，`h_t` 为每题 Team OFF 模板，`a_t` 为 Team ON 模板，`T` 为六题，`H` 为 Sphinx/xarray/seaborn。表中“median合计”为逐格中位数×预定次数后求和；“Σp90”亦逐格求和，**不是整个批次的联合 p90**。“模板范围”为逐格 min/max 加权，不是新实验预测区间。

新反向/同家族模型使用同一 Team 费用模板，是在缺少匹配资料时的暂定资金预留方法，不宣称这些模型实际同价。P2 新 F1/F2/F5 组合、P6 F5 OFF 和交叉预算格同样属于外推；P1 通过后只可基于盲于答案的工程/用量数据修订下阶段资金申请，不得补跑至得到期望结果。

| 阶段 | run数与公式 | Fast median合计 | Fast Σp90 | Fast模板范围 | Standard median合计 |
|---|---|---:|---:|---:|---:|
| P0 离线 | 0模型调用 | 0 | 0 | 0 | 0 |
| P1 工程可用性 | 3题×(Solo+Team)×1=6；Σ三题(s+h) | $12.96 | $14.03 | $12.61–14.30 | $6.68 |
| P2 Solo机制 | 8设置×3题×3=72；24ΣH s | $183.45 | $196.08 | $180.74–199.23 | $93.35 |
| P3core 角色核心 | 5臂×6题×3=90；3ΣT s+12ΣT h | $204.98 | $224.00 | $196.25–231.82 | $106.53 |
| P4 后续补家族 | (6新增+TT/GG同期锚点)×6题×3=144；24ΣT h | $323.41 | $358.30 | $306.64–373.15 | $169.09 |
| P3full 一体角色矩阵 | 11臂×6题×3=198；3ΣT s+30ΣT h；替代P3core+P4 | $447.53 | $492.72 | $426.23–511.69 | $233.34 |
| P5 装配，条件分支 | 6题×3×ON/OFF=36；3ΣT(h+a) | $78.36 | $85.81 | $71.76–88.15 | $41.05 |
| P6 预算，条件分支 | 2token档×2call档×2策略×2题×3=48 | $151.61 | $160.32 | $143.98–162.50 | $78.37 |

P6 的两个 token 档为600k/900k，两个工作调用档为28/40，策略为Solo/锁定Team，任务为Sphinx/xarray；每个 token×strategy×task 的历史代理被两个call档各使用3次。600k/40、900k/28以及若干F5 OFF/锁定Team组合无直接证据，表中没有将它们冒充实测。

| 互斥主包 | 组成 | runs | Fast median合计 | Fast Σp90 | Fast模板范围 | Standard median合计 |
|---|---|---:|---:|---:|---:|---:|
| 最小决策包 | P1+P2+P3core | 168 | $401.39 | $434.11 | $389.60–445.35 | $206.56 |
| 完整一体包 | P1+P2+P3full | 276 | $643.94 | $702.83 | $619.57–725.22 | $333.37 |
| 分两波补全包 | P1+P2+P3core+P4 | 312 | $724.79 | $792.40 | $696.23–818.50 | $375.64 |

**这三个主包不能相加。** P5/P6 是单独门禁触发的增量，不默认加入任何主包。可将 `1.25×Σp90` 作为明确标注“人为25%缓冲”的申请参考：最小包$542.63、一体包$878.54、分两波$990.50；25%不是由样本推得的覆盖保证，也不是已获授权额度。推荐每一阶段通过工程与数据完整性门禁后，才释放下一阶段额度。

### P7/P8 equal-USD：当前 HOLD

- P7-check：新 R-cost adapter 必须另做工程探针，不能复用旧 P1。Astropy/Sphinx×S1/S2/T×1、每episode USD4，共6个episodes、8次候选生成机会、2次selector机会，设计额度 `6×$4=$24`。
- P7正式：108个 episode bundle，3策略×两个美元档×6题×3。配额算式 `54×$2 + 54×$4 = $324`，不含P7-check；check与正式合计设计额度 `$24+$324=$348`。Solo两次尝试的策略使总 candidate generations 为144，另计 selector；若每个双尝试bundle一次selector，则预定36次selector。选择器费用须在同一bundle额度内预留，不加在B之外。
- P8：180个episode bundle、240次candidate generations，另计selector；主计划配额总和 `$720`，最终 manifest 必须逐bundle列出B且求和等于720。若每个双尝试bundle一次selector，则为60次selector。
- episode数、candidate generation数、真实模型调用数、selector调用数分别记账。一个候选run本身可有多个Lead/Worker/CM调用。$24/$324/$720是设计配额；工程门禁未实现前**不是硬成本上限**，也不与API实付等同。
- P7安全护栏固定为episode共享 `max_calls=40`、`cm_call_allowance=12`、`token_limit=1_500_000`、`wall_seconds=1800`，候选重启不重置这些额度；所有角色和selector同时受同一episode美元账本约束。不能把“相同token额度”称为“相同美元额度”。P7/P8是否是递进或替代，以主计划门禁为准；未选分支不加入本次资金总额。

## 6. equal-USD 的最小工程门禁（规划，尚未实现）

1. 固定 run/episode 根账本，所有Lead、Worker、CM、selector、closing和重试共用它；每次实际传输有唯一attempt键，started/terminal事件只结算一次。两次Solo尝试共享同一B，重置候选上下文不重置额度。
2. 新调用准入必须在同一原子锁内满足：`known_settled_USD + pending_hold_USD + unknown_hold_USD + proposed_call_hold_USD <= B`。另保留token、工作调用、CM调用、deadline约束。并行请求先占额度，不能只看已完成费用；收尾调用也不能绕过B。
3. hold须有可靠上界：按完整线端输入、包含工具定义/包装的可验证token上界，以及provider真正强制的输出上限报价。准入不假定缓存命中；GPT存在写入风险时预留1.25×输入，DeepSeek不确定时间带按2×。当前字符数/3估算不是严格token上界，GPT CLI也不强制输出cap，故这一条目前不满足。
4. 锁定价格版本、FX、模型、费率档、上下文价档和返回模型核对。冻结卡只验证短上下文GPT（单次输入≤272k）；超出卡覆盖、模型未覆盖或关键用量未知时禁止用随意单价补齐，应停止新准入并保留缺失。实际tier未知保留null，按预先声明的Fast等价货币计账；若要求实付美元硬封顶，必须另具备可验证计费接口和费率合同。
5. 已知用量以原始usage结算并释放剩余hold；未知用量保留hold，不补0、不把hold当消费，不靠取消请求假定费用消失。若实际金额超hold，记录`over_usd_limit`并停止新调用，保留真实超额；发生过超额则该版本不得宣称严格硬封顶。
6. 离线覆盖并发最后额度、重复回放幂等、失败有usage、失败无usage、模型切换、cache写/读、CM池与根池、selector预留、未知重试、恢复后在途状态等契约；之后必须以新 R-cost 冻结源登记并运行P7-check验证，旧P1不能代替新adapter门禁。更换GPT adapter后全部对照同冻结源，不拼接旧Codex轨迹作为因果基线。

## 7. 未知、重试和失败的资金纪律

- 全108条历史run的已知等价费用为$233.528514，其中9个调用未知，均来自旧批；新增28 runs/912 calls无未知。费用分母包括成功、失败、budget_exhausted、protocol_error等已消耗调用；官方未通过不等于免费。
- 预算预约不是实测消费。展示已知下界时用`≥`并保留unknown_calls/runs；未知usage不能用reservation替换，缓存命中未知也不能静默当已观测0。只有有证据在发送前被拒、实际传输次数为0的本地请求，才可在“模型API已知消费”账上记0，且仍保留失败记录。
- 外层重试若重新发起provider请求，必须创建独立attempt并逐次计费；不要把多次传输仅按最后terminal usage结算。当前历史CSV每个逻辑call的transport_attempt_count都为1，但这不能证明未来SDK内部重连没有额外收费。若内部重试传输/usage不可见，则标未知并持有预留，停止下一调度边界；不要增补“重试费=0”。
- ordinary unresolved按已冻结排程继续，不因失败补跑。合法且已实施的USD额度导致Worker或后续候选未启动、未完成，属于策略结果，必须保留样本与成本，不能因此停批、删除样本或自动补跑。Worker在满足准入条件后却因宿主故障未实际启动或调用，才按基础设施故障在安全边界停检；grader故障、未知/悬空用量仍触发停检。恢复或另开批次保留原run与已付/已知费用；重跑有独立run key且进入运营总开销，是否进入主要统计分母按预注册规则决定。
- selector只能用候选允许看到的证据，不能用官方grader结果挑答案；所有尝试和selector消耗计入同一episode。保留run成功失败成本贡献，不能只统计最终被选中的候选。
- 本文件只计模型API等价值。容器、存储、带宽、人工及可信grader机器成本没有费率证据，单列未计价，不混作0美元基础设施。

## 8. 来源、复算和边界

关键源码证据：`runner.py:33–58`（目录/配置）、`:164–200`（条件和预算）、`:394–430`（普通调用预约/结算）、`:674–695`（CM共享预算）；`dpswarm-plugin/dpswarm/team_runtime/ledger.py:59–173`（CM独立调用池、共享token、unknown保留预约）；`modelbench/swe_verified_20260903/transport.py:344–367`（requested/reported tier及cap）、`:461–519`（DeepSeek原生usage）。规划依据为 `PLAN.md`、`NEXT_EXPERIMENT_PLAN_20260904C.md`、`REV11_FIX_PLAN_20260904.md`；其中旧PLAN的历史默认值不能覆盖当前runner和每run effective limits。

数值来自 `validation/consolidated_v16_20260905/data/run_api_costs.csv`：SHA256 `8dad50f5e7c406d38f44b3b8867831e92802e5affbd7e12c39563bd009577bbf`。冻结ratecard SHA256 `2efda0b10c03468ec39ce1be9ab7fec825b4c6f4e53bc2b4b802795a20ecbd7f`。`cost_baseline.csv` SHA256 `9b35e85992d6f02f1c32ed4c40afe835546218b8f3a2e11a73758f915239d7c0`。

复算：CSV中的`run_keys_json`给出每行所有来源。按它从run_api_costs取值，对`api_cost_known_usd`/`api_standard_cost_known_usd`分别算中位数、线性插值p90、min/max；不先平均成功/失败，也不把unknown补0。阶段金额按本文件公式对planning行加权，保留全精度，最后显示到美分。empirical_configuration严格按instance_id+config_key分组；planning_proxy的跨协议/跨任务借用只为资金规划，不用于效果检验。
