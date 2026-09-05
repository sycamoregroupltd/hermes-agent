#!/usr/bin/env bash
# Wrapper so a direct call can run the python monitor. Cron uses the
# trading-devops in-dir shim (profiles/trading-devops/scripts/...).
set -euo pipefail
LOCK_DIR="/home/frank/.hermes/profiles/trading-devops/cron/state"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }
export GQT_FP_SKIP_LOCK=1
exec timeout --kill-after=5s 30s python3 /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py "$@"
