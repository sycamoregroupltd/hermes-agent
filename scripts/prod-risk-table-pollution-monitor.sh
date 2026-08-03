#!/usr/bin/env bash
# prod-risk-table-pollution-monitor.sh
#
# Detects test-fixture writes into the PRODUCTION circuit_breaker_state row.
#
# WHY: on 2026-07-21 a test process wrote daily_pnl_usd = -500.00 into the live
# risk table; the breaker read |-500| >= dailyLossLimitUSD(500) and halted
# trading for a full day on a loss that never happened. Root cause: the DB
# client's no-config default (postgresql://...@127.0.0.1:5432/postgres) IS the
# production endpoint, so any test without DATABASE_URL silently connected to
# prod. Fixed at the connection boundary by testDatabaseGuard.ts (PR #795).
#
# This is the MONITOR half of that fix: the guard closes the known path, this
# catches any path it does not cover. A guard without a detector is a hope.
#
# Fixture tells (any one is sufficient):
#   - a bare base symbol in asset_losses (e.g. "BTC"); real positions are BTCUSDT
#   - portfolio_peak_usd = 0, or current_portfolio_usd <= 1000 (real base ~100k)
#   - daily_pnl_usd whose magnitude exceeds the day's actual closed P&L by >10x
#
# Exit 0 = clean, 1 = pollution detected (and alerted).
set -uo pipefail

PSQL="docker exec sycodetrading-supabase-db psql -U postgres -d postgres -tAc"
STATE_DIR=/home/frank/.hermes/state
STATE_FILE="$STATE_DIR/prod-risk-pollution.last"
ALERT_TARGET="${RISK_POLLUTION_ALERT_TARGET:-discord:#critical-alerts}"
mkdir -p "$STATE_DIR"

FINDINGS=$($PSQL "
with row as (
  select * from circuit_breaker_state where trading_date = current_date
),
actual as (
  select coalesce(sum(pnl_usd),0) real_pnl, count(*) n
  from trade_close_events where closed_at::date = current_date
)
select concat_ws(' | ',
  case when exists (
    select 1 from row, lateral jsonb_object_keys(row.asset_losses) k
    where k !~ '(USDT|USDC|USD|PERP)\$'
  ) then 'FIXTURE_SYMBOL in asset_losses' end,
  case when (select portfolio_peak_usd from row) = 0
       then 'portfolio_peak_usd=0' end,
  case when (select current_portfolio_usd from row) <= 1000
       then concat('current_portfolio_usd=', (select round(current_portfolio_usd,2) from row)) end,
  case when abs((select daily_pnl_usd from row)) >
            greatest(10 * abs((select real_pnl from actual)), 50)
       then concat('daily_pnl_usd=', (select round(daily_pnl_usd,2) from row),
                   ' vs real closes=', (select round(real_pnl,2) from actual),
                   ' over ', (select n from actual), ' trades') end
)
from row;" 2>/dev/null | sed 's/^ *//; s/ *$//')

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ -z "${FINDINGS:-}" ]; then
  echo "[$TS] CLEAN — no fixture signatures in prod circuit_breaker_state"
  exit 0
fi

echo "[$TS] POLLUTION DETECTED: $FINDINGS"

# Throttle: one alert per distinct finding-set per 6h.
KEY=$(printf '%s' "$FINDINGS" | md5sum | cut -c1-12)
NOW=$(date +%s)
LAST=$(grep -a "^$KEY=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2)
if [ -n "${LAST:-}" ] && [ $((NOW - LAST)) -lt 21600 ]; then
  echo "[$TS] SUPPRESSED key=$KEY (re-alert window)"
  exit 1
fi

BODY="Production risk table shows test-fixture values.

Findings: $FINDINGS

This is the mechanism that halted trading for a full day on 2026-07-21
(daily_pnl_usd=-500 with zero close events -> breaker tripped on a loss that
never happened). testDatabaseGuard.ts (PR #795) closes the known path; this
firing means either a path it does not cover, or the guard regressed.

Check:  docker exec sycodetrading-supabase-db psql -U postgres -d postgres \\
          -c \"select * from circuit_breaker_state where trading_date=current_date;\""

if hermes send -q --json -t "$ALERT_TARGET" -s "PROD RISK TABLE POLLUTED (test fixtures)" "$BODY" >/dev/null 2>&1; then
  echo "[$TS] ALERT-SENT target=$ALERT_TARGET key=$KEY"
  printf '%s=%s\n' "$KEY" "$NOW" >> "$STATE_FILE"
else
  echo "[$TS] ALERT-FAILED target=$ALERT_TARGET key=$KEY — check hermes gateway"
fi
exit 1
