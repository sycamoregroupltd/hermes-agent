#!/usr/bin/env bash
# subagent_stop hook: detect a worker that finished WITHOUT a clean terminal
# kanban signal (the rc=0-without-kanban_complete phantom-block class that
# silently stalls boards). This is the *alert + deterministic record* side of
# the completion-gate; the hard veto/auto-block decision lives in the
# dispatcher reaper (hermes_cli/kanban_db.py:detect_crashed_workers) — this
# hook does NOT block (subagent_stop is post-hoc) but it MUST leave an
# unambiguous, machine-grepable record so the CI gate
# (ci_no_silent_exit_blocks.py) and the reaper (reap_silent_exit_blocks.py)
# can tell a real completion-skip from a provider-stage death.
#
# stdin payload shape (see hermes_cli/hooks.py _DEFAULT_PAYLOADS / _serialize_payload):
#   { "hook_event_name": "subagent_stop",
#     "extra": { "child_status": "...", "child_role": "...", "child_summary": "..." } }
set -uo pipefail
payload=$(cat 2>/dev/null)

quiet() { echo '{}'; exit 0; }
block() { python3 -c "import json,sys;print(json.dumps({'decision':'block','reason':sys.argv[1]}))" "$1"; exit 0; }

status=$(printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
extra=d.get('extra') or {}
print(extra.get('child_status') or '')
" 2>/dev/null) || quiet

role=$(printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
extra=d.get('extra') or {}
print(extra.get('child_role') or '-')
" 2>/dev/null)

summ=$(printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
extra=d.get('extra') or {}
print((extra.get('child_summary') or '')[:400])
" 2>/dev/null)

# Zombie / silent-exit detection: a worker that reached child_status=completed
# but never emitted kanban_complete / kanban_block is a protocol-violation
# zombie. Heuristic: the dispatcher sets the summary to include the exact
# "without calling kanban_complete" text on this class of clean exit.
if [ "$status" = "completed" ]; then
    zombie=$(printf '%s' "$summ" | grep -q "without calling kanban_complete" && echo 1 || echo 0)
else
    zombie=0
fi

# Clean terminal states with no zombie signal -> nothing to flag.
if [ "$zombie" != "1" ]; then
    case "$status" in completed|done|blocked|""|none) quiet ;; esac
fi

# --- Deterministic record for CI gate + reaper ---
LOG="/home/frank/.hermes/logs/silent-exit-watch.log"
mkdir -p "$(dirname "$LOG")"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo now)

# Classify the zombie so the reaper/CI can tell a genuine completion-skip from a
# provider-stage death. The worker nearly always dies BEFORE executing task
# logic when the provider pre-reasoning / API auth fails; that class should be
# re-driven, not hard-blocked. We stamp the record accordingly.
class="completion_skip"   # default: worker may have done real work then forgot
if printf '%s' "$summ" | grep -qiE "provider_|pre_reasoning|provider_error|provider_pre_reasoning|429|rate.?limit|auth|api"; then
    class="provider_stage_death"
fi

echo "$ts role=$role status=$status zombie=$zombie class=$class :: $summ" >> "$LOG"

# Emit a block signal too (used by the regression harness; ignored by
# _parse_response for subagent_stop, but produces a useful terminal signal).
block "missing terminal kanban signal (class=$class) — worker completed (child_status=completed) without calling kanban_complete or kanban_block"

# Best-effort telegram alert (never block on delivery).
hermes send -t telegram -q "⚠ Worker silent exit: role=$role status=$status zombie=$zombie class=$class — $summ" >/dev/null 2>&1 || true

quiet
