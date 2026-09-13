# 0.12.0 Harness 修复：实施与日常安装记录

安装验证时间（UTC）：2026-09-12T02:55:29.209901+00:00。状态：**已安装到日常 web profile，实际安装库与新宿主加载验证通过。**

## 已落地的修复

- 预算预测、收尾和实际请求按相同原生系统消息与工具 schema 计量；在原预算内计入一次工具拒绝后补报告的兼容成本。没有改变角色、模型、effort 或用户限额。
- 补齐真实 installBudget 服务的 issueReportRepair 导出，保留原授权余量、关联关系和结算，不创建隐藏充值。
- 只读 acceptance 接口提供完整版本 schema、当前候选 unknown/blocked 模板及审查身份；Lead 不需要猜字段。错误报告先验证格式和绑定再接管，后续部分提交在错误正文中明示。
- 诊断与报告分页区分 final/progress/truncated/missing；旧文字保留为 unclassified，不改写原账本，也不把进度冒充最终报告。
- 收尾展示执行、当前有效验收、未决 finding/item 与磁盘租约的不同状态；旧验收在需求或证据变化后显示待重新验证。一次事实提示允许诚实报告 partial/blocked，不制造 TEAM_REQUIRED 重试死循环。
- Lead 指导要求核对证据版本、角色权限和方法局限，按实际缺口决定是否重跑。没有写 SVG/Chrome 专项规则或固定验证次数。

## 验证

| 范围 | 结果 |
|---|---|
| 全部 JavaScript 回归 | 633/633 通过，0 失败、0 跳过 |
| 相关 Python 权威验收/审计/会话回归 | 81/81 通过 |
| 实际安装 JS + 实际安装 Python 的真实宿主派发集成 | 3/3 通过 |
| 新进程加载 | 认证页面 HTTP 200，dpswarm-dsh-plugin enabled、fiberPhase=active |
| 安装包核对 | 38 个文件逐字节一致，完整文件集合无残留差异 |
| 原始会话与工作区 | 配置、当前 HTML、原生压缩日志、业务账本和租约均保留 |

插件清单自身没有版本字段。加载结论来自“0.12.0 归档/安装文件逐字节一致 + 新进程 active 插件 + 实际安装库集成验证”的组合，不把清单字段当作版本证明。

真实原生循环测试使用确定性的测试响应；本轮外部模型调用为 **0**，没有把工程回归说成一次真实模型任务完成，也没有替旧动画任务补做验收。预算原记录内存回放中，新策略在 reviewer 第 3 次请求前进入收尾（旧会话为第 5 次前）；这只证明成本窗口变化，不保证模型随后会提供合格报告。多次拒绝或异常长参数仍可能触发原硬额度拒绝。

首轮完整测试启动目录不正确，3 个 harness 找不到路径；另 4 个历史测试漏计原生 system-message 封装。已修正 runner 目录，并以实际提交给 Session 的请求表面核对预估/输出限额/最终准入后完整重跑通过，首轮日志保留。

## 日常安装与进程状态

- JavaScript：`C:/Users/93711/.dsh/profiles/web/node_modules/dpswarm-dsh-plugin`，版本 0.12.0。
- Python：`K:/秋招/项目/DPswarm/.tmp/runtime-0.7.5/python`，37 个源码包文件与安装文件一致，本轮未替换或重启。
- 安装前已确认宿主 33460 自启动以来没有未关闭原生执行，目标四个会话均已结束，25 次 worker 调用全部结算；停止前重新核对进程创建时间、文件集合/哈希、账本、投影和网络连接。
- 只停止经核对的旧 web 宿主；Python 服务 PID 36524 及 start_id 保持不变。
- 临时验证宿主 PID 39204 已确认退出，3080 无监听者。没有留下后台 web 宿主。
- 使用方式：在原终端重新运行 `dsh web`。

## 原会话保全

原 `session-09291d59-ff85-46ed-89ad-c677d7f36e4b` 仍为 2 个 submitted、reviewer terminated、acceptance revision 5、accepted 为空，F1 仍 unknown/open。升级没有把旧任务自动接受或终止，原租约保留供后续恢复。

预检备份 147 个文件，共 3,893,270 字节，另备份当前 `911/pelican-bicycle.html`。包括全部 4 份原生 session.v3.jsonl.zstd。仅排除仍由未停止的 sidecar 持有的两个协调锁文件；未解锁。配置与凭据只核对哈希，没有改写。

## 证据与回退材料

- [原始故障复盘](K:/秋招/项目/DPswarm/reports/2026-09-12/session-09291d59-harness-analysis.md)
- [安装收据](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/installation-receipt.json)
- [真实加载与最终端口验证](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/live-validation.json)
- [预检收据](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/preflight-receipt.json)
- [备份清单](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/backup-manifest.json)
- [JavaScript 回归](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/js-full.log)
- [Python 回归](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/python-full.log)
- [0.11.1 回退包位置](K:/秋招/项目/DPswarm/.tmp/harness-fix-20260912/packed-rollback.json)

备份和回退包只作恢复材料，本轮没有执行回滚。此前复盘文档中的“本轮未安装”描述的是其分析轮次；本记录为随后获得安装指令后的实施结果。
