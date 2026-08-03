#!/usr/bin/env bash
# Hermes pre_tool_call gate for canonical Obsidian/LLM-Wiki mutations.
# The Python classifier fails open off-vault or on ambiguity and blocks only a
# provably malformed, case-colliding, or direct-shell vault write.
set -uo pipefail

if [ "${ALLOW_SECOND_BRAIN_WRITE:-}" = "1" ]; then
  printf '{}\n'
  exit 0
fi

PY=/home/frank/.hermes/agent-hooks/gate-second-brain-writes.py
if [ ! -x "$PY" ]; then
  printf '{}\n'
  exit 0
fi

out=$(python3 "$PY" 2>/dev/null) || out='{}'
[ -n "$out" ] && printf '%s\n' "$out" || printf '{}\n'
exit 0
