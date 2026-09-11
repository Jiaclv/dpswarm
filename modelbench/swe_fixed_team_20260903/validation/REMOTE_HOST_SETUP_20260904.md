# 远程机（AutoDL seeta41）环境准备记录 — 2026-09-04

## 一句话结论

**AutoDL 可承担 agent/orchestration 平面（runner + codex/GLM/DeepSeek 传输层，大规模并发 LLM 调用），不可承担评测平面（任何形式的 Docker 守护进程，三层内核级证据）。** 建议双平面分离架构：并发实验在 AutoDL 跑，grading 留在本地 Docker Desktop（镜像已就绪）或另找 docker-capable 机器。

## 机器信息

| 项 | 值 |
|---|---|
| 接入 | `ssh seeta41`（本地 `~/.ssh/config` 别名；region-41.seetacloud.com:19949，root 免密已装） |
| 平台 | AutoDL 容器（hostname `autodl-container-3a4f118352-d33c8241`），Ubuntu 22.04.5，镜像 PyTorch2.8/Python3.12/CUDA12.8 |
| 无卡模式（当前） | **0.5 核 + 2GB 内存**（cgroup `cpu.max 50000 100000`、`memory.max 2GiB`）——装环境可以，跑重活不行 |
| 有卡模式（正式配置） | 12 vCPU / 43GB 内存 / RTX 2080Ti 11GB / 系统盘 30GB + 数据盘 `/root/autodl-tmp` 50GB |
| 服务端口 | 6006 / 6008（http），无对外端口映射 |
| 权限要点 | `/root` 已 chmod 711（swe 用户需穿越到 `/root/autodl-tmp`）；容器 CapEff 无 SYS_ADMIN/NET_ADMIN/SYS_PTRACE |

## 已完成（全部验证过）

1. **SSH 免密**：本地 `id_ed25519` 公钥装入 remote `authorized_keys`；`Host seeta41` 别名写入本地 `~/.ssh/config`。
2. **代码同步**：`/root/autodl-tmp/DPswarm`（modelbench 源码 + `official/` 评分工件 + dpswarm-plugin + keys.local.json；**未含** `pilot_v*` 历史批次、`.git`、team_eval 的 TeamBench 历史）。首次打包漏了 `team_eval20260903` 代码（gate 哈希需要 `transports.py`），已补齐。
3. **Python**：`/root/miniconda3` 3.12.3 + pytest；另装系统 python3.10 + udocker（swe 用户用）。
4. **测试全绿（Linux）**：dpswarm-plugin `363 passed`；`modelbench/swe_fixed_team_20260903/tests` `52 passed`。
5. **codex**：0.149.0 musl 静态二进制（GitHub release `rust-v0.149.0`）→ `/usr/local/bin/codex`；`~/.codex/{auth.json,config.toml}` 自本地 `D:\codex-home` 同步。
6. **GLM/DeepSeek**：`keys.local.json` 已同步，两端点从远程可达（GLM `https://open.bigmodel.cn/api/coding/paas/v4`；DeepSeek `https://api.deepseek.com`）。
7. **网络**：Docker Hub 直连不通；可用镜像源 docker.m.daocloud.io / docker.1ms.run / dockerproxy.net / hub.rat.dev；`source /etc/network_turbo` 可加速 github/HF（对其他源反而更慢）。

## Docker 三连败（证据，2026-09-04 实测）

1. dockerd 默认启动：`iptables -t nat -N DOCKER: Permission denied`（无 NET_ADMIN）。
2. dockerd `--iptables=false --bridge=none --storage-driver vfs`：**daemon 本体能起来**，但拉镜像解层 `failed to register layer: unshare: operation not permitted`（seccomp 禁 CLONE_NEWNS，root 也不行）。
3. rootless 路线：`unshare -Ur` EPERM（禁 CLONE_NEWUSER）→ rootless docker/podman 全部不可行。

