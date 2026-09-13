# 日常安装版接入记录：0.11.0

状态：已安装并重启，2026-09-12 11:36 Australia/Sydney 完成运行验证。

## 实际接入范围

接入仓库中已经完成的验收修复：可信用户需求与 Lead 计划分离、候选快照、持续缺陷记录、结构化验收、补报告、分阶段交付证据与容量预检。插件由原 0.9.7 加局部修补升级为冻结的 0.11.0，Python 控制服务同步升级。

[abb8d6ec-agent-harness-plan.md](./abb8d6ec-agent-harness-plan.md) 中新提出的预算估计、closeout 请求一致性、角色提示和证据充分性改进仍待实施。本次接入不代表这些新增方案已完成，也不代表历史任务已通过验收。

## 日常运行位置

- 插件：`C:/Users/93711/.dsh/profiles/web/node_modules/dpswarm-dsh-plugin`
- Python 包：`K:/秋招/项目/DPswarm/.tmp/runtime-0.7.5/python`
- 状态目录：`K:/秋招/项目/DPswarm/.tmp/runtime-0.7.5`，沿用原目录，未迁移会话数据。
- 配置：`C:/Users/93711/.dsh/settings.yaml`；配置与凭据哈希保持不变，保留原模型、预算及开关。
- 新宿主 PID：29652，端口 3080；新控制服务 PID：36524，端口 8795。
- 宿主从 `K:/秋招/项目/DPswarm/911` 隐藏启动，未重复启动第二份服务。

## 完成步骤及证据

1. 冻结代码，备份已安装插件、Python 包和用户配置，制作可回滚包。
2. 离线验证冻结候选：JavaScript 全量 589/589；Python 验收及审计相关 95/95，无失败。
3. 用户确认关闭会话后，核对根会话为用户取消、三个子任务均已物理停止，停止原宿主。
4. 原会话 `session-61d96beb-1c95-48a3-bea0-3dd24313d894` 残留两个 submitted 交付。通过旧版正式 review/cleanup 接口将其终止，保留 `failed_takeover` 结果；未授予验收，未删除历史证据。剩余 worker 占用为 0，租约由控制器释放。
5. 停止原 Python 服务，离线安装配对版本。逐文件核对：36 个插件文件、37 个 Python 文件与冻结候选一致。
6. 重启后，真实控制服务返回 `dpswarm-acceptance-v1`；日常宿主公开清单显示 `dpswarm-dsh-plugin` 为 enabled/active。实际提供的客户端资源包含已安装 `client.js` 的完整字节。
7. 认证网页返回 200；3080/8795 监听 PID 与新进程身份一致。保留原审计前缀及产物；根原生日志仅新增宿主元数据，未新增模型请求。

外部模型调用：0。本轮验证覆盖安装、加载、离线协议和生命周期行为，未运行真实模型任务端到端评测。当前公开宿主接口没有无会话的全局工具目录；新增工具注册代码已逐文件核对，运行态插件激活已确认。

## 备份和复核材料

- 工作记录与备份：`K:\秋招\项目\DPswarm\.tmp\daily-install-20260912-1123`
- 备份：`backup/`；包含旧插件、旧 Python 包、配置及升级前会话状态。
- 候选测试：`validation/candidate-validation-summary.json`
- 正式清理：`closed-session-recovery.json`
- 安装与文件校验：`installation-receipt.json`
- 运行验证：`live-validation.json`
- 新包：`D:\codex-home\visualizations\2026\09\12\01a09321-4471-77d2-ae3e-b3d733df67f9\daily-install-artifacts\dpswarm-dsh-plugin-0.11.0-e12f8e215aadc0bdb04d.tgz`
- 旧版已修补内容的回滚包：`D:\codex-home\visualizations\2026\09\12\01a09321-4471-77d2-ae3e-b3d733df67f9\daily-install-artifacts\dpswarm-dsh-plugin-0.9.7-1780da274b4499672d9f.tgz`

如需回滚，应在会话停止后配套恢复插件和 Python 包；不覆盖升级之后产生的会话账本或产物。配置与凭据备份不应对外分享。


## 手动启动方式恢复（11:41）

用户随后手动执行 npx 启动；检查发现安装验证留下的后台宿主 PID 29652 仍占用 3080，最近两次手动命令均退出码 1。核实无活动会话后，已仅停止该后台宿主并确认 3080 释放。0.11.0 安装保留，Python 控制服务 PID 36524 保留；后续由用户在自己的终端执行 `dsh web` 或原 `npx @deepseek-ai/dsh web`。此前运行验证结论仍成立，此刻网页宿主处于等待用户手动启动状态。


## 后续补丁 0.11.1

真实启用新任务暴露了旧测试遗漏的 Cordis 存储访问问题，已安装 0.11.1 并增加真实宿主装配、派发和阻塞恢复回归。606 项测试通过，实际安装与临时运行均验证完成；验证宿主已退出，继续由用户在终端手动启动。详见 [storage-inject-fix.md](./storage-inject-fix.md)。
