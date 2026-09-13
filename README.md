# DPswarm

面向异构多模型 Agent 的协作控制面与研究原型。DPswarm 让不同模型承担实现、测试与审查，由程序管理任务身份、预算、交付证据和验收状态。

**当前 DSH 插件版本：0.15.1。** 插件默认关闭，用户在会话中开启后执行固定团队流程；条件性组队、自动模型路由与普遍成本收益仍需独立验证。

[安装 DSH 插件](dpswarm-dsh-plugin/README.md) · [Python 控制面](dpswarm-plugin/README.md) · [架构](docs/architecture.md) · [开发与验证](docs/development.md) · [研究报告](reports/README.md) · [变更记录](CHANGELOG.md)

## 能做什么

- **异构协作**：按角色配置模型，支持串行、并行实现与分阶段交付；任务启动时冻结路由与约束。
- **程序化验收**：绑定候选文件、合同版本与验证证据；报告声称通过不能单独改变验收状态。
- **预算与恢复**：分别记录实际用量、未知用量与预约额度，保留返工、报告修复及恢复过程。
- **上下文管理**：按角色窗口压力整理历史，保留原文回查与来源边界。

模型负责理解和判断，控制面负责可检查的状态与约束。worker 完成、交付被接受、任务获得独立评分是不同结果。插件中的写范围检查也不是操作系统安全沙箱。

## 快速开始：离线状态演示

需要 Python 3.10+。在仓库根目录运行：

```bash
python -m pip install -e "./dpswarm-plugin[dev]"
python -m dpswarm.cli init --dir .demo
python -m dpswarm.cli status --dir .demo
python -m dpswarm.cli replay --dir .demo
```

这些命令初始化状态、读取投影并重放事件，不调用模型，也不启动团队。任务执行与验收的离线用例见测试套件。

## 在 DSH 中使用

需要已安装的 DSH、Node.js、pnpm，以及 Python 3.10+（含 pip、setuptools 68+）。在仓库根目录运行：

```bash
node dpswarm-dsh-plugin/bin/setup.mjs --check
node dpswarm-dsh-plugin/bin/setup.mjs --install --profile web
```

安装后重启对应 DSH profile，在 **设置 → DPswarm** 中保存角色与 CM 路由，再通过会话罗盘菜单开启 DPSwarm。完整前提、升级边界与运行目录说明见[插件文档](dpswarm-dsh-plugin/README.md)。当前提供源码安装，尚未发布 npm 注册表版本。

## 验证与研究边界

```bash
python -m pytest dpswarm-plugin/tests -q
npm --prefix dpswarm-dsh-plugin run test:unit
```

上面是无凭据的核心检查。完整宿主集成测试需要匹配的 DSH 环境，浏览器检查和真实模型实验另有前提，见[开发说明](docs/development.md)。

历史研究包含固定团队、预算与上下文管理的对照。任务、模型、预算和运行时版本不同的批次不能直接混算；已有结果尚未证明团队结构对所有任务都更优。公开结论与来源见[研究报告](reports/README.md)。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `dpswarm-plugin/` | Python 控制面、事件存储、Provider、CLI 与测试 |
| `dpswarm-dsh-plugin/` | DSH 插件、设置界面与控制服务接入 |
| `modelbench/` | 实验运行器与合同测试 |
| `reports/` | 经过整理的研究报告与汇总数据 |
| `docs/` | 架构、开发和验证说明 |

## 贡献与许可

提交问题或改动前请阅读[贡献指南](CONTRIBUTING.md)。安全问题按[安全报告说明](SECURITY.md)处理。

本项目当前采用[自定义源码可用许可证](LICENSE)，包含针对大型商业实体的商业使用限制。请先阅读许可证；当前许可不应被表述为无使用限制的开源许可。
