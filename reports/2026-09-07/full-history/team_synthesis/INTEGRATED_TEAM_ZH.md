# 全历史固定团队与单人对照：机制整合

覆盖清单逐条落账全部 306 次历史尝试；其中 249 条有合格 SWE 评分。本分析识别 163 个“具体配置对 × 同一道题”对照，涉及 147 个去重 episode、20 道去重任务、56 组独立来源 solo baseline。75 行保持 lead 模型不变，其余明确属于“换模型并换团队配置”，不能归因为团队。所有数字均来自已有结果，没有再次调用模型或 grader。

重复运行先在题内求均值，再在同一冻结配置对内按题等权。三个重复只提高同一道题的重复证据，不产生三道独立题。相同 solo 对照多个团队时沿用相同 baseline ID；不同版本重复出现的 Sphinx 也不能当作新任务。COMPARISONS.csv 保留每条 episode ID、配置、预算、运行时、transport 和设置变化；COVERAGE.csv 给出全部尝试的纳入或排除理由。

## 当前可以成立的判断

固定团队的收益依赖具体角色组合、任务和运行条件。不能从历史结果推出“多agent普遍更强”，也不能用一个最新阶段覆盖此前的反例。最大可合并的同源层 A1-v2+A2 为18题：Sol lead + Terra实现 + GLM测试(T) 对 Sol solo(S) 为13/18对11/18，只有 Matplotlib-20826、Pylint-6386 两道题产生正差，零负差；其余16题相同。逐个去掉这两道信息题后，优势都从11.1个百分点降到5.9个百分点（1/17）；同时去掉则为0/16。这是受少数题支撑的正向证据。

Sol + GLM测试(D)在相同18题对Sol solo为11/18对11/18。这个两角色配置没有净正确率提升，不能把三角色T的收益移植给D。Luna solo是更换lead模型的成本替代方案；与它比较的差异应同时包含模型因素。

## 同一具体组合跨设置是否稳健

下表只去重题号并列出出现过的方向，预算、CM、assembly、transport和runtime仍保留在各自对照中。正负题集可能重叠；一个任务出现过正差，不能说明所有设置下都为正差。层数不充当独立样本数。

| lead / 按实现、测试顺序的worker模型 | 可对照去重题数 | 出现正差的去重任务 | 出现负差的去重任务 |
|---|---:|---|---|
| gpt-5.6-sol / glm-5.3 | 18 | matplotlib__matplotlib-20826 | 无 |
| gpt-5.6-sol / gpt-5.6-terra,glm-5.3 | 19 | matplotlib__matplotlib-20826, pylint-dev__pylint-6386, sphinx-doc__sphinx-8035 | 无 |
| gpt-5.6-sol / glm-5.3-flash,glm-5.3-flash | 4 | matplotlib__matplotlib-20826, sphinx-doc__sphinx-8035 | 无 |
| glm-5.3-flash / glm-5.3-flash,glm-5.3-flash | 1 | 无 | matplotlib__matplotlib-20826 |
| gpt-5.6-sol / glm-5.3,glm-5.3 | 2 | sphinx-doc__sphinx-8035 | 无 |
| gpt-5.6-sol / gpt-5.6-luna,gpt-5.6-luna | 2 | sphinx-doc__sphinx-8035 | 无 |
| gpt-5.6-sol / gpt-5.6-sol,gpt-5.6-sol | 2 | sphinx-doc__sphinx-8035 | sphinx-doc__sphinx-8035 |
| gpt-5.6-sol / gpt-5.6-terra,gpt-5.6-terra | 2 | 无 | sphinx-doc__sphinx-8035 |
| gpt-5.6-sol / glm-5.3,gpt-5.6-luna | 1 | 无 | 无 |
| gpt-5.6-sol / glm-5.3,gpt-5.6-sol | 1 | 无 | 无 |
| gpt-5.6-sol / gpt-5.6-sol,glm-5.3 | 1 | 无 | 无 |
| gpt-5.6-sol / gpt-5.6-terra,glm-5.3-flash | 1 | 无 | 无 |
| gpt-5.6-sol / gpt-5.6-terra,gpt-5.6-luna | 1 | 无 | 无 |

