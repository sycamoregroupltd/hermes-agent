#!/usr/bin/env bash
# SHIM — approved exec wrapper (t_bce90116). Canonical source is
# ~/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py
# Consumer: node-exporter textfile
#   /home/frank/sycode-trading/monitoring/node-exporter-textfile/sycode_db_latency_slo.prom
# read by Prometheus job node + sycode-db-slo-alert-bridge (d7331151ddf8)
# which files [host-alert] cards on the sycode-trading board.
set -uo pipefail
LOCK_DIR="/home/frank/.hermes/profiles/trading-devops/cron/state"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
CANONICAL="/home/frank/sycode-trading/monitoring/scripts/sycode_db_latency_slo_collector.py"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }
# Cadence is widened to 15m at land; 30s wall cap so a candles seq-scan cannot
# run unbounded even if the collector python still uses a 90s statement_timeout.
exec timeout --kill-after=5s 30s python3 "$CANONICAL"
