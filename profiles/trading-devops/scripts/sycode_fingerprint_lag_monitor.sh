#!/usr/bin/env bash
# In-dir cron wrapper for the G-F1 fingerprint lag monitor (t_bce90116).
# Hermes cron `script` resolver REJECTS symlinks and out-of-dir paths, so this
# real in-dir shell shim must exec the canonical producer.
# Canonical: /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py
# Consumer: JSONL ~/.hermes/state/gqt-fingerprint-lag-g-f1.jsonl (watchdog
# stdout + standing kanban card gqt-fingerprint-lag-g-f1-standing on jarvis-os
# assigned to sycode-trading-pm; Telegram best-effort).
set -euo pipefail
LOCK_DIR="/home/frank/.hermes/profiles/trading-devops/cron/state"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }
# Shim already holds the lock; python must not try LOCK_NB on a second fd
# (Linux flock is per-open-file-description and would skip itself).
export GQT_FP_SKIP_LOCK=1
# Wall-clock cap: never let a 7d fingerprint scan overlap the next tick.
exec timeout --kill-after=5s 30s python3 /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py --watchdog
