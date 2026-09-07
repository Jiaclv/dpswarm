# 全历史角色贡献与预算综合

结论：历史证明角色调用、真实编辑和采纳确实发生，也证明这些阶段可以与整题成功脱节。测试角色的执行率、实现角色的保留率、根任务的官方成绩必须分别判断；跨预算与跨模型切换会改变整条执行链，不能从一个阶段的失败推断模型没有实现或测试能力。

本次回溯306条尝试总库存中的292条既有episode子集（未纳入1条A1基础设施尝试和13条A2未合格尝试）；292是审计行覆盖数，不代表每行原始result均可读。其中两条B1中断记录的source_result为空，source_read=False，不能称已读原始结果。该子集恢复248个独立worker与92个脚本编排角色阶段，共340条记录。按family、batch、arm、具体model、根预算和角色分组，保留异常尝试与不同版本。覆盖总量不能合并成总体准确率。

## 统一口径

实例化 → 有调用 → 观察到非空delta → 完成且可交付 → 生产/测试内容 → 采纳 → 最终保留 → 根任务质量。

非空delta可能是被阻塞worker的工作区快照，不自动等于成功交付。UNIFIED_WORKERS.csv新增worker_completed、delta_status、eligible_delivery；后者只在completed且present非空delta时置真，缺少字段保持未知。生产与测试按实际修改路径分类，不按分配角色倒推完成。

采纳使用既有详细审计及legacy review_worker的tool_completed.result；reviewed=true不代表adopt。最终保留要求所有修改文件与worker完成时一致；False只表示不满足严格判据，Lead可能部分吸收或重写。空delta的保留不适用。根质量是同一episode的官方或指定基准结果，两worker不能算两个独立成功样本。

| cohort | 角色记录 | 非空delta（字段已知分母） | 生产/测试 | 非空采纳/空采纳 | 最终严格保留（可检查非空分母） |
|---|---:|---|---|---|---|
| legacy fixed-team |126|94/125，1未知|47/48，有重叠|61/0；采纳字段106/126已知|2/2，其余未知|
| A1 |6|5/6|1/4|4/0|4/5|
| A2 |48|45/48|16/29|41/0|32/45|
| B1全部尝试 |32|20/32|11/9|18/1；采纳字段27/32已知|12/20|
| C1 |36|19/36|6/13|17/2|14/19|
| TeamEval脚本阶段 |60|未知/不适用|未知/不适用|未知/不适用|未知/不适用|
| runtime-v2脚本阶段 |32|未知/不适用|未知/不适用|未知/不适用|未知/不适用|

legacy 126个worker回收到125个delta判定、106个采纳判定；106由103个原始completed review_worker事件（61 adopt、42 discard）加3个终态death_phase支持的False组成，后三个不是采纳完成事件。但只有2个可用直接工作区快照完整核对最终文件，均一致；其余不能写成“不保留”。较早SWE Verified的12次root尝试未回收到实际独立worker，原型日志9条仅有根级信息。COVERAGE.csv逐episode列出缺口。

92个脚本阶段来自原始planner、executor、verifier、oracle、repair等记录。共享工作区缺少每阶段独立补丁，非空、采纳和保留为未知/不适用。ROLE_RESOURCES.csv从control.usage.by_role提取可观测调用及token；root workers=0不代表角色调用或资源为零。

## 角色机制累积判断

A1两题各臂均1/2，D/S与T/S没有成绩变化。A2的16题中S=10/16、D=10/16、T=12/16。D相对S没有胜负不一致题；T相对D或S多解Matplotlib-20826与Pylint-6386两题、无丢失题，小样本不足以支持稳定增益。48角色观察到45个非空delta、41个非空采纳、32个严格保留；其中42个completed、3个blocked；完成且present非空delta为42，不能称45次成功交付。

D/S测量“加入GLM-5.3测试worker的整套配置”，同时影响根预算分配、Lead协调审阅、测试提示和工作区可见性。10/16对10/16支持此配置本轮未显示增量解题，不能当作测试模型内在能力测验。T/D增加Terra实现角色，也不能把两题提升完全归因于某段worker代码而忽略Lead集成。

