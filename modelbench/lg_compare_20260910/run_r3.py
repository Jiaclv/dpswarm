"""r3 轮次驱动：逐 run 子进程执行（单 run 45 分钟硬上限，超时记
timeout-partial 继续下一个），进度经 compare.py 写 r3-progress.log。

用法：python run_r3.py（后台执行；结束后自动跑 --analyze-only 汇总）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = ["classic-serial", "lg-serial", "classic-semantic", "lg-semantic"]
RUN_TIMEOUT = 45 * 60   # 单 run 45 分钟硬上限（纪律条款）


def progress(msg: str) -> None:
    line = "%s [r3-driver] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(HERE / "r3-progress.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def done(run_id: str) -> bool:
    return (HERE / "runs-r3" / run_id / "outcome.json").exists()


def main() -> None:
    for run_id in RUNS:
        if done(run_id):
            progress(f"{run_id} reused (outcome.json 已在)")
            continue
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "compare.py", "--tag", "r3", run_id],
                cwd=str(HERE), timeout=RUN_TIMEOUT)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            progress(f"{run_id} timeout-partial（>{RUN_TIMEOUT // 60}min，已杀；"
                     "残留由 compare.py 幂等清理）")
            continue
        wall = time.time() - t0
        if code != 0:
            progress(f"{run_id} failed exit={code} wall={wall:.0f}s（如实记录，继续）")
    subprocess.run([sys.executable, "-X", "utf8", "compare.py", "--tag", "r3",
                    "--analyze-only"], cwd=str(HERE))
    progress("all done")


if __name__ == "__main__":
    main()