Sol lead+Terra实现+GLM测试的去重正向支持集中在3题：Matplotlib-20826（A2，600k/28）、Pylint-6386（A2，600k/28；B1 Coding，2.4M/160）、Sphinx-8035（v10 assembly ON/OFF；v13 OFF且CM池24）。19道去重可对照任务中未出现负差，但其余任务与这些任务的不同设置仍有大量持平。这是组合特定、条件特定的支持，不能等同为额外19道新测试或普遍可靠收益。

两Sol worker在同一Sphinx上既出现正差也出现负差；两Terra worker有负差而无正差。把这些组合合成一个“team”处理会抹去真实方向冲突。完整设置与来源见CROSS_STRATA_TASK_SUPPORT.csv和SUMMARY.json。

## 各固定组合的可比历史层

| 来源层 / 固定团队 | solo | 题数 | 团队 / solo 按题成功率 | 正/负/平题数 | token比 | API等价成本比 | 生成时间比 |
|---|---|---:|---|---|---:|---:|---:|
| a1-v2,a2-backfill-v1,a2-recovery-b-v1,a2-resume-v2,a2-resume-v3,a2-v1 / D | S | 18 | 61.1% / 61.1% | 0/0/18 | 1.30× | 1.14× | 1.28× |
| a1-v2,a2-backfill-v1,a2-recovery-b-v1,a2-resume-v2,a2-resume-v3,a2-v1 / T | S | 18 | 72.2% / 61.1% | 2/0/16 | 1.30× | 1.00× | 1.38× |
| b1-v1 / D11 | S-S | 1 | 0.0% / 0.0% | 0/0/1 | 1.00× | 0.77× | 0.94× |
| b1-v1 / S-FF | S-S | 1 | 0.0% / 0.0% | 0/0/1 | 未知 | 未知 | 1.02× |
| b1-coding-continuation-v1 / D11 | S-S | 1 | 100.0% / 0.0% | 1/0/0 | 1.39× | 1.05× | 1.52× |
| b1-coding-continuation-v1 / F-T | F-S | 1 | 0.0% / 100.0% | 0/1/0 | 1.88× | 1.86× | 0.97× |
| b1-coding-continuation-v1 / S-FF | S-S | 1 | 100.0% / 0.0% | 1/0/0 | 1.40× | 0.75× | 1.86× |
| b1-coding-continuation-v1 / T11 | S-S | 1 | 0.0% / 0.0% | 0/0/1 | 1.38× | 0.77× | 1.02× |
| b1-mvp-six-v1 / T11 | S-S | 2 | 50.0% / 0.0% | 1/0/1 | 0.71× | 0.45× | 0.57× |
| pilot_v3 / fixed_glm-5.3 | solo | 1 | 100.0% / 0.0% | 1/0/0 | 0.80× | 0.49× | 1.00× |
| pilot_v3 / fixed_glm-5.3-flash | solo | 1 | 100.0% / 0.0% | 1/0/0 | 未知 | 未知 | 3.21× |
| pilot_v3 / fixed_gpt-5.6-luna | solo | 1 | 0.0% / 0.0% | 0/0/1 | 1.13× | 0.38× | 1.13× |
| pilot_v3 / fixed_gpt-5.6-sol | solo | 1 | 100.0% / 0.0% | 1/0/0 | 1.10× | 1.00× | 0.76× |
| pilot_v3 / fixed_gpt-5.6-terra | solo | 1 | 0.0% / 0.0% | 0/0/1 | 0.98× | 0.70× | 0.54× |
| pilot_v4 / fixed_gpt-5.6-sol | solo | 1 | 0.0% / 0.0% | 0/0/1 | 未知 | 未知 | 0.45× |
| pilot_v6 / fixed_glm-5.3 | solo | 1 | 100.0% / 0.0% | 1/0/0 | 0.65× | 0.53× | 0.99× |
| pilot_v7 / fixed_glm-5.3-flash | solo | 1 | 100.0% / 0.0% | 1/0/0 | 0.73× | 0.40× | 1.36× |
| pilot_v6 / fixed_gpt-5.6-luna | solo | 1 | 100.0% / 0.0% | 1/0/0 | 1.17× | 0.37× | 0.59× |
| pilot_v13 / heterooff_gpt-5.6-terra__glm-5.3.cm6 | solo_gpt-5.6-sol.cm6 | 1 | 100.0% / 100.0% | 0/0/1 | 1.02× | 0.79× | 1.04× |
| pilot_v10,pilot_v12 / hetero_gpt-5.6-terra__glm-5.3 | solo_gpt-5.6-sol | 2 | 100.0% / 50.0% | 1/0/1 | 1.06× | 0.86× | 1.42× |
| pilot_v10,pilot_v12 / heterooff_gpt-5.6-terra__glm-5.3 | solo_gpt-5.6-sol | 2 | 100.0% / 50.0% | 1/0/1 | 1.05× | 0.81× | 1.08× |
| pilot_v2 / fixed_glm-5.3 | solo | 1 | 100.0% / 100.0% | 0/0/1 | 2.37× | 1.50× | 2.69× |
| pilot_v2 / fixed_glm-5.3-flash | solo | 1 | 100.0% / 100.0% | 0/0/1 | 2.73× | 1.70× | 4.54× |
| pilot_v2 / fixed_gpt-5.6-luna | solo | 1 | 100.0% / 100.0% | 0/0/1 | 3.03× | 0.98× | 2.46× |
| pilot_v2 / fixed_gpt-5.6-sol | solo | 1 | 100.0% / 100.0% | 0/0/1 | 2.52× | 2.27× | 3.07× |
| pilot_v2 / fixed_gpt-5.6-terra | solo | 1 | 100.0% / 100.0% | 0/0/1 | 1.87× | 1.27× | 1.19× |
| pilot_v8 / fixed_glm-5.3 | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.55× | 0.27× | 0.40× |
| pilot_v8 / fixed_glm-5.3-flash | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 未知 | 未知 | 1.16× |
| pilot_v8 / fixed_gpt-5.6-luna | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 1.05× | 0.24× | 0.67× |
| pilot_v8 / fixed_gpt-5.6-sol | solo_gpt-5.6-sol | 1 | 0.0% / 100.0% | 0/1/0 | 0.97× | 0.81× | 0.42× |
| pilot_v8 / fixed_gpt-5.6-terra | solo_gpt-5.6-sol | 1 | 0.0% / 100.0% | 0/1/0 | 1.03× | 0.61× | 0.46× |
| pilot_v8 / hetero_glm-5.3__gpt-5.6-luna | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.84× | 0.39× | 0.75× |
| pilot_v8 / hetero_glm-5.3__gpt-5.6-sol | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.80× | 0.52× | 0.51× |
| pilot_v8 / hetero_gpt-5.6-sol__glm-5.3 | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.73× | 0.58× | 0.49× |
| pilot_v8 / hetero_gpt-5.6-terra__glm-5.3 | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.81× | 0.39× | 0.56× |
| pilot_v8 / hetero_gpt-5.6-terra__glm-5.3-flash | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 0.75× | 0.45× | 0.55× |
| pilot_v8 / hetero_gpt-5.6-terra__gpt-5.6-luna | solo_gpt-5.6-sol | 1 | 100.0% / 100.0% | 0/0/1 | 1.04× | 0.37× | 0.33× |
| pilot_v13 / heterooff_gpt-5.6-terra__glm-5.3.cm24 | solo_gpt-5.6-sol.cm24 | 1 | 100.0% / 0.0% | 1/0/0 | 0.95× | 1.06× | 1.70× |

