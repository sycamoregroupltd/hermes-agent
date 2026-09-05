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
# (fail-open). TERM/timeout now atomically writes collector_success=0.
set -uo pipefail
LOCK_DIR="${SYCODE_OLTP_LOCK_DIR:-/home/frank/.hermes/profiles/trading-devops/cron/state}"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
CANONICAL="${SYCODE_DB_SLO_COLLECTOR_PY:-/home/frank/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py}"
PROM="${SYCODE_DB_SLO_PROM:-/home/frank/sycode-trading/monitoring/node-exporter-textfile/sycode_db_latency_slo.prom}"
WALL="${SYCODE_DB_SLO_WALL_S:-75}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }

write_fail_closed() {
  local now tmp dir
  now="$(date +%s)"
  dir="$(dirname "$PROM")"
  mkdir -p "$dir"
  tmp="$PROM.tmp.$$"
  cat >"$tmp" <<EOF
# HELP sycode_db_collector_success 1 when the DB SLO textfile collector succeeded.
# TYPE sycode_db_collector_success gauge
sycode_db_collector_success 0
# HELP sycode_db_collector_last_run_timestamp Unix timestamp of the last collector run.
# TYPE sycode_db_collector_last_run_timestamp gauge
sycode_db_collector_last_run_timestamp ${now}
# fail-closed write by trading-devops shim t_bce90116 (TERM/timeout; not a clean skip)
EOF
  mv -f "$tmp" "$PROM"
}

on_term() {
  write_fail_closed
  printf '%s\n' 'OLTP_PROBE_TIMEOUT collector_success=0 (TERM)' >&2
  exit 124
}
trap on_term TERM INT

timeout --kill-after=5s "${WALL}s" python3 "$CANONICAL"
rc=$?
trap - TERM INT
if [ "$rc" -ne 0 ]; then
  write_fail_closed
  printf '%s\n' "OLTP_PROBE_FAIL rc=$rc collector_success=0 (not a clean skip)" >&2
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    exit 124
  fi
  exit "$rc"
fi
exit 0