大预算core中Sol Lead、Terra implementation、GLM-5.3 test保持组成，T00→T11联合增加根token、普通调用、worker工作调用及CM额度。实现非空生产delta从3/3变2/3；测试从1/3变3/3、非空采纳从1/3变2/3、严格保留从0/1变2/3；根通过却从2/3变1/3。“测试角色真正执行得更充分”和“整体更好”是两条不同结论，T00不能称3个有效测试worker。

C1固定有测试角色，36worker中19非空（6生产、13测试），17非空采纳另有2空采纳，14严格保留。6个生产delta全部来自失败的Xarray条件；成功Pylint可以由Lead在实现worker无生产delta时完成。C1能定位阶段断点，但没有tester-off来估计去掉测试者的结果。小调用上限、提前停止和任务差异，与B1的48-call运行不能合并成Flash实现能力排名。

大预算Matplotlib S-FF是直接反例：Flash实现47次调用、测试15次，两者都有真实delta并采纳；Lead之后修改相关文件，严格保留两者为False，但根任务通过。因此“最终字节不等=零贡献”会误删真实实现、测试与采纳。F-T也不是没有修复目标：官方F2P=1/1，但P2P=671/673，两个旧测试回归导致整题失败。

早期pilot_v8同一个Sol Lead下交换Sol/GLM的实现与测试方向，两臂各1/1；它没有比较GLM Lead与GPT Lead。固定Terra实现、切换Luna与GLM测试也各1/1，各只有一题一次。pilot_v10 Sphinx上solo0/3、关闭worker assembly的team3/3，而pilot_v12 Astropy两者3/3；这是任务依赖的整队配置证据，不是统一模型胜负率。

## 全部已审计可比预算变化

BUDGET_COMPARISONS.csv纳入B1已有15对（8对预算组合、7对同预算角色配置）与legacy 5对预算变化（3个CM池、2个根预算后续）。ALL_BUDGET_CONFIGURATIONS.csv保留所有已覆盖cohort的预算设置。未配成对照的历史不能静默删除，也不能自动升级成因果对照。B1每格一次，无随机重复；provider中断与transport差异保留原validity。

legacy 600k/28→900k/40：Sphinx从1/3到2/2，平均token约562k→803k、生成时间8.07→13.08分钟；Xarray从0/2到0/2，token564k→812k、时间8.31→14.56分钟。一个任务改善，一个没有；token、调用数、F5绝对触发阈值及冻结代码有变化，不能称纯token效果。CM池6→24及12→24三组均保留，CM配额不等于实际消耗，共享根预算可能先结束运行。

Matplotlib的T00成功、T01失败、T10成功、T11失败，否定“更多预算必然成功”。D00→D11失败转成功，是同批预算可能有帮助的反例；不抵消T11失败。F-S、F-T、S-FF用于区分Lead/worker模型构成，固定预算对比不能误标预算增量。所有20对逐条在文末列出，预算字段含根token、普通调用、worker工作调用与CM池。

## 预算实际流向

T00→T11三题实际token增加3,064,217。增量中实现33.2%、CM32.8%、测试18.9%、Lead15.2%；额外预算近三分之一用于上下文管理，不等于同量额外编码。

|角色|T00实际token|T11实际token|增加token|增量占比|
|---|---:|---:|---:|---:|
|Lead|663333|1128306|464973|15.2%|
|实现|431356|1447272|1015916|33.2%|
|测试|193343|772115|578772|18.9%|
|CM|195608|1200164|1004556|32.8%|

worker工作调用上限8→48不意味着必须用满48；收尾调用可能另计，不能以总调用9断定超过8的工作调用限制。资源不足判断应同时检查scope、关停阶段、真实产出、Lead集成及回归安全。ROLE_RESOURCES.csv按实际角色和模型列明资源，空token是未知，known subtotal不等于完整账单，API等价价不等于订阅实付；角色调用耗时之和也不等于并行端到端耗时。

R2存在独立局部额度约束：A1+A2四道fallback题8个候选的局部上限均270,000，停止时局部尚余20,871–45,895，根快照尚余104,483–148,986，普通调用尚余3–5。局部scope先于root作reservation且不可转移，余量不足下一次reserve会拒绝，不能解释为全根额度用尽。部分根快照有pending，不能把余额加成可立即自由花费额度。这证明预算切分构成实际约束，尚不能证明允许转移就能救题；全部36个R2候选没有独立官方grade，不能报告候选准确率。见v2根R2_BUDGET_SCOPES.csv、R2_CUMULATIVE.csv。

