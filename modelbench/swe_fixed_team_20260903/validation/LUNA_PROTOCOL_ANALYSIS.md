# Luna 文本协议错误：Codex 消息边界丢失的只读取证

取证快照：2026-09-03 04:14:40 UTC。范围为 `pilot_v2` 的 Sphinx Luna run 中截至此时已完成的 7 个 `invalid_json / Extra data` 调用，以及父代理追加要求的 Matplotlib 6 个已完成 run 的 117 条 metadata。Sphinx Luna 的 `result.json` 在此快照尚不存在；本文不预判最终成绩。未调用模型、容器或 grader，未读取 gold patch，未修改原始证据或冻结源码，未向候选反馈。

已证实是 adapter 拼接缺陷：7/7 错例中，每条非空 `agent_message.text` 都能单独通过该批次冻结的文本协议 parser 和该次 `prompt.json.tools` schema；同一 call 内这些消息解析出的工具动作完全相同。旧 adapter 把不同 `item.completed` 消息用换行拼成一个字符串，随后将整个字符串交给只接受单个 JSON envelope 的 parser，制造了 `Extra data`。不能把这 7 次报错归为模型单条最终响应 JSON 非法。

原始 CLI 事件只提供 item 的 `id/type/text`，没有 channel 或 phase。能确认不同消息事件的边界被丢失，不能从这些证据进一步断言哪条是 commentary、哪条是 final，或重复究竟由服务端还是 CLI 产生。下文“最后消息”仅指 JSONL 中最后一个非空已完成 `agent_message`，不冒称服务端标注的 final。

## 因果链与证据

- 冻结 [transports.py:300](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/team_eval20260903/transports.py:300>) 将每个 `item.completed / agent_message` 的文本追加到 `texts`，[transports.py:324](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/team_eval20260903/transports.py:324>) 执行 `record["text"] = "\n".join(texts)`，没有保留消息边界或选择最终输出。
- 冻结 [SWE transport.py:396](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_verified_20260903/transport.py:396>) 调用这条旧 CLI 路径；397 行把拼接文本构造成一条 assistant 消息；[transport.py:406](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_verified_20260903/transport.py:406>) 将它交给 `parse_text_response`。
- 冻结 [protocol.py:223](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/dpswarm-plugin/dpswarm/team_runtime/protocol.py:223>) 对整个 envelope 解析，229 行调用 `_loads`，34–39 行用严格 `json.loads` 报错。这里的严格 JSON 检查按设计工作；额外第二个 JSON 是上游拼接引入的。
- 冻结 [runner.py:309](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/source_snapshot/modelbench/swe_fixed_team_20260903/runner.py:309>) 在协议报错后记录计数、给历史附上 `executed:false` 反馈并 `continue`，不会进入 337 行的工具执行批次。因此错误消耗调用额度与 token，也使候选后续看到被拼接的 assistant 历史和错误反馈。

代表例 `c81dfe07-4bb5-4cfe-9b96-bae78317aad0`：stdout 第 3 行为 `item_0`，第 4 行为 `item_1`；两条 text 完全相同，均为 311 字符的合法单个 `tool_calls` envelope，包含一个 `bash` 请求。metadata.text 长 623 字符，精确等于 `text(item_0) + "\n" + text(item_1)`；严格解析在第二行第 1 列、字符 312 报 `Extra data`。事件序列随后只有一次 `turn.completed`，不是两个独立 API 调用。本文不复制命令内容或推理文本。

## 7 个原始调用

每行的 stdout/prompt/metadata 都属于同一个不可覆盖 call 目录。全部 `error=null`、`action=null`、`parsed_calls=[]`、CLI `tools_used=0`；每个 stdout 只有一次 `turn.completed`。worker 身份来自 [events.jsonl](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/events.jsonl:3>) 的 `worker_admitted` handle：worker-1=`node-e946c1e150`，worker-2=`node-69a8ebedfd`。

