#!/usr/bin/env bash
# Hermes pre_llm_call hook. Inject the compact, provider-neutral knowledge
# contract on the first turn of every session so workers do not depend on a
# model voluntarily discovering or following a distant SOUL paragraph.
set -uo pipefail

if [ "${1:-}" = "--self-test" ]; then
  printf '%s\n' '{"is_first_turn":true,"session_id":"self-test","platform":"cli"}' | bash "$0" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "context" in d and "obsidian-knowledge-management" in d["context"]; print("{\"status\":\"pass\"}")'
  exit $?
fi

payload=$(cat 2>/dev/null) || { printf '{}\n'; exit 0; }
first=$(printf '%s' "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); e=d.get("extra") if isinstance(d.get("extra"),dict) else {}; print("1" if d.get("is_first_turn") is True or e.get("is_first_turn") is True else "0")' 2>/dev/null) || first=0
[ "$first" = "1" ] || { printf '{}\n'; exit 0; }

python3 - <<'PY'
import json
context = """[MANDATORY SECOND-BRAIN CONTRACT — applies to this session]
Before substantive work, resolve the project in /home/frank/obsidian-fleet-vault/Projects/Portfolio/registry.yaml, read its MOC and owning vault AGENTS.md/SCHEMA.md, search compiled knowledge, and inspect the registered tracker/runtime before claiming current state. Before any durable knowledge write, load and follow the obsidian-knowledge-management skill.

Canonical DGX vaults: fleet/cross-project knowledge at /home/frank/obsidian-fleet-vault; Sycode knowledge at /home/frank/obsidian/sycode-trading (alias of quant-team). Existing path casing is canonical: never create a case-variant sibling; top-level Sycode reviews use Reviews/, not reviews/. Active notes require title, type, status, created, updated, confidence, tags, and sources. Closed confidence values are high|medium|low|unknown; never use evidence/eval-evidence. Use the closed type/status values from SCHEMA.md. A blocked write must be corrected, never bypassed.

For shared work, use the Session Bus for intent/ACK/heartbeat/handoff and the project tracker for execution state. Provider memory/chat are caches. Never put secrets or approval payloads in a vault."""
print(json.dumps({"context": context}, ensure_ascii=False))
PY
