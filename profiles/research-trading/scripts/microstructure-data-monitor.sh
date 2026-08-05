#!/bin/bash
# microstructure-data-monitor.sh
#
# Decides whether the microstructure IC test has a testable sample yet.
#
# GATE (approved by trading-risk-reviewer on t_dc702203, 2026-08-01;
#       implemented under t_18fda1f4):
#
#     Fire the IC re-run task only when the paired complete-case sample
#     n >= 200, measured directly at <= 5 minute join staleness.
#
# The gate variable is MEASURED, not estimated. All measurement lives in the
# tracked script scripts/microstructure/paired-sample-gate.py in the
# sycode-trading repo, which rebuilds the IC test's own row set: clean 1m bars
# with segment-scoped gap masking, a backward join_asof with a hard 5m
# tolerance, and a complete-case count over the micro features, the forward
# label, and the baseline scores.
#
# RETIRED 2026-08-01 -- do not reintroduce:
#   est_bars = hours_span * 60 * 5 * 0.5, with MIN_BARS=1200 / TARGET_BARS=10500.
# `hours_span` was MAX(timestamp)-MIN(timestamp): calendar span, not bars. It is
# monotonically non-decreasing and KEPT RISING WHILE THE COLLECTOR WAS DEAD,
# because the numerator grows from a frozen MAX while MIN stays fixed. It had no
# relation to clean bars, to signal overlap, or to usable paired rows. That is
# the mechanism by which this gate passed twice (1.98x, 2.25x) while the binding
# constraint never moved and two IC rounds were wasted on INCONCLUSIVE-
# INSUFFICIENT-DATA. Any gate expressed in tape bars is unsound here: only ~8.8%
# of signals fire during a live tape minute, so bars can grow indefinitely while
# testable rows stay flat.
#
# There is deliberately NO "partial test" secondary threshold. The partial-test
# branch is what produced the two wasted rounds.

set -uo pipefail

GATE_SCRIPT="/home/frank/sycode-trading/scripts/microstructure/paired-sample-gate.py"
PYTHON_BIN="/home/frank/.hermes/venvs/trading-ml/bin/python"
TARGET_PAIRED_N=200
STALENESS_TOL_MIN=5
JSON_OUT="/home/frank/data/microstructure/results/paired-sample-gate-latest.json"

echo "=== Microstructure Data Monitor ==="
echo "Timestamp: $(date -u)"
echo "Gate: paired complete-case n >= ${TARGET_PAIRED_N} at <= ${STALENESS_TOL_MIN}m join staleness"
echo ""

if [ ! -x "$PYTHON_BIN" ]; then
  echo "MICROSTRUCTURE_MONITOR: ERROR python venv missing at ${PYTHON_BIN} | action=error"
  exit 1
fi

if [ ! -f "$GATE_SCRIPT" ]; then
  echo "MICROSTRUCTURE_MONITOR: ERROR gate script missing at ${GATE_SCRIPT} | action=error"
  exit 1
fi

mkdir -p "$(dirname "$JSON_OUT")"

gate_output=$("$PYTHON_BIN" "$GATE_SCRIPT" \
  --target "$TARGET_PAIRED_N" \
  --tolerance-min "$STALENESS_TOL_MIN" \
  --json "$JSON_OUT" 2>&1)
gate_rc=$?

echo "$gate_output"

if [ "$gate_rc" -ne 0 ]; then
  echo ""
  echo "MICROSTRUCTURE_MONITOR: ERROR measurement failed (rc=${gate_rc}) | action=error"
  echo "A failed measurement is NOT a pass. The gate stays shut."
  exit "$gate_rc"
fi

summary=$(echo "$gate_output" | grep -m1 '^MICROSTRUCTURE_MONITOR:')
action=$(echo "$summary" | sed -n 's/.*action=\([a-z]*\).*/\1/p')

echo ""
if [ "$action" = "run" ]; then
  echo "GATE OPEN: paired complete-case n >= ${TARGET_PAIRED_N} at <= ${STALENESS_TOL_MIN}m staleness."
  echo "The IC re-run task is now warranted. Create the successor card with an"
  echo "--idempotency-key so repeated cron ticks cannot duplicate it."
else
  echo "GATE SHUT: paired complete-case n < ${TARGET_PAIRED_N}. No IC re-run task."
  echo "Read the overlap and cvd_null figures in the summary line above before"
  echo "concluding anything about elapsed time: the standing constraint is"
  echo "CONTINUITY, NOT VOLUME. A low-duty-cycle collector cannot yield a"
  echo "testable sample however long it runs, so a rising bar count is not"
  echo "progress toward this gate."
fi

exit 0