v2-v8里的单题成功不能累计成彼此独立的“团队胜场”：多组使用同一Sphinx solo，且版本、CM和实现不断变化。v8同为Sol lead时，两个Sol worker、两个Terra worker均输给Sol solo；GLM/Flash/Luna及所测异构组合与Sol solo持平。更早v3的Sol/GLM/Flash worker组合和v6-v7的GLM/Luna/Flash组合对Sphinx表现正向。两组证据共同说明：模型标签或“有worker”本身不足以预测收益，旧版对照中的solo失败也随运行时发生变化。

v10与v12的52份runtime源完全相同，且相应预算、模型与设置吻合，因此每个固定组合可合为2题。Terra+GLM的assembly ON和OFF均为2题内100%，Sol solo为50%；优势全部来自Sphinx，Astropy两侧均通过。每臂3次重复不是6道题；去掉Sphinx后优势为0/1。ON/OFF共用相同solo来源，两条效果也不是两项独立证据。OFF对solo同时改变assembly设置，不能称为纯团队开关效果。

v13对相同Terra+GLM OFF组合：CM池6时团队与solo均通过；CM池24时团队通过、solo的2次均失败。池大小保持分层。v13不能直接并入v10/v12：runtime已变化；v14的solo为CM池12，不能冒充v13池6或24的控制。

