#!/bin/bash
# bridge-catalyst-to-market-news.sh
# Run the catalyst bridge script every cycle.
# Sends output to cron job delivery.
set -euo pipefail

cd /home/frank/sycode-trading

# Check if catalyst DB has any rows first
ROWS=$(sqlite3 reports/catalyst-feed/catalyst_events.db "SELECT COUNT(*) FROM catalyst_events WHERE classified_at IS NOT NULL AND url IS NOT NULL AND TRIM(url) <> ''" 2>/dev/null || echo "0")

if [ "$ROWS" -eq 0 ]; then
    echo "[catalyst-bridge] No classified rows to bridge (DB empty or pipeline not running)"
    exit 0
fi

echo "[catalyst-bridge] Found $ROWS classified rows — importing to market_news"
python3 scripts/bridge_catalyst_to_market_news.py --run
echo "[catalyst-bridge] Done"
