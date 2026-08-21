#!/usr/bin/env bash
# Daily compounding rollup (no-agent cron, 00:10Z): computes the day's close, high-water
# mark, drawdown, and rolling daily rate from the watchman's equity tape into
# COMPOUNDING-LEDGER.jsonl — the number the desk optimizes (charter D10.6).
set -u
python3 - << 'PYEOF'
import json, glob, datetime, os, sys

DESK = "/home/frank/dgx-fable-orchestrator/state/hl-live-desk"
EQ = os.path.join(DESK, "equity.jsonl")
OUT = os.path.join(DESK, "COMPOUNDING-LEDGER.jsonl")
START = 378.34  # desk restart equity 2026-08-15; base for cumulative rate

rows = []
for line in open(EQ):
    try:
        r = json.loads(line)
        rows.append((r["ts"], float(r["equity"])))
    except Exception:
        continue
if not rows:
    print("rollup: no equity rows"); sys.exit(1)

today = datetime.datetime.now(datetime.timezone.utc).date()
yday = (today - datetime.timedelta(days=1)).isoformat()
day_rows = [e for ts, e in rows if ts.startswith(yday)]
close = day_rows[-1] if day_rows else rows[-1][1]
hwm = max(e for _, e in rows)
prev = []
if os.path.exists(OUT):
    for line in open(OUT):
        try: prev.append(json.loads(line))
        except Exception: continue
prev_close = prev[-1]["close"] if prev else START
day_ret_bps = (close / prev_close - 1) * 1e4
cum_ret_pct = (close / START - 1) * 100
dd_from_hwm_pct = (close / hwm - 1) * 100
row = {"date": yday, "close": round(close, 4), "hwm": round(hwm, 4),
       "day_return_bps": round(day_ret_bps, 2), "cum_return_pct": round(cum_ret_pct, 3),
       "drawdown_from_hwm_pct": round(dd_from_hwm_pct, 3),
       "floors": {"no_new_risk": round(hwm * 0.90, 2), "flatten_all": round(hwm * 0.85, 2)}}
if prev and prev[-1]["date"] == yday:
    sys.exit(0)  # already rolled up
with open(OUT, "a") as f:
    f.write(json.dumps(row) + "\n")
print(f"compounding rollup {yday}: close {row['close']} day {row['day_return_bps']}bps "
      f"cum {row['cum_return_pct']}% dd {row['drawdown_from_hwm_pct']}% "
      f"ratchet floors {row['floors']}")
PYEOF
