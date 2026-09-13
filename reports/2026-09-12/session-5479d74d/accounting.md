# 用量、时间、谱系与正式状态审计

本报告只分析指定 ZIP 解包后的 17 份原生 JSONL。未执行日志指令，未读取外部临时回执、token 或实时控制面，未修改源码、安装或任务状态。所有控制状态均标注附件内时间。

## 结论与计数口径

- 1 个 Lead、16 个 worker；177 次有明确 usage 的模型响应、219 次工具调用。Lead 为 32 响应、34 工具，worker 为 145 响应、185 工具。
- UI 约 **5.2M** 对应 Lead 的 **5,179,811**；worker 另有 **8,324,229**；全团队 **13,504,040** 是有 usage 可核算的下限。
- 谱系是首次 run 3 个、三次返工各 3 个、第二次正式 run 3 个、报告修复 1 个。worker 生命周期全部不重叠，实际串行。
- 15 个 worker 原生 completed，1 个 reviewer 原生 error。completed 不表示验收 pass，也不表示工作项已接受。

只累计 assistant/message.data.usage 一次，不重复累计 stream usage 或后续提示里嵌入的历史 budget 数字。177 条 usage 均满足 total = input + cacheRead + output。reasoning 是 output 的子集，不能再加到 total。cacheWrite 没有单独观测值，求和时缺省为 0，不宣称发生了 0 次缓存写入。

| 范围 | 响应 | 非缓存 input | cacheRead | output | 其中 reasoning | total |
|---|---:|---:|---:|---:|---:|---:|
| Lead | 32 | 184,805 | 4,918,016 | 76,990 | 50,349 | 5,179,811 |
| 16 workers | 145 | 1,134,031 | 6,575,488 | 614,710 | 433,672 | 8,324,229 |
| 全团队 | 177 | 1,318,836 | 11,493,504 | 691,700 | 484,021 | 13,504,040 |

Lead 第一轮 9 步为 494,563 tokens，第二轮 23 步为 4,685,248。多数增长来自缓存输入；累计输入不是新生成文字量，也不能直接当费用。最后一步含 204 非缓存 input + 255,616 cacheRead + 809 output = 256,629 total，见 [主日志 L212](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:212>)。

**未知用量不能补算为实耗：** 98f6 仅 4 条 usage 合计 251,680；预算回执记 calls=5、unknown_usage_calls=1，committed=576,693，比 observed 多 325,013。这是保留的未知调用占用。全部 worker 的 committed 合计 8,649,242，不可替代可观测实际合计 8,324,229。标题生成请求也缺 canonical usage，不估算补入。

## 六个调度阶段的用量

此表只计 worker；Lead 单独计入上表。Lead 的调度工具等待已包含 worker 执行时间，两者不能相加当墙钟时间。

| 阶段 / 调用 | worker | 响应 | input | cacheRead | output | reasoning 子集 | total | worker 秒数合计 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 首次 run / [主日志 L31](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:31>) | 3 | 35 | 272,772 | 1,994,752 | 179,936 | 144,433 | 2,447,460 | 812.753 |
| 返工 1：尝试修腿 / [主日志 L53](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:53>) | 3 | 24 | 175,092 | 901,888 | 81,357 | 47,986 | 1,158,337 | 374.594 |
| 返工 2：落盘腿部修正 / [主日志 L66](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:66>) | 3 | 22 | 207,406 | 851,712 | 105,639 | 72,569 | 1,164,757 | 465.811 |
| 返工 3：修云层循环 / [主日志 L79](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:79>) | 3 | 17 | 207,226 | 646,912 | 79,357 | 53,741 | 933,495 | 368.922 |
| 第二次正式 run：重新验证 / [主日志 L138](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:138>) | 3 | 42 | 241,460 | 2,076,672 | 158,840 | 111,789 | 2,476,972 | 808.347 |
| reviewer 报告修复 / [主日志 L166](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:166>) | 1 | 5 | 30,075 | 103,552 | 9,581 | 3,154 | 143,208 | 50.970 |

