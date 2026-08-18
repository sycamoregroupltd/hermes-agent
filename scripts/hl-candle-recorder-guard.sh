#!/usr/bin/env bash
# HL candle-recorder liveness guard (no-agent cron, 10m). Lifecycle-matches-responsibility:
# the recorder accrues the OOS research corpus; silent death = silent corpus gap, so a
# stale heartbeat gets a restart and a red exit code (exit code IS the liveness signal).
set -u
HB=/home/frank/hl-desk-data/recorder/heartbeat
REC=/home/frank/hl-desk-data/recorder/hl_candle_recorder.py

now=$(date +%s)
mt=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
age=$((now - mt))

if [ "$age" -lt 360 ]; then
  exit 0
fi

flock -n /home/frank/hl-desk-data/recorder/.guard.lock -c \
  "cd /home/frank/hl-desk-data/recorder && nohup setsid /usr/bin/python3 $REC >> recorder.log 2>&1 &" || true
echo "recorder-guard: heartbeat stale ${age}s — restarted"
sleep 150
mt2=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
if [ $((now + 150 - mt2)) -lt 300 ]; then
  echo "recorder-guard: recovery confirmed"
  exit 0
fi
echo "recorder-guard: RESTART DID NOT RECOVER heartbeat"
exit 1
