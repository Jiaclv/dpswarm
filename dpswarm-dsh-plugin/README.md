# DPswarm for DSH

版本 **0.15.1**。为 DSH 提供用户显式开启的固定团队、上下文管理和可审计验收。插件默认关闭；启用后 Lead 负责分工和控制面操作，worker 负责实现、测试与可选的独立审查。

本插件是研究原型。工程测试检查约束与状态转换，真实模型的质量、成本和协作收益需要另行评估。研究证据见[报告索引](../reports/README.md)。

## 安装

需要已安装的 DSH、Node.js、pnpm，以及 Python 3.10+（含 pip、setuptools 68+）。插件复用宿主的共享模块，不能通过安装另一套 Cordis 依赖替代宿主。当前从完整仓库安装，尚未发布 npm 注册表版本。

在仓库根目录运行：

```bash
node dpswarm-dsh-plugin/bin/setup.mjs --check
node dpswarm-dsh-plugin/bin/setup.mjs --install --profile web
```

Python 不在 PATH 时，在两个命令后添加 `--python "Python 可执行文件的完整路径"`。

安装器会发现宿主、将 Python 控制服务安装到独立目录、生成包含内容哈希的插件包，并通过 DSH 正式插件命令登记。安装过程不调用模型。`--check` 检查模块与控制服务能否导入，不检查 Provider 凭据、额度、浏览器交互或所有宿主接口的兼容性。

安装后重启目标 DSH profile，再进入 **设置 → DPswarm**。宿主版本与测试范围见[开发说明](../docs/development.md)。

## 配置与开启

在“模型分工”中从宿主模型目录保存准确的 Provider 与模型；凭据仍由 DSH 管理。配置保存不会启动任务。一次协作启动后继续使用已经冻结的路由与预算，新设置用于后续协作。

| 角色 | 默认行为 |
|---|---|
| Lead | 当前会话的主模型 |
| 实现者 | 跟随本次 Lead 实际使用的 Provider、模型和推理强度，可显式改选 |
| 测试者 | 预填 `glm-5.3-flash`，首次需选择并保存完整路由 |
| Reviewer | 默认由 Lead 承担；可选择独立模型 |
| CM | 默认 `deepseek-v4-flash`、推理关闭；需保存可用路由 |

预填模型名不代表账户具备相应路由或额度。宿主元数据缺失时，能力与价格保持未知。

在已有会话的罗盘菜单中打开“开启 DPSwarm”。界面的主开关同时启用团队与 CM；后端分别记录两项配置。开启要求相关路由均已配置。协作模式可选串行、并行实现或分阶段协调，默认串行；开启后的任务必须经过团队派发。

## 预算、交付与恢复

每个 worker 的累计 token 与调用上限由用户设置，分别记账，不是团队共享预算，也不限制 Lead 主对话。首次派发可选择手动额度或不限制；返工单独配置固定护栏或不限制。历史 Auto 配置仅作兼容处理，当前工具不能让 Lead 自行发放预算。

交付遵循冻结的验收合同。支持 v2 的匹配 JS/Python 组合可在新任务中协商 v2；已有合同继续使用原版本，缺少版本的历史合同按 v1 解释。候选、需求、计划与证据的 revision 必须匹配，模型不能仅靠报告里的 pass 绕过验收。

| 情况 | 可用入口 |
|---|---|
| 查看状态、合法恢复事实与报告 | `dpswarm_status`、`dpswarm_report` |
| 候选封版中断，尚有原计划验证未派发 | `dpswarm_resume` |
| 需要修改交付 | `dpswarm_rework` |
| 补发符合条件、尚未发行的验证角色 | `dpswarm_verify_rework` |
| 修复报告格式或报告内容 | `dpswarm_repair_report`，按当前控制面诊断选择分支 |
| 读取原始证据 | `dpswarm_read_evidence` |

恢复入口会检查原任务、身份、额度和证据状态。恢复不等于自动接受，不会无条件重发额度。worker 的报告属于待核对证据；未执行的检查不能当作执行成功。

关闭开关或取消任务会请求取消在途 worker，已有交付仍需结案。停止 worker 不会撤销其文件修改。并行任务使用共享工作区与写范围认领；这不是文件系统、进程或网络沙箱，实际执行权限由宿主和运行环境决定。

## 运行目录与升级

默认状态目录为 Windows 的 `%LOCALAPPDATA%/dpswarm/dsh`，其他平台使用 `$XDG_STATE_HOME/dpswarm/dsh` 或 `~/.local/state/dpswarm/dsh`。控制服务默认使用 `http://127.0.0.1:8791`，仅支持本地 HTTP；写操作与敏感读取需要本地认证。

| 设置 | 作用 |
|---|---|
| `DPSWARM_PYTHON` / `--python` | 选择 Python 解释器 |
| `DPSWARM_STATE_DIR` / `--state-dir` | 选择状态目录，不迁移已有任务 |
| `DSH_HOST_ROOT` | 指定宿主的 `node_modules/@deepseek-ai` 目录 |
| `--dsh-home`、`--profile` | 选择安装目标 |

设置页中显式保存的运行路径优先于安装记录。自定义安装后应核对实际服务地址、Python 包目录与状态目录；改变状态目录不会自动改变服务端口，也不会自动覆盖已有显式配置。

升级需要匹配的 JS 与 Python。先完成运行中的任务与待验收交付，备份状态与设置，再安装并重启对应宿主和空闲控制服务。安装器检测到未处理的工作区 lease 会停止升级；不要删除 lease 来绕过检查。隔离安装应使用独立的 DSH home、状态目录和端口。

## 开发与验证

从仓库根目录运行无宿主依赖的单元测试：

```bash
npm --prefix dpswarm-dsh-plugin run test:unit
```

完整插件套件另需可解析的 DSH 宿主和 Python；参见[开发说明](../docs/development.md)。浏览器与原生宿主探针不属于上述单元测试，也不能仅凭单元测试宣称已部署或模型效果更优。

[架构](../docs/architecture.md) · [变更记录](../CHANGELOG.md) · [贡献指南](../CONTRIBUTING.md) · [许可证](../LICENSE)
