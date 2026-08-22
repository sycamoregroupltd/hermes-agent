#!/usr/bin/env bash
# governor-lockgate-t1.sh — Beat-1 read-path ingest of lock-gate Tier-1 relapse (t_53f9956d)
#
# PURPOSE
#   Wires the fleet-governor Beat-1 review-read path to the lock-gate summary output
#   produced by scripts/kanban-approve-block-lockgate.py. It consumes the machine-readable
#   `watch` JSON, filters to Tier-1 findings ONLY (silent relapse: landed then re-blocked
#   with NO reviewer re-open), and emits a compact T1 ALERT with reviewer (marker_author)
#   + reason (evidence) context so Beat-1 can surface it as a T1 alert/flag.
#
# SCOPE (read-path integration only; NO classifier semantics change)
#   - T1  -> surfaced as an ALERT (the whole point of this ingest)
#   - T2 / T3 -> intentionally NOT surfaced here. They are observed by the detector and
#                handled by its own governance path (operator-gated hold / awaiting land).
#                Beat-1 must not duplicate T2/T3 handling.
#
# FAIL-OPEN (non-negotiable)
#   If the lockgate detector errors, is absent, or the parse fails, this script emits a
#   clean UNAVAILABLE/CLEAN line and exits 0 — it can never wedge Beat-1. It reads only.
#
# USAGE
#   governor-lockgate-t1.sh            # emit T1 alert block (or CLEAN/UNAVAILABLE) to stdout
#   governor-lockgate-t1.sh --json    # emit machine-readable JSON to stdout
set -uo pipefail

LOCKGATE="/home/frank/.hermes/scripts/kanban-approve-block-lockgate.py"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [[ ! -x "$LOCKGATE" && ! -f "$LOCKGATE" ]]; then
  echo "LOCKGATE-T1: UNAVAILABLE (detector missing: $LOCKGATE)"
  exit 0
fi

# Fail-open: any detector failure -> UNAVAILABLE, exit 0.
if ! timeout 180 python3 "$LOCKGATE" watch > "$TMP" 2>/dev/null; then
  echo "LOCKGATE-T1: UNAVAILABLE (detector failed; Beat-1 continues)"
  exit 0
fi

if [[ "${1:-}" == "--json" ]]; then
  python3 - "$TMP" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(json.dumps({"lockgate_t1": "UNAVAILABLE", "findings": []}))
    sys.exit(0)
t1 = [f for f in data.get('findings', []) if f.get('tier') == 1]
print(json.dumps({
    "lockgate_t1": "ALERT" if t1 else "CLEAN",
    "t1_count": len(t1),
    "findings": t1,
}, indent=2))
PY
  exit 0
fi

python3 - "$TMP" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("LOCKGATE-T1: UNAVAILABLE (parse failed; Beat-1 continues)")
    sys.exit(0)
t1 = [f for f in data.get('findings', []) if f.get('tier') == 1]
if not t1:
    print("LOCKGATE-T1: CLEAN — 0 silent relapses (no landed->regressed card without reviewer re-open).")
    sys.exit(0)
print("LOCKGATE-T1: %d SILENT RELAPSE(S) — landed then re-blocked with NO reviewer re-open. T1 ALERT." % len(t1))
for f in t1:
    print("  [%s][%s] %s" % (f.get('board'), f.get('task_id'), f.get('title')))
    print("    status=%s approved@%s" % (f.get('current_status'), f.get('approved_at_human')))
    print("    reviewer=%s reason=%s" % (f.get('marker_author'), f.get('evidence')))
PY
