#!/usr/bin/env bash
# HL desk research re-arm triggers (no-agent cron, every 15m). From cycle-1 lanes 08-15:
#  T1 funding: ARB/OP hourly funding < -0.000003 (~ -26% APR) — crowded-short regime back
#  T2 funding: any universe coin ABOVE the +0.0000125/hr cap — true long-crowding
#  T3 LINK pullback into 9.10-9.38 (flag zone) — momentum thesis re-judge zone
#  T4 vol expansion: BTC 1h high-low range > 0.9% — compression break, re-scan everything
# On trigger: append to RESEARCH-TRIGGERS.jsonl (desk-head reads on wake) + stdout.
set -u
DESK=/home/frank/dgx-fable-orchestrator/state/hl-live-desk
OUT=$DESK/RESEARCH-TRIGGERS.jsonl

python3 - << 'PYEOF'
import json, urllib.request, datetime, sys, os

def post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

OUT = "/home/frank/dgx-fable-orchestrator/state/hl-live-desk/RESEARCH-TRIGGERS.jsonl"
now = datetime.datetime.now(datetime.timezone.utc)
trig = []
try:
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    uni = ["BTC","ETH","SOL","XRP","DOGE","AVAX","LINK","ARB","OP","SUI","WLD","kPEPE"]
    idx = {a["name"]: i for i, a in enumerate(meta["universe"])}
    # T1 per the preregistered funding-lane re-arm spec: < -0.3bps/hr (~-26% APR)
    # SUSTAINED 3h+. Persistence read from the desk's own 5m ctx tape. (First version
    # had a 10x-loose threshold and no persistence — fired false T1s on 08-15 23:05Z.)
    import glob
    def sustained_neg(coin, thresh=-0.00003, hours=3):
        rows = []
        for p in sorted(glob.glob("/home/frank/hl-desk-data/ctx/*.jsonl"))[-2:]:
            for line in open(p):
                try: r = json.loads(line)
                except Exception: continue
                c = r.get("coins", {}).get(coin)
                if c and c.get("funding") is not None:
                    rows.append((r["ts"], float(c["funding"])))
        cut = (now - datetime.timedelta(hours=hours)).isoformat()[:19] + "Z"
        recent = [f for ts, f in rows if ts >= cut]
        return len(recent) >= hours * 8 and all(f < thresh for f in recent)
    for c in uni:
        if c not in idx: continue
        ctx = ctxs[idx[c]]
        f = float(ctx.get("funding") or 0)
        if c in ("ARB","OP") and f < -0.00003 and sustained_neg(c):
            trig.append({"t":"T1_neg_funding","coin":c,"funding_hr":f,"sustained_3h":True})
        if f > 0.0000126:
            trig.append({"t":"T2_above_cap","coin":c,"funding_hr":f})
        # T3 RETIRED 2026-08-16 ~17:10Z (desk-head ruling, stint-18 evidence: LINK exited
        # the zone from above, funding flipped positive, 9.3052 whipsawed twice = liquidity
        # magnet not floor). Re-arm requires a NEW journaled zone from fresh structure.
        # if c == "LINK":
        # mid = float(ctx.get("midPx") or 0)
        # if 9.10 <= mid <= 9.38:
        # # debounce: T3's job is done once the thesis pipeline is woken —
        # # suppress re-fires while a T3 row exists in the last 2h
        # recent_t3 = False
        # try:
        # for line in open(OUT).readlines()[-100:]:
        # r = json.loads(line)
        # if r.get("t") == "T3_link_pullback":
        # ts = datetime.datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
        # if (now - ts).total_seconds() < 7200:
        # recent_t3 = True
        # break
        # except Exception:
        # pass
        # if not recent_t3:
        # trig.append({"t":"T3_link_pullback","mid":mid})
    end = int(now.timestamp()*1000); start = end - 3600_000
    cs = post({"type":"candleSnapshot","req":{"coin":"BTC","interval":"5m","startTime":start,"endTime":end}})
    if cs:
        hi = max(float(c["h"]) for c in cs); lo = min(float(c["l"]) for c in cs)
        rng = (hi-lo)/lo*100
        if rng > 0.9:
            trig.append({"t":"T4_vol_expansion","btc_1h_range_pct":round(rng,3)})
except Exception as e:
    print(f"trigger-watch probe error: {e}"); sys.exit(1)

if trig:
    with open(OUT,"a") as f:
        for t in trig:
            f.write(json.dumps({"ts": now.isoformat()[:19]+"Z", **t})+"\n")
    print("RESEARCH TRIGGERS: " + json.dumps(trig))
PYEOF
