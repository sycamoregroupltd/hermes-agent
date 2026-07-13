#!/bin/bash
# monitor-signal-pnl-orphans.sh
# Check orphan count in signal_pnl_points every 6h as part of the 24h FK plateau monitor
# Baseline: 193028 (captured 2026-07-04 ~17:45 UTC)

BASELINE=193028
DB="postgres"
HOST="localhost"
PORT="5432"
USER="postgres"

ORPHANS=$(PGPASSWORD=postgres psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -t -A <<'SQL'
SELECT COALESCE(SUM(c), 0)::text
FROM (
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20260601 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20260701 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20260801 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20260901 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20261001 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_p20261101 pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
  UNION ALL
  SELECT COUNT(*)::bigint AS c FROM signal_pnl_points_default pp
  WHERE NOT EXISTS (SELECT 1 FROM signal_journeys sj WHERE sj.id = pp.journey_id)
) sub;
SQL
)

DELTA=$(( ORPHANS - BASELINE ))
TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

if [ "$DELTA" -eq 0 ]; then
  echo "[$TIMESTAMP] ORPHAN MONITOR: count=$ORPHANS, delta=$DELTA — STABLE (no new orphans)"
  exit 0
elif [ "$DELTA" -gt 0 ]; then
  echo "[$TIMESTAMP] ORPHAN MONITOR WARNING: count=$ORPHANS, delta=+$DELTA — GROWING! FK not fully effective."
  exit 1
else
  echo "[$TIMESTAMP] ORPHAN MONITOR INFO: count=$ORPHANS, delta=$DELTA — DECREASING (orphans being cleaned up)"
  exit 0
fi
