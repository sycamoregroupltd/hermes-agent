#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
set -euo pipefail

# Fix: ensure bun + PG are in PATH
export PATH="$HOME/.bun/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Source secrets from the running container env
SYCODE_READ_TOKEN=$(docker exec sycodetrading-server sh -c 'echo "${SYCODE_READ_TOKEN}"' 2>/dev/null || echo "")
SYCODE_TRADE_TOKEN=$(docker exec sycodetrading-server sh -c 'echo "${SYCODE_TRADE_TOKEN}"' 2>/dev/null || echo "")
PGPASSWORD=$(docker exec sycodetrading-supabase-db sh -c 'echo "${POSTGRES_PASSWORD}"' 2>/dev/null || echo "${PGPASSWORD:-}")

REPO=/home/frank/sycode-trading
REPORT_DIR="$REPO/reports/strategy-promotion-funnel"
mkdir -p "$REPORT_DIR"
REPORT="$REPORT_DIR/latest.md"
JSON_REPORT="$REPORT_DIR/latest.json"
PERFORMANCE_DIR="/home/frank/obsidian/quant-team/strategy-performance"
mkdir -p "$PERFORMANCE_DIR"

cd "$REPO"

# ---- 1. Server health ----
# curl -w prints 000 itself on connect failure; a || echo fallback would double it
# into 000000 (leading-zero invalid JSON — broke the 2026-08-01 run).
SERVER_OK=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:3001/ready 2>/dev/null || true)
SERVER_OK=${SERVER_OK:-000}

