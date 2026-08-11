#!/bin/sh
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
#
# cron_liveness_wrapper.sh — host-cron driver for the missed-occurrence monitor.
#
# WHY HOST CRON: the hermes cron scheduler itself can die (silent-writer-death
# at the scheduler layer is the exact class this monitor detects, so running
# the monitor IN the scheduler we are monitoring is circular). This wrapper
# lives in the OS crontab where the daemon is the single source of truth.
#
# Flow:
#   1. Run the read-only detector. (exit 0 = healthy, 1 = findings, 2 = error)
#   2. ALWAYS run the deduped kanban router — it creates/comments/resolves
#      board cards so the silent-failure doctrine closes the loop.
#   3. Persist a machine+ledger artifact under ~/.hermes/state/cron-liveness/.
#   4. Emit ONE human alert line (per tick) only when findings exist; the
#      router handles card lifecycle, so the alert channel carries a link to
#      the board card, not a raw dump of every failing job.
#
# Env (override for non-DGX hosts):
#   CRON_LIVENESS_HOME   default ~/.hermes
#   CRON_LIVENESS_GRACE_H  default 2.0
#   CRON_LIVENESS_ALERT   default: echo to stderr (host cron mails root; the
#                         gateway terminal guard captures stderr for delivery).
set -eu

HERMES_HOME="${CRON_LIVENESS_HOME:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="$SCRIPT_DIR/cron_liveness_monitor.py"
ROUTER="$SCRIPT_DIR/cron_liveness_kanban_router.py"
STATE_DIR="$HERMES_HOME/state/cron-liveness"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$STATE_DIR/runs"

JSON_OUT="$STATE_DIR/runs/liveness-$STAMP.json"
ROUTER_OUT="$STATE_DIR/runs/router-$STAMP.txt"

export HERMES_HOME

# 1. Detect (read-only). Capture JSON regardless of exit code.
set +e
python3 "$MONITOR" --json > "$JSON_OUT" 2>"$STATE_DIR/runs/detector-$STAMP.err"
DETECT_RC=$?
set -e

# 2. Route to kanban tracker (always — router resolves on healthy too).
HEALTHY=1
if [ "$DETECT_RC" -ne 0 ]; then
  HEALTHY=0
  python3 "$MONITOR" --json > "$JSON_OUT" 2>/dev/null || true
  cat "$JSON_OUT" | CRON_LIVENESS_HEALTHY=0 python3 "$ROUTER" > "$ROUTER_OUT" 2>&1 || true
else
  echo '{"monitor":"cron-liveness","stamp":"'"$STAMP"'","grace_h":'"${CRON_LIVENESS_GRACE_H:-2}"',"scanned":0,"findings":[]}' > "$JSON_OUT"
  echo "" | CRON_LIVENESS_HEALTHY=1 python3 "$ROUTER" > "$ROUTER_OUT" 2>&1 || true
fi

# 3. Human alert (only when UNHEALTHY). One concise line + board link.
if [ "$DETECT_RC" -ne 0 ]; then
  N=$(python3 -c "import json,sys; d=json.load(open('$JSON_OUT')); print(len(d.get('findings',[])))" 2>/dev/null || echo "?")
  CARD_LINK="see kanban sycode-trading board (cron-liveness-monitor)"
  echo "[$STAMP] CRON LIVENESS UNHEALTHY: $N finding(s) — route+resolve on board. $CARD_LINK" \
    > "$STATE_DIR/runs/alert-$STAMP.txt"
  cat "$STATE_DIR/runs/alert-$STAMP.txt" >&2 || true
fi

# 4. Exit mirrors the detector (so host cron / wrapper consumers see liveness).
exit "$DETECT_RC"
