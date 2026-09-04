#!/usr/bin/env bash
# Fail-open regression suite for gate-append-only-writes.py.
# Named cases match the t_42f29466 / t_0a142cbe acceptance list.
# Never points at live arena journals; uses an isolated temp tree.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="${GATE:-$SCRIPT_DIR/gate-append-only-writes.py}"
ROOT="$(mktemp -d -t t42-append-gate-XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT
ARENA="$ROOT/obsidian/quant-team/trading-arena"
mkdir -p "$ARENA/trader-2"
JOURNAL="$ARENA/trader-2/journal.md"
IMPROVEMENTS="$ARENA/IMPROVEMENTS.md"
CYCLE="$ARENA/trader-2/journal-t_deadbeef.md"
printf '%s\n' '---' 'title: Fixture' 'type: task-evidence' 'status: active' 'created: 2026-09-03' 'updated: 2026-09-03' 'confidence: unknown' 'tags: [test]' 'sources: []' '---' 'HISTORY' > "$JOURNAL"
printf '%s\n' '---' 'title: Improvements' 'type: task-evidence' 'status: active' 'created: 2026-09-03' 'updated: 2026-09-03' 'confidence: unknown' 'tags: [test]' 'sources: []' '---' 'HISTORY' > "$IMPROVEMENTS"

run_gate() { printf '%s' "$2" | python3 "$GATE"; }
PASS=0
FAIL=0
assert_block() {
  local name="$1" payload="$2" out
  out="$(run_gate "$name" "$payload")"
  if python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1])
assert obj.get("decision") == "block" and obj.get("action") == "block", obj
PY
  then printf 'PASS block: %s\n' "$name"; PASS=$((PASS + 1))
  else printf 'FAIL block: %s -> %s\n' "$name" "$out"; FAIL=$((FAIL + 1)); fi
}
assert_allow() {
  local name="$1" payload="$2" out
  out="$(run_gate "$name" "$payload")"
  if python3 - "$out" <<'PY'
import json, sys
assert json.loads(sys.argv[1]) == {}, sys.argv[1]
PY
  then printf 'PASS allow: %s\n' "$name"; PASS=$((PASS + 1))
  else printf 'FAIL allow: %s -> %s\n' "$name" "$out"; FAIL=$((FAIL + 1)); fi
}

EXISTING='{"tool_name":"write_file","tool_input":{"path":"'"$JOURNAL"'","content":"stub"}}'
assert_block existing-seat-journal "$EXISTING"
assert_block improvements-md '{"tool_name":"write_file","tool_input":{"path":"'"$IMPROVEMENTS"'","content":"stub"}}'
assert_allow per-cycle-journal '{"tool_name":"write_file","tool_input":{"path":"'"$CYCLE"'","content":"new"}}'
assert_allow patch-append '{"tool_name":"patch","tool_input":{"path":"'"$JOURNAL"'","old_string":"HISTORY","new_string":"HISTORY\nAPPENDED"}}'
assert_block patch-shrink '{"tool_name":"patch","tool_input":{"path":"'"$JOURNAL"'","old_string":"HISTORY","new_string":"stub"}}'
assert_block terminal-redirect '{"tool_name":"terminal","tool_input":{"command":"echo x > '"$JOURNAL"'"}}'
assert_allow terminal-true-append '{"tool_name":"terminal","tool_input":{"command":"echo x >> '"$JOURNAL"'"}}'
assert_allow unrelated-write '{"tool_name":"write_file","tool_input":{"path":"/tmp/foo.md","content":"new"}}'
assert_allow fail-open-garbage 'not-json'
printf 'gate-append-only selftest: PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
