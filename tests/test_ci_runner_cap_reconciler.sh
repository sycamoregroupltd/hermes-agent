#!/usr/bin/env bash
# Tests for scripts/ci-runner-cap-reconciler.sh (card t_44097b86).
#
# Pure fixture-driven: every test sets CI_RECONCILER_RUNNERS_JSON and
# CI_RECONCILER_UNITS_FIXTURE, so NOTHING here touches the live gh api,
# live systemd, or live runners. `--apply` is exercised in the
# apply-is-noop-without-confirm-token test using a systemctl PATH shim
# that records calls instead of executing them. The apply tests cover the
# confirmation gate, stale-plan revalidation, and action-failure propagation.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../scripts/ci-runner-cap-reconciler.sh"
TMP="$(mktemp -d)"
FIX="$TMP/fixtures"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$FIX"

# Keep fixtures local to this test so the reconciler and its test remain the
# only two files introduced by the source-only candidate.
printf 'CAP=2\n' > "$FIX/cap_2.conf"
printf 'CAP=4\n' > "$FIX/cap_4.conf"

cat > "$FIX/runners_2_online_under_cap.json" <<'EOF'
{"total_count":4,"runners":[
{"id":22,"name":"dgx-ci-1","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":23,"name":"dgx-ci-2","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":24,"name":"dgx-ci-3","os":"Linux","status":"offline","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":25,"name":"dgx-ci-4","os":"Linux","status":"offline","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]}
]}
EOF

cat > "$FIX/runners_9_online_busy.json" <<'EOF'
{"total_count":10,"runners":[
{"id":22,"name":"dgx-ci-1","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":23,"name":"dgx-ci-2","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":24,"name":"dgx-ci-3","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":25,"name":"dgx-ci-4","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":26,"name":"dgx-ci-5","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":27,"name":"dgx-ci-6","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":28,"name":"dgx-ci-7","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":29,"name":"dgx-ci-8","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":30,"name":"dgx-ci-9","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"}]},
{"id":21,"name":"sycodetrading-deployer","os":"Linux","status":"offline","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"Linux"},{"name":"ARM64"},{"name":"ci"},{"name":"deployer"}]}
]}
EOF

cat > "$FIX/runners_9_online_mixed.json" <<'EOF'
{"total_count":10,"runners":[
{"id":22,"name":"dgx-ci-1","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":23,"name":"dgx-ci-2","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":24,"name":"dgx-ci-3","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":25,"name":"dgx-ci-4","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":26,"name":"dgx-ci-5","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":27,"name":"dgx-ci-6","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":28,"name":"dgx-ci-7","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":29,"name":"dgx-ci-8","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":30,"name":"dgx-ci-9","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":21,"name":"sycodetrading-deployer","os":"Linux","status":"online","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"},{"name":"deployer"}]}
]}
EOF

cat > "$FIX/runners_at_cap.json" <<'EOF'
{"total_count":2,"runners":[
{"id":22,"name":"dgx-ci-1","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":23,"name":"dgx-ci-2","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]}
]}
EOF

cat > "$FIX/runners_with_zombie.json" <<'EOF'
{"total_count":4,"runners":[
{"id":22,"name":"dgx-ci-1","os":"Linux","status":"online","busy":true,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":23,"name":"dgx-ci-2","os":"Linux","status":"offline","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]},
{"id":24,"name":"dgx-ci-3","os":"Linux","status":"offline","busy":false,"version":"2.337.0","labels":[{"name":"self-hosted"},{"name":"ci"}]}
]}
EOF

cat > "$FIX/units_2_online_under_cap.txt" <<'EOF'
gha-runner-ci-1.service active
gha-runner-ci-2.service active
gha-runner-ci-3.service inactive
gha-runner-ci-4.service inactive
EOF

