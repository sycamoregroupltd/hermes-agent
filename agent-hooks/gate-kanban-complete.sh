#!/usr/bin/env bash
# pre_tool_call hook on kanban_complete: HARD-GATE frontend/web task completion on
# the running-app check. Workers/judges are LLMs and can be fooled by prose (proven:
# QuickNoteTracker marked done while serving HTTP 500). This is the deterministic veto.
# Goal-judge provider-error completion traps also fail closed into a deterministic
# quarantine/override lane so already-reviewed tasks do not recirculate silently.
#
# Contract: read JSON payload on stdin; emit {"decision":"block","reason":...} to veto,
# or {} to allow. FAIL-OPEN on ambiguity — never wedge the fleet on uncertainty.
#
# VERIFICATION_MATRIX
# - store: /home/frank/.hermes/agent-hooks/gate-kanban-complete.sh
# - liveness: bash /home/frank/.hermes/agent-hooks/gate-kanban-complete.selftest.sh
# - deliver target: local hook validation in kanban_complete pre_tool_call path
# - named consumer: jarvis-os-pm / os-reviewer deterministic completion-gate evidence
# - satisfied verification: gate-kanban-complete selftest output + this task's review
set -uo pipefail || true
payload=$(cat 2>/dev/null)
allow() { echo '{}'; exit 0; }
block() { python3 -c "import json,sys;print(json.dumps({'decision':'block','reason':sys.argv[1]}))" "$1"; exit 0; }
kanban_root=${HERMES_HOOK_KANBAN_ROOT:-/home/frank/.hermes}
# Fail-closed for goal-judge provider-error completion traps. Externally emitted
# provider exceptions during completion can otherwise leave an already-reviewed
# task non-terminal. Convert exact repeated judge provider errors into a
# deterministic operator-quarantine/override lane marker instead of allowing
# completion to die silently or loop on the same failure.
goal_judge_provider_error_block() {
  local reason="$1"
  block "GOAL_JUDGE_PROVIDER_ERROR_QUARANTINE: ${reason}. Task evidence/review state is preserved; completion requires an operator override via docs/governance or an evidence-based handoff to a terminal-capable control-plane review."
}

# Only gate kanban_complete
tool=$(printf '%s' "$payload" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('tool_name',''))" 2>/dev/null) || allow
[ "$tool" = "kanban_complete" ] || [ "$tool" = "kanban_sh_complete" ] || allow

# Extract task_id + board from tool_input/extra
read tid board <<<"$(printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ti=d.get('tool_input',{}) or {}
ex=d.get('extra',{}) or {}
tid=ti.get('task_id') or ti.get('id') or ex.get('task_id') or ''
board=ti.get('board') or ex.get('board') or ''
print(tid, board)
" 2>/dev/null)"
[ -n "$tid" ] || allow   # can't identify the task -> fail open

