#!/usr/bin/env bash
# Agent-hooks selftest runner — umbrella test command.
# Runs deterministic local selftests only; no live kanban board, credential,
# network, runtime, or profile configuration mutation required.
#
# VERIFICATION_MATRIX
# - store: /home/frank/.hermes/agent-hooks/run-selftests.sh
# - liveness: bash /home/frank/.hermes/agent-hooks/run-selftests.sh
# - deliver target: test runner invoked by /home/frank/.hermes/agent-hooks/gate-kanban-complete.selftest.sh and component scripts
# - named consumer: jarvis-os-pm / os-reviewer deterministic test evidence
# - satisfied verification: pytest/self-test outputs in task comments/Obsidian Governance note
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0
RESULTS=()

run_test() {
    local name="$1"
    local cmd="$2"
    local log_file
    log_file="$(mktemp -t "selftest-${name}-XXXXX.log")"
    echo "────────────────────────────────────────────────────────────────────────"
    echo "  RUNNING: $name"
    echo "  Command: $cmd"
    echo "────────────────────────────────────────────────────────────────────────"
    if (cd "$SCRIPT_DIR/.." && bash -c "$cmd") > "$log_file" 2>&1; then
        echo "  PASS $name"
        RESULTS+=("PASS  $name")
        PASS=$((PASS + 1))
    else
        echo "  FAIL $name — tail of output:"
        tail -30 "$log_file"
        RESULTS+=("FAIL  $name")
        FAIL=$((FAIL + 1))
    fi
    echo "  Last line: $(tail -1 "$log_file")"
    echo
    rm -f "$log_file"
}

run_test "gate-critic-readonly" "bash agent-hooks/gate-critic-readonly.selftest.sh"
run_test "goal-judge-provider-error-handler" "python3 agent-hooks/goal-judge-provider-error-handler.py"
run_test "gate-kanban-complete" "bash agent-hooks/gate-kanban-complete.selftest.sh"
run_test "verdict-router" "bash agent-hooks/verdict-router.selftest.sh"
run_test "dedupe-guard-correction-cards" "python3 scripts/test_dedupe_guard_correction_cards.py"

echo "════════════════════════════════════════════════════════════════════════"
echo "  AGENT-HOOKS SELFTEST SUMMARY"
echo "════════════════════════════════════════════════════════════════════════"
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
echo "────────────────────────────────────────────────────────────────────────"
echo "  Total: $((PASS + FAIL))  |  PASS: $PASS  |  FAIL: $FAIL"
echo "════════════════════════════════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
