# xarray 已结束运行失败分析

分析时间：2026-09-03T03:11:15.073923+00:00。对象仅为 `pilot_v2` 的 `pydata__xarray-7229` 两个已结束条件。

两次结果应保留为普通候选失败。官方评分均完成，补丁均成功应用，且在同一个目标断言上失败；未发现评分基础设施故障。两边实际只有 `gpt-5.6-sol` Lead，各 15 次调用、0 次委派、0 次协议错误、0 次未知 usage。`dpswarm` 条件本次没有调用 worker，因此这对结果不能用于判断委派效果或 GLM 能力。

| 项目 | solo | dpswarm |
|---|---:|---:|
| 运行停止原因 | `budget_exhausted`，token 预留不足 | `completed`，显式调用 `finish` |
| 已计 token（含缓存输入） | 562,714 | 541,803 |
| 剩余 token / call 数 | 37,286 / 13 | 58,197 / 13 |
| 推理阶段秒数 | 419.532 | 390.687 |
| 官方 `completed` / `resolved` | true / false | true / false |
| FAIL_TO_PASS | 0 成功 / 1 失败 | 0 成功 / 1 失败 |
| PASS_TO_PASS | 280 成功 / 0 失败 | 280 成功 / 0 失败 |
| 官方命令结果 | 1 failed, 280 passed, 1 skipped | 1 failed, 280 passed, 1 skipped |

来源：[solo result](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/result.json)、[dpswarm result](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/result.json)、[solo 官方 report](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/environment/grader/logs/run_evaluation/9863e11b7459443a857fafc6eab7a71c/dpswarm-swe-pilot-solo/pydata__xarray-7229/report.json)、[dpswarm 官方 report](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/environment/grader/logs/run_evaluation/025e6132cb2840e59c1ea623da980341/dpswarm-swe-pilot-dpswarm/pydata__xarray-7229/report.json)。

## 直接失败原因：坐标属性来源处理遗漏

两次官方失败均为 `xarray/tests/test_computation.py::test_where_attrs`。该用例让 `cond`、`x`、`y` 各自携带不同坐标属性。测试期望结果坐标 `a` 的 `attr` 为 `x_coord`，实际为 `cond_coord`。这是对象属性断言失败，不是测试导入、容器、补丁应用或超时错误。证据位于 [solo test_output 的断言](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/environment/grader/logs/run_evaluation/9863e11b7459443a857fafc6eab7a71c/dpswarm-swe-pilot-solo/pydata__xarray-7229/test_output.txt:579)、[dpswarm test_output 的断言](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/environment/grader/logs/run_evaluation/025e6132cb2840e59c1ea623da980341/dpswarm-swe-pilot-dpswarm/pydata__xarray-7229/test_output.txt:575)。

两份候选实现实质相同：移除原来的 attrs 回调，仍以 `keep_attrs=True` 调用 `apply_ufunc`，之后只设置 `result.attrs = getattr(x, "attrs", {})`。因此修正了最外层数据属性，却没有修正三个 DataArray 都提供坐标时的坐标属性选择。这个解释同时受到补丁和官方实际输出支持；不需要参照 gold patch。见 [solo model.patch](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/model.patch:16)、[dpswarm model.patch](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/model.patch:16)。

## 本地测试为何通过

本地通过有命令输出支持，不只来自模型结束自述：solo 的最终检查记录了 computation 模块 `281 passed, 1 skipped`，以及 DataArray where 子集 3 passed、Dataset where 子集 5 passed；dpswarm 也记录了 computation 模块 `281 passed, 1 skipped`，命令退出码均为 0。见 [solo commands/00015.json](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/environment/commands/00015.json)、[dpswarm commands/00014.json](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/environment/commands/00014.json)。

这些本地检查未覆盖官方失败的属性竞争情形。solo 新增坐标属性的 `x`，但其 `cond` 没有显式的不同坐标属性；dpswarm 新增回归直接使用 `xr.where(True, x, x, keep_attrs=True)`。后者的额外手工检查使用 `x > 0` 作为条件，条件坐标也来源于同一个 `x`，同样无法区分 `x_coord` 与独立的 `cond_coord`。见 [solo 新增测试](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/model.patch:41)、[dpswarm 新增测试](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/model.patch:43)及上面的 command 记录。相同测试函数名不代表工作区测试内容与官方测试内容相同。

## 两边停止原因不同

solo 的最后一次已执行调用为 `0d7271cf-fb4d-475c-afdb-e794ec76be03`，执行的是最终本地测试；之后下一次调用在预算 admission 阶段被拒绝，没有第 16 次真实模型调用。虽然尚有 13 个调用名额，37,286 token 不足以支付已有长历史加 32,768 输出预留：仅已保存历史按冻结算法计算就需要 80,726 token，尚未加入下一轮状态提示。它是 token 预留限制触发，不是 call 数上限或墙钟超时。

dpswarm 的第 15 次调用 `213b951b-fcdb-4597-94ad-b31fc337f49a` 明确请求 `finish`，不能归为预算耗尽。该自述声称修复完成，但官方验收仍失败。证据：[solo budget](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/budget.json)、[solo history](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__solo/lead/history.json)、[dpswarm 最后一次 metadata](K:/秋招/项目/DPswarm/modelbench/swe_verified_20260903/pilot_v2/results/pydata__xarray-7229__dpswarm/calls/3241f3174795de8400a5fe58e7f966d79804b7456b48135848a6b43a6a028a10/metadata.json)。

两边都遇到过 `rg` / `apply_patch` 不存在等命令摩擦；dpswarm 还出现两次 `git apply` 输入格式错误。stderr 保留了这些错误，随后两边均改用 Python 成功写入实际补丁并完成本地测试。它们消耗了操作机会，但不构成这次官方评分故障，也不能据此证明增加预算就会修复遗漏。相关记录：solo `commands/00001.json`、`00012.json`、`00014.json`；dpswarm `commands/00001.json`、`00010.json` 至 `00013.json`。

## 评分与证据核对边界

独立只读审计对这两次 run 的 metadata、调用/预算计量、CP 记账、补丁副本 SHA、官方报告 SHA 和 verdict 交叉核对，均为 0 个审计错误。官方报告都明确 `patch_successfully_applied=true`；harness/controller 进程退出码为 0，`cleanup_errors=[]`，`infrastructure_error=null`。

- solo 候选补丁 SHA256：`4d89d560f197f4414fe6e56a27f87150d3a31e069b1b59881d4a3033d244a01b`。
- dpswarm 候选补丁 SHA256：`f3add48c33f8fde082f857a23abe8dd5cf24ed348f814991920494092124bdd9`。
- 两份官方 report SHA256 均为 `6d7b7e7d4a8394562d1012f5d6318fdb8d1f0887ced11779b20bd206307c1cfd`；相同统计和失败节点使报告内容相同，不代表候选补丁相同。

这是对已有证据的有界事后分析。没有读取 gold patch，没有运行模型、容器或 grader，没有修改冻结源码、候选补丁或评分，没有向任何正在运行的候选提供信息。只定位已实际触发的失败，不推断未执行断言或其他输入范围的正确性，不将本地测试通过或 CP 完工当作官方验收通过。
