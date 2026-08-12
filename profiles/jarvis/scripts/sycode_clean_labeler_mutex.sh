#!/bin/bash
# sycode_clean_labeler_mutex.sh — shared nonblocking mutex wrapper for the
# Sycode clean-outcome 24h labeler (kanban t_c089055c, 2026-07-27).
#
# WHY: the two DB-writing 15-minute labelers (clean-outcome + r-multiple) collide
# at the quarter-hour boundary and both frequently overrun their 600s timeout
# (slot-pressure fingerprint from incident 2026-07-24-postgres-connection-pool-
# exhaustion-t_0bfd4fda). Staggering alone cannot fully prevent overlap because a
# single run can exceed the 15-minute grid. This wrapper holds a SHARED flock so
# only one labeler writes Postgres at a time. If the sibling holds the lock, this
# run SKIPS with a bounded alert and relies on the next idempotent run — it never
# queues an unbounded process. (Acceptance test #3.)
#
# Reversible: revert the cron `script` to `clean_outcome_labeler_24h_v2.sh` via
#   hermes -p jarvis --accept-hooks cron edit a70772892543 --script clean_outcome_labeler_24h_v2.sh
# and remove this file. No DB / Redis / container / deploy / credential / trading change.
set -u
LOCKFILE="/home/frank/.hermes/var/sycode-labeler-mutex.lock"
REAL_PRODUCER="/home/frank/.hermes/profiles/jarvis/scripts/clean_outcome_labeler_24h_v2.sh"
MAXTRIES=30   # bounded ~30s wait, then skip (rely on next idempotent run)
mkdir -p "$(dirname "$LOCKFILE")"

# Open the lock fd and try a nonblocking acquire with a bounded retry window.
exec 200>"$LOCKFILE"
acquired=0
for _ in $(seq 1 "$MAXTRIES"); do
  if flock -n 200; then acquired=1; break; fi
  sleep 1
done

if [ "$acquired" -eq 0 ]; then
  echo "ALERT sycode-labeler-mutex (clean-outcome): sibling labeler holds the shared DB-write mutex; skipping this run (next idempotent run catches up)."
  exit 0
fi

# Lock held on fd 200; exec the real producer (fd inherited -> mutex held for run).
if [ ! -x "$REAL_PRODUCER" ]; then
  echo "ALERT sycode_clean_labeler_mutex.sh: real producer missing at $REAL_PRODUCER"
  exit 1
fi
exec "$REAL_PRODUCER"
