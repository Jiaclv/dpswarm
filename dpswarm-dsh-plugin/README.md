# dpswarm-dsh-plugin

DPSwarm 的 dsh（deepseek-harness）运行时插件——给当前主 agent 提供按需多模型协作能力。

默认由当前主 agent 单独处理任务；理解任务后或执行途中，它根据可分性、上下文成本和资源事实自主调用 `dpswarm_*` 工具，兼任 Lead，完成分工、验收并回到原任务。用户不必手动开启团队模式。插件安装、sidecar 自动拉起和面板连接灯只表示能力就绪，不会因此启动 worker；设置与面板用于配置、观测及人工干预。当前实现用系统提示和工具说明承载调用指引，尚没有独立 `SKILL.md` 包。

## 架构（机制文档 §9.1：控制面独立，harness 只作执行底座）

```
dsh Web UI ──(settings 卡片/输入框按钮)──┐
dsh 主 agent (Lead) ── dpswarm_* 工具 ──┼──► DPSwarm sidecar（Python 控制面）
dsh subagent (worker) ◄── ctx.subagents ┘      事件溯源 · 硬准入 · 验收闭环
                                               http://127.0.0.1:8791
```

- **Host 半**（`lib/index.js`，零依赖 ESM）：4 个工具 + settings namespace +
  sidecar 连接/自动拉起。所有 `@deepseek-ai/*` 模块经 `DSH_HOST_ROOT` 从 dsh
  安装内取**同一实例**——`file:` 安装若配上依赖副本，Symbol/模块单例分叉会
  破坏宿主工具调度（实测：`registry[TOOL_RUNTIME_SCHEDULER] undefined`）。
- **Client 半**（`lib/client.js`，手写 factory bundle 免构建）：设置 → 插件 →
  插件配置 的 DPSwarm 卡片（`--dsw-*` 主题 token 对齐官方 PluginCard）+
  composer 工具行罗盘按钮（`conversation.input.left` 座位，"Workspace Write"
  与模型选择器之间）带三态状态灯：黄=启动中（dsh 装载自动拉起 sidecar 的
  冷启动窗口，连续 3 次探测失败后转红）/ 绿=已连接 / 红=离线（曾连上后掉线
  即刻翻红，探测恢复自动回绿）。

## 工具面

| 工具 | 机制 | 说明 |
|---|---|---|
| `dpswarm_models` | 机制一（§3 事实注入） | 目录 + AA 分维 + 容量，委派前先调 |
| `dpswarm_delegate` | 机制五（§7 拓扑） | derive/fission/split → sidecar 硬准入 → dsh subagent 执行 → submit |
| `dpswarm_review` | 机制二（§4 验收制） | accept（证据落盘+原子发布）/ reject（归因四分支，预算 ≤2 重试，矛盾上交） |
| `dpswarm_status` | 观测 | 投影快照 + 全账汇总 |

## 安装与使用

```bash
# 1) 启动控制面 sidecar（务必在 dpswarm-plugin 目录下用默认 workspace 起：
#    写接口 token 写在 .dpswarm-panel/.dpswarm-token，dsh 插件按约定路径读取）
cd K:\秋招\项目\DPswarm\dpswarm-plugin
python -m dpswarm.server --port 8791

# 2) 安装进 dsh profile（首次需 pnpm：npm i -g pnpm）
dsh plugin --profile web add "file:K:\秋招\项目\DPswarm\dpswarm-dsh-plugin"

# 3) 重启 dsh web（profile 装载在启动时）
dsh web
#   → 输入框工具行出现罗盘按钮（角落状态灯黄→绿）；设置→插件→插件配置 出现 DPSwarm 卡片
#   → 正常向主 agent 提交任务；它默认单干，自主判断是否需要协作。
#     需要时由 agent 调用 dpswarm_models → dpswarm_delegate，
#     收到交付后 dpswarm_review 裁决，随后继续原任务。
```

## 安全边界（P0 修复后）

- sidecar 写接口与敏感读（events/observation）要求 `Authorization: Bearer
  <token>`；token 在 sidecar 启动时生成于 `<workspace>/.dpswarm-token`，
  面板页面由 sidecar serve 时注入，dsh Host 半按约定路径读取
