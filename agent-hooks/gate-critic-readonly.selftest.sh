#!/usr/bin/env bash
# Deterministic selftests for gate-critic-readonly.py.
# No live board, credential, network, or runtime mutation required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$SCRIPT_DIR/gate-critic-readonly.py"
DUPE_GATE="$SCRIPT_DIR/gate-kanban-dupe-create.sh"

PASS=0
FAIL=0

run_gate() {
  local tool="$1"
  local input_json="$2"
  python3 "$GATE" <<JSON
{"tool_name":"$tool","tool_input":$input_json,"extra":{"profile":"critic-selftest"}}
JSON
}

assert_allow() {
  local name="$1"
  local tool="$2"
  local input_json="$3"
  local out
  out="$(run_gate "$tool" "$input_json")"
  if python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1] or '{}')
raise SystemExit(0 if obj == {} else 1)
PY
  then
    printf 'PASS allow: %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL allow: %s -> %s\n' "$name" "$out"
    FAIL=$((FAIL + 1))
  fi
}

assert_block() {
  local name="$1"
  local tool="$2"
  local input_json="$3"
  local out
  out="$(run_gate "$tool" "$input_json")"
  if python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1] or '{}')
raise SystemExit(0 if obj.get('decision') == 'block' and obj.get('action') == 'block' and 'Critic read-only gate' in obj.get('reason', '') else 1)
PY
  then
    printf 'PASS block: %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL block: %s -> %s\n' "$name" "$out"
    FAIL=$((FAIL + 1))
  fi
}

# Control-plane routing tools reviewers must retain.
assert_allow "kanban_comment unchanged" "kanban_comment" '{"task_id":"t_parent","body":"REVIEW_VERDICT: APPROVED"}'
assert_allow "kanban_block unchanged" "kanban_block" '{"task_id":"t_parent","reason":"review-required"}'
assert_allow "kanban_complete unchanged" "kanban_complete" '{"summary":"review done","metadata":{"verdict":"approved"}}'

# Regression: exact kanban_create is a control-plane route, not artifact creation.
assert_allow "exact kanban_create with routing fields" "kanban_create" '{"title":"Implement accepted reviewer recommendation","body":"route builder work; no artifact mutation by reviewer","assignee":"devops","parents":["t_parent"],"idempotency_key":"critic-route-t_parent"}'

# Artifact and source mutation remain blocked.
assert_block "create_file source edit" "create_file" '{"path":"/repo/src/app.py","content":"print(1)"}'
assert_block "write_file source edit" "write_file" '{"path":"/repo/src/app.py","content":"print(1)"}'
assert_block "patch source edit" "patch" '{"path":"/repo/src/app.py","old_string":"a","new_string":"b"}'
assert_block "namespaced write tool" "mcp__fs__write_file" '{"path":"/repo/src/app.py","content":"print(1)"}'
assert_block "terminal git mutation" "terminal" '{"command":"git add src/app.py && git commit -m change"}'
assert_block "content-less write tool stays blocked" "create_file" '{"content":"x"}'

# Deceptive names containing create must not inherit the exact kanban_create exception.
assert_block "deceptive create name without path" "kanban_create_file" '{"title":"not routing","body":"write-ish payload"}'
assert_block "namespaced deceptive create name without path" "mcp__board__kanban_create_file" '{"title":"not routing","body":"write-ish payload"}'

# The separate kanban duplicate/Frank-gate hook must remain present and executable for kanban_create.
# A healthy hook may allow this harmless synthetic payload, but it must parse and emit JSON instead of disappearing.
dupe_out="$(printf '%s' '{"tool_name":"kanban_create","tool_input":{"title":"critic selftest synthetic route","body":"selftest only","assignee":"devops","parents":["t_parent"],"idempotency_key":"critic-selftest"}}' | bash "$DUPE_GATE")"
if python3 - "$dupe_out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1] or '{}')
raise SystemExit(0 if isinstance(obj, dict) else 1)
PY
then
  printf 'PASS dupe hook executable for kanban_create: %s\n' "$dupe_out"
  PASS=$((PASS + 1))
else
  printf 'FAIL dupe hook executable for kanban_create -> %s\n' "$dupe_out"
  FAIL=$((FAIL + 1))
fi

printf 'gate-critic-readonly selftest: PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
