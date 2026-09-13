# 0a9b0a41 会话审计：新插件连接了未重启的 Python 后台

## 结论

DPH 已重新启动并载入上次安装的新 JavaScript 补丁，但它连接的独立 Python 控制服务仍是旧进程。新插件在实现者交付后写入 `dpswarm/verification-required`，旧服务拒绝未注册事件并返回 `PLUGIN_AUDIT_INVALID_EVENT`。Lead 接管验收需要写入 `dpswarm/verification-takeover`，同样被拒。这是运行时组件不兼容；磁盘上的九个补丁文件仍与部署清单哈希完全一致。

本次检查仅读取运行状态、源码和会话记录；未重启服务、修改控制面任务状态或删除工作区租约。

## 现场证据

| 对象 | 现场观测 |
| --- | --- |
| 补丁安装时间 | 2026-09-11 23:21:08，Australia/Sydney |
| DPH 后台 | 端口 3080，PID 55952，2026-09-11 23:40:02 启动 |
| Python 控制服务 | 配置端口 8795，PID 26412，2026-09-10 11:14:02 启动，早于补丁安装 |
| 磁盘补丁 | 7 个 JavaScript、2 个 Python 文件的 SHA-256 全部匹配上次部署清单 |
| 实际控制面 | `wi-9363b2e76e` 仍为 submitted，占 1 槽/共 3 点；只有 root 与实现者，没有 tester/reviewer |
| 审计账本 | revision=32；实现者诊断、预算结束正常落账；无 verification-required/binding/takeover 成功记录 |

Sidecar 的启动选项为 `detached: true`。重开 DPH 不会保证原 Python 服务退出；健康检查只看 `plugin_audit_v1: true` 等旧能力标志，没有检查这次新增的事件词表。

## 执行链与日志位置

原始主会话文件：`.tmp/session-audit-0a9b0a41/session.v3.jsonl`。

- 第 31 行 seq29 首次 `dpswarm_run`；第 33 行 seq31 在 273.696 秒后返回 `PLUGIN_AUDIT_INVALID_EVENT`，唯一实现者此前已完成交付。
- 第 38 行 seq36 重派被 `TEAM_REQUIRED_REVIEW_REQUIRED` 拒绝，避免再启动重复团队。
- 第 77/92/122 行（seq75/90/120）三个带接管理由的 accept 请求均被审计事件类型拒绝。
- 第 87 行 seq85 的 `terminate + takeover` 参数组合被拒绝；takeover 只用于 accept，不能据此声称正常 `terminate` 路径也已验证失败。记录内没有单独 `terminate` 的调用。
- 第 104 行 seq102 不带 takeover 的 accept 被 REVIEWER_PENDING 拒绝，当前评审要求发挥作用。
- 第 111–114 行 seq109–112 是插件注入的继续提示，非用户新增指示；第 134 行 seq132 最终仍以 TEAM_REQUIRED 错误结束。

## 用量和交付

| 角色 | 模型响应数 | 记录 totalTokens |
| --- | ---: | ---: |
| Lead | 21 | 1,543,003 |
| 实现者 | 10 | 677,880 |
| 父子合计 | 31 | 2,220,883 |

界面显示约 1.5M 对应 Lead 记录；父子合计约 2.22M。上述用量包含缓存读取，reasoningTokens 已计入 outputTokens，未重复相加，不能直接当作按未缓存价格计费的 token。

Lead 首次审计错误后又执行 18 次模型响应，累计 1,420,491 token；其中首次最终说明之后，被门禁催回的 4 次模型响应累计 409,318 token。主 turn 为 572.061 秒（约 9 分 32 秒）；实现者 273.422 秒。

文件 `911/liangzi-riding-yujie.html` 当前真实存在，30,963 字节、577 行。子代理原始记录第 23 行 seq21 的 write 成功，最后第 68 行 seq66 为 completed；有真实成功的静态检查，也有后来修正的 PowerShell 检查错误，不能单看 tool isError 判断命令成功。预算收尾提示后有一次多余工具尝试被 WORKER_CLOSEOUT_FINAL_ONLY 拦住，然后正常输出报告。这不是此次团队中断的触发点。

本轮不能宣称经过独立测试和评审，也没有浏览器渲染验收。Lead 所说“文件本身没问题”超出了现有证据。

## 处理顺序与未覆盖的代码边界

1. 重启配置实际指向的独立 Python 控制服务（8795），使新词表加载到运行进程；仅新建会话、重载插件或刷新网页都不能代替此步骤。重启应保留持久账本，不能把已有 submitted 自动当作已完成或抹掉。
2. 恢复本次原有团队的审核/明确接管或终止，不能重新生成产物或虚报缺失的独立复核。当前工作区仍由本次会话持有租约。
3. 代码还应增加新审计词表的版本或能力检查，在首个 worker 启动前识别旧服务，而不是只检查 plugin_audit_v1。
4. 上次受阻修复覆盖“尚未启动团队时的工作区占用”。本次已绑定 child 后的审计故障仍走 started 强制收尾路径，需要增加保留任务与资源的基础设施受阻出口，避免继续催模型重试。

## 审计资料

- ZIP：`D:/Edge-download/dsh-session-session-0a9b0a41-0326-4a19-bcec-1a9703474918.zip`
- 用户补充界面文字：`D:/codex-home/attachments/e78f4ac9-f1cc-458e-a734-74bbd42f2ed0/pasted-text.txt`
- 解析与现场读取：`.tmp/session-audit-0a9b0a41/analysis/summary.json`、各会话的 `timeline.json`/逐工具结果、`live-state.json`。