**为什么 udocker/proot 也救不了评分**：`environment.py` 评分是两级架构——宿主 `docker create/start` 控制器容器时挂载 `/var/run/docker.sock`，容器内 `_grade` 用 `docker.from_env()` 连 socket、由官方 `run_instance` 拉起 `sweb.eval.*` 实例容器。无守护进程 = 无 socket = 官方评分链路不成立。且 `--memory/--cpus/--cap-drop/--network none` 等隔离约束在 proot 方案下均不可强制，与冻结的评分契约不等价。

## 已修的跨平台问题

- `dpswarm-plugin/tests/test_mechanisms2.py::TestTxnEnvelope::test_second_writer_cross_process_blocked` 原来用 Windows 路径拼接（`{tmp_path}\\e.jsonl`），Linux 下锁到另一个文件必然失败。已改为 `repr(str(tmp_path / "e.jsonl"))` 嵌入，本地 Win + 远程 Linux 双端通过。属测试可移植性修复，未触碰任何 runtime source。

## 操作速查

```bash
ssh seeta41                                            # 连接
# 插件测试
cd /root/autodl-tmp/DPswarm/dpswarm-plugin && /root/miniconda3/bin/python -m pytest tests -q
# harness 测试
cd /root/autodl-tmp/DPswarm && /root/miniconda3/bin/python -m pytest modelbench/swe_fixed_team_20260903/tests -q
# codex
codex --version
# udocker（swe 用户，数据目录已迁数据盘）
su - swe   # export UDOCKER_DIR=/root/autodl-tmp/udocker 已写入 ~/.profile
udocker pull docker.m.daocloud.io/library/python:3.11-slim
```

## 待决策与后续

1. **架构决策（用户）**：
   - A. 双平面分离：AutoDL 跑 agent 并发（真正需要规模的平面），grading 留本地 Docker Desktop / 另找 docker VPS。需要补跨机结果回流脚本。
   - B. 坚持单机：AutoDL 无法满足，需换 docker-capable 平台（云 VPS + GPU 或特权容器服务）。
2. codex 认证冒烟（`codex login status` / 最小 exec）——二进制下载完成即做（见下）。
3. udocker alpine/python:3.11-slim 运行验证 —— 只影响"未来去 docker 化重构"的可行性，不影响上述架构结论。

## 环境准备期最终状态（2026-09-04 14:15）

- **codex：完成。** `/usr/local/bin/codex` = codex-cli 0.149.0，`codex login status` → Logged in using ChatGPT。agent 平面（codex + GLM + DeepSeek）全部就绪。
- **udocker 工具包：仍在后台下载**（源 download.a.incd.pt 不稳，代理在整 MiB 边界断连，udocker 自带重试；进程 detached，不受 ssh 断开影响）。验证方法：
  ```bash
  ssh seeta41 'ls /root/autodl-tmp/udocker/bin | wc -l'   # >0 即完成
  # 完成后跑 alpine 验证（swe 用户）：
  ssh seeta41 'su - swe -c "udocker pull docker.m.daocloud.io/library/alpine:latest && \
    cid=\$(udocker create --name=smoke docker.m.daocloud.io/library/alpine:latest) && \
    udocker run smoke echo CONTAINER_EXEC_OK"'
  ```
  注意：即便 alpine 验证通过，也**不改变** docker.sock 两级评分架构在此平台不可行的结论（见上）。
- 无卡模式 0.5 核是下载慢的主因；有卡启动后网络相同但 CPU 不再是瓶颈。
- 本地 `C:\Users\93711\AppData\Local\Temp\pytest-of-93711` 被本地实验占用，本地验证测试请用 `--basetemp` 独立目录。
- 大文件从 GitHub 下载时可用 `source /etc/network_turbo` 加速（代理仍会断连，配 `curl -C -` 断点续传循环）；非 GitHub/HF 源开了 turbo 反而更慢。
