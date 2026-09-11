# 实验容器 1 GiB 配置

用户在 2026-09-05 四题并发试跑期间要求：「每个docker 1gb 就行了 没必要3gb 从没用到过」。后续实验候选环境和官方评分执行环境使用 `1g`（1,073,741,824 字节），memory-swap 与 memory 相等。原本为 `768m` 的评分控制器和解析辅助容器保持较低限制。

配置文件为本目录 `resource-defaults.json`，执行覆盖模块为 `memory_profile.py`。已结束的四题试跑采用中途动态调整，不能记为全程 1 GiB 实验；其具体容器及调整时间保存在 `parallel-trial-4-v1/memory-1g-amendment` 和 `parallel-trial-4-v1/memory-1g-enforcer`。

## 后续执行约定

在每个新的独立解题进程中先装载其获准的冻结快照，再按文件路径装载本目录的 `memory_profile.py`，执行 `install(绝对配置路径, 新的绝对回执路径)`，之后调用原实验入口。资格核验进程同样需要安装该配置。安装会记录源码和配置哈希、实际导入路径及环境创建事件；不会调用模型、创建容器或自动选择实验。

这个覆盖方式保留旧源码和旧证据。**旧冻结 CLI 和旧控制器本身仍有 3 GiB 默认值，不能直接重启旧入口并声称已经使用 1 GiB。** 后续调度必须在每个子进程中安装本配置，并把回执纳入该次实验记录。当前串行流水线已停止，不会自行启动后续 A2。

旧的 3 GiB 环境资格结果不证明 1 GiB 下也通过。需要资格核验时，在新的 attempt 中记录 1 GiB 核验；该模块拒绝把未绑定本配置及评分摘要哈希的旧资格缓存当作新资格复用。不得覆盖旧记录或自动重跑已完成的模型实验。

## 已完成的验证

- `tests/test_memory_profile.py` 的 11 项离线检查通过，记录在 `../validation/memory-profile-junit.xml`。
- 原 A1 快照的新进程安装、安装前已取得的类引用及其 fork 均验证为 `1g`；86 份冻结源码哈希未改变，记录在 `../validation/memory-profile-snapshot-preflight-v1/install.json`。
- A2 资格入口和提前导入的函数引用覆盖检查通过，记录在 `../validation/memory-profile-qualification-preflight-v1/install.json`；这只是入口验证，没有启动资格任务。

上述检查不等于后续所有题目都已完成 1 GiB 环境资格核验，也不证明本轮中途变更的四题曾全程使用该上限。

真实容器检查也已通过：`../validation/memory-profile-container-preflight-v1/result.json` 和 `inspect.json` 证明新进程安装本配置后，候选容器从创建时起的 Memory 与 MemorySwap 都为 1,073,741,824 字节。检查仅使用已有 Astropy 镜像，0 次模型调用、0 次评分、0 次拉取，随后删除容器并核验不存在。这是候选容器创建限制的实测证据，不是全部任务的评分资格结论。
