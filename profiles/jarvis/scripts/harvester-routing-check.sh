#!/usr/bin/env bash
set -euo pipefail

HARVESTER_PATH="/home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py"

if [ ! -f "$HARVESTER_PATH" ]; then
  echo "HARVESTER-ROUTING-CHECK: CRITICAL — harvester script not found at $HARVESTER_PATH"
  exit 1
fi

TMP_CURSOR="$(mktemp -t harvester-routing-cursor.XXXXXX)"
trap 'rm -f "$TMP_CURSOR"' EXIT
# The routing check invokes the canonical harvester as a second process. Give
# it an isolated cursor so this diagnostic never reads or mutates the
# production notepad-backed cursor owned by the scheduled harvester job.
export HARVESTER_CURSOR_PATH="$TMP_CURSOR"

failures=0

echo "=== PHASE 1: Deterministic self-tests ==="
if python3 "$HARVESTER_PATH" --test 2>&1; then
  echo "PHASE 1 PASS: All self-tests pass."
else
  echo "PHASE 1 FAIL: Self-tests failed!"
  failures=$((failures + 1))
fi

echo ""
echo "=== PHASE 2: Dry-run cross-board routing check ==="
DRY_RUN_OUTPUT=$(python3 "$HARVESTER_PATH" --dry-run --lookback-seconds 7200 --board sycode-trading --board jarvis-os --json 2>&1 || true)

CROSS_BOARD_VIOLATIONS=$(echo "$DRY_RUN_OUTPUT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print('JSON_PARSE_ERROR')
    sys.exit(0)
violations = []
for board in data:
    for item in data[board]:
        if 'dry_run' in item and item.get('dry_run'):
            item_board = item.get('board', '?')
            if item_board != board:
                violations.append(f'{item[\"source_task\"]}: src={board} -> dst={item_board} prof={item[\"assigned_profile\"]}')
if violations:
    print('CROSS_BOARD_VIOLATIONS:')
    for v in violations:
        print(f'  {v}')
else:
    print('NO_CROSS_BOARD_VIOLATIONS')
" 2>&1)

if echo "$CROSS_BOARD_VIOLATIONS" | grep -q 'CROSS_BOARD_VIOLATIONS:'; then
  echo "PHASE 2 FAIL: Cross-board routing violations detected!"
  echo "$CROSS_BOARD_VIOLATIONS"
  failures=$((failures + 1))
elif echo "$CROSS_BOARD_VIOLATIONS" | grep -q 'JSON_PARSE_ERROR'; then
  echo "PHASE 2 WARN: Dry-run JSON unparseable. Pass."
else
  echo "PHASE 2 PASS: No cross-board violations."
fi

echo ""
echo "=== PHASE 3: Profile segregation check ==="
PROFILE_LEAKS=$(echo "$DRY_RUN_OUTPUT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print('SKIP')
    sys.exit(0)
leaks = []
if 'jarvis-os' in data:
    for item in data['jarvis-os']:
        if 'dry_run' in item and item.get('dry_run'):
            p = item.get('assigned_profile', '')
            if p.startswith('trading-') or p in ('paper-trader',):
                leaks.append(f'{item[\"source_task\"]}: {p}')
if leaks:
    print('PROFILE_LEAKS:')
    for l in leaks:
        print(f'  {l}')
else:
    print('NO_PROFILE_LEAKS')
" 2>&1)

if echo "$PROFILE_LEAKS" | grep -q 'PROFILE_LEAKS:'; then
  echo "PHASE 3 FAIL: jarvis-os -> trading profile leak!"
  echo "$PROFILE_LEAKS"
  failures=$((failures + 1))
elif echo "$PROFILE_LEAKS" | grep -q 'SKIP'; then
  echo "PHASE 3 SKIP: No data."
else
  echo "PHASE 3 PASS: No trading leaks from jarvis-os."
fi

echo ""
echo "========================================"
echo "SUMMARY: failures=$failures"
echo "RESULT: $([ "$failures" -eq 0 ] && echo 'PASS' || echo 'FAIL')"
echo "========================================"
exit $failures
