#!/usr/bin/env bash
# Deterministic selftests for gate-pm-landing.py (t_ecf1d553).
# No live board, credential, network, remote, or runtime mutation required.
#
# The contract under test: a landing PM may do everything its SOUL mandates
# (commit, feature-branch push, dry-run auth check, fetch, merge verification,
# tests) and cannot do the irreversible remote things its SOUL forbids
# (trunk push, NousResearch push, force/delete push, deploy-tree mutation).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$SCRIPT_DIR/gate-pm-landing.py"

PASS=0
FAIL=0

run_gate() {
  local tool="$1" input_json="$2"
  python3 "$GATE" <<JSON
{"tool_name":"$tool","tool_input":$input_json,"extra":{"profile":"pm-landing-selftest"}}
JSON
}

assert_allow() {
  local name="$1" tool="$2" input_json="$3" out
  out="$(run_gate "$tool" "$input_json")"
  if python3 - "$out" <<'PY'
import json, sys
raise SystemExit(0 if json.loads(sys.argv[1] or '{}') == {} else 1)
PY
  then printf 'PASS allow: %s\n' "$name"; PASS=$((PASS + 1))
  else printf 'FAIL allow: %s -> %s\n' "$name" "$out"; FAIL=$((FAIL + 1)); fi
}

assert_block() {
  local name="$1" tool="$2" input_json="$3" out
  out="$(run_gate "$tool" "$input_json")"
  if python3 - "$out" <<'PY'
import json, sys
obj = json.loads(sys.argv[1] or '{}')
raise SystemExit(0 if obj.get('decision') == 'block' and obj.get('action') == 'block'
                 and 'PM landing gate' in obj.get('reason', '') else 1)
PY
  then printf 'PASS block: %s\n' "$name"; PASS=$((PASS + 1))
  else printf 'FAIL block: %s -> %s\n' "$name" "$out"; FAIL=$((FAIL + 1)); fi
}

echo "--- BLOCK: irreversible / out-of-scope remote operations"
assert_block "push to main"                 "terminal" '{"command":"git push origin main"}'
assert_block "push to master"               "terminal" '{"command":"git push origin master"}'
assert_block "push to production"           "terminal" '{"command":"git push origin production"}'
assert_block "push refspec HEAD:main"       "terminal" '{"command":"git push origin HEAD:main"}'
assert_block "push src:dst landing on main" "terminal" '{"command":"git push origin feat/x:main"}'
assert_block "force push feature branch"    "terminal" '{"command":"git push --force origin feat/x"}'
assert_block "force-with-lease"             "terminal" '{"command":"git push --force-with-lease origin feat/x"}'
assert_block "short -f force push"          "terminal" '{"command":"git push -f origin feat/x"}'
assert_block "force refspec +ref"           "terminal" '{"command":"git push origin +feat/x:feat/x"}'
assert_block "delete remote branch"         "terminal" '{"command":"git push origin --delete feat/x"}'
assert_block "refspec deletion :branch"     "terminal" '{"command":"git push origin :feat/x"}'
assert_block "mirror push"                  "terminal" '{"command":"git push --mirror origin"}'
assert_block "push to NousResearch"         "terminal" '{"command":"git push git@github.com:NousResearch/hermes-agent.git feat/x"}'
assert_block "reset --hard"                 "terminal" '{"command":"git reset --hard origin/main"}'
assert_block "branch -D"                    "terminal" '{"command":"git branch -D feat/old"}'
assert_block "clean -fd"                    "terminal" '{"command":"git clean -fd"}'
assert_block "clean -df (flag order)"       "terminal" '{"command":"git clean -df"}'
assert_block "clean -xfd"                   "terminal" '{"command":"git clean -xfd"}'
assert_block "clean --force"                "terminal" '{"command":"git clean --force"}'
assert_block "reset --hard no ref"          "terminal" '{"command":"git reset --hard"}'
assert_block "commit in deploy tree"        "terminal" '{"command":"cd ~/.hermes/deploy-state/build-tree && git commit -m wip"}'
assert_block "trunk push after chained cmd" "terminal" '{"command":"bun test && git push origin main"}'
assert_block "release branch push"          "terminal" '{"command":"git push origin release/2026-08"}'

echo "--- ALLOW: everything a landing PM legitimately must do"
assert_allow "feature branch push"          "terminal" '{"command":"git push origin feat/t_ecf1d553-fix"}'
assert_allow "push -u feature branch"       "terminal" '{"command":"git push -u origin devops/t_cb0c8366"}'
assert_allow "SOUL rule 1 dry-run to main"  "terminal" '{"command":"git push --dry-run origin main"}'
assert_allow "ls-remote auth check"         "terminal" '{"command":"git ls-remote origin"}'
assert_allow "fetch origin main"            "terminal" '{"command":"git fetch origin main"}'
assert_allow "local commit before complete" "terminal" '{"command":"git add . && git commit -m \"feat: land approved work\""}'
assert_allow "merge verification"           "terminal" '{"command":"git merge-base --is-ancestor feat/x origin/main"}'
assert_allow "local merge"                  "terminal" '{"command":"git merge --no-ff feat/x"}'
assert_allow "worktree add"                 "terminal" '{"command":"git worktree add .worktrees/t_x wt/t_x"}'
assert_allow "status/diff/log"              "terminal" '{"command":"git status && git diff && git log --oneline -5"}'
assert_allow "run the tests"                "terminal" '{"command":"bun test"}'
assert_allow "type-check"                   "terminal" '{"command":"bun run type-check"}'
assert_allow "verify-running-app harness"   "terminal" '{"command":"bash /home/frank/.hermes/scripts/verify-running-app.sh http://localhost:3000 /marketplace"}'
assert_allow "pytest"                       "terminal" '{"command":"pytest -q"}'
assert_allow "non-terminal tool untouched"  "kanban_comment" '{"task_id":"t_x","body":"landed"}'
assert_allow "write_file untouched"         "write_file" '{"path":"/tmp/x.md","content":"note"}'
assert_allow "branch named mainline is ok"  "terminal" '{"command":"git push origin feat/mainline-refactor"}'
assert_allow "deploy-tree read is fine"     "terminal" '{"command":"git -C ~/.hermes/deploy-state/build-tree status"}'

echo "--- fail-open contract"
if [ "$(printf 'not json' | python3 "$GATE")" = "{}" ]; then
  printf 'PASS fail-open: malformed payload allows\n'; PASS=$((PASS + 1))
else
  printf 'FAIL fail-open: malformed payload did not allow\n'; FAIL=$((FAIL + 1))
fi

printf '\ngate-pm-landing selftest: PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -ne 0 ] && exit 1
exit 0
