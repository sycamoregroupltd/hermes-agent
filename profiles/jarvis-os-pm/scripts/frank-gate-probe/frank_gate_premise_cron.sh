#!/usr/bin/env bash
# cron wrapper for frank_gate_premise_probe.py — RECOMMEND-ONLY filtered delivery.
# Deploy card: jarvis-os/t_64a1f191 (Frank A3 GO 2026-08-28).
#
# Contract (unchanged from the os-reviewer-approved probe, md5 bccd76a7...):
#   * This wrapper ONLY invokes the probe with its DEFAULT read-only flags
#     (dry-run over jarvis-os + sycode-trading). It adds no flags, no board
#     writes, no status mutation, no hidden modes.
#   * The probe itself remains the sole executable; the wrapper decides only
#     whether the run's output is worth delivering.
#
# Delivery policy (no_agent cron semantics: empty stdout = silent no-op):
#   * 0 blocked frank_gate cards examined  -> print nothing, exit 0 (silent).
#   * >=1 card examined (RECOMMEND-RETIRE or DECLINE evidence lines present)
#     -> print the probe's full dry-run report verbatim (delivered to the
#       jarvis/PM lane by cron's deliver target).
#   * Probe crash or board-unreadable        -> print the failure, exit != 0 so
#     the cron records a durable incident (never a silent false-green).
set -uo pipefail

PROBE=/home/frank/.hermes/scripts/frank-gate-probe/frank_gate_premise_probe.py

OUTPUT=$("$PROBE" 2>&1)
RC=$?

if [ "$RC" -ne 0 ]; then
    printf 'frank_gate premise probe FAILED (rc=%s)\n%s\n' "$RC" "$OUTPUT"
    exit "$RC"
fi

if printf '%s\n' "$OUTPUT" | grep -q 'cannot open board DB'; then
    printf 'frank_gate premise probe: board DB unreadable\n%s\n' "$OUTPUT"
    exit 1
fi

# Silent no-op when there are no blocked frank_gate cards on either board.
if printf '%s\n' "$OUTPUT" | grep -q 'cards examined : 0'; then
    exit 0
fi

# There is at least one frank_gate card with a verdict + evidence lines.
printf '%s\n' "$OUTPUT"
exit 0