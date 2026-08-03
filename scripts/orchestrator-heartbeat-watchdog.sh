#!/usr/bin/env bash
# orchestrator-heartbeat-watchdog.sh — dead-man switch for the orchestrator
# loop, on a DIFFERENT runtime than the thing it watches (plug-AND-monitor
# rule). no-agent cron: empty stdout = healthy/silent; any output is
# delivered via the cron's deliver target.
# MAX math (design constant owned HERE): heartbeat is written at cycle END;
# with a 2h tick and up-to-92min cycles the worst HEALTHY gap between
# writes is ~2h + 92min ≈ 12720s. MAX=14400 (4h) leaves ~28min grace and
# still cannot false-alarm on a healthy loop. Double-stale escalation at
# 2*MAX via the zero-LLM hermes send path.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HB=/home/frank/dgx-fable-orchestrator/state/heartbeat
MAX=14400
now=$(date +%s)
if [ ! -f "$HB" ]; then
  echo "ORCHESTRATOR DEAD: heartbeat file missing at $HB (loop never ran or state dir wiped). Check state/cycle-runner.log and state/last-cycle.txt."
  exit 0
fi
age=$(( now - $(stat -c %Y "$HB") ))
if [ "$age" -gt "$MAX" ]; then
  echo "ORCHESTRATOR STALE: heartbeat ${age}s old (max ${MAX}s). Content: $(head -c 120 "$HB"). Check /home/frank/dgx-fable-orchestrator/state/{cycle.lock,cycle-runner.log,last-cycle.txt} and the tick cron."
  # Double-stale: independent zero-LLM escalation path (alerts only, no secrets)
  if [ "$age" -gt $(( MAX * 2 )) ]; then
    timeout 60 hermes send -t telegram \
      "ORCHESTRATOR loop dead: heartbeat ${age}s old on DGX. See dgx-fable-orchestrator/state/." \
      2>/dev/null || echo "ESCALATION FAILED: hermes send -t telegram also failed"
  fi
fi
