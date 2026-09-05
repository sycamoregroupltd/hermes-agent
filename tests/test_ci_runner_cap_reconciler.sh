#!/usr/bin/env bash
# Tests for scripts/ci-runner-cap-reconciler.sh (card t_44097b86).
#
# Pure fixture-driven: every test sets CI_RECONCILER_RUNNERS_JSON and
# CI_RECONCILER_UNITS_FIXTURE, so NOTHING here touches the live gh api,
# live systemd, or live runners. `--apply` is exercised in the
# apply-is-noop-without-confirm-token test using a systemctl PATH shim
# that records calls instead of executing them, and asserts ZERO calls
# happen without the confirm token, and that --apply alone (no token) is
# a true no-op.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/ci-runner-cap-reconciler.sh"
FIX="$HERE/fixtures"
TMP="$HERE/tmp"
mkdir -p "$TMP"

pass=0
fail=0

assert_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        pass=$((pass + 1))
        echo "PASS: $label"
    else
        fail=$((fail + 1))
        echo "FAIL: $label -- expected to find: $needle"
        echo "--- actual output ---"
        printf '%s\n' "$haystack"
        echo "--- end actual output ---"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        fail=$((fail + 1))
        echo "FAIL: $label -- expected NOT to find: $needle"
    else
        pass=$((pass + 1))
        echo "PASS: $label"
    fi
}

run_reconciler() {
    local runners_fixture="$1" units_fixture="$2" cap_fixture="$3"; shift 3
    CI_RECONCILER_RUNNERS_JSON="$FIX/$runners_fixture" \
    CI_RECONCILER_UNITS_FIXTURE="$FIX/$units_fixture" \
    CI_RECONCILER_CAP_FILE="$FIX/$cap_fixture" \
    CI_RECONCILER_LOG="$TMP/test.log" \
    "$SCRIPT" "$@" 2>&1
}

# --- Test 1: over-cap, all busy (the live 2026-09-05 scenario: 9/9 busy, cap=2)
# Expect: STOP list EMPTY (drain-not-kill), DEFERRED=7 reported, no crash.
out=$(run_reconciler runners_9_online_busy.json units_9_online.txt cap_2.conf)
assert_contains "$out" "DEFERRED: 7 runner(s)" "over-cap all-busy: reports 7 deferred, none stopped"
assert_not_contains "$out" "STOP (idle" "over-cap all-busy: no STOP section printed (nothing idle to stop)"

# --- Test 2: over-cap, mixed idle/busy (5 idle, 4 busy, cap=2)
# excess = 9-2 = 7, but only 5 idle candidates exist, so all 5 are planned to
# stop (drain-to-ceiling exhausts idle supply before reaching cap) and the
# remaining 2 are DEFERRED because only busy runners are left. Busy runners
# must never appear in the STOP plan regardless.
out=$(run_reconciler runners_9_online_mixed.json units_9_online.txt cap_2.conf)
assert_contains "$out" "STOP (idle, over cap):" "over-cap mixed: STOP section present"
stop_count=$(printf '%s' "$out" | sed -n '/STOP (idle, over cap):/,/^$/p' | grep -c '^  - ')
if [ "$stop_count" -eq 5 ]; then
    pass=$((pass + 1)); echo "PASS: over-cap mixed: all 5 idle runners planned to stop (idle supply exhausted before cap reached)"
else
    fail=$((fail + 1)); echo "FAIL: over-cap mixed: expected 5 STOP entries, got $stop_count"
fi
assert_contains "$out" "DEFERRED: 2 runner(s)" "over-cap mixed: 2 still-over-cap runners deferred (all remaining are busy)"
for busy_name in dgx-ci-1 dgx-ci-4 dgx-ci-6 dgx-ci-8; do
    assert_not_contains "$(printf '%s' "$out" | sed -n '/STOP (idle, over cap):/,/^$/p')" "- $busy_name " "over-cap mixed: busy runner $busy_name never selected for STOP"
done

# --- Test 3: under-cap (2 online, cap=4, 2 dead startable)
# Expect: START plan contains exactly the 2 dead runners, up to cap only.
out=$(run_reconciler runners_2_online_under_cap.json units_2_online_under_cap.txt cap_4.conf)
assert_contains "$out" "START (dead, under cap):" "under-cap: START section present"
start_count=$(printf '%s' "$out" | sed -n '/START (dead, under cap):/,/^$/p' | grep -c '^  - ')
if [ "$start_count" -eq 2 ]; then
    pass=$((pass + 1)); echo "PASS: under-cap: exactly 2 dead runners planned to start (up to cap, not beyond)"
else
    fail=$((fail + 1)); echo "FAIL: under-cap: expected 2 START entries, got $start_count"
fi

# --- Test 4: exactly at cap -> true no-op
out=$(run_reconciler runners_at_cap.json units_at_cap.txt cap_2.conf)
assert_contains "$out" "cap enforced: online=2 matches cap=2, no action needed." "at-cap: reports enforced no-op"
assert_not_contains "$out" "STOP (idle" "at-cap: no STOP section"
assert_not_contains "$out" "START (dead" "at-cap: no START section"

