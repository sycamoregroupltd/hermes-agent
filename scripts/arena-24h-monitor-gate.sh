#!/bin/bash
# Arena ML activation 24h monitor — wakeAgent gate
# Run by cron to detect rollback triggers and schedule rechecks
# Emits {"wakeAgent": true/false} based on rollback trigger detection
# Fail-open: if anything errors, emit {"wakeAgent": false}

set -o pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="/home/frank/.hermes/kanban/boards/sycode-trading/workspaces/t_aadefb79"

# Fail-open wrapper: errors return false (safe)
emit_false() {
  echo '{"wakeAgent":false}'
  exit 0
}

# Check runtime health first
READY=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001/ready 2>/dev/null || echo "000")
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001/health 2>/dev/null || echo "000")

# If unhealthy, wake agent (critical)
if [ "$READY" != "200" ] || [ "$HEALTH" != "200" ]; then
  echo '{"wakeAgent":true,"reason":"Runtime unhealthy: ready='"$READY"' health='"$HEALTH"'"}'
  exit 0
fi

# Check for live trade intents in last 24h (rollback trigger #1)
LIVE_INTENTS=$(PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE created_at >= NOW() - INTERVAL '24 hours'
    AND trading_mode = 'live';
" 2>/dev/null | tr -d '[:space:]') || emit_false
LIVE_INTENTS=${LIVE_INTENTS:-0}

if [ "$LIVE_INTENTS" -gt 0 ] 2>/dev/null; then
  echo '{"wakeAgent":true,"reason":"LIVE TRADE INTENTS DETECTED: '"$LIVE_INTENTS"' — ROLLBACK TRIGGER"}'
  exit 0
fi

# Check for arena_ml_closed_loop strategies with liveTradingApproved=true (rollback trigger #4)
PROMOTED=$(PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM strategies
  WHERE meta->>'source' = 'arena_ml_closed_loop'
    AND (meta->>'liveTradingApproved' = 'true' OR meta->>'tradeIntentGenerationApproved' = 'true');
" 2>/dev/null | tr -d '[:space:]') || emit_false
PROMOTED=${PROMOTED:-0}

if [ "$PROMOTED" -gt 0 ] 2>/dev/null; then
  echo '{"wakeAgent":true,"reason":"CLOSED-LOOP STRATEGY PROMOTED TO LIVE: '"$PROMOTED"' — ROLLBACK TRIGGER"}'
  exit 0
fi

# All checks passed — no wake needed
echo '{"wakeAgent":false}'
