#!/usr/bin/env bash
# sycode-exchange-flow-incremental — daily CryptoQuant exchange-flow collection.
#
# Created by t_91b817c1 (board sycode-trading). Staged PAUSED until Frank has
# run the gated 365-day backfill; resume with:
#   hermes cron resume <job-id>
#
# Sources CRYPTOQUANT_API_KEY from server/.env at runtime (never printed).
# Uses --window day: the CryptoQuant Professional plan serves exchange-flows
# at day resolution only (verified via /v1/my/discovery/endpoints 2026-07-05).
# If Frank upgrades to Premium for hourly data, change WINDOW=hour and edit
# the cron schedule to hourly.
set -uo pipefail

REPO=/home/frank/sycode-trading
COLLECTOR="$REPO/execution/exchange_flow_collector.py"
WINDOW=day

if [[ ! -f "$COLLECTOR" ]]; then
    echo "[sycode-exchange-flow] ERROR: $COLLECTOR missing — PR #346 not merged, or the deploy-cron stash-cycled it again (recover: git show 'stash@{N}^3:execution/exchange_flow_collector.py')."
    exit 1
fi

CRYPTOQUANT_API_KEY="$(grep -m1 '^CRYPTOQUANT_API_KEY=' "$REPO/server/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")"
if [[ -z "${CRYPTOQUANT_API_KEY}" ]]; then
    echo "[sycode-exchange-flow] ERROR: CRYPTOQUANT_API_KEY not found in $REPO/server/.env"
    exit 1
fi
export CRYPTOQUANT_API_KEY

cd "$REPO"
OUT="$(python3 "$COLLECTOR" --window "$WINDOW" 2>&1)"
RC=$?

if [[ $RC -ne 0 ]]; then
    echo "[sycode-exchange-flow] FAILED (rc=$RC):"
    echo "$OUT" | tail -25
    exit $RC
fi

# Success: emit the one-line summary (visible in delivery target).
echo "[sycode-exchange-flow] $(echo "$OUT" | grep -E '=== Done:' | tail -1)"
