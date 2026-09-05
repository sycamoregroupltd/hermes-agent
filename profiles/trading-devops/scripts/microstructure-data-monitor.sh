#!/bin/bash
# microstructure-data-monitor.sh  (two-leg wiring, t_b9bec246)
#
# Decides whether the microstructure IC test has a testable sample yet,
# for BOTH venue legs independently. The two legs are NEVER pooled; each
# keeps its own paired complete-case n >= 200 threshold.
#
# GATE (approved by trading-risk-reviewer on t_dc702203, 2026-08-01;
#       implemented under t_18fda1f4; two-leg wiring under t_b9bec246):
#
#     Fire the IC re-run task only when a leg's paired complete-case sample
#     n >= 200, measured directly at <= 5 minute join staleness.
#
# The gate variable is MEASURED, not estimated. All measurement lives in the
# tracked script scripts/microstructure/paired-sample-gate.py in the
# sycode-trading repo, which rebuilds the IC test's own row set: clean 1m
# bars with segment-scoped gap masking, a backward join_asof with a hard 5m
# tolerance, and a complete-case count over the micro features, the forward
# label, and the baseline scores.
#
# RETIRED 2026-08-01 -- do not reintroduce:
#   est_bars = hours_span * 60 * 5 * 0.5, with MIN_BARS=1200 / TARGET_BARS=10500.
# `hours_span` was calendar span, not bars: monotonically non-decreasing and
# still rising while the collector was dead. The mechanism by which this gate
# passed twice (1.98x, 2.25x) while the binding constraint never moved and two
# IC rounds were wasted.
#
# Flow:
#   1. Refresh the Hyperliquid trigger battery (hyperliquid-trigger-battery.py
#      --window-days 7) so the HL signal population is fresh for the HL-leg
#      gate. Idempotent: removes prior hl-battery-* rows, re-inserts fresh.
#      Prefer the tracked checkout path when present; else the isolated
#      worktree copy (t_5c6fca0e lands the branch so the tracked path becomes
#      canonical on origin/main).
#   2. Measure the Binance futures leg (default *USDT symbols).
#   3. Measure the Hyperliquid leg (--symbols BTC,ETH,SOL,AVAX,BNB).
#   4. Print ONE combined decision line:
#      MICROSTRUCTURE_MONITOR_DECISION: binance_paired_n=<n>/200 | hl_paired_n=<n>/200 |
#        binance_overlap=<pct>% | hl_overlap=<pct>% | binance_cvd_null=<rate> |
#        hl_cvd_null=<rate> | binance_collector=<alive|dead> | hl_collector=<alive|dead> |
#        action=<run-full|run-binance|run-hl|wait|error>
#
# There is deliberately NO "partial test" secondary threshold and NO softened
# threshold. The threshold is 200 per leg, full stop.
#
# Env overrides (verification / isolated worktree only; live cron uses defaults):
#   MICROSTRUCTURE_SCRIPTS_DIR     tracked scripts/microstructure directory
#   MICROSTRUCTURE_BATTERY_FALLBACK  fallback worktree scripts/microstructure
#   MICROSTRUCTURE_GATE_WRAPPER    optional absolute wrapper path

set -uo pipefail

SCRIPT_DIR="${MICROSTRUCTURE_SCRIPTS_DIR:-/home/frank/sycode-trading/scripts/microstructure}"
WORKTREE_DIR="${MICROSTRUCTURE_BATTERY_FALLBACK:-/home/frank/sycode-trading/.worktrees/t_db9814a1/scripts/microstructure}"
GATE_SCRIPT="$SCRIPT_DIR/paired-sample-gate.py"
PYTHON_BIN="${MICROSTRUCTURE_PYTHON_BIN:-/home/frank/.hermes/venvs/trading-ml/bin/python}"
TARGET_PAIRED_N=200
STALENESS_TOL_MIN=5
RESULTS_DIR="${MICROSTRUCTURE_RESULTS_DIR:-/home/frank/data/microstructure/results}"
BINANCE_JSON="$RESULTS_DIR/paired-sample-gate-binance.json"
HL_JSON="$RESULTS_DIR/paired-sample-gate-hl.json"
PROFILE_WRAPPER="/home/frank/.hermes/profiles/research-trading/scripts/run-paired-sample-gate.py"

