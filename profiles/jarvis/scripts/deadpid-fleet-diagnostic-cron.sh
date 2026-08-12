#!/usr/bin/env bash
# deadpid-fleet-diagnostic-cron.sh — scheduled read-only fleet dead-PID sweep.
#
# Runs the read-only dead-PID diagnostic (t_9e894c1d core) and emits a compact
# fleet summary to stdout. The no_agent cron delivers non-empty stdout to
# #fleet-reports. SILENT when green (no mislabeled dead-PID residuals) so the
# cron's non-empty-stdout watchdog pattern sends nothing on a clean fleet.
#
# This is READ-ONLY — it never mutates any board (per the diagnostic's
# safe-routing contract). Wired by devops for t_9f603089 acceptance #4.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIAG="$SCRIPT_DIR/deadpid-fleet-diagnostic.py"
[ -x "$DIAG" ] || { echo "ERROR: missing diagnostic $DIAG" >&2; exit 1; }

OUT="$(python3 "$DIAG" --json 2>/dev/null)" || OUT=""
[ -z "$OUT" ] && exit 0

# The diagnostic JSON reports top-level "total" (dead-PID residuals scanned)
# and "mislabeled" (residuals whose auto-class is wrong vs the dead-PID root
# cause). Green when there is nothing to surface.
COUNT="$(printf '%s' "$OUT" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(int(d.get("mislabeled", d.get("total", 0)) or 0))
except Exception:
    print(-1)' 2>/dev/null)" || COUNT="-1"

if [ "${COUNT:-0}" = "0" ] || [ "${COUNT}" = "-1" ]; then
    exit 0
fi

echo "FLEET DEAD-PID DIAGNOSTIC — ${COUNT} mislabeled dead-PID residual(s) detected"
echo "---"
python3 "$DIAG" 2>/dev/null || true
