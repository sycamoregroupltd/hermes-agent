#!/usr/bin/env bash
# pre_llm_call hook: on the FIRST turn of a voice/phone session (platform=api_server),
# inject the pre-computed fleet snapshot so phone-Jarvis opens the call already knowing
# fleet state — zero mid-call latency (the thing that drops calls). Token-lean, fail-quiet.
set -uo pipefail
payload=$(cat 2>/dev/null)
none() { echo '{}'; exit 0; }

read first plat <<<"$(printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('is_first_turn',False), d.get('platform',''))
" 2>/dev/null)" || none

# Only on the first turn of a voice/phone (api_server) session
[ "$first" = "True" ] || [ "$first" = "true" ] || none
[ "$plat" = "api_server" ] || none

SNAP="/home/frank/uaa-rules/FLEET-STATUS.md"
[ -f "$SNAP" ] || none
# inject a compact version (voice doesn't need the full file) — counts + pending review
body=$(sed -n '1,40p' "$SNAP" 2>/dev/null)
[ -n "$body" ] || none
python3 -c "import json,sys;print(json.dumps({'context': '[FLEET SNAPSHOT for this call — you already know this, speak it conversationally if asked]\n'+sys.argv[1]}))" "$body" 2>/dev/null || none