实现、测试、CM和Lead集成在历史中确实发生，同时存在明确阶段断点。预算增加可改善角色执行，也可能带来更多上下文处理、冗余与回归；最终收益依赖任务与分工。后续若归因测试价值，需要同模型同预算同协议的测试角色开关，并独立记录测试区分错误实现、Lead执行测试、目标修复与回归结果；当前历史未完成这一纯变量测量。

## 可重建证据文件

UNIFIED_WORKERS.csv逐角色；COHORT_ROLE_SUMMARY.csv每指标均给true/known/unknown；COVERAGE.csv逐episode缺口；BUDGET_COMPARISONS.csv全部20个已审计配对；ALL_BUDGET_CONFIGURATIONS.csv全部预算设置；ROLE_RESOURCES.csv、ROLE_RESOURCE_SHARES.csv及ROLE_BUDGET_GROWTH.csv实际资源与已观测分母内占比；缺记录或usage不完整时不称全成本占比。synthesize_roles.py可重建表格，无新模型请求、评分或联网。

## 全部配对清单

|历史|任务|变化|根结果|实际token差|性质|
|---|---|---|---|---:|---|
| B1 | matplotlib__matplotlib-20826 | T00 → T11 | True/1 → False/1 | 1787860 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | T11 → S-S | False/1 → False/1 | -645357 | role_configuration |
| B1 | pydata__xarray-6992 | T00 → T11 | False/1 → False/1 | 667285 | budget_bundle |
| B1 | pydata__xarray-6992 | T11 → S-S | False/1 → False/1 | -299532 | role_configuration |
| B1 | pylint-dev__pylint-6386 | T00 → T11 | True/1 → True/1 | 609072 | budget_bundle |
| B1 | pylint-dev__pylint-6386 | T11 → S-S | True/1 → False/1 | 1189621 | role_configuration |
| B1 | matplotlib__matplotlib-20826 | T00 → T10 | True/1 → True/1 | 95052 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | T01 → T11 | False/1 → False/1 | 1808966 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | T00 → T01 | True/1 → False/1 | -21106 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | T10 → T11 | True/1 → False/1 | 1692808 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | F-S → F-T | True/1 → False/1 | 1104259 | role_configuration |
| B1 | matplotlib__matplotlib-20826 | F-T → S-FF | False/1 → True/1 | 29002 | role_configuration |
| B1 | matplotlib__matplotlib-20826 | T11 → S-FF | False/1 → True/1 | 34733 | role_configuration |
| B1 | matplotlib__matplotlib-20826 | D00 → D11 | False/1 → True/1 | 1819783 | budget_bundle |
| B1 | matplotlib__matplotlib-20826 | D11 → T11 | True/1 → False/1 | -26651 | role_configuration |
| legacy | sphinx-doc__sphinx-8035 | solo_gpt-5.6-sol.cm6 → solo_gpt-5.6-sol.cm24 | 2/2 → 0/2 | 28296 | within_version_cm_pool |
| legacy | sphinx-doc__sphinx-8035 | heterooff_gpt-5.6-terra__glm-5.3.cm6 → heterooff_gpt-5.6-terra__glm-5.3.cm24 | 1/1 → 1/1 | -15037 | within_version_cm_pool |
| legacy | sphinx-doc__sphinx-8035 | solo_gpt-5.6-sol → solo_gpt-5.6-sol.cm24 | 1/3 → 0/2 | 17413 | within_version_cm_pool |
| legacy | sphinx-doc__sphinx-8035 | solo_gpt-5.6-sol → solo_gpt-5.6-sol.b900 | 1/3 → 2/2 | 240609 | cross_batch_budget_bundle |
| legacy | pydata__xarray-7229 | solo_gpt-5.6-sol → solo_gpt-5.6-sol.b900 | 0/2 → 0/2 | 248152 | cross_batch_budget_bundle |

B1结果为布尔值/1；legacy为成功次数/n。token差为B1单格差或legacy每次运行均值差，不能直接加总。