# Find the board db if board not given
dbs=()
if [ -n "$board" ]; then dbs=("$kanban_root/kanban/boards/$board/kanban.db"); else
  for d in "$kanban_root"/kanban/boards/*/kanban.db; do dbs+=("$d"); done
fi
title=""; body=""
for db in "${dbs[@]}"; do
  [ -f "$db" ] || continue
  row=$(sqlite3 -separator $'\x1f' "$db" "SELECT title, COALESCE(body,'') FROM tasks WHERE id='$tid'" 2>/dev/null)
  [ -n "$row" ] && { title="${row%%$'\x1f'*}"; body="${row#*$'\x1f'}"; dbfound="$db"; break; }
done
[ -n "$title$body" ] || allow   # task not found -> fail open

# --- Stale-reference reconciliation (dry-run by default; no mutations) ---
# Invoke reconcile-referenced-done.py when a kanban_complete is being allowed
# for an identified task on a known board. Default: dry-run only (no --apply).
# Set HERMES_HOOK_RECONCILE_APPLY=1 to enable live mutations (requires
# os-reviewer sign-off per t_75780eaf acceptance contract).
if [ -n "$tid" ] && [ -n "${dbfound:-}" ]; then
  board_arg=""
  if [ -n "$board" ]; then board_arg="--board $board"; fi
  reconcile_apply=""
  [ "${HERMES_HOOK_RECONCILE_APPLY:-}" = "1" ] && reconcile_apply="--apply"
  reconcile_output=$(python3 "$(dirname "$0")/reconcile-referenced-done.py" \
    $board_arg \
    --db "$dbfound" \
    --done-id "$tid" \
    --completing-id "$tid" \
    $reconcile_apply 2>&1 || true)
  if [ -n "$reconcile_output" ]; then
    # Prefix each line so it's distinguishable from the gate verdict output
    printf '%s\n' "$reconcile_output" | sed 's/^/[reconcile-referenced-done] /' >&2
  fi
fi

# After reconciliation, proceed to the classification gate. (only those need the running-app gate).
# Use token/phrase matching instead of broad substrings: profile-audit tasks can
# contain words like "observed" (includes "serve") or profile names like
# "testproj-ui-builder" without being web/app work.
#
# Read-only evidence/data-health cards often mention HTTP endpoints (/ready,
# /metrics), logs, and SELECTs as OBSERVATIONS. Those are not frontend/app
# implementation tasks and must not be wedged by the running-app gate when
# durable comments/summary already prove their read-only scope. Classify them
# before the broad web check, but only when no explicit UI/app implementation
# surface is present.
comments=""
if [ -n "${dbfound:-}" ]; then
  comments=$(sqlite3 "$dbfound" "SELECT COALESCE(group_concat(body, char(10)),'') FROM (SELECT body FROM task_comments WHERE task_id='$tid' ORDER BY created_at DESC LIMIT 8)" 2>/dev/null || true)
fi
input_text=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
inp=d.get("tool_input",{}) or {}
parts=[]
for key in ("summary", "result"):
    val=inp.get(key)
    if val: parts.append(str(val))
md=inp.get("metadata")
if md is not None:
    parts.append(json.dumps(md, sort_keys=True))
print("\n".join(parts))
' 2>/dev/null || true)
classifier="${HERMES_HOOK_CLASSIFIER:-$(dirname "$0")/gate-kanban-complete-classifier.py}"
class=$(printf '%s\n---BODY---\n%s\n---COMMENTS---\n%s\n---INPUT---\n%s' "$title" "$body" "$comments" "$input_text" | python3 "$classifier" 2>/dev/null) || allow
# Fail-closed goal-judge provider-error trap MUST be evaluated BEFORE the
# readonly_nonapp short-circuit: otherwise a card that merely quotes a child
# review's REVIEW_VERDICT gets allowed before the quarantine lane can fire.
# The trap is content-based (no task-id format prerequisite) so non-hex /
# synthetic goal-mode ids cannot fail open. The classifier's strict
# verified-review override lane (ALLOW_VERIFIED_REVIEW_WITH_EVIDENCE_OVERRIDE)
# is the only way such a card becomes readonly_nonapp and escapes the trap.
if [[ "$title${body}${comments}${input_text}" =~ ([Gg]oal[-_ ]?[Jj]udge|[Gg]oal[-_ ]?mode).*([Gg]emini|[N]ot[Ff]ound|provider[-_ ]?error) ]]; then
  if [ "$class" != "readonly_nonapp" ]; then
    goal_judge_provider_error_block "Task title/body/comments indicate goal-judge provider error for task $tid"
  fi
fi
[ "$class" = "readonly_nonapp" ] && allow
[ "$class" = "web" ] || allow   # not a web task -> not our gate -> allow

# DO NOT trust "VERIFY_PASS" in the body — task descriptions contain the word as an
# INSTRUCTION, not evidence. Trust only either:
#   1. the current kanban_complete input (summary/result/metadata) pasted by the worker, or
#   2. recent task comments that include the RUNNING_APP_VERIFICATION marker.
# If the body carries a runnable local URL, prefer the strongest signal: RUN the gate now.
url=$(printf '%s' "$body" | grep -oiE "https?://[^ \"')]+" | grep -iE 'localhost|127.0.0.1|spark-4be3' | head -1)
# Task bodies sometimes document placeholder URLs such as http://127.0.0.1:<port>
# as instructions for the worker. Do not run the gate against placeholders; they
# are not reusable endpoints and would wedge an otherwise-evidenced completion
# before the VERIFY_PASS comment/input checks below can run.
if [ -n "$url" ]; then
  runnable_url=$(printf '%s' "${url%/}" | python3 -c '
import sys
from urllib.parse import urlparse
u=sys.stdin.read().strip()
try:
    p=urlparse(u)
    if p.scheme in ("http", "https") and p.hostname and p.port is not None:
        print(u)
except Exception:
    pass
' 2>/dev/null || true)
  if [ -n "$runnable_url" ]; then
    out=$(bash /home/frank/.hermes/scripts/verify-running-app.sh "$runnable_url" / 2>/dev/null)
    case "$out" in
      *VERIFY_PASS*) allow ;;
      *VERIFY_FAIL*) block "Running-app gate FAILED: $out. 'done' requires the app to actually serve (HTTP 200 + real content). Run verify-running-app.sh and paste VERIFY_PASS, or fix the app." ;;
    esac
  fi
fi

# Accept explicit evidence supplied with this completion attempt. This lets a worker
# complete web work after running verify-running-app.sh even when the task body does
# not contain a reusable localhost URL. The original task body is intentionally not
# part of input_text.
if printf '%s' "$input_text" | grep -Eq '(^|[^A-Z0-9_])VERIFY_PASS([^A-Z0-9_]|$)' \
  && ! printf '%s' "$input_text" | grep -Eiq '(no|without|missing|requires?|not|lacks?).{0,60}VERIFY_PASS'; then
  allow
fi

# Accept recent durable reviewer/worker comments only when a single explicit,
# non-negated RUNNING_APP_VERIFICATION packet includes a non-negated VERIFY_PASS
# line. Do not accept aggregate comment text where one sentence negates/misses the
# packet marker and another merely quotes bare VERIFY_PASS as discussion.
if printf '%s' "$comments" | python3 -c '
import re, sys

lines = sys.stdin.read().splitlines()
verify_re = re.compile(r"(^|[^A-Z0-9_])VERIFY_PASS([^A-Z0-9_]|$)")
neg_marker_re = re.compile(
    r"\b(no|without|missing|requires?|not|lacks?|cannot|can.t)\b.{0,80}\bRUNNING_APP_VERIFICATION\b"
    r"|\bRUNNING_APP_VERIFICATION\b.{0,80}\b(no|without|missing|requires?|not|lacks?|cannot|can.t)\b",
    re.I,
)
neg_verify_re = re.compile(
    r"\b(no|without|missing|requires?|not|lacks?|cannot|can.t|bare|example|quoted)\b.{0,80}\bVERIFY_PASS\b"
    r"|\bVERIFY_PASS\b.{0,80}\b(no|without|missing|requires?|not|lacks?|cannot|can.t|bare|example|quoted)\b",
    re.I,
)

for idx, line in enumerate(lines):
    if "RUNNING_APP_VERIFICATION" not in line:
        continue
    if neg_marker_re.search(line):
        continue
    packet = "\n".join(lines[idx : idx + 4])
    if verify_re.search(packet) and not neg_verify_re.search(packet):
        raise SystemExit(0)
raise SystemExit(1)
'; then
  allow
fi

# Web task, no VERIFY_PASS evidence, no runnable url -> require the evidence (block).
block "This is a frontend/web task but no VERIFY_PASS evidence is present. Run /home/frank/.hermes/scripts/verify-running-app.sh against the running route and include its VERIFY_PASS output before completing. 'type-check green' is not done."
