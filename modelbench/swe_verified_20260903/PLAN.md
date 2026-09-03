# SWE-bench Verified 首批评测

授权：用户于 2026-09-03 选择“先做 swe，能并行就并行”。本批采用独立评测 RunnerAdapter，把真实 DPswarm 控制面接入 SWE 环境；原 DSH 宿主桥接复审缺口继续保留，不把本批成绩表述为 DSH 已完成接入。

## 冻结设计

- 数据：官方 `princeton-nlp/SWE-bench_Verified`，revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`，500 题。只用 repo/instance_id 元数据、固定种子 `20260903`：按仓库哈希排序取前 10 个仓库，再各取 instance_id 哈希最小的一题。选择先于读取问题/答案，记录于 `official/selection.json`。不因失败替换题。
- 官方 harness：`SWE-bench/SWE-bench` v4.1.0，commit `726c5461e2ef52d83cf1ea2107870a8bb3328d57`，匹配该原始 Verified 数据格式。新版 v5 数据 schema 不同，不能只换程序而默认为同一评测。源码不修改。
- 每题两个条件，共 20 run：Sol 单 agent；相同 Sol 主 agent＋可按需调用 DPswarm。主 agent 自己决定是否委派、选模型、定义任务、回应澄清和验收补丁。
- 子模型池：GLM-5.3、GLM-5.3-Flash、GPT Sol、Terra、Luna。GPT 请求 max/fast；GLM 请求 thinking enabled/max。只把服务端实际回显的设置记为 observed，其余 null。某个候选未被选择时报告 0 调用，不算该模型经过评测。
- CP 目录使用统一 B 级标签和每节点 1 点，纯粹消除准入分级的混杂，不代表模型能力/AA 排名。每个 run 单独 root/store；caller handle 与每次调用、item、node、attempt、epoch 绑定。本批允许最多 2 worker、4 次委派、worker 不再嵌套委派。

固定任务：pallets__flask-5014、pydata__xarray-7229、pytest-dev__pytest-7571、mwaskom__seaborn-3069、astropy__astropy-14995、matplotlib__matplotlib-25122、sphinx-doc__sphinx-8035、sympy__sympy-16792、scikit-learn__scikit-learn-25232、django__django-14140。

## 执行、并行与预算

两条件均最多 28 次实际模型调用、600,000 全队 token 准入额度、1,800 秒执行墙钟。worker 最多 8 次调用，所有 worker 和 Lead 共享同一预算，给 Lead 保留最后 2 次调用以整合。输入 token 预留采用保守字符估算＋输出请求上限；准入控制不等于模型服务端绝对封顶，实际超出也如实记录。GPT CLI 不强制相同输出 cap，配置差异披露。

同一题两条件在独立容器并行，最多同时 2 个 run、4 个模型调用、4 个工作/评分容器。每个工作/评分 testbed 为 2 CPU、3 GiB；可信 Linux grader-controller 额外占一个容器槽且限制 768 MiB。worker 在委派瞬间的 Lead 补丁快照上独立工作，完成后立即关闭容器；Lead 显式 adopt 才把 delta patch 合并。冲突不自动覆盖，失败、取消和放弃都关闭 CP 生命周期。原生工具结果严格按 ID 配对，非法 ID 终止该历史。

worker 提问保持同一上下文和预算，Lead 在下一请求边界看到问题，collect 等待会因问题提前返回。问题至多等待 120 秒，过期明确记录；不创建固定 Planner 模型。

Docker 本次探测为 24 CPU / 约 15.2 GiB。C 盘余量约 47.5 GiB，镜像按实例波次拉取，同题共用只读 image、不同容器可写层隔离；两条件和评分结束后仅清理本批新拉取且无容器引用的实例 image。不会全局 prune 或删除预存镜像。

## 评分与证据边界

候选只看到 problem_statement、repo、base_commit 和自己的代码环境。官方 gold patch、test_patch、FAIL_TO_PASS/PASS_TO_PASS 只在可信评分端使用；不挂载到候选，不作为模型工具，不在评分后再让模型修正同一 run。候选容器不挂宿主目录、Docker socket 或凭据，网络关闭。

所有候选停止并冻结最终 patch 后，用新容器调用官方 `run_instance`。Windows 本机不能直接导入该版本 Linux resource 依赖，故使用可信 Linux controller 驱动官方评分代码；controller 的 Docker socket 只用于它启动隔离 testbed，候选无访问。CP 的成功仅表示 Lead 交付和证据完成，不代表 SWE resolved；正式结果取官方 report.json。

执行前做无模型的工具、取消、计量、真实 CP 和容器隔离测试，并用不解决题目的非空补丁验证官方评分能完成且判 unresolved。不同补丁使用唯一 run_id，避免官方缓存混用。

每次调用记录原始请求/响应、模型与 requested/observed 设置、input/cache/output/reasoning token、时间和失败。缓存已包含于输入，reasoning 已包含于输出，不能重复累加。未知 usage 保留 null 和已知小计；无可靠价格来源时美元成本为 null。每个 worker、每题与整个 batch 分别记录墙钟，模型耗时和不能冒充并行后的墙钟。

## 停止与报告

所有失败保留，不补跑最好成绩。每个题目对完成后，若有基础设施错误、官方 grader 未完成、未知/悬空用量，则停止调度后续题目并记录原因；普通任务 unresolved 继续下一题。空或纯空白补丁是确定的候选无交付，单独记 candidate_empty_patch、resolved=false、official_completed=false，不计完整评分，但继续后续题目。无法确认的 grader 未完成不猜测原因。容器清理失败会保留错误、关闭控制面并禁止新的资源准入及评分，不把逻辑槽释放当成物理容器已移除。新题调度截止为 batch 开始后 3 小时；已启动的题目按各自执行及评分上限收束。中断留下的在途 run 不自动重放；只有已完整完成的 run 可被调度器跳过。

源码、数据、选择、工具 schema、预算和环境版本在 `pilot_v1/manifest.json` 固定，源码另存快照；启动后不改同一批次实现。若发现需修 bug，停止该批次，单独修订并保留旧结果。

2026-09-03 启动记录：pilot_v1 首对两个模型请求返回后，旧通用脱敏正则把 Flask run ID 中的 `sk-…` 误识别为凭据，控制面按身份不一致拒收。两次调用 33,281 token、0 工具执行、0 官方评分完整保留于 pilot_v1，作为基础设施开销，不计任务成绩。修复与回归后从独立 pilot_v2 重启相同 10 题和两条件；不向候选传递前次回答或任何评分反馈，不更换题目。每批单独保存 manifest、源码/输入及验证证据快照。

报告包括两条件 resolved、完整官方评分数、token、调用、耗时、协议错误、委派率、模型选择频率、澄清和配对 team-harm/rescue。10 个仓库等权抽样不代表 Verified 500 的总体比例；不据此宣称 SOTA，也不自动提交榜单。正式入榜须另跑完整同版 split，披露完整配置并经官方验证。

官方依据：[数据集](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)、[评分文档](https://www.swebench.com/SWE-bench/guides/evaluation/)、[提交说明](https://www.swebench.com/submit.html)、[官方代码](https://github.com/SWE-bench/SWE-bench)。
