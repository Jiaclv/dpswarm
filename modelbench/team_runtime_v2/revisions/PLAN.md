# 预算提示与等价路径修正后的独立工程复验

v2 真实回归发现接入遗漏：旧 runner 在每次调用前明确提示 `call n/phase_limit`，新 loop 未携带这一提示。已有执行/验证阶段耗尽预算且未写 attestation，任务功能检查通过也无法通过完整评分。遗漏本身由源码对照确认；它对每一个失败的因果贡献不作未经对照的断言。

当前 v2 的8轮冻结批次完整保留，不修改其中源码或追加预算。此修订通过子类恢复逐调用 phase/global 剩余调用数和最后一轮的交付提醒；不改变20调用上限、任务实例、P/E/V模型、权限、原生协议、澄清状态机或评分器。v2 源码同时保存在原批次的 source_snapshot。

复核另发现交接误拒绝：Planner 给出 `/shared/workspace/config_system.py`，公开规格给出 `config_system.py`，实际是同一交付物。TeamBench adapter 现把这个已声明工作区前缀及前导 `./` 规范为相对路径，再交给原有限事实契约；工具结果记录每次表示规范化，原请求和推理原样留存。没有补填事实、更改文件名或放宽数量/值校验。`..`、反斜杠和其他绝对根不参与规范化，错误/越界路径仍被拒绝。这个变化也属于组合修订，不能把得分变化单独归于预算提示。

复验是两种 GLM × 两题 × 一次，共4轮，随机种子20260903，并发2。固定P=Sol、V=Terra；GPT请求max+fast，GLM thinking+max。每轮最多20调用、token准入边界600,000；共最多80调用，合计准入边界2,400,000。沿用原单调用600秒/单轮14,400秒约束。批次14,400秒后不再开新wave；工程/评分器错误或显式STOP记录亦停止后续wave，已启动有界轮次收尾。失败不自动重跑、不替换。

先用离线测试验证每次真实transport请求中的预算、最后一轮提醒与恢复不重置；启动前固定全部源文件与实例hash，复制完整修复源码快照。此次4轮用于确认工程修订，不代替多题、多种子性能排名，也不声称可靠性达到某个人群水平。

从仓库根目录运行：

```powershell
python -m modelbench.team_runtime_v2.revisions.budget_visibility prepare
python -m modelbench.team_runtime_v2.revisions.budget_visibility run
python -m modelbench.team_runtime_v2.revisions.budget_visibility report
```
