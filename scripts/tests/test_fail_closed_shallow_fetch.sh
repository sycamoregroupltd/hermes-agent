#!/usr/bin/env bash
# Fixture: SessionStart fetch gate is fail-closed.
# Fetch origin main only when `git rev-parse --is-shallow-repository` prints exactly "false".
# Does not run live reconcile-state.py (that writes vault STATE.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SEAT="$SCRIPT_DIR/seat-live-state.sh"
REC="$SCRIPT_DIR/reconcile-state.py"
fail=0

if [ ! -f "$SEAT" ] || [ ! -f "$REC" ]; then
  echo "FAIL: missing producer file(s): SEAT=$SEAT REC=$REC"
  fail=1
else
  # Source-level: no --depth=50 in producers.
  if grep -n -- '--depth=50' "$SEAT" "$REC"; then
    echo "FAIL: --depth=50 still present"
    fail=1
  else
    echo "PASS: no --depth=50 in producers"
  fi

  # Source-level: fail-closed predicate (space-tolerant; still exactly-false).
  # Codex P1: both producers must also require a zero exit from the probe.
  if grep -Eq '\[\s+"\$_SHALLOW"\s+=\s+"false"\s+\]' "$SEAT" \
     && grep -Eq '_SHALLOW_RC"\s+-eq\s+0' "$SEAT" \
     && grep -Eq '_shallow\s*==\s*"false"' "$REC" \
     && grep -Eq '_shallow_rc\s*==\s*0' "$REC"; then
    echo "PASS: both producers require exit 0 and exactly false"
  else
    echo "FAIL: fail-closed predicate missing"
    fail=1
  fi
fi

run_case() {
  local label="$1"
  local probe="$2"
  local expect_fetch="$3"
  local probe_rc="${4:-0}"
  local tmp
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/bin"
  cat > "$tmp/bin/git" <<'EOF'
#!/usr/bin/env bash
log="${GIT_CALL_LOG:?}"
printf '%s\n' "$*" >> "$log"
# Strip leading -C <path>
if [ "${1:-}" = "-C" ]; then
  shift 2
fi
case " $* " in
  *" --is-shallow-repository "*)
    printf '%s\n' "${FIXTURE_SHALLOW-}"
    exit "${FIXTURE_SHALLOW_RC:-0}"
    ;;
  *" fetch "*)
    echo FETCH >> "$log.fetches"
    exit 0
    ;;
  *" rev-parse refs/remotes/origin/main"*)
    echo 0000000000000000000000000000000000000000
    exit 0
    ;;
  *" rev-parse --abbrev-ref HEAD"*)
    echo main
    exit 0
    ;;
  *" cat-file "*)
    exit 1
    ;;
  *" rev-list "*)
    echo 0
    exit 0
    ;;
esac
exit 0
EOF
  chmod +x "$tmp/bin/git"
  : > "$tmp/calls"
  rm -f "$tmp/calls.fetches"
  FIXTURE_SHALLOW="$probe" FIXTURE_SHALLOW_RC="$probe_rc" \
    GIT_CALL_LOG="$tmp/calls" PATH="$tmp/bin:$PATH" \
    timeout 20 bash "$SEAT" >/dev/null 2>&1 || true
  got_fetch=0
  [ -f "$tmp/calls.fetches" ] && got_fetch=1
  if [ "$expect_fetch" = "yes" ] && [ "$got_fetch" -eq 1 ]; then
    echo "PASS: probe='$probe' fetched"
  elif [ "$expect_fetch" = "no" ] && [ "$got_fetch" -eq 0 ]; then
    echo "PASS: probe='$probe' skipped fetch"
  else
    echo "FAIL: probe='$probe' expect_fetch=$expect_fetch got_fetch=$got_fetch"
    echo "calls:"; sed 's/^/  /' "$tmp/calls" || true
    fail=1
  fi
  rm -rf "$tmp"
}

run_case false_allows_fetch "false" yes
run_case true_skips "true" no
run_case empty_skips "" no
run_case garbage_skips "yes" no
run_case false_timeout_rc_skips "false" no 124
run_case false_trailing_junk_skips "false extra" no

if [ "$fail" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
exit 0
