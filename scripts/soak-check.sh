#!/usr/bin/env bash
# SOAK CHECK: Run blocked-state + reviewer-routing guards and output summary.
# Usage: soak-check.sh [label]
#   label: optional marker like "T+6h" or "T+12h" (default: "check")
set -uo pipefail

LABEL="${1:-check}"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
SCRIPTDIR="/home/frank/.hermes/scripts"
OUTDIR="/tmp/soak-results"
mkdir -p "$OUTDIR"

BLOCKED_OUT="${OUTDIR}/blocked-state-${LABEL}-${TS}.json"
REVIEWER_OUT="${OUTDIR}/reviewer-routing-${LABEL}-${TS}.json"

# run blocked-state guard (exit 1 when violations found — expected)
python3 "${SCRIPTDIR}/blocked-state-dispatch-guard.py" --json > "$BLOCKED_OUT" 2>/dev/null || true

# run reviewer-routing guard
python3 "${SCRIPTDIR}/reviewer-routing-guard.py" --json > "$REVIEWER_OUT" 2>/dev/null || true

# Extract key numbers
BLOCKED_TOTAL=$(python3 -c "import json; d=json.load(open('${BLOCKED_OUT}')); print(d['total_violations'], d['total_post_fix'], d['total_pre_fix'])")
REVIEWER_TOTAL=$(python3 -c "import json; d=json.load(open('${REVIEWER_OUT}')); print(d['total_capability_events'], d['total_spawn_fail_reblocks'], d['total_post_fix_reblocks'])")

IFS=' ' read -r BV BP BF <<< "$BLOCKED_TOTAL"
IFS=' ' read -r CE SR PR <<< "$REVIEWER_TOTAL"

# Determine verdict
VERDICT="PASS"
if [ "$BP" -gt 3 ]; then
    VERDICT="FAIL: blocked-state post-fix violations increased ($BP, baseline was 3)"
fi
if [ "$PR" -gt 9 ]; then
    VERDICT="FAIL: reviewer-routing post-fix reblocks increased ($PR, baseline was 9)"
fi

cat <<EOF
=== SOAK CHECK: ${LABEL} @ ${TS} ===
Blocked-state guard: ${BV} total, ${BP} post-fix, ${BF} pre-fix (baseline: 3 post-fix)
Reviewer-routing guard: ${CE} capability events, ${SR} spawn-fail reblocks, ${PR} post-fix (baseline: 9 post-fix)
VERDICT: ${VERDICT}
Artifacts:
  ${BLOCKED_OUT}
  ${REVIEWER_OUT}
EOF
