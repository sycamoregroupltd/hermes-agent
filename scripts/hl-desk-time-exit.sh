#!/usr/bin/env bash
# Seat-independent time-exit enforcer (no-agent cron, 10m). Reads TIME-EXITS.json:
#   {"exits": [{"coin": "XRP", "exit_at": "2026-08-18T08:00:00Z", "note": "..."}]}
# If now >= exit_at AND the wallet holds that coin: flatten it via the gateway
# (reduce-only path — allowed even under DESK_HALT). The desk watchman enforces
# PRICE stops; this enforces TIME. Both survive any seat/session death.
set -u
DESK=/home/frank/dgx-fable-orchestrator/state/hl-live-desk
PY=/home/frank/hl-maker-measurement/.venv/bin/python
GW="$DESK/gateway/hl_desk_gateway.py"
F="$DESK/TIME-EXITS.json"
WALLET=0x62d250e94005a4B892c83cc180CE5C4e6404d747

[ -f "$F" ] || exit 0

python3 - << 'PYEOF'
import json, subprocess, sys, urllib.request
from datetime import datetime, timezone

DESK = "/home/frank/dgx-fable-orchestrator/state/hl-live-desk"
F = DESK + "/TIME-EXITS.json"
GW = ["/home/frank/hl-maker-measurement/.venv/bin/python",
      DESK + "/gateway/hl_desk_gateway.py", "--actor", "stop-runner/time-exit"]

cfg = json.load(open(F))
due = [e for e in cfg.get("exits", [])
       if datetime.now(timezone.utc) >= datetime.fromisoformat(e["exit_at"].replace("Z", "+00:00"))]
if not due:
    sys.exit(0)

req = urllib.request.Request("https://api.hyperliquid.xyz/info",
    data=json.dumps({"type": "clearinghouseState",
                     "user": "0x62d250e94005a4B892c83cc180CE5C4e6404d747"}).encode(),
    headers={"Content-Type": "application/json"})
st = json.loads(urllib.request.urlopen(req, timeout=15).read())
held = {p["position"]["coin"] for p in st.get("assetPositions", []) if float(p["position"]["szi"]) != 0}

rc = 0
remaining = [e for e in cfg.get("exits", []) if e not in due]
for e in due:
    coin = e["coin"]
    if coin not in held:
        print(f"time-exit: {coin} already flat, clearing entry")
        continue
    p = subprocess.run(GW + ["flatten", "--coin", coin], capture_output=True, text=True, timeout=90)
    print(f"time-exit EXECUTED {coin}: rc={p.returncode} {(p.stdout or p.stderr)[-200:]}")
    if p.returncode != 0:
        rc = 1
        remaining.append(e)  # keep so next run retries

json.dump({"exits": remaining}, open(F, "w"), indent=1)
sys.exit(rc)
PYEOF