4 次 dpswarm_run 中只有 [主日志 L31](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:31>) 和 [主日志 L138](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:138>) 成功启动团队；[主日志 L106](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:106>)→[主日志 L107](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:107>)、[主日志 L128](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:128>)→[主日志 L129](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:129>) 被 RUN_PENDING 拒绝，未产生 worker。dpswarm_rework 恰 3 次，dpswarm_repair_report 恰 1 次。

## 16 个 worker 的谱系与时间

所有 worker 的 parentSession 都指向同一个根。角色之间依靠工作项传递依赖，不是递归生成孙代理。所有角色原生 header 都为 deepseek-official / deepseek-v4-flash / reasoningEffort=max，本次未使用角色异构模型。多数初始 maxTokens=256,000，报告修复初始为 178,623；后续变化全部保留在 JSON headers 中。

run A：09252281-e4b8-4346-801b-7d7a4b59a0d4（首次 run 与三次返工）。run B：adee37e4-fa63-4cc5-beba-b91cbc80215c（第二次正式 run 与报告修复）。

| 顺序 | child | 角色 / 阶段 | 工作项 | 来源工作项 | Sydney 开始→结束 | 秒 | 原生终止 |
|---:|---|---|---|---|---|---:|---|
| 1 | [5dc0a0dd](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/5dc0a0dd-1079-4361-bf37-c666b7637b21/session.v3.jsonl:1>) | implementer / 首次 run | wi-6b96c310ee | 初始 run | 13:57:15.391→14:02:33.057 | 317.666 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/5dc0a0dd-1079-4361-bf37-c666b7637b21/session.v3.jsonl:93>) |
| 2 | [77739ebe](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/77739ebe-df65-485a-85f2-70d2be3e6304/session.v3.jsonl:1>) | tester / 首次 run | wi-264582acc3 | 初始 run | 14:02:33.220→14:07:33.948 | 300.728 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/77739ebe-df65-485a-85f2-70d2be3e6304/session.v3.jsonl:84>) |
| 3 | [e340fa6c](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/e340fa6c-cbbb-4912-9351-f4f9c8e2c518/session.v3.jsonl:1>) | reviewer / 首次 run | wi-9cb301b569 | 初始 run | 14:07:34.095→14:10:48.454 | 194.359 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/e340fa6c-cbbb-4912-9351-f4f9c8e2c518/session.v3.jsonl:73>) |
| 4 | [655d220b](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/655d220b-3ea2-4dc7-8b59-8687f790b863/session.v3.jsonl:1>) | implementer rework / 返工 1：尝试修腿 | wi-f75f9c091c | wi-6b96c310ee | 14:11:40.670→14:14:15.995 | 155.325 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/655d220b-3ea2-4dc7-8b59-8687f790b863/session.v3.jsonl:68>) |
| 5 | [5b2328b2](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/5b2328b2-567e-447e-8f5f-f56216fe36a1/session.v3.jsonl:1>) | tester re-verify / 返工 1：尝试修腿 | wi-7530bb10f9 | wi-6b96c310ee | 14:14:16.203→14:16:00.063 | 103.860 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/5b2328b2-567e-447e-8f5f-f56216fe36a1/session.v3.jsonl:53>) |
| 6 | [e6ef99b0](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/e6ef99b0-9efa-406f-87e6-da68bbb5bade/session.v3.jsonl:1>) | reviewer re-review / 返工 1：尝试修腿 | wi-061c9ef6d1 | wi-6b96c310ee | 14:16:00.341→14:17:55.750 | 115.409 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/e6ef99b0-9efa-406f-87e6-da68bbb5bade/session.v3.jsonl:61>) |
| 7 | [46d24383](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/46d24383-22c8-459b-a6c7-e3fe8c2464b7/session.v3.jsonl:1>) | implementer rework / 返工 2：落盘腿部修正 | wi-85707941a1 | wi-f75f9c091c | 14:18:24.065→14:19:11.096 | 47.031 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/46d24383-22c8-459b-a6c7-e3fe8c2464b7/session.v3.jsonl:57>) |
| 8 | [491d97a9](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/491d97a9-c1bb-4b45-8514-a5d9e1f56878/session.v3.jsonl:1>) | tester re-verify / 返工 2：落盘腿部修正 | wi-092f486157 | wi-f75f9c091c | 14:19:11.502→14:22:39.639 | 208.137 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/491d97a9-c1bb-4b45-8514-a5d9e1f56878/session.v3.jsonl:63>) |
| 9 | [50307b72](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/50307b72-ece3-4f6b-bc46-4979e1910311/session.v3.jsonl:1>) | reviewer re-review / 返工 2：落盘腿部修正 | wi-96859e70da | wi-f75f9c091c | 14:22:40.064→14:26:10.707 | 210.643 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/50307b72-ece3-4f6b-bc46-4979e1910311/session.v3.jsonl:58>) |
| 10 | [be9d5849](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/be9d5849-7f3d-4eb1-a6a9-ac0b1fbc17fc/session.v3.jsonl:1>) | implementer rework / 返工 3：修云层循环 | wi-3675910c2c | wi-85707941a1 | 14:26:41.621→14:27:39.472 | 57.851 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/be9d5849-7f3d-4eb1-a6a9-ac0b1fbc17fc/session.v3.jsonl:45>) |
| 11 | [94e62920](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/94e62920-83cc-4c82-9b43-9d3da8a83b1a/session.v3.jsonl:1>) | tester re-verify / 返工 3：修云层循环 | wi-b0c0f1212d | wi-85707941a1 | 14:27:39.947→14:30:27.606 | 167.659 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/94e62920-83cc-4c82-9b43-9d3da8a83b1a/session.v3.jsonl:60>) |
| 12 | [98f6cb5d](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/98f6cb5d-1cf8-4bb7-8f1d-58f44d8aa912/session.v3.jsonl:1>) | reviewer re-review / 返工 3：修云层循环 | wi-f6022e16f2 | wi-85707941a1 | 14:30:28.201→14:32:51.613 | 143.412 | [error](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/98f6cb5d-1cf8-4bb7-8f1d-58f44d8aa912/session.v3.jsonl:46>) |
| 13 | [14b40392](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/14b40392-a2cb-4145-a664-838f50d1130d/session.v3.jsonl:1>) | implementer / 第二次正式 run：重新验证 | wi-29d77d2efd | 初始 run | 14:38:06.856→14:39:17.930 | 71.074 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/14b40392-a2cb-4145-a664-838f50d1130d/session.v3.jsonl:68>) |
| 14 | [dec6e49e](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/dec6e49e-1e14-4183-bae9-329d098fd588/session.v3.jsonl:1>) | tester / 第二次正式 run：重新验证 | wi-5ea56945a7 | 初始 run | 14:39:18.465→14:46:53.854 | 455.389 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/dec6e49e-1e14-4183-bae9-329d098fd588/session.v3.jsonl:120>) |
| 15 | [fa83aa27](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/fa83aa27-c5ff-47be-a49b-b03116b33273/session.v3.jsonl:1>) | reviewer / 第二次正式 run：重新验证 | wi-b1c4060e23 | 初始 run | 14:46:54.206→14:51:36.090 | 281.884 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/fa83aa27-c5ff-47be-a49b-b03116b33273/session.v3.jsonl:125>) |
| 16 | [93a2a78c](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/93a2a78c-d435-4ffd-b1f5-476e7e8b6884/session.v3.jsonl:1>) | reviewer report repair / reviewer 报告修复 | wi-0037542947 | wi-b1c4060e23 | 14:52:31.866→14:53:22.836 | 50.970 | [completed](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/93a2a78c-d435-4ffd-b1f5-476e7e8b6884/session.v3.jsonl:46>) |

