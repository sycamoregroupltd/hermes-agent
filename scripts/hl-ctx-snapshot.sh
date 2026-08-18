#!/usr/bin/env bash
# HL context snapshotter (no-agent cron, every 5m): appends funding/OI/mark/premium for
# the 12-coin universe to the desk-owned store. Closes two data gaps found by the 4D
# rotation (no-computer-says-no): HL has NO OI-history API, and funding windows currently
# depend on the venue's retention. Our own tape makes OI trajectories + funding windows
# first-party forever. Store: ~/hl-desk-data/ctx/YYYY-MM-DD.jsonl (outside git checkouts).
set -u
OUT_DIR=/home/frank/hl-desk-data/ctx
mkdir -p "$OUT_DIR"

python3 - << 'PYEOF'
import json, urllib.request, datetime, socket, sys

_orig = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [ai for ai in _orig(*a, **k) if ai[0] == socket.AF_INET]

UNI = ["BTC","ETH","SOL","XRP","DOGE","AVAX","LINK","ARB","OP","SUI","WLD","kPEPE"]
req = urllib.request.Request("https://api.hyperliquid.xyz/info",
    data=json.dumps({"type":"metaAndAssetCtxs"}).encode(),
    headers={"Content-Type":"application/json"})
try:
    meta, ctxs = json.loads(urllib.request.urlopen(req, timeout=15).read())
except Exception as e:
    print(f"ctx-snapshot probe failed: {e}"); sys.exit(1)

now = datetime.datetime.now(datetime.timezone.utc)
idx = {a["name"]: i for i, a in enumerate(meta["universe"])}
row = {"ts": now.isoformat()[:19]+"Z", "coins": {}}
for c in UNI:
    if c not in idx: continue
    x = ctxs[idx[c]]
    row["coins"][c] = {"funding": x.get("funding"), "oi": x.get("openInterest"),
                       "mark": x.get("markPx"), "oracle": x.get("oraclePx"),
                       "mid": x.get("midPx"), "vol24h": x.get("dayNtlVlm"),
                       "premium": x.get("premium")}
out = f"/home/frank/hl-desk-data/ctx/{now.strftime('%Y-%m-%d')}.jsonl"
with open(out, "a") as f:
    f.write(json.dumps(row, separators=(",", ":")) + "\n")
PYEOF
