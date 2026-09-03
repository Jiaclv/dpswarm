# Terra 取消调用：原因与未知用量对账

只读取证完成于 2026-09-03T04:19:14.684202+00:00。范围仅为 `sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra` 的 worker-1 调用 `6f82fad6-9908-4d6a-bffb-5db273681612`、其运行结束事件、冻结调用路径，以及原始 thread ID 对应的本地 session 文件名查询。未调用模型、容器或 grader，未读取 gold/private test 内容，未改原始 metadata 或冻结源码。

**结论：真实 token 用量仍为 unknown，未能恢复。取消是 Lead 预算准入失败后触发的运行器清理，不能解释成模型运行满 600 秒。** `error.code=cancelled` 与真实路径一致；`message=Codex exceeded 600 seconds` 是旧异常映射留下的误导文案。

## 取消原因

- [调用 metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/metadata.json>) 记录起止 `04:14:58.998572` 至 `04:15:57.442593 UTC`，wall=**58.437 秒**，`transport_attempt_count=1`，`timeout_kind=null`。`cancellation` 记录 PID 4984 已通过 `taskkill_tree` 终止，`tree_termination_confirmed=true`、`parent_exited=true`。
- [events.jsonl](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl:124>) 记录 Lead 执行 `collect(worker-1, wait_seconds=60)`；[第 128 行](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl:128>) 记录该 worker 调用预留 48,761 tokens；[第 129 行](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl:129>) 的 collect 在 `04:15:56.992777 UTC` 返回。[第 130 行](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl:130>) 在 `04:15:57.475516 UTC` 结算被取消调用。
- [第 132 行](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl:132>) 保存根运行的真实 outcome：`budget_exhausted` / `[TOKEN_BUDGET_EXHAUSTED] Reservation exceeds remaining token budget`。[最终 result](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/result.json>) 的 `infrastructure_error=null`、`deadline_exceeded=false`，不是全运行的 1800 秒 deadline 或该调用的 600 秒 deadline 触发。
- 冻结 [runner.py:248](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_fixed_team_20260903/runner.py:248>) 先进行全队预算预留，失败在 loop 的 302–303 行转成 `budget_exhausted`。[runner.py:502](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_fixed_team_20260903/runner.py:502>) 在 Lead loop 返回后立刻设置根取消事件，并在 504–505 行设置所有子 worker 的取消事件，然后等待它们停止。这条路径与上述事件顺序一致：Lead 的 collect 返回，下一次预算准入失败，尚在执行的 worker-1 被取消。没有证据表明 batch 的 STOP_AFTER_PAIR 标记直接杀掉了此调用。
- 最终该 worker 标记 `transport_error`，运行器在第 131 行自动清理其未交付结果；这不代表 worker 自主完成，也不应被描述成候选自身请求停止。

具体拒绝发生在 token **预留**门槛，并不等于已观测 token 达到 600,000：该 run 已知小计 **506,216**，未知调用仍保留 **48,761** 的 admission reservation，committed=**554,977**，remaining=**45,023**。下一次 Lead 的完整历史请求加输出预留无法通过；失败的具体预留值未另记事件，本文不补造该数字。预算门槛具有全队作用：一个角色无法继续请求时，本运行器会结束 Lead loop 并取消仍工作的子角色，而不是等待它们耗尽各自局部次数。

## 为何错误文案写成 600 秒

冻结 [SWE transport.py:219](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_verified_20260903/transport.py:219>) 明确区分 `cancelled` 和 `total_deadline`。取消分支终止进程后抛 `_Interrupted`；其 code 由 [transport.py:356](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_verified_20260903/transport.py:356>) 保留。旧 [transports.py:264](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/team_eval20260903/transports.py:264>) 将该异常按 `subprocess.TimeoutExpired` 捕获，随后 [transports.py:347](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/team_eval20260903/transports.py:347>) 统一抛 `TimeoutError("Codex exceeded 600 seconds")`。SWE 外层随后用 interruption code 生成 `cancelled`，却保留了这个旧异常文字。因此应优先按结构化 code 与实际时间解释，不能用 message 推断超时事实。

未来若恢复开发，应独立修正文案以区分主动取消和真正 deadline；是否在 Lead 预算失败后允许已准入 worker 收束属于另一项调度政策变更，需要另行明确契约。此次只记录问题，没有启动新版本或修改政策。

## 用量查询结果与边界

[stdout.jsonl](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/stdout.jsonl:1>) 仅两行：

```json
{"type":"thread.started","thread_id":"01a0657a-2673-7222-bd43-02476c4789b8"}
{"type":"turn.started"}
```

没有 `turn.completed`、usage 或 token_count。原始 [request.json](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/request.json>) argv 使用 `--ephemeral`。本次仅枚举 `C:/Users/93711/.codex/sessions` 的文件名并精确过滤 UUID `01a0657a-2673-7222-bd43-02476c4789b8`，未找到匹配 JSONL；没有扫描其他会话正文，也没有使用账户全局 usage、其他调用的 token_count 或估算值。

因此 `input_tokens / cached_input_tokens / output_tokens / reasoning_tokens / total_tokens` 都保持 **null**。预留 48,761 是预算准入担保，不是实际服务端 token，也不是可靠补账上界；GPT CLI 的 max_tokens 并未强制执行。无法从没有完成事件推断服务端消耗为零，无法排除被取消前已产生但未返回的 usage。

该 run 的已知 token 小计与未知项应并列报告；8 个已完成 run 的父级报告同样应保留 **3,462,637 已知 tokens + 1 个未知调用**，不能把 48,761 加成“实测总 token”。本报告不回写计量，也不重新结算控制平面。

## 证据哈希

以下 SHA-256 固定此次只读输入；原文件均未改动。

- [metadata.json](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/metadata.json>)：`6d5d1833c8cd5c4dc6d4150e359797c2e500634a9585d46c96281c2a888cb5f4`
- [stdout.jsonl](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/stdout.jsonl>)：`777a1c2d57ffae7e0392762c04d6fd02331717bf209a0e85faa80615187a8104`
- [stderr.txt](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/stderr.txt>)：`ee79c82b28860d21a26fa232cdcc6dc2946aa92daf4fe91fa2d4f16cb6f6f1ad`
- [request.json](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/calls/ef7ebf62af0f828f03c5278bdabc7e4e8cb9a02cbf4635607b1d6ab6a0a0ac95/request.json>)：`6759612a6a2e36ebed2cec76b09233d16c5045a8a6f77d2538340757dec4ae6d`
- [events.jsonl](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/events.jsonl>)：`0642c76db7212c3030f840a82c2ffaa88edab440d018f8a9bdf71678991f8782`
- [result.json](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-terra/result.json>)：`49a91ee09c03255e3d546586f431ca538cde4500f1ff0ccc9443fc0a46e98f78`
- 冻结 `modelbench/swe_fixed_team_20260903/runner.py`：`e451dad44b1955aa697a6d66bbf726022f85c9ebdccc6477c24d3176c710ca30`（与 manifest 一致）
- 冻结 `modelbench/swe_verified_20260903/transport.py`：`eccbb08ab5ffb4f01b6f931e73e06608c2ad16153bb11803434afca30c9e198f`（与 manifest 一致）
- 冻结 `modelbench/team_eval20260903/transports.py`：`3213e2fab551ef33b86ce60d5875a507036d036c6a32f428468bb16401e602fc`（与 manifest 一致）
