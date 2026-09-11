"""通用实验轮驱动：逐 run 子进程执行（单 run 45 分钟硬上限，超时记
timeout-partial 继续下一个），进度经 compare.py 写 <tag>-progress.log。

用法：python run_round.py <tag> [run_id ...]
  例：python run_round.py r4 classic-serial lg-serial classic-semantic lg-semantic
  run_id 缺省 = 全部 task × 两臂。结束后自动 --analyze-only 汇总。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 单 run 硬上限（纪律条款）；可用环境变量 RUN_TIMEOUT_MIN 覆盖（r5 瘦身轮用 30）
RUN_TIMEOUT = int(float(os.environ.get("RUN_TIMEOUT_MIN", "45")) * 60)


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "r4"
    runs = sys.argv[2:] or []
    progress_file = HERE / f"{tag}-progress.log"

    def progress(msg: str) -> None:
        line = "%s [%s-driver] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg)
        with open(progress_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    if not runs:
        import compare
        runs = [f"{arm}-{task}" for task in compare.TASKS
                for arm in ("classic", "lg")]
    for run_id in runs:
        if (HERE / f"runs-{tag}" / run_id / "outcome.json").exists():
            progress(f"{run_id} reused (outcome.json 已在)")
            continue
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "compare.py", "--tag", tag, run_id],
                cwd=str(HERE), timeout=RUN_TIMEOUT)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            progress(f"{run_id} timeout-partial（>{RUN_TIMEOUT // 60}min，已杀；"
                     "残留由 compare.py 幂等清理）")
            continue
        wall = time.time() - t0
        if code != 0:
            progress(f"{run_id} failed exit={code} wall={wall:.0f}s（如实记录，继续）")
    subprocess.run([sys.executable, "-X", "utf8", "compare.py", "--tag", tag,
                    "--analyze-only"], cwd=str(HERE))
    progress("all done")


if __name__ == "__main__":
    main()
