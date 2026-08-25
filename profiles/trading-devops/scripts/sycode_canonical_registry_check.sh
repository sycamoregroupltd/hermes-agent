#!/usr/bin/env bash
# In-dir cron wrapper for the canonical-dataset-registry acceptance check.
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths (see
# trading-devops SOUL), so this real in-dir shim must exec the canonical
# checker at ~/.hermes/scripts/.
# Canonical: /home/frank/.hermes/scripts/sycode_canonical_registry_check.py
#
# Delivery contract (kanban t_46963e33, scheduling card):
#   exit 0 (every canonical dataset passes its SLO)  -> silent (watchdog contract)
#   exit 1 (one or more canonical datasets FAIL)     -> write CRITICAL alert to
#             the jarvis alert spool + print the FAIL table, exit 1
#   exit 3 (harness error)                           -> write WARNING alert to
#             the jarvis alert spool, exit 3
# The jarvis drain job (sycode-alertmanager-oob-spool-drain, every 1m) relays
# spooled alerts to Discord (#critical-alerts). Read-only; no DDL/DML.
set -uo pipefail

CHECKER="/home/frank/.hermes/scripts/sycode_canonical_registry_check.py"
SPOOL_WRITER="/home/frank/.hermes/scripts/spool_alert_write.py"
SPOOL="/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming"

out="$(python3 "$CHECKER" 2>&1)"
rc=$?

printf '%s\n' "$out"

# exit 0 => all canonical datasets pass: stay silent (watchdog contract).
if [ "$rc" -eq 0 ]; then
  exit 0
fi

# Build a compact alert summary from the FAIL / ERROR / HARNESS rows.
fail_rows="$(printf '%s\n' "$out" | grep -E '^\| .*(\*\*FAIL\*\*|HARNESS ERROR|ERROR:)' | head -12)"
nfail_rows="$(printf '%s\n' "$fail_rows" | grep -c '^\|' || true)"

case "$rc" in
  1)
    summary="CANONICAL DATASET REGISTRY FAIL (${nfail_rows} gate-breaking row(s)):"
    if [ -n "$fail_rows" ]; then
      summary="$summary"$'\n'"$fail_rows"
    else
      summary="$summary"$'\n'"$(printf '%s\n' "$out" | tail -6)"
    fi
    python3 "$SPOOL_WRITER" --spool "$SPOOL" --alertname canonical-registry-fail \
      --severity critical --summary "$summary" 2>&1 || true
    ;;
  3)
    summary="CANONICAL DATASET REGISTRY HARNESS ERROR: $(printf '%s\n' "$out" | grep -m1 'HARNESS ERROR' || echo 'checker harness failure')"
    python3 "$SPOOL_WRITER" --spool "$SPOOL" --alertname canonical-registry-harness-error \
      --severity warning --summary "$summary" 2>&1 || true
    ;;
  *)
    # unexpected exit code: surface it rather than silently passing
    summary="CANONICAL DATASET REGISTRY CHECK returned unexpected exit ${rc}."
    python3 "$SPOOL_WRITER" --spool "$SPOOL" --alertname canonical-registry-check-error \
      --severity warning --summary "$summary" 2>&1 || true
    ;;
esac

exit "$rc"
