# 轨迹法医学：v2–v14 实验日志五路深挖综合（2026-09-04）

五路 explore 子代理（glm-5.3）并行解剖约 40 个 run 的原始工件（events.jsonl / calls.jsonl / calls/{hash}/prompt.json / lead+worker history.json / cm/*.before.json / delta.patch / budget.json），全部结论有事件级证据。本文件是五路报告（S1 sphinx solo 全谱 / S2 team 跨设置 / S3 CM 内部 / S4 易题+worker 逆转 / S5 空补丁法医）的综合。

## 0. 被本轮换代/修正的旧结论

| 旧记录 | 新证据结论 |
|---|---|
| v13 非单调谱（池6→2/2、12→0/3、24→0/2）"方向性反常，无机制解释" | **有机制解释**：压缩是否侵入实现期是唯一变量（见 §1） |
| "solo 死于步数×单步成本的累计撞预算；压缩做好了本职但没救到" | 更精确：**高配额下压缩是主动致死因素**——压缩↔重读极限环把 Lead 锁死在定位期 |
| "池 6 的 2/2 是死前交付的运气" | 是因果链不是纯运气：池早耗尽→压缩停止→完整上下文滞留→转入实现 |
| v8/v9 "26 个 worker 零 completed" | worker 级实为 **7/26 completed**；零的是 run 级双完成（13 团队 run 无一 2/2，team_execution_valid 全 false） |
| worker 逆转归因于"臂级限额+600k" | v12 的 28 调用/600k/个人8上限与 v8 **完全相同**；唯一结构性变化是 **CM 挪出共享调用池**（v2 快照即有 cm_call_allowance） |
| "无法区分没想出/写出未交付/交付未合入" | 已判别：8 个失败 run = **6 没写出 / 2 写出未交付 / 0 交付未合入** |

## 1. 头号发现：压缩循环（compression↔re-read 极限环）

**赢家与输家的分水岭（S1，token 流向分段）**：通过的 solo run（v13 cm6×2、v8）把 27–31% token 花在实现期；五个空补丁 run（v10×3、cm24×2）**实现期 token 为 0，100% 烧在定位期**。

机制链（S1+S3 双向验证）：
1. CM 触发线约 12–17k est；压缩后存活历史恒为 6 条（system+摘要+最近一轮 tool 结果原文）；
2. 摘要保留行号级事实但**丢代码正文**，且是"事实清单而非行动状态"（不含"已读过什么、下一步做什么"），摘要自己写着"检查 filter_members 约 536–700 区域"——指示重读；
3. 于是同一 sed 窗口被反复重读：cm24 里 `__init__.py` 的 4 个窗口在 **9 个不同调用**中整组重读；`git log --grep=8035` 跑 3 次、每次紧跟一次压缩；
4. 每次重读 ~18k 字符 → 2–3 步回到触发线 → 再压缩；CM 调用自身每次再耗 7–10k → 极限环；
5. 死亡时全部死在读文件（最后 1–3 事件均为 sed/grep/git log），**死亡时 CM 池还剩 7–8 次**——不是额度不够，是节奏本身致死。

对照组（cm6）：6 次压缩在前 2 分 11 秒用完 → 后续 8–9 步零压缩、历史无界增长 20.6k→52.5k → 调查结论全部滞留上下文 → 第 14 步转入实现、死前 2 步完成编辑 → resolved。**压缩停，重读停，收敛发生**。

v8✅→v10❌×3 翻转的隐藏原因（S1）：v8 的 CM **共享** 28 调用池且有 `cm_skipped("low remaining call budget")` 保护——实现期天然不压缩；v10 起 CM 改独立池（v10 单 run 32 票>28 可证），隐性护盾被移除，压缩得以贯穿到死亡。

**可操作推论**：无压缩窗口 ≥4–5 步是"调查→编辑"转换的必要条件（cm6 用 5 步、rep2 用 4 步）；池大小（压缩节奏）是比 token 预算更强的杠杆——900k/40 救不了 cm24 形态（池没用完，更多预算=更多压缩轮次），只能救 v3/v4 那种"实现中途阵亡"形态。

## 2. CM 的工程缺陷：66% 输出被截断（S3）

- 56/85 次 CM 调用 `stop_reason=length`（`cm_max_tokens=2048`，runner.py:45）；被截掉的恰是摘要尾部的"未决问题/下一动作"——对推进最关键的部分；有的摘要在句子中间截断。
- 拒绝语义核实：纯池机制（`No CM pool calls remain`），与 token 无关；`budget_exhausted` 实为"下一次预留被拒"（cm6 死时 remaining_tokens=40,402、remaining_calls=11）。
- 所有 run 无论池大小总消耗收敛 529–575k：CM 只重排步数×单步成本，不改总量。
- 团队臂池用不满的三重原因：worker 分流探索（Lead 历史全程仅 17→31k）、`cm_skipped` 保护只在团队臂出现、CM 兼任 scout 蒸馏/bootstrap 装配。

## 3. 团队协议的内部结构（S2，13 个 terra→glm run 12/13 通过）

- **Lead 整合主模式 = 逐字采纳 W1 生产 delta**：6 个 run 的 model.patch 与 worker-1/delta.patch sha256 字节级相同；"Lead 完全重写"从未发生，只存在 W1 空交付时的 Lead 兜底（3 run 全 resolved）；唯一"采纳后扩展"= v13-cm24。
- **W2 测试 delta 经常被丢弃也照样通过**（官方评分用自带用例）——当前评分制度下测试 worker 的边际价值存疑。
- **装配包唯一被证实的收益 = 环境先验**：scout 踩到 `rg: command not found` 并蒸馏进包 → ON 臂 W1 首轮 100% 用 grep（4/4），OFF 臂 100% 先踩 rg 失败（5/5）；但 OFF 臂首轮同样一步定位关键词（issue 文本可 grep），scout 的情报内容近空包。装配"改善起点"是真的，但幅度只值 1–2 轮工具试错。
- 制度性发现：`review_worker(adopt)` 对未完成 worker 返回 `WORKER_NOT_COMPLETED`（delta 结构性不可采纳）+ `finish` 被 `WORKERS_UNSETTLED` 拦截——LEAD_RESERVE 截断的 W2 工作全部作废的制度原因。
- v11-he1 唯一失败：三 agent 并发 + CM 池 3.5 分钟早夭 + terra 单调用输入 34k（GLM 的 3–4 倍）→ 池死后 30 万 token 不可控消耗 → Lead 只分到 7 次调用 → 双 worker 在飞被 cleanup 丢弃。fixed_glm 2/2 的内部原因：GLM 单调用输入只有 terra 的 1/3–1/4，根本够不到 token 天花板。

## 4. worker 完成率逆转的真相（S4）

- 纠正：v8/v9 worker 级 7/26 completed（W1 常有收尾 finish）；零的是 run 级双完成。死因分布：**14 LEAD_RESERVE 池尽** / 2 local_call_limit / 2 blocked 空交付 / 1 transport_error；13/13 团队 run 的工作+CM 调用恰好=28（CM 共享主池，每 run 吃掉 5–8 票）。
- 逆转关键 = **CM 挪出工作池**（v8→v10 池尽死亡 13/26→0/12 已先验证），不是臂级限额（v12 用默认值）。**池独立是必要条件，题目难度是充分条件**（v10 sphinx 池已独立仍有 4 个 blocked worker；v12 astropy W1 只需 4–6 步）。
- astropy 易题画像：5 步定位溯源 + 1 步复现（issue 自带脚本，但 solo Lead 第 9 步才跑，最早跑的是 W2）+ 1 步单行修复 + 1 步验证。团队 +20% 溢价的构成：W1 并行实现 + W2 更宽测试 + Lead 复验，代价是 Lead 与 W1 的探索部分重复（OFF 臂还出现 W2 文本交付→Lead 重写测试的纯重复）。

## 5. 空补丁法医学（S5，8 个失败 run 判定）

| 判定 | 数量 | run |
|---|---|---|
| 没写出（全程零编辑，命令正则扫描 0 命中） | 6 | v14 xarray/seaborn/sklearn、v13 cm24×2、v11 fixed_deepseek |
| 写出未交付（delta 非空、未 finish 即被 cleanup_discarded） | 2 | v11 fixed_sol（W1 4,627B 修复已写入+在调测试）、v11 hetero（W1 2,597B 修复自测 100% 通过 + W2 3,878B 测试，双双物理丢弃） |
| 交付未合入 | 0 | 无一例；patch_frozen 9/9 正常发生、sha 链一致，排除管道 bug |

- 两个"写出未交付"都**只差一次 finish 调用**；fixed_sol 同批 rep2 的 W1 在死线前 19 秒 finish 就全合入通过了。
- LEAD_RESERVE 方向反了：把最后调用保留给"Lead 整合"，但实际死因是 worker 交付缺一口气、Lead 根本无预算整合（hetero Lead 全程 10 次 collect、0 次 review）；deepseek 例中 worker 的收尾调用被 `closing_call_not_admitted [LEAD_RESERVE]` 直接拒绝。
- v13 cm24 证明"根因已定位 ≠ 能写出"：CM 摘要 100% 正确陈述根因（bool_option 不接受名单），Lead 20+ 次调用零编辑——**探索→编码转换没有 deadline 机制**。

## 6. rev11+ 可操作候选（按证据支撑排序）

1. **压缩宵禁 / 实现期保护**：Lead 开始编辑后禁止压缩（或保留最近 N 步不压）——v8 的 `cm_skipped` 隐性护盾的显式化；v13 池 6/12/24 对照已给出同题证据。
2. **CM 池调小而非调大**（或大幅延后触发线）：池 6 的"早死早解脱"机制上优于池 24；"CM 池 12 保留默认"的结论需要在这层证据下重审。
3. **`cm_max_tokens` 2048→4096**：消灭 66% 截断（丢"下一动作"）——一行常量改动。
4. **cleanup 保底导出**：`worker_cleanup_discarded` 时把非空 delta.patch 自动存为"未采纳候选"并对 Lead/审计可见（v11-he1 双份有效 delta 被物理丢弃仍判空补丁）。
5. **失败阶段机器可读标记**（零模型成本）：在 cleanup/freeze 事件加 `worker_delta_bytes`、`finish_completed`、`death_phase`（no_edit | edited_no_delivery | delivered_not_adopted | adopted）；tool 层加 `worktree_edited` 首编辑时间戳，运行中即可预警"探索阶段死亡"。
6. **LEAD_RESERVE 规则重审**：允许 worker 的 finish/收尾调用优先于 Lead reserve（deepseek、fixed_sol 两例的直接死因）。
7. **探索→编码转换提示**：预算过半仍零编辑时向 Lead 注入推进提示（6/8 失败是"从没开始写"）。

## 证据索引

五路报告的完整事件级索引见各分报告（会话存档）；核心工件：`pilot_v{3,4,8,10,11,12,13,14}/results/*/{budget.json,result.json,events.jsonl,calls.jsonl,lead/history.json,worker-*/delta.patch,cm/}`；机制源码点：`runner.py:45`（cm_max_tokens）、`:180`（memory 基座）、`:307`（bootstrap 包门槛）、`:321-322`（LEAD_RESERVE）、`:453-492`（装配器）、`:884-899`（scout→start_workers 串行化）。