# --- Test 5: zombie runner is reported but NOT selected for START/STOP
out=$(run_reconciler runners_with_zombie.json units_with_zombie.txt cap_2.conf)
assert_contains "$out" "ZOMBIES (out of scope" "zombie: zombie section present and explicitly out-of-scope"
assert_contains "$out" "dgx-ci-2" "zombie: dgx-ci-2 (unit active, GH offline) identified as zombie"
start_section=$(printf '%s' "$out" | awk '/^START \(dead, under cap\):/{f=1; next} /^[A-Z]/{f=0} f{print}')
zombie_in_start=$(printf '%s' "$start_section" | grep -c '\- dgx-ci-2 ' || true)
if [ "${zombie_in_start:-0}" -eq 0 ]; then
    pass=$((pass + 1)); echo "PASS: zombie: not included in the START plan (would be a no-op restart, not this script's job)"
else
    fail=$((fail + 1)); echo "FAIL: zombie: dgx-ci-2 incorrectly appears in START plan"
fi

# --- Test 6: deployer excluded from pool entirely (present in fixture 1, online)
out=$(run_reconciler runners_9_online_busy.json units_9_online.txt cap_2.conf)
assert_not_contains "$out" "sycodetrading-deployer" "deployer: never mentioned anywhere in reconciler output (excluded by label)"

# --- Test 7: apply without confirm token is a true no-op (no systemctl call)
SHIM_DIR="$TMP/shim"
mkdir -p "$SHIM_DIR"
CALL_LOG="$TMP/systemctl_calls.log"
: > "$CALL_LOG"
cat > "$SHIM_DIR/systemctl" <<EOF
#!/usr/bin/env bash
echo "systemctl \$*" >> "$CALL_LOG"
exit 0
EOF
chmod +x "$SHIM_DIR/systemctl"
PATH="$SHIM_DIR:$PATH" \
CI_RECONCILER_RUNNERS_JSON="$FIX/runners_9_online_mixed.json" \
CI_RECONCILER_UNITS_FIXTURE="$FIX/units_9_online.txt" \
CI_RECONCILER_CAP_FILE="$FIX/cap_2.conf" \
CI_RECONCILER_LOG="$TMP/test.log" \
"$SCRIPT" --apply >/dev/null 2>&1
if [ ! -s "$CALL_LOG" ]; then
    pass=$((pass + 1)); echo "PASS: --apply without CI_RECONCILER_APPLY_CONFIRM token issues ZERO systemctl calls"
else
    fail=$((fail + 1)); echo "FAIL: --apply without confirm token issued systemctl calls: $(cat "$CALL_LOG")"
fi

# --- Test 8: apply WITH confirm token DOES call systemctl (proves the plumbing works end-to-end
# using a shim, never touching a real unit) — this is the only test that exercises the apply path.
: > "$CALL_LOG"
PATH="$SHIM_DIR:$PATH" \
CI_RECONCILER_RUNNERS_JSON="$FIX/runners_9_online_mixed.json" \
CI_RECONCILER_UNITS_FIXTURE="$FIX/units_9_online.txt" \
CI_RECONCILER_CAP_FILE="$FIX/cap_2.conf" \
CI_RECONCILER_LOG="$TMP/test.log" \
CI_RECONCILER_APPLY_CONFIRM="I-UNDERSTAND-THIS-STOPS-STARTS-LIVE-CI-RUNNERS" \
"$SCRIPT" --apply >/dev/null 2>&1
call_count=$(wc -l < "$CALL_LOG" | tr -d ' ')
if [ "$call_count" -eq 5 ]; then
    pass=$((pass + 1)); echo "PASS: --apply WITH confirm token calls systemctl exactly 5 times (matches the 5-runner idle-exhausted STOP plan) via shim"
else
    fail=$((fail + 1)); echo "FAIL: --apply with confirm token: expected 5 systemctl calls via shim, got $call_count: $(cat "$CALL_LOG")"
fi
if grep -q '^systemctl --user stop' "$CALL_LOG" && ! grep -q '^systemctl --user start' "$CALL_LOG"; then
    pass=$((pass + 1)); echo "PASS: shim calls are all 'stop' (matches over-cap plan), no stray 'start'"
else
    fail=$((fail + 1)); echo "FAIL: unexpected shim call shape: $(cat "$CALL_LOG")"
fi

# --- Test 9: missing cap file fails closed (no crash, clean non-zero + message)
out=$(CI_RECONCILER_RUNNERS_JSON="$FIX/runners_at_cap.json" \
      CI_RECONCILER_UNITS_FIXTURE="$FIX/units_at_cap.txt" \
      CI_RECONCILER_CAP_FILE="$TMP/does-not-exist.conf" \
      CI_RECONCILER_LOG="$TMP/test.log" \
      "$SCRIPT" 2>&1)
rc=$?
assert_contains "$out" "FAILED to build plan" "missing cap file: fails visibly, not silently"
if [ "$rc" -ne 0 ]; then
    pass=$((pass + 1)); echo "PASS: missing cap file: non-zero exit code ($rc)"
else
    fail=$((fail + 1)); echo "FAIL: missing cap file: exit code was 0, expected non-zero"
fi

echo ""
echo "==================================="
echo "RESULTS: $pass passed, $fail failed"
echo "==================================="
[ "$fail" -eq 0 ]
