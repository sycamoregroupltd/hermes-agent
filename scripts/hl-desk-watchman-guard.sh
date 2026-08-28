#!/usr/bin/env bash
# OOB watchman-liveness guard (no-agent cron). The 08-14 desk died leaving an
# unmanaged live position; this guard makes that structurally impossible:
# if positions exist and the watchman's equity feed is stale, restart the
# watchman and alert Frank. Exit code IS the liveness signal (rule: no_agent
# cron stdout is never parsed).
set -u
DESK=/home/frank/dgx-fable-orchestrator/state/hl-live-desk
PY=/home/frank/hl-maker-measurement/.venv/bin/python
WALLET=0x62d250e94005a4B892c83cc180CE5C4e6404d747

npos=$(curl -4 -s -m 15 -X POST https://api.hyperliquid.xyz/info \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"clearinghouseState\",\"user\":\"$WALLET\"}" \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('assetPositions',[])))" 2>/dev/null) || npos=""

if [ -z "$npos" ]; then
  # Venue unreachable from guard — cannot assert safety. Red, but no action.
  echo "guard: venue probe failed"
  exit 1
fi

now=$(date +%s)
mt=$(stat -c %Y "$DESK/equity.jsonl" 2>/dev/null || echo 0)
age=$((now - mt))

if [ "$npos" -eq 0 ] && [ "$age" -lt 180 ]; then
  exit 0
fi
if [ "$npos" -eq 0 ]; then
  # Flat and watchman stale: quiet degradation, restart but stay green.
  flock -n "$DESK/.watchman-guard.lock" -c \
    "cd $DESK && nohup setsid $PY gateway/watchman.py >> watchman.log 2>&1 &" || true
  echo "guard: flat, watchman stale (${age}s) — restarted"
  exit 0
fi
if [ "$age" -lt 120 ]; then
  exit 0
fi

# Positions exist AND watchman stale: restart + alert, red exit.
flock -n "$DESK/.watchman-guard.lock" -c \
  "cd $DESK && nohup setsid $PY gateway/watchman.py >> watchman.log 2>&1 &" || true
hermes send -t telegram "HL DESK GUARD: watchman was DEAD (${age}s stale) with $npos open position(s) on 0x62d2..d747. Restarted it. Check the desk." || true
env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban --board jarvis-os create \
  "HL DESK GUARD: watchman restarted (${age}s stale, $npos open)" \
  --body "Watchman was DEAD (${age}s stale) with $npos open position(s) on 0x62d2..d747. Guard restarted it. Telegram alert also sent. Check the desk." \
  || true
echo "guard: positions=$npos watchman stale ${age}s — restarted + alerted"
exit 1
