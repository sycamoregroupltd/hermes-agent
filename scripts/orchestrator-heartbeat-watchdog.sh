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
#
# PARK awareness (fleet-engineer, t_b4495b45, policy card t_2105d982):
# the orchestrator tick cron has been A3-DISABLED since 2026-08-21 (runaway
# restart-loop incident) with no re-enable date; under that hold a stale
# heartbeat is an EXPECTED, already-classified condition, not an incident.
# Without this sentinel the watchdog re-minted the identical STALE report
# card every 15-60min for 3+ days (8 occurrences: t_fce6725c, t_e6c6a2bc,
# t_3b29552b, t_91b60484, t_66b305fc, plus 3 refreshes of t_b4495b45).
# Behavior is UNCHANGED (byte-identical) whenever the sentinel file is
# absent — this only changes output cadence while the orchestrator is
# known-PARKED, and self-reverts the instant the sentinel is removed or
# the heartbeat freshens (i.e. the orchestrator resumes).
PARK_SENTINEL=/home/frank/dgx-fable-orchestrator/state/ORCHESTRATOR-PARKED
ACK_STAMP=/home/frank/dgx-fable-orchestrator/state/.heartbeat-watchdog-park-ack
ACK_INTERVAL=86400   # one low-noise "still parked" ack per day, not every 15m
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
  if [ -f "$PARK_SENTINEL" ]; then
    # Known-PARKED (Frank A3 hold): never auto-restart, never escalate.
    # Emit at most once per ACK_INTERVAL so the condition stays visible
    # without re-minting board noise every cron fire.
    last_ack=0
    [ -f "$ACK_STAMP" ] && last_ack=$(stat -c %Y "$ACK_STAMP" 2>/dev/null || echo 0)
    if [ $(( now - last_ack )) -ge "$ACK_INTERVAL" ]; then
      echo "ORCHESTRATOR PARKED (expected, no action needed): heartbeat ${age}s old (max ${MAX}s) while orchestrator tick is A3-disabled ($(head -c 200 "$PARK_SENTINEL")). No restart attempted. Daily ack; see policy card t_2105d982."
      touch "$ACK_STAMP"
    fi
    exit 0
  fi
  echo "ORCHESTRATOR STALE: heartbeat ${age}s old (max ${MAX}s). Content: $(head -c 120 "$HB"). Check /home/frank/dgx-fable-orchestrator/state/{cycle.lock,cycle-runner.log,last-cycle.txt} and the tick cron."
  # Double-stale: independent zero-LLM escalation path (alerts only, no secrets)
  if [ "$age" -gt $(( MAX * 2 )) ]; then
    timeout 60 hermes send -t telegram \
      "ORCHESTRATOR loop dead: heartbeat ${age}s old on DGX. See dgx-fable-orchestrator/state/." \
      2>/dev/null || echo "ESCALATION FAILED: hermes send -t telegram also failed"
  fi
fi