if [ -n "${MICROSTRUCTURE_GATE_WRAPPER:-}" ] && [ -f "${MICROSTRUCTURE_GATE_WRAPPER}" ]; then
  GATE_WRAPPER="$MICROSTRUCTURE_GATE_WRAPPER"
elif [ -f "$SCRIPT_DIR/run-paired-sample-gate.py" ]; then
  GATE_WRAPPER="$SCRIPT_DIR/run-paired-sample-gate.py"
else
  GATE_WRAPPER="$PROFILE_WRAPPER"
fi

# Battery script: prefer the tracked checkout path (exists on this branch and
# once land/t_db9814a1-hl-trigger-battery lands on main), else the worktree copy.
if [ -f "$SCRIPT_DIR/hyperliquid-trigger-battery.py" ]; then
  BATTERY_SCRIPT="$SCRIPT_DIR/hyperliquid-trigger-battery.py"
  BATTERY_SOURCE="tracked"
else
  BATTERY_SCRIPT="$WORKTREE_DIR/hyperliquid-trigger-battery.py"
  BATTERY_SOURCE="fallback-worktree"
fi

echo "=== Microstructure Data Monitor ==="
echo "Timestamp: $(date -u)"
echo "Gate: paired complete-case n >= ${TARGET_PAIRED_N} at <= ${STALENESS_TOL_MIN}m join staleness (per leg; never pooled)"
echo "SCRIPT_DIR=$SCRIPT_DIR"
echo "GATE_SCRIPT=$GATE_SCRIPT"
echo "GATE_WRAPPER=$GATE_WRAPPER"
echo "Battery source: $BATTERY_SOURCE path=$BATTERY_SCRIPT"

mkdir -p "$RESULTS_DIR"

# tick_trades GROUP BY scans exceed the default 30s statement_timeout
# (dgx-trading-data-engineering §9). Raise for this process only.
# Does NOT change the 200 paired-n gate, pooling, or any span heuristic.
export PGOPTIONS="-c statement_timeout=180000"

# 1. Refresh the Hyperliquid trigger battery (idempotent marked rows).
#    Non-fatal: if it is missing or fails, the gate is still measured on the
#    rows already present. Battery freshness is NOT the gate.
echo ""
echo "--- Hyperliquid trigger battery refresh ---"
if [ -x "$PYTHON_BIN" ] && [ -f "$BATTERY_SCRIPT" ]; then
  BATTERY_DIR="$(dirname "$BATTERY_SCRIPT")"
  ( cd "$BATTERY_DIR" && "$PYTHON_BIN" "$BATTERY_SCRIPT" --window-days 7 ) || \
    echo "MICROSTRUCTURE_MONITOR: WARN battery refresh failed (non-fatal; gate measured on existing rows)"
else
  echo "MICROSTRUCTURE_MONITOR: WARN battery script missing at $BATTERY_SCRIPT (non-fatal; gate measured on existing rows)"
fi

measure_leg() {
  # $1 = label, $2 = symbols arg ("" = default *USDT), $3 = json path
  local label="$1" symbols="$2" json_path="$3"
  echo ""
  echo "--- $label paired-sample gate ---"
  local cmd=( "$PYTHON_BIN" "$GATE_WRAPPER" "$GATE_SCRIPT" --target "$TARGET_PAIRED_N" --tolerance-min "$STALENESS_TOL_MIN" --json "$json_path" )
  if [ -n "$symbols" ]; then cmd+=( --symbols "$symbols" ); fi
  local out
  if out=$("${cmd[@]}" 2>&1); then
    echo "$out"
    return 0
  else
    local rc=$?
    echo "$out"
    echo "MICROSTRUCTURE_MONITOR: ERROR $label measurement failed (rc=${rc})"
    return 1
  fi
}

overall_error=0
measure_leg "Binance"     ""                          "$BINANCE_JSON" || overall_error=1
measure_leg "Hyperliquid" "BTC,ETH,SOL,AVAX,BNB"     "$HL_JSON"      || overall_error=1

# Robust field extraction from each leg's JSON artifact.
parse_json() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    print("ERR|ERR|ERR|ERR"); sys.exit(0)
paired  = d.get("gate_paired_n", 0) or 0
overlap = d.get("signal_tape_overlap_pct", 0.0)
if overlap is None: overlap = 0.0
coll    = d.get("collector", "unknown")
hor     = d.get("horizons", []) or []
cvd     = None
for h in hor:
    if isinstance(h, dict) and "cvd_zscore_null_rate" in h:
        cvd = h["cvd_zscore_null_rate"]; break