| call_id | worker | agent_message 行/结构 | total tokens | 原始证据 |
|---|---|---|---:|---|
| `eeb2263f-32d8-4e63-bd27-6ea87d8760f0` | worker-2 | 3/4/5：空 + JSON + 相同 JSON | 25,915 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/622326100aef16512c851e267e9e189355c919fce51f9b48d4fdc1164420004a/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/622326100aef16512c851e267e9e189355c919fce51f9b48d4fdc1164420004a/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/622326100aef16512c851e267e9e189355c919fce51f9b48d4fdc1164420004a/metadata.json>) |
| `ff34312a-9414-4317-999f-23c503157f41` | worker-1 | 3/4：相同 JSON（仅末尾空白不同） | 43,682 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/75a7a08f5594e2d0016040931ebb8c578cef95031887f6a86ce2549db75be46d/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/75a7a08f5594e2d0016040931ebb8c578cef95031887f6a86ce2549db75be46d/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/75a7a08f5594e2d0016040931ebb8c578cef95031887f6a86ce2549db75be46d/metadata.json>) |
| `4d61a963-1151-42f3-ac3e-346652c5c021` | worker-2 | 3/4：相同 JSON | 12,931 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3b2db2c024100d8333322bd1780514d53a1fd2a08a65ec52cffe2b78b77c1a3e/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3b2db2c024100d8333322bd1780514d53a1fd2a08a65ec52cffe2b78b77c1a3e/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3b2db2c024100d8333322bd1780514d53a1fd2a08a65ec52cffe2b78b77c1a3e/metadata.json>) |
| `ea1b9bcb-42d7-4b18-a97a-0a0edea5247f` | worker-2 | 3/4：相同 JSON | 26,858 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3154dcfb4e86e147f55d930a91b70774205b587e60a158ad521464c9d5f469ec/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3154dcfb4e86e147f55d930a91b70774205b587e60a158ad521464c9d5f469ec/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/3154dcfb4e86e147f55d930a91b70774205b587e60a158ad521464c9d5f469ec/metadata.json>) |
| `c81dfe07-4bb5-4cfe-9b96-bae78317aad0` | worker-2 | 3/4：相同 JSON | 27,749 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/2a54cef942196c46b6b6e3eb40304aefef33f806f7e9bc9ad5d15e9fd5d8888e/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/2a54cef942196c46b6b6e3eb40304aefef33f806f7e9bc9ad5d15e9fd5d8888e/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/2a54cef942196c46b6b6e3eb40304aefef33f806f7e9bc9ad5d15e9fd5d8888e/metadata.json>) |
| `92742406-8be7-4dd2-a78c-465e164bbaa8` | worker-1 | 3/4：相同 JSON | 28,816 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/7e230654604e73690ea5d17930656fb10bf96396e2e44ec83a62b35a9620eeef/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/7e230654604e73690ea5d17930656fb10bf96396e2e44ec83a62b35a9620eeef/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/7e230654604e73690ea5d17930656fb10bf96396e2e44ec83a62b35a9620eeef/metadata.json>) |
| `02eeaae7-ae81-4007-a341-15d1db6e6210` | worker-2 | 3/4/5：空 + JSON + 相同 JSON | 28,689 | [stdout](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/e8143299a910ebea31b45a5e0591f5cbbe6c46a2a6cb0821f9cfeb46cf929650/stdout.jsonl:3>) / [schema](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/e8143299a910ebea31b45a5e0591f5cbbe6c46a2a6cb0821f9cfeb46cf929650/prompt.json>) / [metadata](<K:/秋招/项目/DPswarm/modelbench/swe_fixed_team_20260903/pilot_v2/results/sphinx-doc__sphinx-8035__fixed_gpt-5.6-luna/calls/e8143299a910ebea31b45a5e0591f5cbbe6c46a2a6cb0821f9cfeb46cf929650/metadata.json>) |

上述 7 calls 总计 **194,640 tokens**，模型调用 wall 之和 **135.608 秒**（两个 worker 并行，不能当成 run elapsed）。worker-1 受影响 2 calls，worker-2 受影响 5 calls。五例有两个非空事件；两例另有一个空事件。全部非空事件经 `.strip()` 后文本相同，且工具 action 完全相同；其中一例仅原始末尾空白数量不同。没有发现某一条独立事件自身含两个 JSON envelope。

本次仅离线检验可解析性，没有重新执行这些工具、补算成绩或修改历史；通过 schema 不代表工具结果、候选补丁或任务正确性。7 次失败调用开销仍应完整计账，不能通过选择最后消息追溯成成功执行。

## 已完成 Matplotlib 6 run 的影响边界

逐个读取全部 117 metadata；其中 85 为 GPT CLI，32 为 GLM 原生调用。85/85 GPT 调用没有 transport/protocol error，并且每次只有 **一个非空**已完成 `agent_message`。用各次真实 tools schema 和冻结 parser 分别解析原 `metadata.text` 与最后一个非空消息，85/85 得到完全一致的 action，均为合法 `tools`。没有因末尾选择而发生 action 改变的历史 GPT 调用。

