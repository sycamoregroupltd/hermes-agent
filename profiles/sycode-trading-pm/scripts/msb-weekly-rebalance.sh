#!/bin/bash
# Weekly Multi-Sleeve Book Rebalance
# Runs every Monday 09:30 UTC
# Output: /tmp/msb-rebalance-YYYY-MM-DD.json
# Log: /tmp/msb-rebalance.log
set -euo pipefail

DATE=$(date -u '+%Y-%m-%d')
OUTPUT="/tmp/msb-rebalance-${DATE}.json"
LOG="/tmp/msb-rebalance.log"

cd /home/frank/sycode-trading
/home/frank/.hermes/venvs/trading-ml/bin/python \
  execution/multi_sleeve_book.py \
  --mode=rebalance \
  --days=365 \
  --output="${OUTPUT}" \
  2>&1 | tee -a "${LOG}"

RC=$?
if [ $RC -eq 0 ]; then
    echo "OK: rebalance output at ${OUTPUT}"
    # Also write a status line
    grep -c 'ann_vol\|weights\|sharpe' "${OUTPUT}" > /dev/null 2>&1 && echo "VALID OUTPUT" >> "${LOG}"
else
    echo "FAIL: exit code $RC" >> "${LOG}"
fi

exit $RC
