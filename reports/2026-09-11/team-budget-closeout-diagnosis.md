# 19:52 团队运行故障核查

核查对象：DSH 根会话 session-7fdf60d9-3ab1-4b36-b9c7-6ceeb46c60dc（2026-09-11 19:52–19:57，Australia/Sydney）。本次为日志与已安装源码诊断；未更改运行插件，未重新调用模型，未验证 HTML 视觉效果。

## 结论

此前任务读取门控已放行。本轮另有两处代码兼容问题，并叠加失败结算后的重派限制：

1. **首次输出额度计算遗漏待提交输入。** 已安装 budget.js:129–152 在 agent/request 读取 deriveMessages，但 DSH agent-loop/lib/index.js:1017 先运行该阶段，1028 才提交 decision.messages。三个角色此时只计算系统提示词和 35 个工具，序列化估算均为 14,501；用户任务、运行上下文和技能目录尚未入账。最终 stream 按完整消息重新计算，因输出额度已占满原估计剩余空间而超额。故不能归结为模型选择的 18 万预算本身太小。

| 角色 | 总额度 | 提前估算输入 | 输出上限 | 准入实际估算输入 | 预留合计 |
| --- | ---: | ---: | ---: | ---: | ---: |
| implementer | 180,000 | 14,501 | 165,499 | 18,598 | 184,097 |
| tester | 120,000 | 14,501 | 105,499 | 19,536 | 125,035 |
| reviewer | 120,000 | 14,501 | 105,499 | 20,394 | 125,893 |

三者均 0 次调用、0 token，错误 WORKER_TOKEN_RESERVATION_DENIED。提前估算值使用各自日志中的系统文本、完整 request/header.tools、空 messages 重新计算，全部精确得到 14,501。

2. **失败也结束本次团队执行要求。** plugin-audit 记录 dpswarm/team-required-finished，outcome=failed_takeover。随后同一用户任务再次 dpswarm_run 被 TEAM_REQUIRED_ALREADY_FULFILLED 拒绝。这里 finished 表示运行已结算并交由 Lead 接管，不代表任务已验收成功。当前恢复路径是 rework；它没有用第二次 dpswarm_run 中被拒绝的新预算。

3. **收尾指令实际存在，但校验仍读取旧字段。** rework 实现者 a7e67478-2e00-469a-88cc-17b714df86b7 使用固定 600,000 token / 28 calls。完成 7 次调用、398,734 token 后剩 201,266。收尾预测认为继续工作并留足后续报告空间不够，进入 final_only（预测输入 85,282，报告输入 76,513，裕量 53,932，报告保底 4,096，交付预留 2,048；合计 221,871）。这个提前收尾行为符合当前预算规则。

真正的中断在随后的准入：最后请求预留 198,527，小于剩余额度 201,266，却抛 WORKER_CLOSEOUT_INSTRUCTION_MISSING。子会话 seq 68 已保存含 DPSWARM_WORKER_FINAL_ONLY 的 system/message；新版宿主把系统提示词放在 messages（agent-loop:1204–1217），而已安装 budget-runtime.js:573 只检查 options.system。请求尚未到模型即遭误拒。

## 结果与用量边界

- 初始三个角色未执行；只有返工实现者实际运行。没有团队 deliveries，没有独立 tester/reviewer 结论。主模型接管后生成的 HTML 不等于团队流程验收成功。
- 主会话 19 次 assistant/message 的 totalTokens 合计 687,231，与界面 687K 相符；其中 cacheReadTokens=647,424，outputTokens=13,850。
- 返工子会话另计 398,734（7 次调用）；两者合计 1,085,965，包含反复发送的缓存上下文。不是生成了这些数量的新文字，也不等于同价计费。初次三个被拒角色用量为 0。
- Lead 的“你要求不加浏览器检查”在这轮原始用户消息中没有依据；限制出现在 Lead 自己提交的任务规格里。本次未浏览器渲染，不能认可其“控制台无错误/循环不增长内存”等动态结论。

## 修复方向

先让预算输出额度与最终输入使用一致的完整请求范围，再让收尾标记校验支持宿主实际 system 消息；都应保留预算和收尾约束。失败状态应区分未发出模型请求的准入失败和已经执行后的失败，明确哪类可修正预算后重试，不能把 finished 文案当成成功验收。以上为诊断与建议，本轮未实施。

## 证据位置

- 已安装插件：%USERPROFILE%/.dsh/profiles/web/node_modules/dpswarm-dsh-plugin/lib/
- 宿主：%USERPROFILE%/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js
- 原生会话：%USERPROFILE%/.dsh/sessions/--K-~79CB~62DB-~9879~76EE-DPswarm-911--/ 下本报告列出的会话目录。
- 插件审计：.tmp/runtime-0.7.5/sessions/773903b2c252ebbbc2324c6ef2ef456ce53dbc1f98a9556be32e58604beede8f/plugin-audit/journal.jsonl

## 独立复核

- 原生零模型复现：NativeSession 的 system/message 含收尾标记，未经修改的原生 ReactLoopAgent.buildRequest 返回 messages 中的 system 消息，options.system 为 undefined；已安装 runtime.admit 抛 WORKER_CLOSEOUT_INSTRUCTION_MISSING。仅在内存中的对照请求补回旧 system 字段即准入成功。未发送任何模型请求。正式修复应读取实际 system 消息，不能重复注入，也不能将 user/tool 文本视为收尾指令。
- 状态机复核：delegation.js:83–98 在子会话发布和路由绑定后记录 started，早于模型请求；team-dispatch.js:27–47 在清理完成后对失败也记录 finished(failed_takeover)。建议对所有子会话均已结束、确认清理且 0 模型调用的失败引入可重试 attempt，保持有实际执行失败的现有接管约束。不能仅删除一处 ALREADY_FULFILLED 检查。
- 另发现工具说明与默认配置不一致：已安装 index.js:132 的 rework 文案称 unlimited by default，但 index.js:32–33、budget-runtime.js:120–126 的实际默认值是 fixed 600,000 tokens / 28 calls。


> 发布说明：文中本机 `.tmp` 日志、安装备份和运行状态不随仓库发布；源码与回归测试随项目同步。安装和测试数字是记录时的结果，不代表读者环境或后续版本已经验证。
