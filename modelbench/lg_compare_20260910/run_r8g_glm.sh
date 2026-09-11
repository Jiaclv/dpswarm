#!/usr/bin/env bash
# r8g（GLM-5.3-Flash 轮）：机制对照臂，4 路并发执行 6 run + 收尾分析。
# 与 r8（DeepSeek Flash 跨模型泛化轮）分开目录，互不覆盖。
set -u
cd "$(dirname "$0")"
export EXP_PROVIDER=glm
export EXP_MODEL=glm-5.3-flash
LOG=r8g-progress.log
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

RUNS="lg-direct lg-hiddenreq lg-longdoc lg-semantic classic-semantic lg-serial"
N=4
i=0
for r in $RUNS; do
  (
    echo "$(stamp) [r8g] $r started" >> "$LOG"
    python compare.py --tag r8g "$r" > "r8g-$r.out" 2>&1
    echo "$(stamp) [r8g] $r done rc=$?" >> "$LOG"
  ) &
  i=$((i + 1))
  if [ $((i % N)) -eq 0 ]; then wait; fi
done
wait
echo "$(stamp) [r8g] all runs finished, analyze" >> "$LOG"
python compare.py --tag r8g --analyze-only >> "r8g-analyze.out" 2>&1
echo "$(stamp) [r8g] analyze done rc=$?" >> "$LOG"
