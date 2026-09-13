# 0.11.1：宿主存储访问及启动阻塞修复

状态：已修复、安装并完成临时运行验证；验证宿主已退出，日常版由用户在原终端执行 `dsh web` 启动。Python 控制服务保留原 PID 36524，未修改 Python 代码或模型配置。

## 本次真实故障

会话：`session-ef2bcae9-90b7-4131-8c25-899a98909356`。

原生记录共 161 行：实际 9 次 `dpswarm_run`，其中 5 次 `cannot get property "storage" without inject`；其余分别是 1 次候选路径缺失、2 次预算决策缺失、1 次原合同重试冲突。因此模型最后自述的“7 次全部同一错误”不准确。

控制面仅有 root，worker 占用为 0、没有 worker 派发或验收。6 次进入团队初始化后回滚（5 次 storage 错误、1 次合同冲突），没有残余工作区租约。已登记合同仍为 rev 1，evidence_revision=0；现成 HTML 不是本次团队交付。

## 两层实现问题

1. `index.js` 创建 `KvMailboxStorage(ctx)`，`openMailbox()` 调用 `available()` 时读取 `ctx.storage`。真实 Cordis 插件作用域没有注入该属性，即使服务存在也会拒绝。换成嵌套 runtime 仍失败。可选服务应通过宿主公开的 `ctx.get(name, false)` 查询。
2. 普通未分类异常未进入基础设施 blocked 状态；没有已绑定 worker，所以要求仍为 required。模型不断重试 run，提问和结束说明又受到 TEAM_REQUIRED 拦截。

此前 589 项插件回归虽然通过，但入口测试主要使用普通对象替身，完整验收夹具直接构造控制器；都绕过了实际启用入口的 Cordis 依赖访问约束。这是此前安装验证的覆盖缺口。

## 修复范围

- 新增可选宿主服务查询接口，修复 mailbox、session resolver 以及预算模块中的同类属性回退。没有改动预算分配规则。
- 启动前探测 storage/sessions 服务接口，并联合宿主、Python 控制服务能力生成兼容性证据。
- 只有探针实际覆盖的服务查询/形状故障才转换为 `HOST_RUNTIME_INCOMPATIBLE`。真实 KV IO、会话读取、未被探针覆盖的 tokenMeter 等操作保留原错误，避免错误地宣告环境恢复。
- 此类故障进入 deny-only blocked：任务保持 pending；允许提问与说明阻塞；禁止生产操作绕过或重复派发。健康 Python 服务不能单独解除宿主故障，宿主探针恢复后才允许继续。
- 持久化的阻塞指纹保留，重复查询和拒绝重试不制造账本增长。已发布子任务的所有权与清理规则保持原约束。

这属于通用宿主接入与失败恢复修复，没有按鹈鹕任务内容或固定工具步骤写特殊分支。之前计划中的预算 closeout 等其余改进不在本补丁范围。

## 验证

- 对原日常 0.11.0 安装运行新增测试：使用真实 Cordis 插件 fiber、真实 DSH JSON 存储后端和真实 Python HTTP 控制面，复现原错误及调用栈。
- 0.11.1 完整 JavaScript 回归：**606/606**，无失败、跳过或取消；桥接实际日常 Python 安装包。验证前后源码哈希一致。
- 实际安装目录再次运行 3 个真实宿主集成场景：正常存储、缺少可选存储、存储接口故障并恢复。均通过。子任务终态使用受控本地夹具，用于确认真实装配、派发与清理；不是模型质量评测。
- 发布包 37 个文件逐项哈希匹配。临时启动日常 profile 后认证页面 200，插件 enabled/active。
- 外部模型调用为 **0**；未声称真实模型已经完成鹈鹕动画。
- 配置、凭据、HTML 哈希保持不变，原会话记录语义前缀及审计前缀保留；新增宿主元数据不计为模型执行。验证实例已退出，3080 已释放。

## 证据与回滚

证据和备份目录：`.tmp/storage-inject-fix-20260912/`。

- `readonly-runtime-evidence.json`：真实会话计数、原始行号、控制面状态；原生日志仅保存错误消息，完整调用栈来自隔离回归复现，二者未混淆。
- `validation-summary.json`、`js-full-final.log`：完整回归结果。
- `installed-host-dispatch.log`：实际安装包集成回归。
- `installation-receipt.json`、`live-validation.json`：安装文件和运行验证。
- `installed-backup/`、`packed-rollback.json`：原插件、配置及失败会话备份；不应对外分享配置或凭据备份。

如回滚，仅在空闲时恢复配对插件版本；不覆盖后续产生的用户会话、账本或产物。
