#!/usr/bin/env bash
# Regression tests for microstructure-data-monitor.sh paired complete-case gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
MONITOR="$ROOT/profiles/research-trading/scripts/microstructure-data-monitor.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/scripts" "$TMP/results"
touch "$TMP/scripts/paired-sample-gate.py"

# The monitor invokes the Python interpreter for both the battery and gate
# wrapper. These deterministic fixtures model the gate artifacts produced by
# the real signal_journeys/tick_trades complete-case join.
cat >"$TMP/scripts/hyperliquid-trigger-battery.py" <<'PY'
#!/usr/bin/env python3
print("fixture battery refreshed")
PY
chmod +x "$TMP/scripts/hyperliquid-trigger-battery.py"

cat >"$TMP/scripts/run-paired-sample-gate.py" <<'PY'
#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
out = args[args.index("--json") + 1]
is_hl = "--symbols" in args
key = "FAKE_HL_JSON" if is_hl else "FAKE_BINANCE_JSON"
with open(os.environ[key], encoding="utf-8") as fh:
    payload = json.load(fh)
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
print(f"fixture {'hl' if is_hl else 'binance'} gate written")
PY
chmod +x "$TMP/scripts/run-paired-sample-gate.py"

run_case() {
  local name="$1" binance="$2" hl="$3"
  printf '%s\n' "$binance" >"$TMP/binance.json"
  printf '%s\n' "$hl" >"$TMP/hl.json"
  FAKE_BINANCE_JSON="$TMP/binance.json" \
  FAKE_HL_JSON="$TMP/hl.json" \
  MICROSTRUCTURE_SCRIPTS_DIR="$TMP/scripts" \
  MICROSTRUCTURE_PYTHON_BIN="$(command -v python3)" \
  MICROSTRUCTURE_RESULTS_DIR="$TMP/results/$name" \
  bash "$MONITOR"
}

assert_decision() {
  local output="$1" expected="$2"
  grep -F "MICROSTRUCTURE_MONITOR_DECISION:" <<<"$output" | grep -F "action=$expected" >/dev/null
}

# Live tape has 199 paired rows; gap-masked/forward-filled bars and a high
# bars_valid count must not substitute for a complete micro-feature row.
out=$(run_case below-threshold \
  '{"gate_paired_n":199,"bars_valid":10500,"target_bars":10500,"collector":"alive","signal_tape_overlap_pct":91.0,"horizons":[{"cvd_zscore_null_rate":0.12}]}' \
  '{"gate_paired_n":198,"bars_valid":10500,"target_bars":10500,"collector":"alive","signal_tape_overlap_pct":90.0,"horizons":[{"cvd_zscore_null_rate":0.25}]}')
assert_decision "$out" wait
grep -F "GATE SHUT: both legs paired complete-case n < 200" <<<"$out" >/dev/null
if grep -F "action=run-full" <<<"$out" >/dev/null; then exit 1; fi

# Exactly 200 paired complete-case rows on each leg opens the full test,
# regardless of legacy bar-count values (including below the old 4602 gate).
out=$(run_case paired-threshold \
  '{"gate_paired_n":200,"bars_valid":1,"target_bars":1,"GATE_INT":1,"collector":"alive","horizons":[{"cvd_zscore_null_rate":0.0}]}' \
  '{"gate_paired_n":200,"bars_valid":1,"target_bars":1,"GATE_INT":1,"collector":"alive","horizons":[{"cvd_zscore_null_rate":0.0}]}')
assert_decision "$out" run-full
grep -F "GATE OPEN (both legs)" <<<"$out" >/dev/null

# A large bar count alone cannot open the gate when complete-case pairing is
# absent (missing micro features / forward labels in the joined fixture).
out=$(run_case bars-only \
  '{"gate_paired_n":0,"bars_valid":20000,"target_bars":10500,"collector":"alive"}' \
  '{"gate_paired_n":0,"bars_valid":20000,"target_bars":10500,"collector":"alive"}')
assert_decision "$out" wait
if grep -F "action=run-full" <<<"$out" >/dev/null; then exit 1; fi

# Collector liveness and staleness diagnostics remain visible independently of
# the gate result; a dead/stale leg must not be silently normalized away.
out=$(run_case liveness-staleness \
  '{"gate_paired_n":199,"bars_valid":20000,"collector":"dead","signal_tape_overlap_pct":12.5,"horizons":[{"cvd_zscore_null_rate":1.0}]}' \
  '{"gate_paired_n":200,"bars_valid":20000,"collector":"stale","signal_tape_overlap_pct":2.5,"horizons":[{"cvd_zscore_null_rate":0.9}]}')
assert_decision "$out" run-hl
grep -F "binance_collector=dead" <<<"$out" >/dev/null
grep -F "hl_collector=stale" <<<"$out" >/dev/null
grep -F "binance_overlap=12.5%" <<<"$out" >/dev/null
grep -F "hl_overlap=2.5%" <<<"$out" >/dev/null

echo "PASS: microstructure paired complete-case monitor regression tests (4 cases)"