## 每个 worker 的预算与收尾

12/16 个 worker 进入 final_only。它根据后续输入、输出预留和报告空间切换收尾，不能统称“预算全部耗尽”。多数结束时仍有正数剩余。此轨道计入每步重发的输入和缓存输入；Lead 不受 worker 轨道约束。

| child | 轨道 tokens/calls | 可观测 calls | observed tokens | 最后剩余 tokens/calls | final_only | 报告状态 | 来源 |
|---|---:|---:|---:|---:|---|---|---|
| 5dc0a0dd | 1,500,000/30 | 15 | 1,258,674 | 241,326/15 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:41>) |
| 77739ebe | 1,000,000/24 | 11 | 754,643 | 245,357/13 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:41>) |
| e340fa6c | 1,000,000/24 | 9 | 434,143 | 565,857/15 | 否 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:41>) |
| 655d220b | 600,000/28 | 10 | 477,429 | 122,571/18 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:57>) |
| 5b2328b2 | 600,000/28 | 7 | 311,795 | 288,205/21 | 否 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:57>) |
| e6ef99b0 | 600,000/28 | 7 | 369,113 | 230,887/21 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:57>) |
| 46d24383 | 600,000/28 | 7 | 261,985 | 338,015/21 | 否 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:95>) |
| 491d97a9 | 600,000/28 | 8 | 455,032 | 144,968/20 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:70>) |
| 50307b72 | 600,000/28 | 7 | 447,740 | 152,260/21 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:95>) |
| be9d5849 | 600,000/28 | 5 | 199,736 | 400,264/23 | 否 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:124>) |
| 94e62920 | 600,000/28 | 8 | 482,079 | 117,921/20 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:134>) |
| 98f6cb5d | 600,000/28 | 4 | 251,680 | 23,307/23 | 是 | progress | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:95>) |
| 14b40392 | 350,000/10 | 9 | 267,957 | 82,043/1 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:173>) |
| dec6e49e | 1,400,000/26 | 18 | 1,220,368 | 179,632/8 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:178>) |
| fa83aa27 | 1,200,000/24 | 15 | 988,647 | 211,353/9 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:183>) |
| 93a2a78c | 211,353/9 | 5 | 143,208 | 68,145/4 | 是 | final | [可见预算回执](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:188>) |