cat > "$FIX/units_9_online.txt" <<'EOF'
gha-runner-ci-1.service active
gha-runner-ci-2.service active
gha-runner-ci-3.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-4.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-5.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-6.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-7.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-8.service active
actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-9.service active
actions.runner.sycamoregroupltd-sycode-trading.sycodetrading-deployer.service inactive
EOF

cat > "$FIX/units_at_cap.txt" <<'EOF'
gha-runner-ci-1.service active
gha-runner-ci-2.service active
EOF

cat > "$FIX/units_with_zombie.txt" <<'EOF'
gha-runner-ci-1.service active
gha-runner-ci-2.service active
gha-runner-ci-3.service inactive
EOF

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
    CI_RECONCILER_REVALIDATE_RUNNERS_JSON='' \
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
assert_contains "$out" "dgx-ci-2 -> gha-runner-ci-2.service" "unit resolution: legacy gha-runner naming is supported"
assert_contains "$out" "dgx-ci-5 -> actions.runner.sycamoregroupltd-sycode-trading.dgx-ci-5.service" "unit resolution: actions.runner naming is supported"
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
exit "\${SYSTEMCTL_SHIM_EXIT:-0}"
EOF
chmod +x "$SHIM_DIR/systemctl"
PATH="$SHIM_DIR:$PATH" \
CI_RECONCILER_RUNNERS_JSON="$FIX/runners_9_online_mixed.json" \
CI_RECONCILER_UNITS_FIXTURE="$FIX/units_9_online.txt" \
CI_RECONCILER_CAP_FILE="$FIX/cap_2.conf" \
CI_RECONCILER_LOG="$TMP/test.log" \
CI_RECONCILER_APPLY_CONFIRM='' \
"$SCRIPT" --apply >/dev/null 2>&1
if [ ! -s "$CALL_LOG" ]; then
    pass=$((pass + 1)); echo "PASS: --apply without CI_RECONCILER_APPLY_CONFIRM token issues ZERO systemctl calls"
else
    fail=$((fail + 1)); echo "FAIL: --apply without confirm token issued systemctl calls: $(cat "$CALL_LOG")"
fi

# --- Test 8: apply WITH confirm token DOES call systemctl (proves the plumbing
# works end-to-end using a shim, never touching a real unit).
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

# --- Test 9: a runner that becomes busy after planning is never drained.
# The planning fixture has idle candidates; the revalidation fixture represents
# the race winner with every candidate busy. The first planned STOP must fail
# closed before the command shim sees any action.
: > "$CALL_LOG"
out=$(PATH="$SHIM_DIR:$PATH" \
      CI_RECONCILER_RUNNERS_JSON="$FIX/runners_9_online_mixed.json" \
      CI_RECONCILER_REVALIDATE_RUNNERS_JSON="$FIX/runners_9_online_busy.json" \
      CI_RECONCILER_UNITS_FIXTURE="$FIX/units_9_online.txt" \
      CI_RECONCILER_CAP_FILE="$FIX/cap_2.conf" \
      CI_RECONCILER_LOG="$TMP/test.log" \
      CI_RECONCILER_APPLY_CONFIRM="I-UNDERSTAND-THIS-STOPS-STARTS-LIVE-CI-RUNNERS" \
      "$SCRIPT" --apply 2>&1)
rc=$?
assert_contains "$out" "is no longer idle" "apply race: fresh busy state fails closed"
if [ "$rc" -ne 0 ] && [ ! -s "$CALL_LOG" ]; then
    pass=$((pass + 1)); echo "PASS: apply race: non-zero result and ZERO systemctl action calls"
else
    fail=$((fail + 1)); echo "FAIL: apply race: expected non-zero and zero action calls; rc=$rc calls=$(cat "$CALL_LOG")"
fi

