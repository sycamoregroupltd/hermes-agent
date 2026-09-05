#!/usr/bin/env bash
# In-dir cron wrapper for the G-F1 fingerprint lag monitor (t_bce90116).
# Hermes cron `script` resolver REJECTS symlinks and out-of-dir paths, so this
# real in-dir shell shim must exec the canonical producer.
# Canonical: /home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py
# Consumer: JSONL ~/.hermes/state/gqt-fingerprint-lag-g-f1.jsonl (watchdog
# stdout + standing kanban card gqt-fingerprint-lag-g-f1-standing on jarvis-os
# assigned to sycode-trading-pm; Telegram best-effort).
#
# Wall 60s (>=45s requested; observed 4-query SQL ~26s + fork-pressure variance)
# still << planned 60m cadence. GNU timeout 124 is mapped to watchdog exit 3
# (probe failure), never treated as a silent healthy skip.
set -uo pipefail
LOCK_DIR="${SYCODE_OLTP_LOCK_DIR:-/home/frank/.hermes/profiles/trading-devops/cron/state}"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
FP_PY="${GQT_FP_PY:-/home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py}"
WALL="${GQT_FP_WALL_S:-60}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }
# Shim already holds the lock; python must not try LOCK_NB on a second fd
# (Linux flock is per-open-file-description and would skip itself).
export GQT_FP_SKIP_LOCK=1
on_term() {
  printf '%s\n' "🔴 G-F1 FP MONITOR PROBE FAILURE — wall timeout (TERM). Fingerprint lag is INVISIBLE. Escalate."
  exit 3
}
trap on_term TERM INT
timeout --kill-after=5s "${WALL}s" python3 "$FP_PY" --watchdog
rc=$?
trap - TERM INT
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  printf '%s\n' "🔴 G-F1 FP MONITOR PROBE FAILURE — wall timeout (GNU timeout rc=$rc). Fingerprint lag is INVISIBLE. Escalate."
  exit 3
fi
exit "$rc"
