#!/usr/bin/env bash
# r8g 第二批（classic-semantic / lg-serial）：接在第一批空出的并发槽位上跑。
# 只启这两个 run；全部 6 run 齐了再做收尾分析。
set -u
cd "$(dirname "$0")"
export EXP_PROVIDER=glm
export EXP_MODEL=glm-5.3-flash
LOG=r8g-progress.log
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

for r in classic-semantic lg-serial; do
  (
    echo "$(stamp) [r8g] $r started" >> "$LOG"
    python compare.py --tag r8g "$r" > "r8g-$r.out" 2>&1
    echo "$(stamp) [r8g] $r done rc=$?" >> "$LOG"
  ) &
done
wait

# 等第一批的 lg-longdoc（可能还在跑）：轮询它是否出 outcome
for _ in $(seq 1 180); do
  [ -f runs-r8g/lg-longdoc/outcome.json ] && break
  sleep 30
done
echo "$(stamp) [r8g] batch2 结束，lg-longdoc outcome=$([ -f runs-r8g/lg-longdoc/outcome.json ] && echo 有 || echo 无)" >> "$LOG"
python compare.py --tag r8g --analyze-only >> "r8g-analyze.out" 2>&1
echo "$(stamp) [r8g] analyze done rc=$?" >> "$LOG"
