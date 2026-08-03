#!/usr/bin/env bash
# Microstructure P2 daily health-check monitor
# Runs from cron to verify the 7-day capture is healthy
set -euo pipefail

DATA_DIR="/home/frank/data/microstructure/raw"
LOG_FILE="$DATA_DIR/health-check.log"
WRAPPER_LOG="$DATA_DIR/collector-wrapper.log"

# Timestamp for this check
TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# 1. Process health — is the tmux session alive?
TMUX_OK=false
if tmux has-session -t microstructure-p2 2>/dev/null; then
    TMUX_OK=true
fi

# 2. Recent data growth — check new raw files in last 24h
BINANCE_NEW=$(find "$DATA_DIR" -name 'raw-binance-*.jsonl' -newer "$DATA_DIR" -mmin -1440 2>/dev/null | wc -l)
HL_NEW=$(find "$DATA_DIR" -name 'raw-hyperliquid-*.jsonl' -newer "$DATA_DIR" -mmin -1440 2>/dev/null | wc -l)

# 3. Latest raw file sizes
LATEST_BINANCE=$(ls -t "$DATA_DIR"/raw-binance-*.jsonl 2>/dev/null | head -1)
LATEST_HL=$(ls -t "$DATA_DIR"/raw-hyperliquid-*.jsonl 2>/dev/null | head -1)
BINANCE_SIZE="?"
HL_SIZE="?"
[ -n "$LATEST_BINANCE" ] && BINANCE_SIZE=$(stat --format=%s "$LATEST_BINANCE" 2>/dev/null || echo "?")
[ -n "$LATEST_HL" ] && HL_SIZE=$(stat --format=%s "$LATEST_HL" 2>/dev/null || echo "?")

# 4. DB row count — total tick_trades and last 24h
DB_TOTAL=$(docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -c "SELECT COUNT(*) FROM tick_trades;" 2>/dev/null || echo "DB_ERROR")
DB_24H=$(docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -c "SELECT COUNT(*) FROM tick_trades WHERE created_at > now() - interval '24 hours';" 2>/dev/null || echo "DB_ERROR")

# 5. Check for errors in wrapper log last 24h
WRAPPER_ERRORS="?"
if [ -f "$WRAPPER_LOG" ]; then
    WRAPPER_ERRORS=$(grep -c 'ERROR\|FAILED\|RETRY ALSO FAILED' "$WRAPPER_LOG" 2>/dev/null || echo "0")
fi

# 6. Segment progress — count completed segments
SEGMENTS_DONE=$(grep -c 'SEGMENT.*completed\|SEGMENT.*OK\|SEGMENT.*SUCCEEDED' "$WRAPPER_LOG" 2>/dev/null || echo "0")

# Output as JSON for cron delivery
echo "{
  \"timestamp\": \"$TS\",
  \"tmux_alive\": $TMUX_OK,
  \"binance_new_files_24h\": $BINANCE_NEW,
  \"hyperliquid_new_files_24h\": $HL_NEW,
  \"latest_binance_size_bytes\": $BINANCE_SIZE,
  \"latest_hyperliquid_size_bytes\": $HL_SIZE,
  \"db_total_rows\": \"$DB_TOTAL\",
  \"db_rows_last_24h\": \"$DB_24H\",
  \"wrapper_errors\": \"$WRAPPER_ERRORS\",
  \"segments_completed\": $SEGMENTS_DONE,
  \"healthy\": $(if [ "$TMUX_OK" = true ] && [ "$DB_TOTAL" != "DB_ERROR" ]; then echo true; else echo false; fi)
}"

# Append to log
echo "[$TS] tmux=$TMUX_OK seg=$SEGMENTS_DONE db_24h=$DB_24H errors=$WRAPPER_ERRORS binance_files=$BINANCE_NEW" >> "$LOG_FILE"
