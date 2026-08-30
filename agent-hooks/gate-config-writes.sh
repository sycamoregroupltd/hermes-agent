#!/usr/bin/env bash
# gate-config-writes.sh — pre_tool_call hook.
# BLOCK any agent write/edit to a Hermes config.yaml (profile config or global ~/.hermes/config.yaml).
# Provider/model/toolset config is HUMAN-MANAGED (Frank-approved only). Audit 2026-06-26 found 17
# profiles held terminal+file and could rewrite any config.yaml unguarded; elon's quota-aware
# propagation pushed dead models to 62 profiles. This is the deterministic WRITE-TIME veto that
# gate-provider-governance.sh (runtime provider-USE only) does not provide.
#
# Contract (mirrors gate-kanban-complete.sh): read JSON payload on stdin; emit a block JSON to veto,
# or {} to allow. FAIL-OPEN on any error/ambiguity — never wedge the fleet. Only edits to .hermes
# config.yaml are gated; app/project config.yaml files and plain reads are untouched.
# Bypass: ALLOW_CONFIG_WRITE=1 in the environment (set by the orchestrator/dispatcher for an
# explicitly-approved, logged repair) lets the write through. Agents cannot set this mid-reasoning.
set -uo pipefail || true
LOG=/home/frank/.hermes/cron/state/config-write-gate.log
PY=/home/frank/.hermes/agent-hooks/gate-config-writes.py
payload=$(cat 2>/dev/null)

# Approved-repair bypass
if [ "${ALLOW_CONFIG_WRITE:-}" = "1" ]; then
  printf '%s ALLOW(bypass) ALLOW_CONFIG_WRITE=1\n' "$(date -u +%FT%TZ)" >> "$LOG" 2>/dev/null || true
  echo '{}'; exit 0
fi

# Fast path: allow instantly unless the payload references a .hermes config.yaml OR a NATIVE
# config-mutating command. `hermes config set` / `hermes fallback add` rewrite that very file but
# name no path, so a path-only fast path returned an unconditional {} and the Python logic below
# never ran for them (verified 2026-08-29 via `hermes hooks test`: the native write was ALLOWED
# through the real chain while `sed -i` on the path was blocked). That is how the dispatcher caps
# acquired three independent writers in one day. Read-only forms (config get/show, fallback list)
# still short-circuit here and stay cheap.
if ! printf '%s' "$payload" | grep -qiE '\.hermes/[^"[:space:]]*config\.yaml|hermes[^|;&]*(config[[:space:]]+(set|unset)|fallback[[:space:]]+(add|remove|rm|clear))'; then echo '{}'; exit 0; fi

out=$(printf '%s' "$payload" | python3 "$PY" 2>/dev/null)
[ -n "$out" ] && printf '%s\n' "$out" || echo '{}'
exit 0