首次 run 生效预算为 1,500,000/30、1,000,000/24、1,000,000/24；三次返工各角色实际均为 600,000/28（authority=fixed-team-rework），不能按首次 run 额度理解。第二次实际获准 run 为 350,000/10、1,400,000/26、1,200,000/24；先前被 RUN_PENDING 拒绝的 900,000/1,300,000/1,300,000 提案没有生效。

报告修复沿用剩余 reviewer 预算：fa83 剩余 1,200,000−988,647=211,353 tokens、24−15=9 calls；93a2 的轨道恰为 211,353/9，用 143,208 后剩 68,145/4，没有新发满额 reviewer 配额。

98f6 的第 5 步只有 step/start、step/end、turn/end error（child L44–46），没有 request/header、assistant/message、stream 或 usage。最后一条有 usage 的第 4 步 L40 为 77,733，header 输出上限已降至 9,009；其工具 L42 被 FINAL_ONLY 拒绝。第 5 步时间窗口内，root L95 可见 last_denial stage=request_output_limit，input_estimate=89,618、output_limit=1、required_reservation=89,619，超过 remaining=23,307，故拒绝。既不是剩余为 0，也不是 callLimit 用尽（仍剩 23）。

root L95 另记 calls=5、unknown_usage_calls=1；附件缺逐调用 admission ID 流，不能把 unknown 那次确定归到第 5 步，更不能声称第 5 步已调用供应商。报告状态是 progress、report_available=false；只有恢复进度，未形成最终报告。该诊断还确认原生 error 与 physical_cleanup_confirmed=true。

