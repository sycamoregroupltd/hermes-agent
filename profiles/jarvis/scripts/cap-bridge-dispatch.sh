#!/bin/bash
# Temporary throughput driver: applies the raised caps (13/3/5, Frank-approved
# 2026-08-30) via explicit CLI dispatch passes, which read config fresh each
# call. The gateway's embedded dispatcher keeps its startup-captured caps
# (12/2/4) until its next natural restart; this bridges the gap without
# restarting the gateway (KillMode=mixed would kill live workers).
# no_agent cron: stdout only when something actually spawned.
export PATH=/home/frank/.hermes/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin
cd /home/frank
for b in jarvis-os sycode-trading upero; do
  out=$(env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban --board "$b" dispatch --json 2>/dev/null)
  spawned=$(echo "$out" | python3 -c "import json,sys
try: print(len(json.load(sys.stdin).get('spawned',[])))
except Exception: print(0)")
  [ "${spawned:-0}" != "0" ] && echo "$b: spawned $spawned"
done
exit 0
