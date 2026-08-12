#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Multi-pair funding rate arb monitor. Checks BTC, ETH, SOL across Binance + Bybit."""
# Duplicate-drift note (t_8c18ef11, 2026-07-03): this root copy is archival.
# The Jarvis runtime copy is canonical until the DB write behavior is reconciled.
import json, urllib.request
from datetime import datetime, timezone
UTC = timezone.utc

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BASE_URLS = {
    "binance": "https://fapi.binance.com/fapi/v1/fundingRate?symbol={}&limit=1",
    "bybit": "https://api.bybit.com/v5/market/funding/history?category=linear&symbol={}&limit=1",
}
TOKEN = None  # resolved below via env file or env vars
DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres"]

import os, sys, subprocess

# Credential loading: shared env file (defaults to sycode-credential.env); env vars override.
_CRED_ENV_FILE = os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env")
if os.path.exists(_CRED_ENV_FILE):
    from dotenv import load_dotenv
    load_dotenv(_CRED_ENV_FILE, override=False)

TOKEN = os.environ.get("SYCODE_TRADE_TOKEN") or os.environ.get("OPENCLAW_TRADE_TOKEN")
if not TOKEN:
    print(f"[FATAL] Missing OpenClaw TRADE token. Set SYCODE_TRADE_TOKEN or OPENCLAW_TRADE_TOKEN\n"
          f"       in env or in {_CRED_ENV_FILE}.", flush=True)
    sys.exit(3)

def fetch(url):
    try: return json.loads(urllib.request.urlopen(url, timeout=10).read())
    except: return {}

def write_db(payload_json):
    sql = f"INSERT INTO n8n_market_data (source, payload) VALUES ('funding-arb', $TAG${payload_json}$TAG$::jsonb);"
    subprocess.run(DB, input=sql.encode(), capture_output=True, timeout=10)

def log(msg):
    print(f"  {msg}")

for pair in PAIRS:
    b = fetch(BASE_URLS["binance"].format(pair))
    by = fetch(BASE_URLS["bybit"].format(pair))
    b_rate = b[0].get('fundingRate','0') if isinstance(b, list) and b else '0'
    by_rate = by.get('result',{}).get('list',[{}])[0].get('fundingRate','0') if by else '0'
    spread = abs(float(b_rate) - float(by_rate))

    if spread >= 0.0003:  # Alert at 3bps instead of 5bps for more hits
        log(f"⚠️ {pair} arb: {spread*10000:.2f}bps (Binance={b_rate} Bybit={by_rate})")

    write_db(json.dumps({"symbol": pair, "binance": b_rate, "bybit": by_rate, "spread": spread, "ts": datetime.now(UTC).isoformat()}))

    # If arb > 5bps, open paper position
    if spread >= 0.0005:
        sym_base = pair.replace("USDT", "")
        r = subprocess.run(["curl","-s","-X","POST",
            "-H",f"X-Sycode-Token:{TOKEN}","-H","Content-Type:application/json",
            "-d",json.dumps({"symbol":pair,"direction":"LONG","sizeUsd":100,"leverage":1}),
            "http://localhost:3001/api/openclaw/trade/open"], capture_output=True, text=True, timeout=10)
        print(f"  [TRADE] Arb {pair} {r.stdout[:80]}")

print(f"Checked {len(PAIRS)} pairs")