## UI 两轮时长与总跨度

全部时刻转换为 2026-09-12 Sydney UTC+10。

| 区间 | 时刻 / 来源 | 精确时长 |
|---|---|---:|
| 创建→第一轮开始 | 13:56:48.101→13:56:53.267；[主日志 L1](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:1>)、[主日志 L6](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:6>) | 5.166s |
| 第一轮 | 13:56:53.267→14:30:28.263；[主日志 L6](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:6>)→[主日志 L85](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:85>) | 2,014.996s = 33m34.996s |
| 两轮之间 | 14:30:28.263→14:35:32.211；[主日志 L85](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:85>)→[主日志 L88](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:88>) | 303.948s = 5m3.948s |
| 第二轮 | 14:35:32.211→14:54:47.817；[主日志 L88](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:88>)→[主日志 L214](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:214>) | 1,155.606s = 19m15.606s |
| 两轮之和 | 不含间隔和创建前缀 | 3,170.602s = 52m50.602s |
| 创建→最终结束 | 包含所有区间 | 3,479.716s = 57m59.716s |

UI 33m34 + 19m15 对各轮秒数取整后相符；显示相加 52m49，比原生精确合计少 1.602s，来自显示粒度。主要差额是 303.948s 轮次间隔及 5.166s 创建前缀。

**两轮之间并非全为空闲。** 第一轮在 98f6 创建约 62ms 后中断，但这个 worker 一直运行到 14:32:51.613；父 turn 结束后它又执行约 143.350s，之后到下一轮才相隔 160.598s。来源：[主日志 L82](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:82>)、[主日志 L85](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:85>) 与 [subagents/98f6cb5d-1cf8-4bb7-8f1d-58f44d8aa912/session.v3.jsonl L46](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/subagents/98f6cb5d-1cf8-4bb7-8f1d-58f44d8aa912/session.v3.jsonl:46>)。父 turn interrupted 不能推出当时所有 worker 已停止。

16 个 worker 生命周期合计 2,881.397s（48m1.397s）。大部分已含在 Lead 调度工具等待中，不能再加到两轮耗时。对子代理区间两两求交没有正重叠，故本次实际串行；UI 子代理数不是并发数。

## 附件内正式状态与边界

- [主日志 L95](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:95>) 是 14:35:35.196 的历史 status。可见 JSON 主体后带宿主 spill 提示，本报告只解析附件已有的 JSON 字段，没有读 spill。此时 candidate 04d2e495、contract revision 13、not_accepted，2 submitted / 2 open slots，租约仍归本 session，旧 PID 显示不存活。execution=not_running 不等于验收已完成。
- [主日志 L124](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:124>) 首次正式 takeover 成功：outcome=accepted、mode=lead，candidate 04d2e495、review-4、contract-7a5eb62f1a462c2e150b3d7b。[主日志 L134](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:134>) 接受该 tester 工作项。
- [主日志 L173](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:173>) 第二次正式 takeover 成功：outcome=accepted、mode=lead，candidate 1362e199、review-3、contract-13ce32a4df3690a5e9fe97a2。
- [主日志 L178](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:178>) 接受第二轮 tester；[主日志 L183](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:183>) 原 reviewer 返回 **already_terminated**、未发生状态转移，不能写成又接受 reviewer；[主日志 L188](<K:/秋招/项目/DPswarm/.tmp/session-5479d74d-audit/native/session.v3.jsonl:188>) 接受报告修复 item。
- 按各 item 最后可见状态，重建为 5 accepted / 11 terminated。这是历史 snapshot 加后续回执逐项推导，不是最终全量 status。最后 lease 是否释放、pending 是否归零、当前实时状态仍为“未直接复核”。
- 两次是 Lead takeover，不能改写为独立 reviewer 最终 pass；正式接受证明控制面认可该次 Lead 报告，不能反推每个视觉证据一定充分。

