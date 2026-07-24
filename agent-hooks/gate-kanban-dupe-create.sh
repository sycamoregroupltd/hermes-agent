#!/usr/bin/env bash
# pre_tool_call hook (matcher: kanban_create): HARD-GATE task creation against
# (1) cloning a currently gate-blocked task (link, don't clone) and
# (2) assigning Frank-gated work to a profile with no gate language in its SOUL.
#
# Born from the 2026-07-05 out-of-band production DDL incident: sycode-trading-pm
# cloned gate-blocked t_5c25f222 into t_d0fcaddb on a gate-agnostic profile,
# which applied production DDL out-of-band.
#
# Contract: read JSON payload on stdin; emit {"decision":"block","reason":...}
# to veto, or {} to allow. FAIL-OPEN on any ambiguity/error — never wedge the
# fleet on guard malfunction. Detection logic lives in
# ~/.hermes/scripts/kanban_dedupe_guard.py --hook-check (single source of truth,
# shared with the cron backstop).
set -uo pipefail || true
payload=$(cat 2>/dev/null)
allow() { echo '{}'; exit 0; }

[ -n "$payload" ] || allow
guard=/home/frank/.hermes/scripts/kanban_dedupe_guard.py
[ -f "$guard" ] || allow

# Only gate kanban_create (matcher already filters, but double-check: the
# matcher regex would also hit hypothetical tools with kanban_create as a
# substring, and a config typo could drop the matcher entirely).
tool=$(printf '%s' "$payload" | python3 -c "import json,sys;print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null) || allow
case "$tool" in kanban_create|kanban_sh_create) ;; *) allow ;; esac

reason=$(printf '%s' "$payload" | timeout 15 python3 "$guard" --hook-check 2>/dev/null) || allow
[ -n "$reason" ] || allow

python3 -c "import json,sys;print(json.dumps({'decision':'block','reason':sys.argv[1]}))" "$reason" 2>/dev/null || allow
exit 0
