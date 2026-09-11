# DPswarm 交接 Playbook v1（prompt 资产，非控制面状态）

用途：deps 交接包（三层）与依赖类型判型的语义指引。加载方：CM
（ContextManagerLLM）压缩/判型调用与 L1 确定性提取的优先级配置。
硬约束：L1 提取/晋升门/记账/审计永不委给 LLM——本文件只引导语义层。

## 1. 交接摘要结构指引（L2，CM 压缩时遵循）

按以下章节组织，缺省写"无"：
- 目标：上游交付要解决的问题（一句话）；
- 已验收决定与不变量：逐条列，关键标识（版本号/hash/引用 ID/路径）原样保留；
- 对下游的接口契约：下游必须遵守的约束（类型、格式、边界条件）；
- 未决问题与已确认死路；
- 原文定位：上游 item 标识与 package 引用（逐字内容下游应查 L1/L0，摘要不承担逐字保真）。

## 2. verbatim / semantic 判型准则

判 verbatim（逐字依赖）——下游验收约束需要上游交付的**逐字内容**：
- 任务书含"逐字/原样/引用原文/与…一致"等验收措辞；
- 下游要用上游的 schema/接口定义/签名/常量/配置作为代码或测试的输入；
- 验收可由字符串级比对核验（diff 相等、doctest 内嵌原文）。

判 semantic（语义依赖）——下游只需要上游的**结论与决定**：
- 任务书要求"基于/根据/参考…的结论、分析、建议"进行再创作；
- 验收按语义覆盖与一致性判断，不要求逐字；
- 上游交付是分析、报告、计划类文本而非契约类文本。

判定输出：只输出一个词，`verbatim` 或 `semantic`。拿不准判 verbatim
（保真优先，代价只是 L1 更长）。

## 3. L1 模式优先级（任务型侧重）

默认丢弃序（超上限时低优先先丢）：
`fenced > json > signature > misc`

- fenced：fenced code block 整段（契约/代码的逐字载体）；
- json：standalone schema/JSON 键值行；
- signature：函数/类签名行；
- misc：markdown 表格行、配置键值、文件路径行、版本标识。

任务型侧重建议（判型/选路时参考，不改变确定性提取动作）：
- 契约/代码类上游：保持默认；
- 数据/表格类上游：可置 `l1_priority: fenced > misc > json > signature`；
- 接口/库类上游：可置 `l1_priority: fenced > signature > json > misc`。

配置覆盖格式（单独一行，仅编排层解析）：
`l1_priority: fenced > json > signature > misc`