# Emit a number only if the value is numeric; unknown becomes null, never a fabricated 0.
num_or_null() { [[ "${1:-}" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && printf '%s' "$1" || printf 'null'; }

# ---- 2. Signal pipeline (24h) ----
PG_QUERY="docker exec sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -F'|'"

NEW_JOURNEYS=$($PG_QUERY -c "SELECT count(*) FROM signal_journeys WHERE created_at > now() - interval '24h'" 2>/dev/null || echo "N/A")
OPEN_POSITIONS=$($PG_QUERY -c "SELECT count(*) FROM managed_positions WHERE status='open'" 2>/dev/null || echo "N/A")
CLEAN_CLOSES=$($PG_QUERY -c "SELECT count(*) FROM managed_positions WHERE status='closed' AND close_reason NOT IN ('stop_loss','kill_switch') AND updated_at > now() - interval '24h'" 2>/dev/null || echo "N/A")
PNL_24H=$($PG_QUERY -c "SELECT COALESCE(SUM(realized_pnl), 0) FROM managed_positions WHERE status='closed' AND updated_at > now() - interval '24h'" 2>/dev/null || echo "N/A")

# ---- 3. Strategy performance (7d) ----
STRAT_PERF=$($PG_QUERY -c "
SELECT COALESCE(NULLIF(sp.name, ''), 'no-strategy') AS name,
       COUNT(mp.id) AS trades,
       SUM(CASE WHEN mp.status='closed' AND mp.close_reason NOT IN ('stop_loss','kill_switch') THEN 1 ELSE 0 END) AS wins,
       COALESCE(SUM(mp.realized_pnl), 0) AS total_pnl
FROM managed_positions mp
LEFT JOIN signal_journeys sj ON sj.id::text = mp.signal_id
LEFT JOIN strategy_pool sp ON sp.id::text = sj.strategy_id
WHERE mp.updated_at > now() - interval '7d'
GROUP BY sp.name
ORDER BY total_pnl DESC
LIMIT 20" 2>/dev/null || echo "N/A")

# ---- 3b. Clean canonical cohort progress by strategy/arm ----
CLEAN_COHORT_TARGET=300
CLEAN_COHORT_PROGRESS=$($PG_QUERY -c "
WITH clean AS (
  SELECT
    COALESCE(
      NULLIF(ti.source_signal->'meta'->'canonical_outcomes_v2_lineage'->>'arm_id', ''),
      NULLIF(ti.source_signal->'meta'->>'arm_id', ''),
      NULLIF(ti.strategy_name, ''),
      NULLIF(c.signal_time_features->>'model_version', ''),
      concat_ws('_', c.direction, c.timeframe),
      'unattributed'
    ) AS arm_id,
    COUNT(*)::int AS n,
    MIN(c.signal_time) AS first_signal_time,
    MAX(c.signal_time) AS last_signal_time
  FROM canonical_outcomes_v2 c
  LEFT JOIN trade_intents ti ON ti.correlation_id = c.correlation_id
  WHERE c.has_signal_features = true
    AND c.is_win_net IS NOT NULL
  GROUP BY 1
), paced AS (
  SELECT
    arm_id,
    n,
    first_signal_time,
    last_signal_time,
    GREATEST(${CLEAN_COHORT_TARGET} - n, 0) AS remaining,
    EXTRACT(EPOCH FROM (last_signal_time - first_signal_time)) / 86400.0 AS observed_days
  FROM clean
)
SELECT
  '- ' || arm_id || ': ' || n || ' / ${CLEAN_COHORT_TARGET}' ||
  ' clean outcomes; remaining=' || remaining || '; ' ||
  CASE
    WHEN n >= ${CLEAN_COHORT_TARGET} THEN 'target reached'
    WHEN n < 2 THEN 'ETA unavailable: need at least 2 resolved clean outcomes to estimate pace'
    WHEN observed_days <= 0 THEN 'ETA unavailable: clean outcomes share the same signal_time'
    ELSE 'ETA ' || to_char(
      now() + ((remaining / NULLIF(n / observed_days, 0)) * interval '1 day'),
      'YYYY-MM-DD'
    ) || ' at ' || round((n / observed_days)::numeric, 2) || '/day observed pace'
  END AS clean_cohort_progress
FROM paced
ORDER BY n DESC, arm_id ASC
LIMIT 25" 2>/dev/null || echo "N/A")

# ---- 4. OpenClaw API status ----
OPENCLAW_STATUS=$(curl -s -H "X-Sycode-Token: ${SYCODE_READ_TOKEN:-}" http://localhost:3001/api/openclaw/status 2>/dev/null || echo '{"error":"unreachable"}')

# ---- 5. Auto-trader status ----
AUTO_TRADER=$(curl -s http://localhost:3001/api/auto-trader/status 2>/dev/null || echo '{"error":"unreachable"}')

# ---- 6. Docker fleet health ----
FLEET=$(docker ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null)

# ---- Generate JSON report ----
cat > "$JSON_REPORT" << JSONEOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "server_ready": "$SERVER_OK",
  "pipeline": {
    "new_journeys_24h": $(num_or_null "${NEW_JOURNEYS:-}"),
    "open_positions": $(num_or_null "${OPEN_POSITIONS:-}"),
    "clean_closes_24h": $(num_or_null "${CLEAN_CLOSES:-}"),
    "pnl_24h": $(num_or_null "${PNL_24H:-}")
  },
  "openclaw_status": $(echo "$OPENCLAW_STATUS" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)))" 2>/dev/null || echo '"unreachable"'),
  "auto_trader": $(echo "$AUTO_TRADER" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)))" 2>/dev/null || echo '"unreachable"'),
  "named_consumer": "elon-governor-sweep gate evidence: read latest.json as paper-only strategy-promotion funnel status before claiming readiness."
}
JSONEOF

# ---- Generate Markdown report ----
cat > "$REPORT" << MDEOF
# Strategy Promotion Funnel Report
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

## Server Health
- /ready endpoint: $SERVER_OK
- sycodetrading-server: $(docker ps --filter name=sycodetrading-server --format '{{.Status}}' 2>/dev/null || echo 'N/A')

## Signal Pipeline (24h)
- New signal journeys: ${NEW_JOURNEYS}
- Open positions: ${OPEN_POSITIONS}
- Clean trade closes (24h): ${CLEAN_CLOSES}
- PnL (24h): ${PNL_24H}

## Strategy Performance (7d)
\`\`\`
${STRAT_PERF}
\`\`\`

## Clean Cohort Progress (canonical_outcomes_v2)
Target per arm: ${CLEAN_COHORT_TARGET} resolved clean outcomes. Query source: canonical_outcomes_v2 with has_signal_features=true and is_win_net resolved; arm identity uses signal-time lineage when present, falling back to strategy name/model/timeframe.

\`\`\`
${CLEAN_COHORT_PROGRESS}
\`\`\`

## Named JSON Consumer
- elon-governor-sweep gate evidence reads \`/home/frank/sycode-trading/reports/strategy-promotion-funnel/latest.json\` as the paper-only funnel status before claiming readiness.

## OpenClaw Status
\`\`\`json
$(echo "$OPENCLAW_STATUS" | python3 -m json.tool 2>/dev/null || echo 'unreachable')
\`\`\`

## Auto-Trader Status
\`\`\`json
$(echo "$AUTO_TRADER" | python3 -m json.tool 2>/dev/null || echo 'unreachable')
\`\`\`

## Docker Fleet
\`\`\`
${FLEET}
\`\`\`
MDEOF

DATED_JSON="$PERFORMANCE_DIR/strategy-promotion-funnel-$(date -u +%Y-%m-%d).json"
PYTHONPATH=/home/frank/.hermes/scripts /usr/bin/python3 -c '
import json, sys
from second_brain_writer import write_json_atomic
write_json_atomic(sys.argv[2], json.load(open(sys.argv[1], encoding="utf-8")))
' "$JSON_REPORT" "$DATED_JSON"

# Final output for cron delivery
echo "=== Strategy Promotion Funnel Report ==="
echo "Server: $SERVER_OK | Journeys(24h): $NEW_JOURNEYS | Open: $OPEN_POSITIONS | Closes: $CLEAN_CLOSES | PnL(24h): $PNL_24H"
echo "Clean cohort target: ${CLEAN_COHORT_TARGET} per arm"
echo "$CLEAN_COHORT_PROGRESS" | head -5
echo "Report saved to $REPORT"
echo "JSON mirror saved to $DATED_JSON"
