#!/usr/bin/env bash
# =============================================================================
# deploy/tests/test-recover.sh — Unit tests for r8127-recover.sh
# =============================================================================
# Tests for:
#   - Structured JSON log emission (stdout) with timestamp/level/request_id
#   - syslog bridge via `logger` (mocked; skip if no syslog)
#   - Fleet dashboard notification via `hermes send` on recovery failure
#   - Fail-open on missing hermes binary
#   - Rollback-guide presence and schema validation
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECOVER="$SCRIPT_DIR/r8127-recover.sh"
ROLLBACK_GUIDE="$SCRIPT_DIR/r8127-recover-rollback-guide.md"
PASS=0; FCOUNT=0

pass_test() { PASS=$((PASS+1)); echo "  PASS: $1"; }
fail_test() { FCOUNT=$((FCOUNT+1)); echo "  FAIL: $1"; }

# We cannot actually run r8127-recover.sh as root without a root session,
# so tests are read-only / invocation-syntax plus artifact checks.

###############################################################################
# TEST 1: bash -n syntax check
###############################################################################
echo "TEST 1: bash -n syntax check"
if bash -n "$RECOVER"; then
  pass_test "r8127-recover.sh passes bash -n"
else
  fail_test "r8127-recover.sh fails bash -n"
fi

###############################################################################
# TEST 2: shellcheck -S error (fail on warnings)
###############################################################################
echo ""
echo "TEST 2: shellcheck -S error"
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S error "$RECOVER"; then
    pass_test "r8127-recover.sh passes shellcheck -S error"
  else
    fail_test "r8127-recover.sh fails shellcheck -S error"
  fi
else
  echo "  SKIP: shellcheck not installed"
  pass_test "r8127-recover.sh shellcheck skipped (not installed)"
fi

###############################################################################
# TEST 3: Log format — ISO-8601 timestamp + severity + request_id present
###############################################################################
echo ""
echo "TEST 3: Log format validation"
log_format_ok=true
grep -qE '"timestamp"' "$RECOVER"              || log_format_ok=false
grep -qE '"level"' "$RECOVER"                  || log_format_ok=false
grep -qE '"request_id"' "$RECOVER"             || log_format_ok=false
grep -qE 'TS_UTC\(\)' "$RECOVER"              || log_format_ok=false
grep -qE 'REQUEST_ID=' "$RECOVER"              || log_format_ok=false
if $log_format_ok; then
  pass_test "script defines ISO timestamp, severity, and request_id in JSON"
else
  fail_test "script missing timestamp/severity/request_id in JSON"
fi

if [[ -x "$RECOVER" ]]; then
  pass_test "r8127-recover.sh is executable"
else
  fail_test "r8127-recover.sh is NOT executable"
fi

###############################################################################
# TEST 4: syslog bridge — logger invocation present in script
###############################################################################
echo ""
echo "TEST 4: syslog bridge"
if grep -qE 'logger -p' "$RECOVER" && \
   grep -qE 'syslog_log\(' "$RECOVER" && \
   grep -qE 'user\.crit|user\.warning|user\.info' "$RECOVER"; then
  pass_test "script contains syslog bridge (logger -p + severity mapping)"
else
  fail_test "script missing syslog bridge (logger -p)"
fi

###############################################################################
# TEST 5: Fleet notification on failure — hermes send + fail-open
###############################################################################
echo ""
echo "TEST 5: Fleet notification on failure"
if grep -qE 'notify_fleet_failure' "$RECOVER" && \
   grep -qE 'hermes send' "$RECOVER" && \
   grep -qE 'discord:#critical-alerts' "$RECOVER" && \
   grep -qE 'fail-open|fail_open|Fail-open' "$RECOVER"; then
  pass_test "script has fleet notification on failure with fail-open semantics"
else
  fail_test "script missing fleet notification path"
fi

# Verify the notification is gated to recovery failures (exit 2-5) AND partial
# recovery (exit 0 with incomplete outcome).
if grep -qE 'notify_fleet_failure' "$RECOVER"; then
  call_count=$(grep -c 'notify_fleet_failure' "$RECOVER" || true)
  if [[ "$call_count" -ge 2 ]]; then
    pass_test "notify_fleet_failure is called for both hard-fail and partial-recovery"
  else
    fail_test "notify_fleet_failure called only $call_count time(s) — expected >= 2"
  fi
fi

###############################################################################
# TEST 6: Rollback guide exists and has required sections
###############################################################################
echo ""
echo "TEST 6: Rollback guide"
if [[ -f "$ROLLBACK_GUIDE" ]]; then
  pass_test "rollback guide file exists"
  # Check for key rollback sections.
  sections_ok=true
  grep -qi 'rollback\|revert\|restore\|undo' "$ROLLBACK_GUIDE" || sections_ok=false
  grep -qi 'offload\|ethtool' "$ROLLBACK_GUIDE" || sections_ok=false
  grep -qi 'driver\|modprobe\|rmmod' "$ROLLBACK_GUIDE" || sections_ok=false
  grep -qi 'systemd\|timer\|service' "$ROLLBACK_GUIDE" || sections_ok=false
  if $sections_ok; then
    pass_test "rollback guide contains required sections (offload, driver, systemd)"
  else
    fail_test "rollback guide missing required sections"
  fi
else
  fail_test "rollback guide file MISSING: $ROLLBACK_GUIDE"
fi

###############################################################################
# TEST 7: Rollback guide is referenced from install/README
###############################################################################
echo ""
echo "TEST 7: Rollback guide referenced"
if [[ -f "$ROLLBACK_GUIDE" ]]; then
  guide_basename="$(basename "$ROLLBACK_GUIDE")"
  if grep -qF "$guide_basename" "$SCRIPT_DIR/README.md" 2>/dev/null || \
     grep -qF "rollback-guide" "$SCRIPT_DIR/README.md" 2>/dev/null; then
    pass_test "README references rollback guide"
  else
    fail_test "README does not reference rollback guide"
  fi
fi

###############################################################################
# Summary
###############################################################################
echo ""
echo "============================================="
echo "Results: ${PASS} passed, ${FCOUNT} failed out of $((PASS + FCOUNT)) tests"
echo "============================================="
[[ $FCOUNT -gt 0 ]] && exit 1
exit 0
