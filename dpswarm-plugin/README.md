# DPswarm Python 控制面

Python 包提供事件存储、任务生命周期、预算与身份绑定、候选验收、Provider 抽象及本地控制服务。它由 DSH 插件和实验运行器调用；服务启动本身不会启动多 Agent 任务。

当前 DSH 产品入口由用户显式开启固定团队，详见[插件安装说明](../dpswarm-dsh-plugin/README.md)。自主组队与动态路由属于研究方向，不能从控制接口的存在推导其收益。

## 快速开始

需要 Python 3.10+。在仓库根目录运行：

```bash
python -m pip install -e "./dpswarm-plugin[dev]"
python -m dpswarm.cli init --dir .demo
python -m dpswarm.cli status --dir .demo
python -m dpswarm.cli replay --dir .demo
```

以上命令只验证初始化、状态读取与事件回放，不调用模型。测试套件通过确定性 MockProvider 检查执行路径。可选 LangGraph 编排实现需要额外安装 `"./dpswarm-plugin[dev,lg]"`；核心控制面不依赖它。

## 本地面板

使用独立演示目录和端口启动：

```bash
python -m dpswarm.server --port 8790 --workspace .demo-panel
```

在浏览器打开 `http://127.0.0.1:8790`。服务提供状态、事件、任务与人工指令接口；敏感操作需要本地认证。DSH 插件使用支持会话隔离的 `dpswarm.session_server`，其安装与设置独立于此演示面板，不依赖修改宿主网页文件。

## 接入真实模型

OpenAI 兼容 Provider 从本地环境读取 `DPSWARM_BASE_URL` 与 `DPSWARM_API_KEY`。按服务商实际支持的接口和模型配置后，可运行：

```bash
python -m dpswarm.cli run --task "任务描述" --model PROVIDER/MODEL --dir .model-run
```

真实调用会使用服务商额度。兼容接口、账户访问权限与模型可用性需要分别核实；不要把密钥、完整请求或含凭据的配置写入仓库。常规测试使用本地 fixture。

## 主要模块

| 模块 | 职责 |
|---|---|
| `control.py`、`state.py`、`invariants.py` | 状态转换、回放与不变量检查 |
| `events.py` | 事件持久化与写入边界 |
| `acceptance.py` | 冻结合同、报告解析、候选和证据约束 |
| `server.py`、`session_server.py` | 本地 HTTP 接口与会话隔离 |
| `context/` | 上下文装配与记忆生命周期 |
| `providers/` | MockProvider 与兼容 Provider 传输 |
| `orchestrator.py`、`team_runtime.py` | 编排与工具协议 |

事件记录与回放是状态依据。候选文件、提交、worker 执行结束和最终验收分别记录；未知调用用量保持未知。v1/v2 合同在绑定时冻结，历史任务不因服务升级而自动更换合同。

实现边界与 DSH 接线见[架构说明](../docs/architecture.md)。离线检查通过不代表所有宿主、Provider 或长任务都已经验证。

## 测试

安装开发依赖后，从仓库根目录运行：

```bash
python -m pytest dpswarm-plugin/tests -q
```

Windows 可设置 `PYTHONUTF8=1` 统一子进程文本编码。未安装 LangGraph 时其可选测试跳过；外部记忆服务测试只有显式配置端点时才运行。CI 不提供模型或外部服务凭据。

完整测试分层与复现范围见[开发说明](../docs/development.md)，历史研究见[报告索引](../reports/README.md)。

[贡献指南](../CONTRIBUTING.md) · [安全报告](../SECURITY.md) · [许可证](../LICENSE)
