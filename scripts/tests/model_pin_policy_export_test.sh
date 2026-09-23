#!/usr/bin/env bash
# Deterministic regression test for kanban t_6b347dd9 (jarvis-os).
#
# CONTEXT (2026-09-23): the stack-health audit wired the approved model-pin policy
# (scripts/model-pin-policy.env, Frank binder POLICY 2026-09-19) into the monitor,
# but sourced it with a BARE `. file`. Shell sourcing does NOT export the variables,
# so model-pin-drift-check.py (a child process) never saw MODEL_PIN_EXPECTED and fell
# back to its own expired default (deepseek/deepseek-v4-flash-0731). The policy-
# sanctioned pin ~deepseek/deepseek-v4-flash-latest|nous was therefore reported as
# DRIFT on every 10-minute tick — a false "DGX stack degraded" page.
#
# TEST SHAPE (red/green on the REAL artifact):
#   1. build a fixture state.db whose ONLY nous-billed row is the sanctioned pin.
#   2. extract the REAL policy stanza from scripts/stack-health-audit.sh and eval it
#      (we execute the actual lines, we do not grep for their shape).
#   3. run the REAL scripts/model-pin-drift-check.py the way the wrapper does.
#   BASE (no stanza / bare sourcing)  -> DRIFT  rc=1  (RED)
#   FIXED (exported sourcing)         -> CLEAN  rc=0  (GREEN)
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(dirname "$HERE")"
AUDIT="$SCRIPTS/stack-health-audit.sh"
CHECKER="$SCRIPTS/model-pin-drift-check.py"
POLICY="$SCRIPTS/model-pin-policy.env"
PY="${PYTHON:-/home/frank/.hermes/hermes-agent/venv/bin/python}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

[ -f "$CHECKER" ] || fail "checker missing: $CHECKER"
[ -f "$POLICY" ]  || fail "policy env missing: $POLICY"
[ -x "$PY" ]      || fail "python missing: $PY"

# --- fixture: only the sanctioned pin, nous-billed, inside the window ------------
"$PY" - "$TMP/state.db" <<'PYEOF'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE session_model_usage (model TEXT, billing_provider TEXT, last_seen REAL)")
con.execute("INSERT INTO session_model_usage VALUES (?,?,?)",
            ("~deepseek/deepseek-v4-flash-latest", "nous", time.time()))
con.commit(); con.close()
PYEOF

# --- extract the REAL policy stanza (execute it, don't pattern-match it) ---------
STANZA="$(awk '/^MODEL_PIN_POLICY_ENV=/{f=1} f{print} f&&/^fi$/{exit}' "$AUDIT")"
[ -n "$STANZA" ] || fail "stack-health-audit.sh has no MODEL_PIN_POLICY_ENV stanza -> policy never applied (RED)"

# --- run the REAL checker exactly as the wrapper does ---------------------------
OUT="$(bash -c '
  set -u
  eval "$1"
  export MODEL_PIN_DB="$2" MODEL_PIN_NO_CONFIG=1
  exec "$3" "$4"
' _ "$STANZA" "$TMP/state.db" "$PY" "$CHECKER" 2>&1)"
rc=$?

echo "checker rc=$rc :: $OUT"
[ "$rc" -eq 0 ] || fail "expected CLEAN (rc=0) after exported policy, got rc=$rc"
case "$OUT" in
  *"MODEL-PIN: CLEAN"*) ;;
  *) fail "output has no 'MODEL-PIN: CLEAN' line" ;;
esac
echo "PASS: approved pin policy is exported to the checker; sanctioned pin is CLEAN"