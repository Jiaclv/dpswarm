# Revision 12 工程合同

日期：2026-09-05。对应审查：[项目现状分析](../../../项目现状分析-20260905.md)。本修订实施持久化、验收归属、运行异常和文件观测修复；测试与交付状态见[工程修复记录](../../../工程修复记录-20260905.md)。

## 版本与证据

- 新 `result.schema_version=12`。新源码不得用 revision 11 的 gate 放行；`gate_revision11.json` 与历史 source/results/input/validation snapshots 保留原样。
- 新 gate 区分离线验收和真实运行放行。环境探针改变后，历史环境 negative control 不能直接充当本修订验证。
- 不改 F1/F3/F5 默认开关；F1/F5 的输入改成下文的真实文件观测，属于协议变化。与 v15/v16 比较时必须承认此差异。

## 宿主写入与用量

- JSON 投影使用同目录唯一临时文件、flush/fsync、原子替换。只对替换环节的临时 `PermissionError` 最多尝试三次，不重试模型请求或日志事件。
- 事件追加失败后停止新准入；预算投影失败提升为 `execution_health.status=host_error`，同时记录 phase、原异常类型/消息、errno/winerror。
- 已收到的响应先结算，再写可重建投影。其他已发出的并发请求仍分别结算真实响应；拿不到终态的预约保持 pending/unknown，不能补零。
- 独立 `host-errors.jsonl` 尽力保存错误与内存预算快照。整个磁盘不可写时无法保证诊断落盘，调用异常及批次停止仍保留。
- Lead/Worker 在清理前尽力导出实际补丁；`lead_artifact`、`delta_status` 与大小/hash 表示实际保存状态。缺失大小为 null；可读取空补丁才是零。

## 文件观测与 F1/F5

- Git 只枚举 tracked 与 non-ignored untracked 路径；隔离 Python 直接读取文件字节和符号链接目标，不运行仓库模块、Git filter、hook，也不改 index。
- 每个 actor 启动后、执行工具前建立基线；每次 bash 和正式采纳操作后保存观测 JSON 与事件。文件添加、删除、二进制变更、回撤均按字节/hash 比较。
- `edit_attempted` 和旧 `worktree_edit_detected`/`first_edit_ordinal` 是正则识别的命令尝试，继续保留。
- `observed_persisted_change` 表示某个采样点曾观察到相对基线的改动，首次 ordinal/origin 固化；之后撤回仍为 true。
- `current_nonempty_delta` 表示最近采样点的净改动。Lead 采纳 Worker 补丁计入 Lead 状态，origin 为 adoption。
- 工具内部先改后撤回、最终采样时已恢复的瞬态不保证可见。这是离散采样，不是每次文件写入的系统级审计。
- 探针在本地临时 Git 仓库验证；容器内实际兼容性、扫描耗时和完整性仍需新的无模型环境 canary。

## 工具阶段与预算

- scout 的提示和 schema 只声明 bash；宿主在单轮后结束 scout。非法工具明确记录未执行。
- 恢复正常 Lead 阶段时替换为正常提示；原生工具协议使用对应 tool_call_id 配对反馈。
- CM 与主调用均在准入锁内二次检查取消、排空和 deadline。CM 预约量为输入估算 + 有效 cm_max_tokens + cm_reservation_slack；新事件保存预约量与配置，默认预约量较旧实现增加，属于本修订协议变化。
- closing 仅声明 finish。它可按开关使用 Lead 保留票，但仍受总票数、token 预约、deadline、取消和并发准入约束。
- 固定团队与异构团队共用“两名 Worker 实际调用覆盖”停止检查。普通 unresolved、正常 blocked 和已知空补丁继续沿用原停止合同。

## 配置与上下文

- 运行前校验 override 的字段、类型、数值范围和装配依赖。每条新 schedule 写入有效配置与角色顺序配置 hash，并拒绝与运行时计算不一致的 effective_limits。
- 进程级模型/容器并发和固定团队人数不能通过单臂 override 伪装改变；此类值现在明确拒绝。
- 上下文预算覆盖最终骨架、标题、正文、引用。摘要超额时整段放弃；材料按完整条目进入 required 或 optional/pull，不将截断材料标记为完整 required。
- 骨架与引用仍超预算时，在压缩和落盘前报错，并由运行器记录 context_assembly 失败。token 仍为现有字符估算口径，不等同 provider tokenizer。

## 后续门槛

本合同不增加美元硬预算、完整动态实验注册表、自动 DAG 后继调度或真实 DSH 用量采集能力。P0 中这些未完成合同仍须单列，不能把本修订的离线通过称为整个 P0/P1 完成。真实模型实验须继续使用新冻结版本、独立环境验证和明确的运行数/预算。
