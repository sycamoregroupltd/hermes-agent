#!/bin/bash
# Signal Fusion Fill Rate Check
# Runs hourly to check if new signals have been created with fusion enrichment.
# Only reports when there's meaningful data to report.

DB_CONTAINER="sycodetrading-supabase-db"
SERVER_DEPLOY_AT="2026-07-05T12:13:00Z"

# Check signals created since deploy
NEW_SIGNALS=$(docker exec $DB_CONTAINER psql -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM signal_journeys 
  WHERE created_at >= '$SERVER_DEPLOY_AT';
")

if [ "$NEW_SIGNALS" -gt 0 ] 2>/dev/null; then
  # We have signals — report fill rates
  docker exec $DB_CONTAINER psql -U postgres -d postgres -c "
  SELECT 
    'conviction_score' as field,
    COUNT(*) as total,
    COUNT(*) FILTER (WHERE conviction_score IS NOT NULL) as filled,
    ROUND(100.0 * COUNT(*) FILTER (WHERE conviction_score IS NOT NULL) / NULLIF(COUNT(*), 0), 2) as fill_pct
  FROM signal_journeys 
  WHERE created_at >= '$SERVER_DEPLOY_AT'
  UNION ALL
  SELECT 
    'composite_confidence_score',
    COUNT(*),
    COUNT(*) FILTER (WHERE composite_confidence_score IS NOT NULL),
    ROUND(100.0 * COUNT(*) FILTER (WHERE composite_confidence_score IS NOT NULL) / NULLIF(COUNT(*), 0), 2)
  FROM signal_journeys 
  WHERE created_at >= '$SERVER_DEPLOY_AT'
  UNION ALL
  SELECT 
    'signal_fusion_metadata',
    COUNT(*),
    COUNT(*) FILTER (WHERE signal_fusion_metadata IS NOT NULL),
    ROUND(100.0 * COUNT(*) FILTER (WHERE signal_fusion_metadata IS NOT NULL) / NULLIF(COUNT(*), 0), 2)
  FROM signal_journeys 
  WHERE created_at >= '$SERVER_DEPLOY_AT';
  "
  echo ""
  echo "=== ACCEPTANCE: >=80% fill rate on signal_fusion_metadata ==="
else
  echo "No new signals created since deploy ($SERVER_DEPLOY_AT). Last signal at:"
  docker exec $DB_CONTAINER psql -U postgres -d postgres -t -A -c "
    SELECT MAX(created_at) FROM signal_journeys;
  "
fi
