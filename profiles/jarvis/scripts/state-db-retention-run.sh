#!/usr/bin/env bash
# Monthly state-db retention for BOTH the default store and profile stores.
# Wrapper for jarvis cron job 7ad0e11f7790 / state-db-retention.
#
# Runs the hard-scoped default-store prune (prune-default-state-db.py) then the
# parameterized profile-store prune for the profile(s) listed in PROFILES.
# Each script has its own backup-then-prune + integrity-verify + abort-not-force
# safety contract, and appends to /home/frank/.hermes/logs/state-db-retention.log.
#
# NOTE: profiles can be added here over time; the wrapper is order-independent.
set -uo pipefail

DEFAULT_PRUNE=/home/frank/.hermes/scripts/prune-default-state-db.py
PROFILE_PRUNE=/home/frank/.hermes/scripts/prune-profile-state-db.py
# Profiles whose state.db gets the same retention treatment. Add names here.
PROFILES=(jarvis)

rc=0

echo "[state-db-retention] default store: $DEFAULT_PRUNE"
if [ -x "$DEFAULT_PRUNE" ]; then
    "$DEFAULT_PRUNE" || { echo "WARN: default prune exit $?"; rc=1; }
else
    echo "WARN: default prune script missing: $DEFAULT_PRUNE"
    rc=1
fi

for p in "${PROFILES[@]}"; do
    echo "[state-db-retention] profile store: $PROFILE_PRUNE --profile $p"
    if [ -x "$PROFILE_PRUNE" ]; then
        "$PROFILE_PRUNE" --profile "$p" || { echo "WARN: profile($p) prune exit $?"; rc=1; }
    else
        echo "WARN: profile prune script missing: $PROFILE_PRUNE"
        rc=1
    fi
done

# Exit code is the only liveness signal a no-agent cron propagates.
exit "$rc"
