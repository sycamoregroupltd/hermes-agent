#!/usr/bin/env bash
# SHIM — approved exec wrapper (t_bce90116). Canonical source is
# ~/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py
# Consumer: node-exporter textfile
#   /home/frank/sycode-trading/monitoring/node-exporter-textfile/sycode_db_latency_slo.prom
# read by Prometheus job node + sycode-db-slo-alert-bridge (d7331151ddf8)
# which files [host-alert] cards on the sycode-trading board.
#
# Wall is 75s (live 24h p50 ~36.6s, max 108s) still << planned 15m cadence.
# Do NOT 30s-kill: GNU timeout 124 used to leave a stale last-success .prom
# (fail-open). TERM/timeout overlays collector_success=0 + last_run onto the
# previous FULL textfile so journeys/oltp/cache/candles stay present for
# db-slo-alerts.yml (those rules do NOT key collector_success). A 2-series
# stub wipe can --resolve a firing SLO card; overlay must not.
set -uo pipefail
LOCK_DIR="${SYCODE_OLTP_LOCK_DIR:-/home/frank/.hermes/profiles/trading-devops/cron/state}"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
CANONICAL="${SYCODE_DB_SLO_COLLECTOR_PY:-/home/frank/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py}"
PROM="${SYCODE_DB_SLO_PROM:-/home/frank/sycode-trading/monitoring/node-exporter-textfile/sycode_db_latency_slo.prom}"
WALL="${SYCODE_DB_SLO_WALL_S:-75}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }

# Python collector failure-shaped file (all Prom rule series present, success=0).
# Used only when there is no previous full textfile to overlay.
emit_full_failure_shape() {
  local dest="$1" now="$2"
  cat >"$dest" <<EOF
# HELP sycode_db_collector_success 1 when the DB SLO textfile collector succeeded.
# TYPE sycode_db_collector_success gauge
# HELP sycode_db_collector_last_run_timestamp Unix timestamp of the last collector run.
# TYPE sycode_db_collector_last_run_timestamp gauge
# HELP sycode_db_cache_hit_ratio Postgres cache hit ratio (0-1), SLO > 0.95.
# TYPE sycode_db_cache_hit_ratio gauge
# HELP sycode_db_dead_tup_ratio Dead tuple ratio of user tables, warning > 0.20.
# TYPE sycode_db_dead_tup_ratio gauge
# HELP sycode_db_oltp_p95_ms OLTP hot-path p95 latency estimate (ms), SLO < 10ms.
# TYPE sycode_db_oltp_p95_ms gauge
# HELP sycode_db_monitoring_p95_ms Monitoring/analytics p95 latency estimate (ms), SLO < 500ms.
# TYPE sycode_db_monitoring_p95_ms gauge
# HELP sycode_db_statement_cancel_recent Count of statement cancels in recent postgres log tail.
# TYPE sycode_db_statement_cancel_recent gauge
# HELP sycode_db_signal_journeys_30m Count of signal_journeys triggered in the last 30 minutes.
# TYPE sycode_db_signal_journeys_30m gauge
# HELP sycode_db_candle_staleness_seconds Age (s) of the newest candle per timeframe.
# TYPE sycode_db_candle_staleness_seconds gauge
# HELP sycode_db_candle_staleness_slo_seconds SLO max-age threshold per timeframe (s).
# TYPE sycode_db_candle_staleness_slo_seconds gauge
sycode_db_cache_hit_ratio 0
sycode_db_dead_tup_ratio 0
sycode_db_oltp_p95_ms 0
sycode_db_monitoring_p95_ms 0
sycode_db_signal_journeys_30m 0
sycode_db_candle_staleness_seconds{timeframe="1m"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="1m"} 90
sycode_db_candle_staleness_seconds{timeframe="5m"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="5m"} 360
sycode_db_candle_staleness_seconds{timeframe="15m"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="15m"} 1200
sycode_db_candle_staleness_seconds{timeframe="1h"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="1h"} 5400
sycode_db_candle_staleness_seconds{timeframe="4h"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="4h"} 21600
sycode_db_candle_staleness_seconds{timeframe="1D"} -1
sycode_db_candle_staleness_slo_seconds{timeframe="1D"} 129600
sycode_db_statement_cancel_recent 0
sycode_db_collector_success 0
sycode_db_collector_last_run_timestamp ${now}
# fail-closed full failure shape by trading-devops shim t_bce90116 (no previous SLO series to overlay)
EOF
}

write_fail_closed() {
  local now tmp dir
  now="$(date +%s)"
  dir="$(dirname "$PROM")"
  mkdir -p "$dir"
  tmp="$PROM.tmp.$$"
  # Overlay success=0 + last_run onto the previous full file so Prom rules that
  # key journeys/oltp/cache/candles (not collector_success) cannot fail-open
  # or --resolve. Only emit the python failure shape when there is no prior
  # SLO series to keep.
  if [ -s "$PROM" ] && grep -q '^sycode_db_signal_journeys_30m' "$PROM"; then
    if ! awk -v now="$now" '
      BEGIN { seen_s=0; seen_t=0 }
      /^sycode_db_collector_success([ {]|$)/ {
        print "sycode_db_collector_success 0"
        seen_s=1
        next
      }
      /^sycode_db_collector_last_run_timestamp([ {]|$)/ {
        print "sycode_db_collector_last_run_timestamp " now
        seen_t=1
        next
      }
      { print }
      END {
        if (!seen_s) print "sycode_db_collector_success 0"
        if (!seen_t) print "sycode_db_collector_last_run_timestamp " now
        print "# fail-closed overlay by trading-devops shim t_bce90116 (TERM/timeout; keep SLO series)"
      }
    ' "$PROM" >"$tmp"; then
      rm -f "$tmp"
      emit_full_failure_shape "$tmp" "$now"
    fi
  else
    emit_full_failure_shape "$tmp" "$now"
  fi
  mv -f "$tmp" "$PROM"
}

on_term() {
  write_fail_closed
  printf '%s\n' 'OLTP_PROBE_TIMEOUT collector_success=0 overlay (TERM)' >&2
  exit 124
}
trap on_term TERM INT

timeout --kill-after=5s "${WALL}s" python3 "$CANONICAL"
rc=$?
trap - TERM INT
if [ "$rc" -ne 0 ]; then
  write_fail_closed
  printf '%s\n' "OLTP_PROBE_FAIL rc=$rc collector_success=0 overlay (not a clean skip)" >&2
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    exit 124
  fi
  exit "$rc"
fi
exit 0
