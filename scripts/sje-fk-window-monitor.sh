#!/usr/bin/env bash
# sje-fk-window-monitor.sh
# 24h post-restart FK-error validation monitor for sycodetrading-server.
# Samples GET /metrics + docker logs each tick for the target error
# signal_journey_events_journey_id_fkey, appends an evidence row, and
# emits a wakeAgent signal ONLY on regression or at window end.
#
# This is a wakeAgent LOOP gate (mirrors elon-stall-watch pattern):
#   - output identical to previous tick  -> scheduler skips (no agent run)
#   - output changes (REGRESSION / WINDOW_END) -> wakes the LLM
# It must FAIL OPEN: any error -> emit OK (no wake), never wedge.
set -u

# --- config --------------------------------------------------------------
# Server was redeployed 2026-08-28T21:03:31Z (deploy #184) onto origin/main
# tip 72ea72246ef2 with the FK fix confirmed byte-identical. Observation
# window = 24h from that restart.
RESTART_TS="2026-08-28T21:03:31Z"
WINDOW_END_EPOCH=1788037411                # 2026-08-29T21:03:31Z (24h after redeploy)
METRICS_URL="http://localhost:3001/metrics"
CONTAINER="sycodetrading-server"
WS="${HERMES_KANBAN_WORKSPACE:-/home/frank/.hermes/kanban/boards/jarvis-os/workspaces/t_20053371}"
EVIDENCE="$WS/sje-fk-evidence.jsonl"
mkdir -p "$WS"

# --- sample --------------------------------------------------------------
NOW_EPOCH=$(date -u +%s)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# In-scope target: signal_journey_events_journey_id_fkey errors since restart
SJE=$(docker logs --since "$RESTART_TS" "$CONTAINER" 2>&1 | grep -a -c 'signal_journey_events_journey_id_fkey' || true)
[ -z "$SJE" ] && SJE="NA"
# Out-of-scope signal_pnl_points FK loss (known orphan-guard, not this fix)
SPP=$(docker logs --since "$RESTART_TS" "$CONTAINER" 2>&1 | grep -a -cE 'signal_pnl_points.*(fkey|foreign key constraint|FK)' || true)
[ -z "$SPP" ] && SPP="NA"
# Metrics counters (process-local, reset at restart)
FT=$(curl -s -m 15 "$METRICS_URL" 2>/dev/null | grep '^sycodetrading_bullmq_queue_fk_terminal_total' | awk '{print $2}' | tail -1)
PR=$(curl -s -m 15 "$METRICS_URL" 2>/dev/null | grep '^sycodetrading_bullmq_queue_fk_parent_race_total' | awk '{print $2}' | tail -1)
DLQ=$(curl -s -m 15 "$METRICS_URL" 2>/dev/null | grep '^sycodetrading_bullmq_queue_dlq_total' | awk '{print $2}' | tail -1)
FT=${FT:-NA}; PR=${PR:-NA}; DLQ=${DLQ:-NA}

printf '{"ts":"%s","sje_fkey":"%s","signal_pnl_fk":"%s","fk_terminal":"%s","fk_parent_race":"%s","dlq":"%s"}\n' \
  "$TS" "$SJE" "$SPP" "$FT" "$PR" "$DLQ" >> "$EVIDENCE" 2>/dev/null

# --- gate decision (FAIL OPEN) -------------------------------------------
if [ "$SJE" != "NA" ] && [ "$SJE" != "0" ]; then
  echo "REGRESSION sje_fkey=$SJE ts=$TS"
elif [ "$NOW_EPOCH" -ge "$WINDOW_END_EPOCH" ]; then
  echo "WINDOW_END ts=$TS sje_fkey=$SJE signal_pnl_fk=$SPP fk_terminal=$FT"
else
  # Clean / mid-window: CONSTANT output so the monitor skips each tick.
  # (changing counters go only to the evidence file, not to the gate line)
  echo "OK"
fi
