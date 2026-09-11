# 2026-09-05 单次困难任务实验

用户授权：修复后用一道难题做一次实际实验。此轮只创建一个任务实例、一个团队、一次重复；不启动 NEXT_EXPERIMENT_PLAN_20260905 的 P1/P2/P3 多臂矩阵，不根据结果补跑或换题。

## 选择和研究边界

- 实例：`pydata__xarray-7229`，使用既有冻结的 SWE-bench Verified public row、原始 issue 和 base commit。
- 选择依据只用历史汇总：v15b solo b900 0/2，v16 异构 0/4。该实例已在本项目使用过，属于暴露任务回归，不是新题或独立泛化评测。历史团队有效性不一，不据此证明模型强弱。
- 测试目的：检验修复后真实模型的工具执行、双 worker 协作、提交采纳、预算结算、直接文件观察和官方评分链路，并报告本次是否解决问题。
- Solver 是新启动的实验 transport，会收到原始公开问题和隔离仓库；主任务和审查子 agent 的历史答案、评分私有数据、负对照日志均不注入 solver。
- 一次成功或失败不能证明异构优势、因果增益、排名或生产就绪。固定 DERIVE；不覆盖自主分裂、FISSION 或 DSH。

## 冻结条件

| 项目 | 本次设置 |
|---|---|
| Lead | gpt-5.6-sol；通过 Codex CLI；请求 effort=max、fast |
| Worker 1 | gpt-5.6-terra；生产修改；通过 Codex CLI；请求 effort=max、fast |
| Worker 2 | glm-5.3；独立回归测试；原生 HTTP，请求高推理设置 |
| Context manager | deepseek-v4-flash；thinking=disabled；按需压缩 |
| 预算 | 600,000 总 token 准入额度，共享工作和 CM 用量 |
| 调用限制 | 28 次工作调用，12 次独立 CM 调用额度；每 worker 最多 8 次；Lead 预留 2 次 |
| 时间 | 候选运行 1,800 秒；单模型调用最多 600 秒；CM socket 120 秒；命令 120 秒；官方评分 900 秒 |
| 输出 | 工作请求 max_tokens=32,768；原生 HTTP 可约束，Codex CLI 输出上限不保证执行；CM max_tokens=4,096 |
| CM | context_budget=12,000，保留最近 4 条，reservation_slack=8,192 |
| 当前默认机制 | edit curfew ON、edit status banner ON、closing reserve exemption ON |
| Assembly | team memory / scout distillation / bootstrap package 全 OFF；不声称验证这些路径 |
| 并发与资源 | 实际 1 个 run，2 个 worker；进程上限 4 model slots、4 candidate containers；每候选容器 2 CPU、3 GiB |
| 环境 | 本机 Docker Linux；候选无网络、UID 1000，无宿主敏感挂载 |
| 镜像 | xarray `sha256:ff7d2a1c6f69491a593147a1356df8469f0d67b75d2583b19fe28bf776abb88e` |

设置以新 manifest 中的 `effective_limits` 和配置哈希为执行真值。这里只使用当前默认机制，区别于后续 P1 计划中的 F1/F5 OFF 对照。请求设置和 provider 实际回显分开记录；缺失回显及缺失 usage 保持 unknown/null。600k 是调用前预留的准入额度，不等同于账单金额硬上限，未知/超额如实报告。

## 准入、评分与停止

1. 保留原 revision12 OFFLINE_PASS gate。修复评分独立脚本入口，并增加显式 prepare --gate，定向离线测试通过。
2. 在同实例镜像中运行无模型文件观察 canary：文本/二进制新增、tracked 删除、撤回、工作区模块遮蔽；观察不得改变 git index。记录性能。
3. 只提交无语义新增文件作为官方负对照。必须实际完成官方测试、resolved=false、无进程/清理错误；空 patch 不算负对照。
4. 创建本次独立 PASS gate，绑定新源码、定向验证、当前评分输入、环境结果和本计划。通过标准 prepare --gate 冻结恰好一个 schedule entry，再复核哈希后调用模型。
5. 首次正式调用后不改源码/预算/提示/评分数据；失败、超时、预算耗尽均保留，不补跑。主任务只做只读监测和结果审计，不给 solver 解题提示。
6. 候选结束、最终 patch 冻结、所有候选容器释放后，由独立官方控制器按既有冻结 harness 和私有 TestSpec 评分。检查 completed、resolved、官方报告、patch/base/image/source/input binding。
7. 记录每次实际 attempt、请求/回显模型、token 分项和 known/unknown/reserved、持续时间、worker 是否实际执行和是否被采纳、host health、最终评分。价格无可靠账单来源则金额保持 null。
8. 只清理实验拥有的容器；不清理无关服务。此次为一次性任务，无自动后续运行。

环境准备可以在零模型调用状态下修复明确基础设施问题，但每次失败/调整均保留记录。正式实验启动时间以 batch.start 和首条 call_started 原始记录为准。
