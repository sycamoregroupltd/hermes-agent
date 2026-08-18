#!/usr/bin/env bash
# catalyst-feed freshness watchdog (card t_52eff885, 2026-08-16).
# no-agent cron contract: exit 0 = healthy, NONZERO exit = the alert
# (stdout is never parsed on success paths — the exit code is the signal).
set -u
DB="${CATALYST_DB_OVERRIDE:-/home/frank/sycode-feeds/catalyst-feed/catalyst_events.db}"
MAX_AGE_S=$((3*3600))

if [ ! -s "$DB" ]; then
  echo "catalyst-feed DEAD: $DB missing or zero bytes"
  exit 1
fi
LAST=$(sqlite3 "file:$DB?mode=ro" "SELECT strftime('%s', MAX(ingested_at)) FROM catalyst_events" 2>&1) || { echo "catalyst-feed DEAD: query failed: $LAST"; exit 1; }
[ -n "$LAST" ] && [ "$LAST" != "" ] || { echo "catalyst-feed DEAD: zero rows"; exit 1; }
AGE=$(( $(date +%s) - LAST ))
if [ "$AGE" -gt "$MAX_AGE_S" ]; then
  echo "catalyst-feed STALE: newest ingested_at is ${AGE}s old (max ${MAX_AGE_S}s) — check catalyst-feed.timer"
  exit 1
fi
# success is SILENT (no-agent cron delivers stdout verbatim; hourly "ok" = noise)
exit 0