B1 Coding同为2.4M token/160普通调用，T11对Sol solo在控制器严格分层为1题与2题。补充合并三题得到团队33.3%对solo0.0%，正/负/平为1/0/2；唯一正差在Pylint，去掉后0/2。两个层的132份runtime仅execution.py不同，已保存diff：变化涉及MVP准入、前置条件与身份记账，131份其余源及transport一致。该合并保留控制器变化说明，不声称全部源完全相同。

B1 Coding的Matplotlib单题上，Sol+Flash+Flash(S-FF)优于Sol solo；Flash+Flash+Flash(F-T)却输给Flash solo。同题的Sol+GLM测试(D11)优于Sol solo，Terra+GLM(T11)与Sol solo都失败；因此“大预算团队”也不能归为一个统一处理。Sol solo在这题有transport/tool-policy终止，优势首先是所交付系统结果的差异，不能剥离成纯推理能力因果效果。

## 没有直接对照的证据仍保留

v5没有同源solo；v9只有DeepSeek solo，team的Sol lead同时变化，因此只进入换模型组合比较。v11虽与v10/v12同runtime，预算却是900k/40，没有同预算solo。v16与v15b同runtime但团队600k/28、solo900k/40，不纳入直接团队效应。hard canary与更晚solo存在实现或控制差异，同样保留为无可比对照。

swe_verified名为dpswarm的五条正式评分，实际workers=0、delegations=0，只能评价当时的单lead控制路径。C1是固定Flash团队内部CM开关，没有同源solo；其18条结果不进入团队对solo分母。B1早期标准API与后续Coding运行分别保留；标准API有未知usage的成本/token保持未知。T00对S-S同时改变预算，不能用于等预算团队收益。

表内token、成本与生成时间均为先题内重复均值、再题间等权均值；时间不是中位数。API等价成本是冻结价格情景，不是订阅实际现金；未知usage不补零。历史题目经过诊断性选择且多次暴露，这份分析给出特定样本与冻结条件下的描述结果，不作总体SWE题库推断。共享baseline、同题跨版本复用及不同配置对之间的相关性，使全表合并胜率或全表显著性检验没有合理独立单位。

## 可复核文件

- COMPARISONS.csv：题级全部可比固定组合，含共享baseline与源episode IDs。
- SERIES_AGGREGATES.csv / SUMMARY.json：每个同源固定配置对的等题权汇总、正负一致性、逐个剔除信息题的敏感性。
- COVERAGE.csv：306次尝试完整纳入/排除路径。
- CROSS_STRATA_TASK_SUPPORT.csv：每个具体角色模型组合跨设置的去重正向、负向、混合和仅持平任务，以及每次支持所在的设置。
- MODEL_COMBINATION_CONSISTENCY.csv：同一具体模型角色组合跨严格历史层的方向一致性、冲突及任务重用；不把层数当独立样本。
- B1_CONTROLLER_DIFF.txt：Coding两控制器层的唯一源差异。
- synthesize_team.py：可离线重建全部本目录结果。
