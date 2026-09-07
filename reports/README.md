# 实验报告与数据

最新发布快照：**2026-09-07**。本页区分可以直接阅读、可以从汇总数据复算，以及仍依赖本地原始归档的内容。

| 报告 | GitHub 正文 | 可下载后离线打开的交互 HTML | 重点 |
|---|---|---|---|
| 全历史综合 | [完整报告](2026-09-07/full-history/REPORT_ZH.md) | [HTML](2026-09-07/full-history/REPORT_ZH.html) | 306 次尝试的去向；249 条合格 SWE 评分、26 题；按可比配置与机制综合 |
| 具体贡献拆解 | [完整报告](2026-09-07/component-attribution/REPORT_ZH.md) | [HTML](2026-09-07/component-attribution/REPORT_ZH.html) | 生产交付分解、10 次正向团队运行的生产来源、测试→补修事件与 CM 收支 |

GitHub 直接渲染 Markdown 和 PNG。HTML 为自包含快照，下载文件后在浏览器打开；它不是实时实验看板。两份报告均保留全部原有章节，综合报告补充了最新归因解释与入口。

## 最重要的证据边界

- 249 条评分不是 249 道独立题；跨版本、预算、模型和重复任务不混成一个总胜率。
- 团队正向信号集中于 Sphinx-8035、Matplotlib-20826、Pylint-6386 三道去重题，不能把重复运行当成更多新题。
- 具体代码、测试与修复链可以追踪；测试者平均净贡献、review 正确率、自主路由收益仍未识别。
- CM 的直接对照是 Sol Lead＋两个 GLM-5.3-Flash worker、DeepSeek 压缩模型，不能直接推广到 Terra／GLM 团队或全 Flash Lead。
- 费用是冻结 API 等价价卡，不是订阅现金账单。未知用量不补零。

## 可直接复核的数据

| 数据 | 用途 |
|---|---|
| [全部尝试](2026-09-07/full-history/ALL_ATTEMPTS.csv) / [合格 SWE 评分](2026-09-07/full-history/QUALIFIED_SWE_EPISODES.csv) | 检查库存、资格与分母；机器路径已规范化 |
| [按范围汇总](2026-09-07/full-history/FOCUS_AGGREGATES.csv) | 复核 A2、A1+A2、B1、C1 等各自口径，重叠范围不可相加 |
| [题目特征](2026-09-07/full-history/TASK_CHARACTERISTICS.csv) | 问题类型、验收形式与事后难度描述；缺失的榜单题级率保留为空 |
| [团队比较层](2026-09-07/full-history/team_synthesis/COMPARISONS.csv) | 同 Lead 与更换 Lead 分开、共享对照去重 |
| [18 题交付配对](2026-09-07/component-attribution/PAIRED_18_DELIVERY.csv) | 重算生产交付与通过差异 |
| [十次生产来源](2026-09-07/component-attribution/POSITIVE_10_ORIGINS.csv) / [关键事件](2026-09-07/component-attribution/KEY_EVENTS.json) | 查看被采用的生产改动、Lead 接管与测试反馈顺序 |
| [CM 角色账](2026-09-07/component-attribution/CM_ROLE_ACCOUNTING.csv) / [CM 差额](2026-09-07/component-attribution/CM_COMPONENT_BRIDGE.json) | 重算输入、输出、压缩自耗与价卡变化 |

## 发布副本与原始归档

完整模型调用记录、工作区、模型响应、镜像、逐次评分目录、运行状态和本地账号配置保留在本地，未随这次 GitHub 同步发布。报告中的相应链接明确标为“本地归档”，数据表中的路径只作来源标识，不承诺对应文件已在仓库中。

发布版剥离机器专属路径前缀、更新文档导航，并为综合报告增加归因补充。数值观察、既有章节、图表和原始实验文件未改写。[PUBLICATION_MANIFEST.json](2026-09-07/PUBLICATION_MANIFEST.json) 同时保留来源 hash 与发布副本 hash，不能要求规范化后的 CSV 与原文件具有相同字节 hash。

保留的 `DATA_VALIDATION.json`、`REPORT_VALIDATION.json` 及独立复核记录属于**原始分析的历史凭据**；发布副本的内容、链接、渲染和同步检查分别见 [PUBLICATION_VALIDATION.json](2026-09-07/PUBLICATION_VALIDATION.json) 与 [SYNC_VALIDATION.json](2026-09-07/SYNC_VALIDATION.json)。自检不会改称新增独立审阅。

从这些公开表可以复算核心比较和账目分解；重放完整历史实验还需要原始输入、冻结运行时、评分环境和调用归档。导出脚本也以本地完整分析目录为输入，不声称仅克隆仓库就能重新生成全部原始证据。
