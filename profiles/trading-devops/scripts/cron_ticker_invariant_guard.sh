#!/usr/bin/env bash
#
# cron_ticker_invariant_guard.sh — OUT-OF-BAND rung wrapper (t_69f6c49a).
#
# WHY: the colocated rung of this guard runs as a cron job in the JARVIS store
# (b5b352684f45, every 30m). If the jarvis store's ticker wedges — exactly the
# P0 that happened 2026-08-10 (heartbeat 69m stale, 76 unstarted claims) — that
# in-band job cannot run because its own scheduler is the thing that's stuck.
# This wrapper is invoked from the TRADING-DEVOPS store (a different store with
# its own gateway/ticker), so it keeps scanning ALL profile stores even when the
# guarded jarvis store is wedged. Rule: never colocate a guard with the store it
# guards.
#
# It execs the canonical guard script (single source of truth, jarvis/scripts)
# which: scans profiles/*/cron/ticker_heartbeat (+ root store), and for any store
# with ENABLED jobs whose heartbeat is missing or >900s stale, emits a RED alert
# line (stdout -> delivered per the job's --deliver target) and auto-disables the
# dead store's jobs with paused_reason set. Silent (empty stdout) when clean, so
# a no_agent cron job delivers nothing on healthy runs (watchdog pattern).
#
# This wrapper intentionally does NOT symlink (Hermes cron rejects symlinked /
# out-of-scripts-dir script paths); it is a real in-dir file that execs the
# canonical absolute path.
set -euo pipefail

CANONICAL_GUARD="/home/frank/.hermes/profiles/jarvis/scripts/cron_ticker_invariant_guard.py"

if [[ ! -f "$CANONICAL_GUARD" ]]; then
  echo "🔴 GUARD-ERROR cron_ticker_invariant_guard.sh: canonical guard missing at $CANONICAL_GUARD (dead-pin recurrence)" >&2
  exit 3
fi

exec /usr/bin/env python3 "$CANONICAL_GUARD" "$@"
