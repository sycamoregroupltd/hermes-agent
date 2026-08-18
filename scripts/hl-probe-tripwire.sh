#!/usr/bin/env bash
# ALT-REV-K1.5-H24 probe tripwire (no-agent cron, 30m). Mechanizes the D8 decay contract:
# rolling last-10 round_trips net < 0 OR 20-event win rate < 50% => writes PROBE_HALT
# (fail-closed flag; gateway-side enforcement lands in the Monday patch window — until
# then the probe seat brief REQUIRES checking this flag before any entry). Deletes the
# flag NEVER — un-halting a tripped probe is a journaled desk-head decision.
set -u
python3 - << 'PYEOF'
import json, os, sys

DESK = "/home/frank/dgx-fable-orchestrator/state/hl-live-desk"
LED = os.path.join(DESK, "PROBE-LEDGER.jsonl")
FLAG = os.path.join(DESK, "PROBE_HALT")

if os.path.exists(FLAG):
    print("probe tripwire: PROBE_HALT already set"); sys.exit(0)
if not os.path.exists(LED):
    sys.exit(0)
rts = []
for line in open(LED):
    try:
        r = json.loads(line)
        if r.get("kind") == "round_trip":
            rts.append(float(r.get("net_usd", 0)))
    except Exception:
        continue
if len(rts) >= 10 and sum(rts[-10:]) < 0:
    reason = f"rolling last-10 net {sum(rts[-10:]):.4f} < 0"
elif len(rts) >= 20 and sum(1 for x in rts[-20:] if x > 0) / 20 < 0.50:
    reason = f"20-event win rate {sum(1 for x in rts[-20:] if x > 0)/20:.0%} < 50%"
else:
    sys.exit(0)
with open(FLAG, "w") as f:
    f.write(f"PROBE_HALT set by tripwire cron: {reason} (n={len(rts)} round trips). "
            "ALT-REV-K1.5-H24 probe entries FROZEN; un-halt only by journaled desk-head "
            "decision after ledger review.\n")
print(f"probe tripwire FIRED: {reason}")
sys.exit(1)
PYEOF