print(f"{paired}|{overlap}|{cvd}|{coll}")
PY
}

if [ "$overall_error" -ne 0 ]; then
  action="error"
  b_pn=0;  h_pn=0;  b_ov=0;   h_ov=0;   b_cvd="NA"; h_cvd="NA"; b_coll="unknown"; h_coll="unknown"
else
  b_parse=$(parse_json "$BINANCE_JSON")
  h_parse=$(parse_json "$HL_JSON")
  if [ "$b_parse" = "ERR|ERR|ERR|ERR" ] || [ "$h_parse" = "ERR|ERR|ERR|ERR" ]; then
    overall_error=1
    action="error"
    b_pn=0; h_pn=0; b_ov=0; h_ov=0; b_cvd="NA"; h_cvd="NA"; b_coll="unknown"; h_coll="unknown"
  else
    b_pn=$(echo "$b_parse" | cut -d'|' -f1)
    b_ov=$(echo "$b_parse" | cut -d'|' -f2)
    b_cvd=$(echo "$b_parse" | cut -d'|' -f3)
    b_coll=$(echo "$b_parse" | cut -d'|' -f4)
    h_pn=$(echo "$h_parse" | cut -d'|' -f1)
    h_ov=$(echo "$h_parse" | cut -d'|' -f2)
    h_cvd=$(echo "$h_parse" | cut -d'|' -f3)
    h_coll=$(echo "$h_parse" | cut -d'|' -f4)

    binance_ok=0; hl_ok=0
    if [ "$b_pn" -ge "$TARGET_PAIRED_N" ]; then binance_ok=1; fi
    if [ "$h_pn" -ge "$TARGET_PAIRED_N" ]; then hl_ok=1; fi

    if [ "$binance_ok" -eq 1 ] && [ "$hl_ok" -eq 1 ]; then
      action="run-full"
    elif [ "$binance_ok" -eq 1 ]; then
      action="run-binance"
    elif [ "$hl_ok" -eq 1 ]; then
      action="run-hl"
    else
      action="wait"
    fi
  fi
fi

echo ""
echo "MICROSTRUCTURE_MONITOR_DECISION: binance_paired_n=${b_pn}/${TARGET_PAIRED_N} | hl_paired_n=${h_pn}/${TARGET_PAIRED_N} | binance_overlap=${b_ov}% | hl_overlap=${h_ov}% | binance_cvd_null=${b_cvd} | hl_cvd_null=${h_cvd} | binance_collector=${b_coll} | hl_collector=${h_coll} | action=${action}"

if [ "$action" = "error" ]; then
  echo ""
  echo "MEASUREMENT ERROR: a leg's measurement failed. A failed measurement is NOT a pass. The gate stays shut."
elif [ "$action" = "wait" ]; then
  echo ""
  echo "GATE SHUT: both legs paired complete-case n < ${TARGET_PAIRED_N}. No IC re-run task."
  echo "Read the overlap and cvd_null figures above before concluding anything about elapsed time:"
  echo "the standing constraint is CONTINUITY, NOT VOLUME. A low-duty-cycle collector cannot yield a"
  echo "testable sample however long it runs, so a rising bar count is not progress toward this gate."
elif [ "$action" = "run-full" ]; then
  echo ""
  echo "GATE OPEN (both legs): paired complete-case n >= ${TARGET_PAIRED_N} at <= ${STALENESS_TOL_MIN}m staleness."
  echo "The full two-venue IC re-run task is now warranted."
elif [ "$action" = "run-binance" ]; then
  echo ""
  echo "GATE OPEN (Binance leg only): Binance paired n >= ${TARGET_PAIRED_N}; HL leg still short."
  echo "A scoped Binance-only IC re-run is warranted; the full two-venue test is the terminal goal."
elif [ "$action" = "run-hl" ]; then
  echo ""
  echo "GATE OPEN (HL leg only): HL paired n >= ${TARGET_PAIRED_N}; Binance leg still short."
  echo "A scoped HL-only IC re-run is warranted; the full two-venue test is the terminal goal."
fi

exit 0
