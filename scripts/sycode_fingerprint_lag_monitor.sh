#!/usr/bin/env bash
# Wrapper so a direct call can run the python monitor. Cron uses the
# trading-devops in-dir shim (profiles/trading-devops/scripts/...).
# Wall 60s; GNU timeout 124/137 maps to watchdog exit 3 (not a silent skip).
set -uo pipefail
LOCK_DIR="${SYCODE_OLTP_LOCK_DIR:-/home/frank/.hermes/profiles/trading-devops/cron/state}"
LOCK="$LOCK_DIR/sycode-oltp-probe.lock"
FP_PY="${GQT_FP_PY:-/home/frank/.hermes/scripts/sycode_fingerprint_lag_monitor.py}"
WALL="${GQT_FP_WALL_S:-60}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK"
flock -n 9 || { printf '%s\n' 'OLTP_PROBE_SKIP locked'; exit 0; }
export GQT_FP_SKIP_LOCK=1
timeout --kill-after=5s "${WALL}s" python3 "$FP_PY" "$@"
rc=$?
if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
  printf '%s\n' "🔴 G-F1 FP MONITOR PROBE FAILURE — wall timeout (GNU timeout rc=$rc). Fingerprint lag is INVISIBLE. Escalate." >&2
  exit 3
fi
exit "$rc"
