#!/usr/bin/env bash
# Intent-writer liveness monitor (fable 2026-07-30, silent-failure doctrine).
# The 07-29 incident: trades executed all evening while trade_intents had
# ZERO new rows (writer bypassed) — lineage (bandit arm, model version) and
# execution_events (FK) silently degraded. This alerts when the intent writer
# is stale while the rest of the pipeline is demonstrably alive.
#
# DELIVERY (repointed 2026-08-01, kanban t_86527603): this used to write bare
# JSON into ~/.hermes/alert-spool/ — a dir with NO reader fleet-wide (black
# hole; 11 alerts sat undelivered Jul 30 → Aug 1). Now writes Alertmanager-
# webhook-shaped JSON into the PROVEN spool drained every minute by hermes
# cron abc411626232 (sycode_alertmanager_spool_drain.py -> discord:#critical-alerts).
# write_alert pattern copied from kanban-audit-chain-monitor.sh. Lessons baked in
# (alertmanager-spool-delivery-chain + alert-delivery-silent-failure):
#   - chmod 644 the spool file on EVERY write (root-owned-dir incident class);
#   - the throttle stamp is written ONLY AFTER a successful spool write —
#     a failed send must never arm its own throttle.
# Env overrides exist for RED-PATH TESTING ONLY (point at a scratch spool so a
# drill never posts a false alert to Discord).
# --test writes ONE clearly-labeled TEST alert through the same path (bypasses
# throttle and the DB checks).
set -u
SPOOL_DIR="${INTENT_LIVENESS_SPOOL_DIR:-/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming}"
STATE="${INTENT_LIVENESS_STATE:-/tmp/intent-writer-liveness.last}"
PSQL="docker exec sycodetrading-supabase-db psql -U postgres -d postgres -Atc"

write_alert() {  # $1 = severity, $2 = summary text
  local severity="$1" summary="$2"
  local ts file
  ts=$(date -u +%FT%TZ)
  file="$SPOOL_DIR/intent-writer-liveness-$(date +%s%3N).json"
  mkdir -p "$SPOOL_DIR" || return 1
  python3 - "$file" "$severity" "$summary" "$ts" <<'PYEOF' || return 1
import json, sys
file, severity, summary, ts = sys.argv[1:5]
payload = {
    "status": "firing",
    "alerts": [{
        "labels": {
            "alertname": "IntentWriterLiveness",
            "severity": severity,
            "job": "intent-writer-liveness",
        },
        "annotations": {"summary": summary},
        "startsAt": ts,
    }],
}
with open(file, "w") as f:
    json.dump(payload, f)
PYEOF
  # Lesson: the spool writer must chmod on every write or the drain can't read it.
  chmod 644 "$file" 2>/dev/null
  return 0
}

if [ "${1:-}" = "--test" ]; then
  write_alert "info" \
    "INTENT-WRITER-LIVENESS: TEST — deliberate test alert from intent-writer-liveness.sh --test (t_86527603 repoint verification); no staleness implied, ignore." \
    && echo "TEST alert written OK to $SPOOL_DIR" || { echo "TEST alert write FAILED"; exit 1; }
  exit 0
fi

INTENT_AGE_H=$($PSQL "select coalesce(extract(epoch from (now() - max(created_at)))/3600.0, 999) from trade_intents;" 2>/dev/null | cut -d. -f1)
SNAPS_2H=$($PSQL "select count(*) from decision_snapshots where created_at > now() - interval '2 hours';" 2>/dev/null)

[ -z "$INTENT_AGE_H" ] && exit 0   # DB unreachable — other monitors own that

# Alive pipeline (snapshots flowing) + stale intent writer = the incident signature.
if [ "${INTENT_AGE_H:-0}" -ge 3 ] && [ "${SNAPS_2H:-0}" -ge 50 ]; then
  # throttle: one alert per 6h — stamped ONLY after a successful spool write
  last=$(cat "$STATE" 2>/dev/null || echo 0)
  now=$(date +%s)
  if [ $((now - last)) -ge 21600 ]; then
    if write_alert "critical" \
      "trade_intents writer stale ${INTENT_AGE_H}h while pipeline alive (snapshots_2h=${SNAPS_2H}) — lineage + execution_events silently degrading (07-29 incident class)"; then
      echo "$now" > "$STATE"
    else
      echo "ERROR: alert spool write failed — throttle NOT armed, will retry next run"
    fi
  fi
fi
exit 0
