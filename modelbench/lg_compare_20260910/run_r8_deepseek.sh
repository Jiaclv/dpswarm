#!/usr/bin/env bash
# r8 完整轮（DeepSeek Flash）：6 task × 2 臂 = 12 run，并行 4 路执行 + 收尾分析。
set -u
cd "$(dirname "$0")"
export EXP_PROVIDER=deepseek
export EXP_MODEL=deepseek-flash
LOG=r8-progress.log
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

: > "$LOG"
RUNS=""
for t in parallel serial semantic longdoc hiddenreq direct; do
  for a in classic lg; do RUNS="$RUNS $a-$t"; done
done

echo "$RUNS" | tr ' ' '\n' | grep -v '^$' | xargs -P 4 -I{} bash -c '
  echo "$(date "+%Y-%m-%d %H:%M:%S") [r8] {} started" >> r8-progress.log
  python compare.py --tag r8 {} > "r8-{}.out" 2>&1
  rc=$?
  echo "$(date "+%Y-%m-%d %H:%M:%S") [r8] {} done rc=$rc" >> r8-progress.log
'

echo "$(stamp) [r8] all runs finished, analyze" >> "$LOG"
python compare.py --tag r8 --analyze-only >> r8-analyze.out 2>&1
echo "$(stamp) [r8] analyze done rc=$?" >> "$LOG"
