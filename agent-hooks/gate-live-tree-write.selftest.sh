#!/usr/bin/env bash
# Selftest for gate-live-tree-write.sh — reproduces the exact t_8b5495cd
# incident payload and asserts the hook now blocks it, plus checks positive
# controls (interactive session, non-git command, docker command already
# covered elsewhere) stay allowed.
set -euo pipefail
HOOK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gate-live-tree-write.sh"
FAIL=0

check_block() {
  local desc="$1" payload="$2"
  shift 2
  out=$(printf '%s' "$payload" | env -u ALLOW_LIVE_TREE_WRITE "$@" "$HOOK")
  if printf '%s' "$out" | grep -q '"decision": *"block"\|"decision":"block"'; then
    echo "PASS (blocked): $desc"
  else
    echo "FAIL (expected block, got allow): $desc -> $out"
    FAIL=1
  fi
}

check_allow() {
  local desc="$1" payload="$2"
  shift 2
  out=$(printf '%s' "$payload" | env -u ALLOW_LIVE_TREE_WRITE "$@" "$HOOK")
  if [ "$out" = "{}" ]; then
    echo "PASS (allowed): $desc"
  else
    echo "FAIL (expected allow, got block): $desc -> $out"
    FAIL=1
  fi
}

# 1. The exact incident: `git commit` inside the live tree, as a kanban worker.
check_block "git commit in live tree, kanban worker" \
  '{"tool_name":"terminal","tool_input":{"command":"cd /home/frank/.hermes/hermes-agent && git commit -am \"stricter guards\""}}' \
  HERMES_KANBAN_TASK=t_test123

# 2. git -C form (option-order gap seen in the t_de45fb07 review history).
check_block "git -C commit in live tree, kanban worker" \
  '{"tool_name":"terminal","tool_input":{"command":"git -C /home/frank/.hermes/hermes-agent commit -m x"}}' \
  HERMES_KANBAN_TASK=t_test123

# 3. Direct file write tool targeting the live tree.
check_block "write_file into live tree, kanban worker" \
  '{"tool_name":"write_file","tool_input":{"path":"/home/frank/.hermes/hermes-agent/tools/kanban_tools.py","content":"x"}}' \
  HERMES_KANBAN_TASK=t_test123

# 4. Same git commit, but NOT a kanban worker (interactive/operator session) -> allow.
check_allow "git commit in live tree, interactive session (no HERMES_KANBAN_TASK)" \
  '{"tool_name":"terminal","tool_input":{"command":"cd /home/frank/.hermes/hermes-agent && git commit -am x"}}'

# 5. Read-only git command in the live tree, kanban worker -> allow.
check_allow "git status in live tree, kanban worker" \
  '{"tool_name":"terminal","tool_input":{"command":"cd /home/frank/.hermes/hermes-agent && git status"}}' \
  HERMES_KANBAN_TASK=t_test123

# 6. git commit in an unrelated (non-protected) worktree, kanban worker -> allow.
check_allow "git commit in scratch worktree, kanban worker" \
  '{"tool_name":"terminal","tool_input":{"command":"cd /home/frank/.hermes/kanban/boards/jarvis-os/workspaces/t_x/wt/extract && git commit -am x"}}' \
  HERMES_KANBAN_TASK=t_test123

# 7. Operator-only bypass still works.
check_allow "ALLOW_LIVE_TREE_WRITE=1 bypass" \
  '{"tool_name":"terminal","tool_input":{"command":"cd /home/frank/.hermes/hermes-agent && git commit -am x"}}' \
  HERMES_KANBAN_TASK=t_test123 ALLOW_LIVE_TREE_WRITE=1

# 8. Completion gate: no baseline file -> allow (fail-open, nothing configured yet).
check_allow "kanban_complete with no known-good baseline configured" \
  '{"tool_name":"kanban_complete","tool_input":{"summary":"done"}}' \
  HERMES_KANBAN_TASK=t_test123 HERMES_LIVE_TREE_GUARD_ROOTS=/tmp/nonexistent-root-xyz

# 9. Completion gate: baseline present and MATCHES current HEAD -> allow.
BASELINE_ROOT="/tmp/gate-live-tree-selftest-repo-$$"
rm -rf "$BASELINE_ROOT" 2>/dev/null || true
mkdir -p "$BASELINE_ROOT"
(cd "$BASELINE_ROOT" && git init -q && git config user.email t@t && git config user.name t && touch f && git add f && git commit -qm init)
SHA=$(cd "$BASELINE_ROOT" && git rev-parse HEAD)
BASELINE_FILE="/tmp/gate-live-tree-selftest-known-good-$$.sha"
echo "$BASELINE_ROOT=$SHA" > "$BASELINE_FILE"
check_allow "kanban_complete with matching known-good baseline" \
  '{"tool_name":"kanban_complete","tool_input":{"summary":"done"}}' \
  HERMES_KANBAN_TASK=t_test123 "HERMES_LIVE_TREE_GUARD_ROOTS=$BASELINE_ROOT" \
  "HERMES_LIVE_TREE_KNOWN_GOOD_FILE=$BASELINE_FILE"

# 10. Completion gate: baseline present and DIVERGED -> block.
(cd "$BASELINE_ROOT" && git commit -q --allow-empty -m second)
check_block "kanban_complete with diverged known-good baseline" \
  '{"tool_name":"kanban_complete","tool_input":{"summary":"done"}}' \
  HERMES_KANBAN_TASK=t_test123 "HERMES_LIVE_TREE_GUARD_ROOTS=$BASELINE_ROOT" \
  "HERMES_LIVE_TREE_KNOWN_GOOD_FILE=$BASELINE_FILE"
rm -rf "$BASELINE_ROOT" "$BASELINE_FILE" 2>/dev/null || true

if [ "$FAIL" = "1" ]; then
  echo "SELFTEST: FAILURES PRESENT"
  exit 1
fi
echo "SELFTEST: ALL PASS"