| Matplotlib arm | 全部 call records | GPT CLI records | 单非空消息 / schema 合法 / action 相同 |
|---|---:|---:|---:|
| solo | 7 | 7 | 7 / 7 / 7 |
| fixed_glm-5.3 | 26 | 10 | 10 / 10 / 10 |
| fixed_glm-5.3-flash | 27 | 11 | 11 / 11 / 11 |
| fixed_gpt-5.6-sol | 19 | 19 | 19 / 19 / 19 |
| fixed_gpt-5.6-terra | 15 | 15 | 15 / 15 / 15 |
| fixed_gpt-5.6-luna | 23 | 23 | 23 / 23 / 23 |
| 总计 | 117 | 85 | 85 / 85 / 85 |

这证明该拼接边界问题没有改变上述 85 条历史 GPT action；GLM 32 条走原生 tools，不经过此拼接分支。可以保留六个 run 的原始版本证据，并在版本间报告明确这项离线等价核验。它不等于新 adapter 与旧 adapter 对所有可能输出完全同构，也不是把其他 worker 预算、正式完成或补丁验收问题一并排除。

## 修订门槛与不可证明事项

主代理已根据取证写入停止于当前 pair 的标记。冻结运行期间不修改 transport。后续修订应基于 CLI 的明确最终消息边界或可验证的最终输出工件，保留每个原始事件，禁止把任意文本前缀中的 JSON 截出后执行；对空输出、重复/多个消息、不同消息、工具事件及超时都需有离线 fixture。本文观察到的同 call 重复 action 可作为回归证据，但不能自行证明“任取最后 JSON”适用于所有 CLI 输出。

此次错误说明实验工具适配层损坏了响应边界；不能据此判断 Luna 的编码能力，也不能推断排除此缺陷后的 Sphinx 成绩。当前受影响 run 应保留基础设施污染说明；任何后续重测必须有新版本、独立成本和清晰的比较口径，不能覆盖原 run。

复核方法（纯读取）：枚举完成 run 的 `calls/*/metadata.json`，按 `raw_artifacts.stdout` 读取 JSONL；仅收集 `type=item.completed && item.type=agent_message`。检查 `metadata.text == "\n".join(item.text)`；把每个非空 text 单独传入冻结 `parse_text_response(text, prompt.tools)`，与同 call 最后一条非空消息解析出的 action 比较。此过程不调用 transport 或 runner，不执行工具。

冻结 source_snapshot 的以下字节 SHA-256 与 manifest.runtime_sources 一致：

- `modelbench/team_eval20260903/transports.py`：`3213e2fab551ef33b86ce60d5875a507036d036c6a32f428468bb16401e602fc`
- `modelbench/swe_verified_20260903/transport.py`：`eccbb08ab5ffb4f01b6f931e73e06608c2ad16153bb11803434afca30c9e198f`
- `dpswarm-plugin/dpswarm/team_runtime/protocol.py`：`8a65102e095cca0f0fc53215c78f2115e3ff1bb10d118c0b342aefd178dc1615`
- `modelbench/swe_fixed_team_20260903/runner.py`：`e451dad44b1955aa697a6d66bbf726022f85c9ebdccc6477c24d3176c710ca30`

原始错误调用 stdout SHA-256（用于固定本次快照）：

- `eeb2263f-32d8-4e63-bd27-6ea87d8760f0`：`7ad539535b6189677427bc2e8d1c09b659ad204d5a1e8c203672bb5c7fdcba94`
- `ff34312a-9414-4317-999f-23c503157f41`：`fde4b6286c2de2bb652c726fd7dbfc8816f5aa036d05dd9fbe4e6e952a382bcf`
- `4d61a963-1151-42f3-ac3e-346652c5c021`：`f3f975b1126031b0e99b85f714c77232cef28f05c88f86fc41bf8078eb7178e2`
- `ea1b9bcb-42d7-4b18-a97a-0a0edea5247f`：`825022e64e41f09ed1b605f2380a320a44bfe577f94b4bb1eeb8dffc91d613c1`
- `c81dfe07-4bb5-4cfe-9b96-bae78317aad0`：`93c2a403916dc7059ba9eece702a028b31cd253bc1da1a9a83b4978e63b6090a`
- `92742406-8be7-4dd2-a78c-465e164bbaa8`：`312382a534a28c047ac1eed9dfdda9da46ea7600ec33dd1bcf81fd505d53e90b`
- `02eeaae7-ae81-4007-a341-15d1db6e6210`：`29a69192c3476538d84d9c6ead39ea3f30a11edfcb8998ffe27bd390c8207c31`
