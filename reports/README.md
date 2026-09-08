# 实验报告与数据

最新工程验证：**[插件 0.7.4 显式团队与用户预算](2026-09-08/team-required/README.md)**；此前安装记录见 [0.7.3 模型注册接入与部署验证](2026-09-08/host-model-registry/README.md)。鹈鹕动画发布快照仍保留原时间点和 0.7.2 验证摘要。历史 SWE 分析仍冻结于 2026-09-07。本页区分当前快照、历史效果分析、可复算汇总数据及本地原始归档。

| 报告 | GitHub 正文 | 可下载后离线打开的交互 HTML | 重点 |
|---|---|---|---|
| DPH 鹈鹕动画快照 | [实验说明](2026-09-08/pelican/README.md) | [动画对比页](2026-09-08/pelican/index.html) | 标准／创造模式、对话模型、CM／团队与 worker 额度；进行中实验的发布时点记录 |
| 全历史综合 | [完整报告](2026-09-07/full-history/REPORT_ZH.md) | [HTML](2026-09-07/full-history/REPORT_ZH.html) | 306 次尝试的去向；249 条合格 SWE 评分、26 题；按可比配置与机制综合 |
| 具体贡献拆解 | [完整报告](2026-09-07/component-attribution/REPORT_ZH.md) | [HTML](2026-09-07/component-attribution/REPORT_ZH.html) | 生产交付分解、10 次正向团队运行的生产来源、测试→补修事件与 CM 收支 |

GitHub 直接渲染 Markdown 和 PNG。2026-09-07 两份 SWE 报告的 HTML 可单文件下载后打开。鹈鹕对比页的打开方式及所需相邻素材见其 README；发布文件是快照，不会随本地实验自动更新。

## 鹈鹕快照与 SWE 的边界

鹈鹕任务要求生成 SVG 动画 HTML，并明确不运行测试。作品交付、视觉展示和消耗记录分别解释，保存文件不等于通过官方测试。该批使用 `glm-5.3-flash` 作为 CM；插件产品默认仍为 `deepseek-v4-flash`。模型路由偏差、运行时版本、失败与恢复须按逐组证据区分，不能把旧尝试重新标成已修复版本。

此发布不把鹈鹕结果并入历史 SWE 的 249 条评分或 26 道题，也不把图像观感直接换算成团队、CM 或预算的因果收益。完成范围和待核验状态以快照自己的时间戳、索引及边界说明为准。

## 历史 SWE 的证据边界

- 249 条评分不是 249 道独立题；跨版本、预算、模型和重复任务不混成一个总胜率。
- 团队正向信号集中于 Sphinx-8035、Matplotlib-20826、Pylint-6386 三道去重题，不能把重复运行当成更多新题。
- 具体代码、测试与修复链可以追踪；测试者平均净贡献、review 正确率、自主路由收益仍未识别。
- CM 的直接对照是 Sol Lead＋两个 GLM-5.3-Flash worker、DeepSeek 压缩模型，不能直接推广到 Terra／GLM 团队或全 Flash Lead。
- 费用是冻结 API 等价价卡，不是订阅现金账单。未知用量不补零。

## 历史 SWE 可直接复核的数据

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


## 2026-09-08 插件与发布核验

[SYNC_VALIDATION.json](2026-09-08/SYNC_VALIDATION.json) 区分已执行的 143 项 Node、479 项 Python、当时 73 项批次检查、原生请求／冷恢复探针，以及后续 collector-4 的 33 项离线检查。原有 6 项实机设置检查因客户端字节一致而复用，本次没有重跑浏览器；公开同步执行的是源码与凭据哈希核验，不是新增效果实验。

鹈鹕快照的可发布文件、来源与隐藏的本地信息由[该快照说明](2026-09-08/pelican/README.md)单独列出。历史 SWE 的发布清单和原始结论继续使用 2026-09-07 的版本，后续插件修复不会回写这些历史数据。