- Origin 门只认 loopback（127.0.0.1/localhost/[::1] 任意端口）：dph Web UI
  页面的跨源状态灯轮询（GET /api/status 只读快照）可达，恶意网页 Origin
  一律 403 且无 CORS 头可读——跨站接管/读取两条路都关死
- 请求体上限 5MB；面板渲染全部 DOM API/textContent（无 innerHTML，存储型
  XSS 面清零）；Spec 发布有合法域校验（含 deadline > 单节点 wall-clock 交叉约束）

## 已知实现层决策

- **执行合同（2026-09-05）**：worker 用 `Promise.allSettled` 并行；`onPublished`
  将公开 `SubagentRun.id` 绑定为真实执行 session，后续 submit 使用这一身份，不能用
  原准入 session 冒充执行。交付须等待 `run.result` 和 `dispose()` 成功，再由 Lead 验收。
- **根归属和模型**：仅支持顶层 DSH Agent。读取公开 `session.header.delegationDepth`
  与 `options.subagentDepth` 拒绝嵌套调用；要求公开 `options.provider/model` 明确，
  经 CP 可信模型目录校验后绑定根 requested 路由及其等级。不同根会话、模型改变或
  未知模型均明确拒绝，需要独立 sidecar workspace。requested 模型不是上游响应模型的实测证明。
- **失败收束**：已发布子 run 的失败、取消、绑定失败和 dispose 失败都会向带 fence 的
  `/api/execution/fail` 报告，原错误和清理错误分别保留，并终止对应 CP item、释放逻辑资源。
  若物理清理尚未确认，根准入在同事务中封存，重启后仍阻断新执行；不可通过换 item 绕过。
  未发布的启动失败不误封根。无自动解除这一封存的接口：先确认旧进程停止，再用新的独立 workspace。
  sidecar 失联导致结算失败时，`control_settlement.ok=false` 明确保留，不声称已经释放资源。
- **用量证据**：当前已安装公共 `SubagentResult` 仅声明 output/structured/diagnostic/stopReason，
  不提供 token usage。根会话、worker 和协助者未知用量显式记录为 null；报告分别给出完整总额、
  已知小计和未知事件数，不把 null 合计成零或免费。没有采集完整真实宿主调用账。
- **设置和端口**：Host 使用 `installSettingsSection` 提供的当前 source；每次工具调用固定
  一个设置快照，运行中修改设置不会将交付投向另一 sidecar。浏览器读取公共 `settingsScope`
  的同一 URL；无可用设置时不猜地址。默认端口统一 8791，仅支持本地 HTTP origin。
  Windows 自动启动使用独立 argv、`shell:false`、`windowsHide:true`。
- **本地验证范围**：公开模块和类型从已安装 DSH 读取。mock 生命周期合同、真实 loopback
  Python sidecar + 假 SubagentRun 已验证委派、真实 handle 绑定、提交、验收、重启恢复、
  根冲突和清理失败封存。未启动真实模型，未完成实际 DSH Web 会话/浏览器点击验证。
- **AA 评分数据源**（§8 V1 选型依据）：`dpswarm-plugin/dpswarm/data/aa_scores.json`
  是外部榜单快照（2026-09-01，AA Intelligence Index v4.1.1 + Coding Index，
  37 条，来源 AA 官网 FAQ + BenchLM 镜像）。真实模型委派注册时按模型名匹配
  快照 → 分维/级别（S/A/B/C/D 自定分档 ≥60/50/40/30，映射在快照 level_bands）
  均取外部数据；未命中时仅接受服务端预先登记的目录事实，否则拒绝未知模型。
  Agent 请求不能自报级别或伪装 human 来源；演示目录标 `demo`。
  缺维（reasoning/math）不硬造，选型走 overall 兜底。更新 = 换快照文件。
- npm 版 dsh 与仓库源码的 API 差异由运行时兼容层吸收：settings 用
  `installSettingsSection`（npm 独立函数）优先、`settings.installSection`
  （源码服务方法）兜底；`output.render` 必填且返回 content blocks。
- Host 路径默认 `C:/Users/93711/AppData/Roaming/npm/node_modules/...`，
  可用 `DSH_HOST_ROOT` 覆盖。
- 诊断开关：`DPSWARM_SKIP_SETTINGS=1` / `DPSWARM_SKIP_TOOLS=1` 分别跳过
  两半（二分排查用）。
