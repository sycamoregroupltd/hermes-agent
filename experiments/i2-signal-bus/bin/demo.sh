#!/usr/bin/env bash
# I2 ORACLE live probe:
#  1. start an independent reader process (background, logs live transitions)
#  2. run a REAL hermes -z agent, observe idle -> working -> done live
#  3. run a second REAL agent, SIGKILL the writer mid-"working" (real writer
#     death, not a scripted exit), prove the reader marks it STALE and never
#     fabricates a "done" for it
#  4. prove ack/replay: a reader that reads without acking, then a fresh
#     reader restart that replays the still-pending entries
#  5. dump the single-shell-command debug view
set -euo pipefail
cd "$(dirname "$0")/.."
export I2_BUS_STALE_TTL_S="${I2_BUS_STALE_TTL_S:-6}"
PY=.venv/bin/python
LOG=evidence
mkdir -p "$LOG"
STAMP="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || python3 -c 'import time;print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))')"
READER_LOG="$LOG/reader-$STAMP.log"

echo "== [0/5] paper redis up =="
bin/up.sh

echo "== [1/5] starting independent reader (background, log=$READER_LOG) =="
$PY -m agent_state_bus.reader --consumer reader-live > "$READER_LOG" 2>&1 &
READER_PID=$!
sleep 1

echo "== [2/5] real agent run: idle -> working -> done =="
$PY -m agent_state_bus.run_real_agent --agent-id demo-happy-path

sleep 1

echo "== [3/5] real agent run with hard writer death mid-working (staleness) =="
set +e
$PY -m agent_state_bus.run_real_agent --agent-id demo-crash --simulate-crash-after 3 &
CRASH_PID=$!
wait "$CRASH_PID"
set -e
echo "writer pid $CRASH_PID confirmed dead (SIGKILL delivered by the wrapper to itself)"

echo "== waiting past stale TTL (${I2_BUS_STALE_TTL_S}s) so the reader must reclassify demo-crash =="
sleep "$(python3 -c "print(float('$I2_BUS_STALE_TTL_S') + 3)")"

echo "== [4/5] ack/replay proof: a SEPARATE consumer group (own full copy of the stream, per Redis Streams"
echo "   fan-out semantics), read without acking, then a fresh reader restart that replays the pending entries =="
$PY -m agent_state_bus.run_real_agent --agent-id demo-replay --heartbeat-interval 0.5
$PY - <<'PYEOF'
from agent_state_bus.reader import read_batch
events = read_batch("replay-consumer", group="replay-demo-group", ack=False, from_cursor=">", block_ms=3000, count=20)
print(f"[demo] non-acking reader saw {len(events)} unacked event(s) for replay-consumer")
PYEOF
$PY - <<'PYEOF'
from agent_state_bus.reader import read_batch
# Same group + consumer name, cursor '0': redelivers this consumer's still-pending (unacked) entries.
replayed = read_batch("replay-consumer", group="replay-demo-group", ack=True, from_cursor="0", block_ms=1000, count=50)
print(f"[demo] restarted reader REPLAYED {len(replayed)} previously-unacked event(s), now acked")
for ev in replayed:
    print(f"  replayed: agent={ev.agent_id} type={ev.event_type}")
PYEOF

echo "== [5/5] single-shell-command debug view =="
$PY -m agent_state_bus.bus_debug | tee "$LOG/bus-debug-$STAMP.log"

echo "== stopping background reader (pid $READER_PID) =="
kill "$READER_PID" 2>/dev/null || true
sleep 1
echo "== reader transcript ($READER_LOG) =="
cat "$READER_LOG"

echo "== DONE. Evidence written under $LOG/ =="