## 每个会话的完整用量分项

| 会话 | steps | 有 usage 响应 | 工具调用 | input | cacheRead | output | 其中 reasoning | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Lead | 32 | 32 | 34 | 184,805 | 4,918,016 | 76,990 | 50,349 | 5,179,811 |
| 5dc0a0dd implementer | 15 | 15 | 15 | 138,172 | 1,047,808 | 72,694 | 57,868 | 1,258,674 |
| 77739ebe tester | 11 | 11 | 16 | 115,600 | 572,928 | 66,115 | 54,194 | 754,643 |
| e340fa6c reviewer | 9 | 9 | 15 | 19,000 | 374,016 | 41,127 | 32,371 | 434,143 |
| 655d220b implementer rework | 10 | 10 | 9 | 64,740 | 377,344 | 35,345 | 23,759 | 477,429 |
| 5b2328b2 tester re-verify | 7 | 7 | 8 | 23,515 | 266,368 | 21,912 | 13,106 | 311,795 |
| e6ef99b0 reviewer re-review | 7 | 7 | 11 | 86,837 | 258,176 | 24,100 | 11,121 | 369,113 |
| 46d24383 implementer rework | 7 | 7 | 10 | 24,392 | 226,816 | 10,777 | 5,402 | 261,985 |
| 491d97a9 tester re-verify | 8 | 8 | 10 | 87,122 | 321,920 | 45,990 | 33,500 | 455,032 |
| 50307b72 reviewer re-review | 7 | 7 | 9 | 95,892 | 302,976 | 48,872 | 33,667 | 447,740 |
| be9d5849 implementer rework | 5 | 5 | 7 | 23,552 | 165,120 | 11,064 | 8,860 | 199,736 |
| 94e62920 tester re-verify | 8 | 8 | 8 | 84,727 | 361,216 | 36,136 | 24,929 | 482,079 |
| 98f6cb5d reviewer re-review | 5 | 4 | 5 | 98,947 | 120,576 | 32,157 | 19,952 | 251,680 |
| 14b40392 implementer | 9 | 9 | 9 | 37,125 | 219,520 | 11,312 | 6,914 | 267,957 |
| dec6e49e tester | 18 | 18 | 22 | 105,647 | 1,028,096 | 86,625 | 63,766 | 1,220,368 |
| fa83aa27 reviewer | 15 | 15 | 26 | 98,688 | 829,056 | 60,903 | 41,109 | 988,647 |
| 93a2a78c reviewer report repair | 5 | 5 | 5 | 30,075 | 103,552 | 9,581 | 3,154 | 143,208 |

原生 step/start 总数为 178（Lead 32、worker 146），但有 usage 的响应数为 177；差额就是 98f6 第 5 步没有 assistant/message 的失败步骤。不是有一条 assistant/message 被统计脚本遗漏。各用量逐条行号保存在 accounting-analysis.json 的 sessions[].usage_rows。

## 输出与缺口

accounting-analysis.json 保存全部 177 条 canonical usage 的文件和行号、17 个会话的每步时间/工具/header、child→item→run→generation 与预算引用。accounting-summary.json 是主报告可直接读取的汇总和阶段表。control-exported-evidence.json 保存附件内状态与接受回执，未查询实时状态。

6 个大型工具 JSON 主体截断：root L41、57、70、119、142、168。可完整解析的内部对象与后续 child 提示内的历史 JSON 支持主要谱系和预算交叉核验；不可见字段保持未知。

## 修复阶段补充核对

98f6 原生 L29/seq27 为无 usage 的 TRANSPORT assistant/attempt，L30–32 为重试及新请求头；因此可见记录支持未知占用对应这次失败 attempt，而非第 5 步被拒请求。没有供应商最终 usage，325,013 仍按未知预留处理，13,504,040 的可核算下限不变。