# --- Test 10: an external action failure reaches the top-level result.
: > "$CALL_LOG"
out=$(PATH="$SHIM_DIR:$PATH" \
      SYSTEMCTL_SHIM_EXIT=42 \
      CI_RECONCILER_RUNNERS_JSON="$FIX/runners_9_online_mixed.json" \
      CI_RECONCILER_UNITS_FIXTURE="$FIX/units_9_online.txt" \
      CI_RECONCILER_CAP_FILE="$FIX/cap_2.conf" \
      CI_RECONCILER_LOG="$TMP/test.log" \
      CI_RECONCILER_APPLY_CONFIRM="I-UNDERSTAND-THIS-STOPS-STARTS-LIVE-CI-RUNNERS" \
      "$SCRIPT" --apply 2>&1)
rc=$?
assert_contains "$out" "systemctl stop failed" "action failure: failure is reported"
if [ "$rc" -ne 0 ] && [ "$(wc -l < "$CALL_LOG" | tr -d ' ')" -eq 1 ]; then
    pass=$((pass + 1)); echo "PASS: action failure: non-zero top-level result after failing command shim"
else
    fail=$((fail + 1)); echo "FAIL: action failure: expected non-zero and one attempted action; rc=$rc calls=$(cat "$CALL_LOG")"
fi

# --- Test 11: missing cap file fails closed (no crash, clean non-zero + message)
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

# --- Test 12: bounded, fixture-only admission/load harness.
# This test budget bounds only the synthetic work admitted by this harness; it
# is not a claimed production capacity threshold. The receipt records observed
# wall latency and CPU use without turning either measurement into an assertion.
run_load_admission_harness() {
    local requested="$1" fixture_budget="$2" admitted=0 rejected=0 scenario
    while [ $((admitted + rejected)) -lt "$requested" ]; do
        if [ "$admitted" -ge "$fixture_budget" ]; then
            rejected=$((rejected + 1))
            continue
        fi
        scenario=$((admitted % 4))
        case "$scenario" in
            0) run_reconciler runners_9_online_busy.json units_9_online.txt cap_2.conf >/dev/null || return 1 ;;
            1) run_reconciler runners_9_online_mixed.json units_9_online.txt cap_2.conf >/dev/null || return 1 ;;
            2) run_reconciler runners_2_online_under_cap.json units_2_online_under_cap.txt cap_4.conf >/dev/null || return 1 ;;
            3) run_reconciler runners_at_cap.json units_at_cap.txt cap_2.conf >/dev/null || return 1 ;;
        esac
        admitted=$((admitted + 1))
    done
    printf 'ADMISSION RECEIPT: requested=%d admitted=%d rejected=%d fixture_budget=%d\n' \
        "$requested" "$admitted" "$rejected" "$fixture_budget"
}

RESOURCE_RECEIPT="$TMP/load_resource_receipt.log"
TIMEFORMAT='RESOURCE RECEIPT: latency_seconds=%R user_cpu_seconds=%U system_cpu_seconds=%S'
{ time load_out=$(run_load_admission_harness 11 8); } 2> "$RESOURCE_RECEIPT"
rc=$?
resource_out=$(cat "$RESOURCE_RECEIPT")
printf '%s\n%s\n' "$load_out" "$resource_out"
if [ "$rc" -eq 0 ]; then
    pass=$((pass + 1)); echo "PASS: load harness: all admitted fixture runs completed"
else
    fail=$((fail + 1)); echo "FAIL: load harness: fixture run failed with rc=$rc"
fi
assert_contains "$load_out" "requested=11 admitted=8 rejected=3 fixture_budget=8" "load harness: deterministic budget admits 8 and rejects excess"
if printf '%s' "$resource_out" | grep -Eq '^RESOURCE RECEIPT: latency_seconds=[0-9.]+ user_cpu_seconds=[0-9.]+ system_cpu_seconds=[0-9.]+$'; then
    pass=$((pass + 1)); echo "PASS: load harness: emits measured latency and CPU receipt without threshold assertions"
else
    fail=$((fail + 1)); echo "FAIL: load harness: malformed resource receipt: $resource_out"
fi

echo ""
echo "==================================="
echo "RESULTS: $pass passed, $fail failed"
echo "==================================="
[ "$fail" -eq 0 ]
