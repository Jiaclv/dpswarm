# 待审批：测试交付与 Lead 复核要求

状态：**未批准、未实施**。此文件是本次修复后的待决策提案，未修改 `DPswarm-机制架构.md`、角色提示、固定分工、验收状态机或实验条件。

## 为什么提出

本次 xarray 难题里，两个 Worker 实际完成且交付均被 Lead 采纳，官方核心测试仍失败。测试用例给不同输入设置相同属性，无法区分正确的属性来源与错误来源。运行完整性检查不会自动识别这种语义缺口。

当前已确认机制由 Lead 自主决定语义验收，bench 另做校准，见机制架构 §4。下面提案把一部分检查从“Lead 自行选择”改为“测试交付和最终审查必须明确说明”，因此先申请批准。它仍不能保证解题成功，改进效果须另行评估。

## 提议批准的最小范围

1. **测试 Worker 交付必须说明反例。** 从公开需求选取相关输入差异、冲突、边界或异常，说明至少一种看似合理但错误的实现会如何被测试识别。仅当多个输入存在来源语义时，使用可区分的输入值；不机械要求所有任务使用同一种测试模板。
2. **由能接触对应版本的角色记录实际验证。** 测试 Worker 在自己的未修复基线上记录命令、真实测试退出状态和结果；若无法得到有意义的基线失败，明确说明原因。它不能声称验证了尚未看到的生产 Worker 补丁。
3. **Lead 在合并后复核同组测试。** 记录实际运行范围、通过/失败/跳过/未选择数量；检查用例是否能区分错误实现。证据缺失时明确给出仍采纳或丢弃的理由，不能把局部测试通过写成官方任务通过。

拟加入测试 Worker 分工的具体文本：

> For behavior-changing fixes, derive a focused counterexample from the public issue: explain one plausible wrong implementation the tests distinguish, using distinguishable input sources or relevant boundary conditions where applicable. Run the tests against your unchanged production baseline when feasible. Report the exact command, observed test status and selected scope; if a meaningful failing baseline cannot be established, explain why. Do not infer test success from a pipeline's final command status, and do not claim validation of a production patch you have not received.

拟加入 Lead 实验提示的具体文本：

> During final integration review, inspect the test worker's counterexample and baseline evidence, then run the same focused tests with the adopted production changes when feasible. State the observed results and test scope. If evidence is unavailable or the tests cannot distinguish a plausible wrong implementation, explicitly record the limitation and the reason for your adoption or discard decision. Local test success is not an official benchmark result.

审批后的第一步只会更新机制文档和实验角色合同、补离线回归。首次适用范围为现有 fixed-team SWE 实验运行器，不自动推广到所有产品任务，也不自动启动新模型实验。

## 保持的边界

- Lead 保留最终语义验收权；不新增 Reviewer、自动否决或新的 accepted 前置状态。
- 不更换模型，不扩充人数、调用票数、token 或时间预算，不修改 F1/F3/F5/CM/Assembly 默认值。
- 不强制 Bash `pipefail`。当前普通 Bash 管道按最后一段返回状态，`pytest | tail` 的 0 不等于 pytest 的 0；强制 `pipefail` 会改变其他合法管道和后续 `&&` 行为，应另作 Shell 合同决策。本提案只要求测试证据保留真实状态。
- 不更改官方评分、原始问题、gold 数据或已冻结候选补丁。本次 xarray 已被事后诊断，后续再用它只能算已暴露任务回归，不能作为盲测增益证据。
- 不预先设定“反例要求一定提高成功率”。将来如要比较效果，需另行冻结相同预算下的对照实验。

**待用户决定：是否批准上述三项测试交付与 Lead 复核要求？** 未批准前只完成实现修复，不把本提案注入任何模型任务。
