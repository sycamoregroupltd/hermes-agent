#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Collect external market data from host (bypasses n8n data extraction issues).
Runs as a cron job. Writes results to Sycode DB."""
import subprocess, json, urllib.request, time
from datetime import datetime

DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]

def fetch_json(url, method="GET", body=None, timeout=10):
    try:
        req = urllib.request.Request(url, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"_error": str(e)}

def write_to_db(source, data):
    payload = json.dumps({"source": source, "data": data, "ts": datetime.utcnow().isoformat()})
    sql = "INSERT INTO n8n_market_data (source, payload) VALUES ('{}', $TAG${}$TAG$::jsonb);".format(source, payload)
    subprocess.run(DB, input=sql.encode(), capture_output=True, timeout=10)

# 1. Fear & Greed
fg = fetch_json("https://api.alternative.me/fng/?limit=1")
if 'data' in fg and len(fg['data']) > 0:
    write_to_db("fear-greed", {
        "value": fg['data'][0].get('value'),
        "classification": fg['data'][0].get('value_classification'),
    })
    print(f"  Fear/Greed: {fg['data'][0].get('value_classification')} ({fg['data'][0].get('value')})")

# 2. BTC Funding Rate (Binance)
bf = fetch_json("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1")
if isinstance(bf, list) and len(bf) > 0:
    write_to_db("binance-funding", {
        "symbol": "BTCUSDT",
        "rate": bf[0].get('fundingRate'),
        "time": bf[0].get('fundingTime'),
    })
    print(f"  Binance funding: {bf[0].get('fundingRate')}")

# 3. BTC Funding Rate (Hyperliquid)
hl = fetch_json("https://api.hyperliquid.xyz/info", method="POST", body={"type": "allMids"})
if isinstance(hl, dict):
    btc_price = hl.get('BTC', 'N/A')
    write_to_db("hyperliquid-price", {"BTC": btc_price})
    print(f"  Hyperliquid BTC: {btc_price}")

# 4. Arbitrage opportunity check (Binance vs Hyperliquid)
# Get a few more funding rates for arb detection
bybit = fetch_json("https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=1")
if 'result' in bybit:
    r = bybit['result'].get('list', [{}])[0]
    bybit_rate = r.get('fundingRate')
    binance_rate = bf[0].get('fundingRate') if isinstance(bf, list) and len(bf) > 0 else '0'
    if bybit_rate and binance_rate:
        spread = abs(float(bybit_rate) - float(binance_rate))
        if spread >= 0.0005:
            print(f"  ⚠️ ARB: spread {spread*10000:.2f}bps")
        write_to_db("funding-arb", {
            "binance": binance_rate,
            "bybit": bybit_rate,
            "spread": spread,
            "arbitrage": spread >= 0.0005,
        })
        print(f"  Funding arb: spread={spread*10000:.2f}bps")

print("Done")
